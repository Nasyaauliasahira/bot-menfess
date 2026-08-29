"""
Bot Menfess (Confession Bot) - Telegram
----------------------------------------
Bot menerima pesan pribadi dari user, memvalidasi kata kunci trigger,
lalu memposting pesan tersebut secara anonim ke channel publik.

Fitur:
- Konfigurasi via environment variable / file .env (token TIDAK hardcode lagi)
- Mendukung teks & foto (dengan caption)
- Nomor urut otomatis untuk tiap menfess (tersimpan di SQLite)
- Cooldown anti-spam per user
- Escaping otomatis agar teks user tidak merusak format pesan (fix bug Markdown)
- Perintah /stats khusus admin
- Error yang lebih informatif + notifikasi ke admin bila gagal posting
- Hanya bisa dipakai lewat chat pribadi (bukan grup)
"""

import html
import logging
import os
import sqlite3
import time
from datetime import datetime
from typing import Optional, Tuple

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# KONFIGURASI
# ============================================================
load_dotenv()  # baca file .env kalau ada

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")  # contoh: -1001234567890
TRIGGER_WORD = os.getenv("TRIGGER_WORD", "#fess").lower()  # sudah tidak wajib, lihat CATEGORIES

# Kategori menfess yang bisa dipilih user lewat inline keyboard.
# key -> (label yang ditampilkan, hashtag yang dipasang otomatis di channel)
CATEGORIES = {
    "confess": ("Confess", "#Confess"),
    "spill": ("Spill", "#Spill"),
    "ask": ("Ask", "#Ask"),
    "findpartner": ("Find Partner", "#FindPartner"),
}

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "60"))
MAX_TEXT_LEN = 4000       # batas aman pesan teks Telegram (limit asli 4096)
MAX_CAPTION_LEN = 1000    # batas aman caption foto (limit asli 1024)
DB_PATH = os.getenv("DB_PATH", "menfess.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN belum diset. Isi di file .env atau environment variable.")
if not CHANNEL_ID_RAW:
    raise RuntimeError("CHANNEL_ID belum diset. Isi di file .env atau environment variable.")

CHANNEL_ID = int(CHANNEL_ID_RAW)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("menfess_bot")


# ============================================================
# DATABASE (SQLite) - nomor urut menfess & cooldown
# ============================================================
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                content_type TEXT NOT NULL,
                message TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        # Migrasi ringan: tambahkan kolom category kalau belum ada (DB lama).
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        if "category" not in existing_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN category TEXT")
        if "telegram_message_id" not in existing_cols:
            conn.execute("ALTER TABLE posts ADD COLUMN telegram_message_id INTEGER")
        conn.commit()


def get_last_post_time(user_id: int) -> Optional[float]:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT created_at FROM posts WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return datetime.fromisoformat(row[0]).timestamp()


def save_post(
    user_id: int, username: str, content_type: str, message: str, category: str = ""
) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO posts (user_id, username, content_type, message, created_at, category) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, username, content_type, message, datetime.now().isoformat(), category),
        )
        conn.commit()
        return cur.lastrowid


def get_stats() -> Tuple[int, int]:
    """Return (total_posts, posts_today)."""
    today = datetime.now().date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        today_count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE created_at LIKE ?", (f"{today}%",)
        ).fetchone()[0]
    return total, today_count


def set_telegram_message_id(post_number: int, telegram_message_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE posts SET telegram_message_id = ? WHERE id = ?",
            (telegram_message_id, post_number),
        )
        conn.commit()


def get_post(post_number: int) -> Optional[Tuple[int, Optional[int]]]:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute(
            "SELECT id, telegram_message_id FROM posts WHERE id = ?",
            (post_number,),
        ).fetchone()


