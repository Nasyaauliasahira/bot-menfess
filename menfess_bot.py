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
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    ApplicationBuilder,
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
TRIGGER_WORD = os.getenv("TRIGGER_WORD", "#fess").lower()
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


def save_post(user_id: int, username: str, content_type: str, message: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO posts (user_id, username, content_type, message, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, username, content_type, message, datetime.now().isoformat()),
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


# ============================================================
# HELPER
# ============================================================
def strip_trigger(text: str) -> str:
    """Hapus kata kunci trigger di awal pesan (case-insensitive)."""
    return text[len(TRIGGER_WORD):].strip()


def format_menfess(post_number: int, body: str) -> str:
    safe_body = html.escape(body) if body else "<i>(tanpa teks)</i>"
    return f"💌 <b>Menfess #{post_number}</b>\n\n{safe_body}"


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


# ============================================================
# HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Selamat datang di bot Menfess!\n\n"
        f"Kirim pesan (teks, foto, lagu/audio, atau video+caption) diawali kata kunci "
        f"<b>{html.escape(TRIGGER_WORD)}</b> untuk memposting secara anonim ke channel.\n\n"
        f"⏳ Ada jeda {COOLDOWN_SECONDS} detik antar-pengiriman untuk mencegah spam.\n"
        "Ketik /help untuk bantuan lebih lanjut.",
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Cara pakai:</b>\n"
        f"1. Ketik pesanmu (atau caption foto/lagu/video) diawali <b>{html.escape(TRIGGER_WORD)}</b>\n"
        "2. Kirim ke bot ini lewat chat pribadi (teks, foto, lagu/audio, atau video)\n"
        "3. Bot akan memposting pesanmu secara anonim ke channel\n\n"
        "Identitas kamu <b>tidak</b> ditampilkan di channel, tapi tetap tercatat "
        "di sistem untuk keperluan moderasi bila ada penyalahgunaan.",
        parse_mode=ParseMode.HTML,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Perintah ini khusus admin.")
        return
    total, today_count = get_stats()
    await update.message.reply_text(
        f"📊 Statistik Menfess\nTotal: {total}\nHari ini: {today_count}"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text or ""
    user = update.effective_user

    if not text.lower().startswith(TRIGGER_WORD):
        await message.reply_text(
            f'❌ Pesan ditolak. Harus diawali kata kunci "{TRIGGER_WORD}".'
        )
        return

    remaining = check_cooldown(user.id)
    if remaining > 0:
        await message.reply_text(f"⏳ Tunggu {int(remaining)} detik lagi sebelum kirim menfess baru.")
        return

    body = strip_trigger(text)
    if not body:
        await message.reply_text("❌ Isi menfess tidak boleh kosong.")
        return
    if len(body) > MAX_TEXT_LEN:
        await message.reply_text(f"❌ Pesan terlalu panjang (maks {MAX_TEXT_LEN} karakter).")
        return

    try:
        post_number = save_post(user.id, user.username or "", "text", body)
        await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=format_menfess(post_number, body),
            parse_mode=ParseMode.HTML,
        )
        await message.reply_text(f"🚀 Menfess #{post_number} berhasil terbit di channel!")
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

    if not caption.lower().startswith(TRIGGER_WORD):
        await message.reply_text(
            f'❌ Foto ditolak. Caption harus diawali kata kunci "{TRIGGER_WORD}".'
        )
        return

    remaining = check_cooldown(user.id)
    if remaining > 0:
        await message.reply_text(f"⏳ Tunggu {int(remaining)} detik lagi sebelum kirim menfess baru.")
        return

    body = strip_trigger(caption)
    if len(body) > MAX_CAPTION_LEN:
        await message.reply_text(f"❌ Caption terlalu panjang (maks {MAX_CAPTION_LEN} karakter).")
        return

    try:
        file_id = message.photo[-1].file_id
        post_number = save_post(user.id, user.username or "", "photo", body)
        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=file_id,
            caption=format_menfess(post_number, body),
            parse_mode=ParseMode.HTML,
        )
        await message.reply_text(f"🚀 Menfess #{post_number} (foto) berhasil terbit di channel!")
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

    if not caption.lower().startswith(TRIGGER_WORD):
        await message.reply_text(
            f'❌ Lagu ditolak. Caption harus diawali kata kunci "{TRIGGER_WORD}".'
        )
        return

    remaining = check_cooldown(user.id)
    if remaining > 0:
        await message.reply_text(f"⏳ Tunggu {int(remaining)} detik lagi sebelum kirim menfess baru.")
        return

    body = strip_trigger(caption)
    if len(body) > MAX_CAPTION_LEN:
        await message.reply_text(f"❌ Caption terlalu panjang (maks {MAX_CAPTION_LEN} karakter).")
        return

    try:
        file_id = message.audio.file_id
        post_number = save_post(user.id, user.username or "", "audio", body)
        await context.bot.send_audio(
            chat_id=CHANNEL_ID,
            audio=file_id,
            caption=format_menfess(post_number, body),
            parse_mode=ParseMode.HTML,
        )
        await message.reply_text(f"🚀 Menfess #{post_number} (lagu) berhasil terbit di channel!")
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

    if not caption.lower().startswith(TRIGGER_WORD):
        await message.reply_text(
            f'❌ Video ditolak. Caption harus diawali kata kunci "{TRIGGER_WORD}".'
        )
        return

    remaining = check_cooldown(user.id)
    if remaining > 0:
        await message.reply_text(f"⏳ Tunggu {int(remaining)} detik lagi sebelum kirim menfess baru.")
        return

    body = strip_trigger(caption)
    if len(body) > MAX_CAPTION_LEN:
        await message.reply_text(f"❌ Caption terlalu panjang (maks {MAX_CAPTION_LEN} karakter).")
        return

    try:
        file_id = message.video.file_id
        post_number = save_post(user.id, user.username or "", "video", body)
        await context.bot.send_video(
            chat_id=CHANNEL_ID,
            video=file_id,
            caption=format_menfess(post_number, body),
            parse_mode=ParseMode.HTML,
        )
        await message.reply_text(f"🚀 Menfess #{post_number} (video) berhasil terbit di channel!")
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