def delete_post_record(post_number: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM posts WHERE id = ?", (post_number,))
        conn.commit()


# ============================================================
# HELPER
# ============================================================
def strip_trigger(text: str) -> str:
    """Hapus kata kunci trigger di awal pesan (case-insensitive)."""
    return text[len(TRIGGER_WORD):].strip()


def format_menfess(post_number: int, body: str, hashtag: str = "") -> str:
    safe_body = html.escape(body) if body else "<i>(tanpa teks)</i>"
    tag_part = f" {hashtag}" if hashtag else ""
    return f"💌 <b>Menfess #{post_number}</b>{tag_part}\n\n{safe_body}"


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except TelegramError:
            logger.warning("Gagal mengirim notifikasi ke admin %s", admin_id)


def check_cooldown(user_id: int) -> float:
    """Return sisa detik cooldown (0 kalau boleh kirim)."""
    last_time = get_last_post_time(user_id)
    if last_time is None:
        return 0
    elapsed = time.time() - last_time
    remaining = COOLDOWN_SECONDS - elapsed
    return max(0.0, remaining)


def build_category_keyboard() -> InlineKeyboardMarkup:
    """Tombol pilihan kategori, 2 per baris."""
    buttons = [
        InlineKeyboardButton(label, callback_data=f"cat_{key}")
        for key, (label, _hashtag) in CATEGORIES.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


def build_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Batal", callback_data="cancel")]]
    )


def get_session_category(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """Ambil key kategori yang sedang dipilih user (None kalau belum pilih)."""
    return context.user_data.get("category")


def clear_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset kategori yang tersimpan supaya menfess berikutnya tidak ikut-ikutan."""
    context.user_data.pop("category", None)


# ============================================================
# HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_session(context)
    await update.message.reply_text(
        "👋 Selamat datang di bot Menfess!\n\n"
        "Pilih dulu kategori menfess-mu di bawah ini, lalu kirim isi pesannya "
        "(teks, foto, lagu/audio, atau video) — tanpa perlu ketik hashtag apa pun.\n\n"
        f"⏳ Ada jeda {COOLDOWN_SECONDS} detik antar-pengiriman untuk mencegah spam.\n"
        "Ketik /help untuk bantuan lebih lanjut.",
        parse_mode=ParseMode.HTML,
        reply_markup=build_category_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category_list = "\n".join(f"• {label}" for label, _ in CATEGORIES.values())
    await update.message.reply_text(
        "📖 <b>Cara pakai:</b>\n"
        "1. Ketik /start, lalu pilih salah satu kategori:\n"
        f"{html.escape(category_list)}\n"
        "2. Kirim isi menfess-mu (teks, foto, lagu/audio, atau video) — langsung saja, "
        "tanpa hashtag atau keyword apa pun.\n"
        "3. Bot otomatis menambahkan hashtag kategori dan memposting secara anonim ke channel.\n"
        "4. Sedang di tengah proses dan berubah pikiran? Tekan tombol ❌ Batal.\n\n"
        "Identitas kamu <b>tidak</b> ditampilkan di channel, tapi tetap tercatat "
        "di sistem untuk keperluan moderasi bila ada penyalahgunaan.",
        parse_mode=ParseMode.HTML,
    )


async def category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback saat user menekan salah satu tombol kategori."""
    query = update.callback_query
    await query.answer()

    key = query.data[len("cat_") :]
    if key not in CATEGORIES:
        await query.edit_message_text("❌ Kategori tidak dikenali, coba /start lagi.")
        return

    label, _hashtag = CATEGORIES[key]
    context.user_data["category"] = key

    await query.edit_message_text(
        f"✅ Kategori dipilih: <b>{html.escape(label)}</b>\n\n"
        "Sekarang kirim isi menfess-mu (teks, foto, lagu/audio, atau video). "
        "Tidak perlu pakai hashtag, langsung kirim saja.",
        parse_mode=ParseMode.HTML,
        reply_markup=build_cancel_keyboard(),
    )


async def cancel_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback saat user menekan tombol Batal."""
    query = update.callback_query
    await query.answer()
    clear_session(context)
    await query.edit_message_text(
        "🚫 Dibatalkan. Ketik /start lagi kalau mau kirim menfess baru."
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Perintah ini khusus admin.")
        return
    total, today_count = get_stats()
    await update.message.reply_text(
        f"📊 Statistik Menfess\nTotal: {total}\nHari ini: {today_count}"
    )


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Perintah ini khusus admin.")
        return

    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Format: /hapus <nomor menfess>")
        return

    post_number = int(context.args[0])
    post = get_post(post_number)
    if post is None:
        await update.message.reply_text(f"❌ Menfess #{post_number} tidak ditemukan.")
        return

    _post_id, telegram_message_id = post
    if telegram_message_id is None:
        await update.message.reply_text(
            f"❌ Menfess #{post_number} belum punya ID pesan Telegram. "
            "Posting lama sebelum fitur hapus tidak bisa dihapus otomatis."
        )
        return

    try:
        await context.bot.delete_message(CHANNEL_ID, telegram_message_id)
    except Forbidden:
        await update.message.reply_text(
            "❌ Bot tidak punya izin menghapus pesan di channel. Jadikan bot admin "
            "dengan izin menghapus pesan."
        )
        return
    except BadRequest as error:
        logger.error("Gagal menghapus menfess #%s: %s", post_number, error)
        await update.message.reply_text(
            "❌ Pesan channel tidak ditemukan atau sudah terhapus. Data lokal tetap disimpan."
        )
        return

    delete_post_record(post_number)
    await update.message.reply_text(f"✅ Menfess #{post_number} berhasil dihapus.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text or ""
    user = update.effective_user

    category_key = get_session_category(context)
    if category_key is None:
        await message.reply_text(
            "❌ Kamu belum pilih kategori. Ketik /start dulu dan pilih kategorinya ya.",
        )
        return

    remaining = check_cooldown(user.id)
    if remaining > 0:
        await message.reply_text(f"⏳ Tunggu {int(remaining)} detik lagi sebelum kirim menfess baru.")
        return

    body = text.strip()
    if not body:
        await message.reply_text("❌ Isi menfess tidak boleh kosong.")
        return
    if len(body) > MAX_TEXT_LEN:
        await message.reply_text(f"❌ Pesan terlalu panjang (maks {MAX_TEXT_LEN} karakter).")
        return

    label, hashtag = CATEGORIES[category_key]

    try:
        post_number = save_post(user.id, user.username or "", "text", body, category_key)
        sent_message = await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=format_menfess(post_number, body, hashtag),
            parse_mode=ParseMode.HTML,
        )
        set_telegram_message_id(post_number, sent_message.message_id)
        clear_session(context)
        await message.reply_text(
            f"🚀 Menfess #{post_number} ({label}) berhasil terbit di channel!"
        )
    except Forbidden:
        await message.reply_text("❌ Gagal posting: bot belum jadi admin di channel.")
    except BadRequest as e:
        logger.error("BadRequest saat posting: %s", e)
        await message.reply_text("❌ Gagal posting: format pesan ditolak Telegram.")
    except TelegramError as e:
        logger.error("TelegramError saat posting: %s", e)
        await message.reply_text("❌ Gagal posting. Coba lagi nanti.")
        await notify_admins(context, f"⚠️ Gagal posting menfess dari user {user.id}: {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    caption = message.caption or ""
    user = update.effective_user

    category_key = get_session_category(context)
    if category_key is None:
        await message.reply_text(
            "❌ Kamu belum pilih kategori. Ketik /start dulu dan pilih kategorinya ya.",
        )
        return

    remaining = check_cooldown(user.id)
    if remaining > 0:
        await message.reply_text(f"⏳ Tunggu {int(remaining)} detik lagi sebelum kirim menfess baru.")
        return

    body = caption.strip()
    if len(body) > MAX_CAPTION_LEN:
        await message.reply_text(f"❌ Caption terlalu panjang (maks {MAX_CAPTION_LEN} karakter).")
        return

    label, hashtag = CATEGORIES[category_key]

    try:
        file_id = message.photo[-1].file_id
        post_number = save_post(user.id, user.username or "", "photo", body, category_key)
        sent_message = await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=file_id,
            caption=format_menfess(post_number, body, hashtag),
            parse_mode=ParseMode.HTML,
        )
        set_telegram_message_id(post_number, sent_message.message_id)
        clear_session(context)
        await message.reply_text(
            f"🚀 Menfess #{post_number} (foto - {label}) berhasil terbit di channel!"
        )
    except Forbidden:
        await message.reply_text("❌ Gagal posting: bot belum jadi admin di channel.")
    except TelegramError as e:
        logger.error("TelegramError saat posting foto: %s", e)
        await message.reply_text("❌ Gagal posting. Coba lagi nanti.")
        await notify_admins(context, f"⚠️ Gagal posting foto menfess dari user {user.id}: {e}")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    caption = message.caption or ""
    user = update.effective_user

    category_key = get_session_category(context)
    if category_key is None:
        await message.reply_text(
            "❌ Kamu belum pilih kategori. Ketik /start dulu dan pilih kategorinya ya.",
        )
        return

    remaining = check_cooldown(user.id)
    if remaining > 0:
        await message.reply_text(f"⏳ Tunggu {int(remaining)} detik lagi sebelum kirim menfess baru.")
        return

    body = caption.strip()
    if len(body) > MAX_CAPTION_LEN:
        await message.reply_text(f"❌ Caption terlalu panjang (maks {MAX_CAPTION_LEN} karakter).")
        return

    label, hashtag = CATEGORIES[category_key]

    try:
        file_id = message.audio.file_id
        post_number = save_post(user.id, user.username or "", "audio", body, category_key)
        sent_message = await context.bot.send_audio(
            chat_id=CHANNEL_ID,
            audio=file_id,
            caption=format_menfess(post_number, body, hashtag),
            parse_mode=ParseMode.HTML,
        )
        set_telegram_message_id(post_number, sent_message.message_id)
        clear_session(context)
        await message.reply_text(
            f"🚀 Menfess #{post_number} (lagu - {label}) berhasil terbit di channel!"
        )
    except Forbidden:
        await message.reply_text("❌ Gagal posting: bot belum jadi admin di channel.")
    except TelegramError as e:
        logger.error("TelegramError saat posting lagu: %s", e)
        await message.reply_text("❌ Gagal posting. Coba lagi nanti.")
        await notify_admins(context, f"⚠️ Gagal posting lagu menfess dari user {user.id}: {e}")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    caption = message.caption or ""
    user = update.effective_user

    category_key = get_session_category(context)
    if category_key is None:
        await message.reply_text(
            "❌ Kamu belum pilih kategori. Ketik /start dulu dan pilih kategorinya ya.",
        )
        return

    remaining = check_cooldown(user.id)
    if remaining > 0:
        await message.reply_text(f"⏳ Tunggu {int(remaining)} detik lagi sebelum kirim menfess baru.")
        return

    body = caption.strip()
    if len(body) > MAX_CAPTION_LEN:
        await message.reply_text(f"❌ Caption terlalu panjang (maks {MAX_CAPTION_LEN} karakter).")
        return

    label, hashtag = CATEGORIES[category_key]

    try:
        file_id = message.video.file_id
        post_number = save_post(user.id, user.username or "", "video", body, category_key)
        sent_message = await context.bot.send_video(
            chat_id=CHANNEL_ID,
            video=file_id,
            caption=format_menfess(post_number, body, hashtag),
            parse_mode=ParseMode.HTML,
        )
        set_telegram_message_id(post_number, sent_message.message_id)
        clear_session(context)
        await message.reply_text(
            f"🚀 Menfess #{post_number} (video - {label}) berhasil terbit di channel!"
        )
    except Forbidden:
        await message.reply_text("❌ Gagal posting: bot belum jadi admin di channel.")
    except TelegramError as e:
        logger.error("TelegramError saat posting video: %s", e)
        await message.reply_text("❌ Gagal posting. Coba lagi nanti.")
        await notify_admins(context, f"⚠️ Gagal posting video menfess dari user {user.id}: {e}")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Perintah tidak dikenali. Ketik /help untuk bantuan.")


# ============================================================
# RUNNER
# ============================================================
def main() -> None:
    init_db()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("hapus", delete_command))
    app.add_handler(CallbackQueryHandler(category_selected, pattern=r"^cat_"))
    app.add_handler(CallbackQueryHandler(cancel_selected, pattern=r"^cancel$"))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_text)
    )
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.PHOTO, handle_photo)
    )
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.AUDIO, handle_audio)
    )
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.VIDEO, handle_video)
    )
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info("Bot menfess siap & jalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
