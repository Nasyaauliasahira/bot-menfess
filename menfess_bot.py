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
- Pengaturan runtime (cooldown, batas panjang teks/caption) yang HANYA bisa
  diubah oleh admin lewat perintah /pengaturan, tersimpan di database
  sehingga tidak perlu edit .env atau restart bot.
- Mendukung kirim BANYAK foto/video sekaligus (album/media group) dalam satu
  menfess, otomatis digabung jadi satu post di channel.

CATATAN INSTALASI:
Fitur album butuh job-queue dari python-telegram-bot, pasang dengan:
    pip install "python-telegram-bot[job-queue]"
"""

import html
import logging
import os
import sqlite3
import time
from datetime import datetime
from typing import Optional, Tuple

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo, Update
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
    "random": ("Random", "#Random"),
}

ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
DB_PATH = os.getenv("DB_PATH", "menfess.db")

# Batas Telegram: maksimal 10 item per album (media group).
MAX_MEDIA_GROUP_ITEMS = 10
# Jeda tunggu (detik) setelah item terakhir album diterima sebelum diposting,
# supaya semua foto/video dalam satu album sempat terkumpul dulu.
MEDIA_GROUP_DEBOUNCE_SECONDS = 1.5

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
# PENGATURAN RUNTIME (hanya admin yang boleh mengubah)
# ============================================================
# Definisi tiap setting: key -> (label buat ditampilkan, tipe data, nilai default,
# batas minimum, batas maksimum). Nilai batas dipakai buat validasi input admin
# supaya nggak salah masukin angka aneh (misal cooldown negatif).
SETTINGS_META = {
    "cooldown_seconds": {
        "label": "Cooldown antar-kiriman (detik)",
        "type": int,
        "default": 60,
        "min": 0,
        "max": 3600,
    },
    "max_text_len": {
        "label": "Batas panjang teks menfess",
        "type": int,
        "default": 4000,
        "min": 50,
        "max": 4096,
    },
    "max_caption_len": {
        "label": "Batas panjang caption (foto/audio/video)",
        "type": int,
        "default": 1000,
        "min": 20,
        "max": 1024,
    },
}


def init_settings_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    # Isi nilai default kalau belum ada di DB, jangan overwrite yang sudah diubah admin.
    for key, meta in SETTINGS_META.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, str(meta["default"])),
        )
    conn.commit()


def get_setting(key: str):
    meta = SETTINGS_META[key]
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return meta["default"]
    try:
        return meta["type"](row[0])
    except (TypeError, ValueError):
        return meta["default"]


def set_setting(key: str, raw_value: str) -> Tuple[bool, str]:
    """Validasi & simpan nilai setting baru. Return (berhasil, pesan)."""
    meta = SETTINGS_META[key]
    try:
        value = meta["type"](raw_value)
    except (TypeError, ValueError):
        return False, f"❌ Nilai harus berupa angka ({meta['label']})."

    if "min" in meta and value < meta["min"]:
        return False, f"❌ Nilai minimal untuk {meta['label']} adalah {meta['min']}."
    if "max" in meta and value > meta["max"]:
        return False, f"❌ Nilai maksimal untuk {meta['label']} adalah {meta['max']}."

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()
    return True, f"✅ {meta['label']} berhasil diubah jadi {value}."


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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
        init_settings_db(conn)


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
    remaining = get_setting("cooldown_seconds") - elapsed
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


def build_settings_keyboard() -> InlineKeyboardMarkup:
    """Tombol daftar pengaturan yang bisa diubah admin, isi tombolnya menampilkan nilai saat ini."""
    rows = []
    for key, meta in SETTINGS_META.items():
        current = get_setting(key)
        rows.append(
            [InlineKeyboardButton(f"{meta['label']}: {current}", callback_data=f"setting_{key}")]
        )
    return InlineKeyboardMarkup(rows)


def get_session_category(context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
    """Ambil key kategori yang sedang dipilih user (None kalau belum pilih)."""
    return context.user_data.get("category")


def clear_session(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset kategori yang tersimpan supaya menfess berikutnya tidak ikut-ikutan."""
    context.user_data.pop("category", None)


# ------------------------------------------------------------
# ALBUM / MEDIA GROUP (kirim banyak foto & video sekaligus)
# ------------------------------------------------------------
# Telegram mengirim tiap foto/video dalam satu album sebagai UPDATE TERPISAH,
# tapi semuanya berbagi message.media_group_id yang sama. Jadi kita tampung
# dulu item-itemnya di buffer, tunggu sebentar (debounce) sampai tidak ada
# item baru masuk, baru diposting sekaligus sebagai satu album ke channel.
def get_media_group_buffer(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data.setdefault("media_groups", {})


async def handle_media_group_item(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    message = update.message
    group_id = message.media_group_id
    user = update.effective_user

    buffers = get_media_group_buffer(context)
    entry = buffers.get(group_id)
    if entry is None:
        entry = {
            "items": [],
            "caption": None,
            "user_id": user.id,
            "username": user.username or "",
            "category_key": get_session_category(context),
            "chat_id": message.chat_id,
            "job": None,
        }
        buffers[group_id] = entry

    # Caption album di Telegram biasanya cuma nempel di salah satu item saja
    # (urutan kedatangan tidak dijamin), jadi kita tangkap begitu ketemu.
    if message.caption:
        entry["caption"] = message.caption.strip()

    if len(entry["items"]) < MAX_MEDIA_GROUP_ITEMS:
        if kind == "photo":
            entry["items"].append(("photo", message.photo[-1].file_id))
        else:
            entry["items"].append(("video", message.video.file_id))

    # Reset timer debounce tiap kali ada item baru masuk untuk grup ini.
    if entry["job"] is not None:
        entry["job"].schedule_removal()
    entry["job"] = context.job_queue.run_once(
        process_media_group,
        MEDIA_GROUP_DEBOUNCE_SECONDS,
        data=group_id,
        name=f"media_group_{group_id}",
    )


async def process_media_group(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dipanggil otomatis oleh job_queue setelah semua item album selesai terkumpul."""
    group_id = context.job.data
    buffers = context.application.bot_data.get("media_groups", {})
    entry = buffers.pop(group_id, None)
    if entry is None or not entry["items"]:
        return

    user_id = entry["user_id"]
    chat_id = entry["chat_id"]
    category_key = entry["category_key"]

    if category_key is None:
        await context.bot.send_message(
            chat_id, "❌ Kamu belum pilih kategori. Ketik /start dulu dan pilih kategorinya ya."
        )
        return

    remaining = check_cooldown(user_id)
    if remaining > 0:
        await context.bot.send_message(
            chat_id, f"⏳ Tunggu {int(remaining)} detik lagi sebelum kirim menfess baru."
        )
        return

    body = (entry["caption"] or "").strip()
    max_caption_len = get_setting("max_caption_len")
    if len(body) > max_caption_len:
        await context.bot.send_message(
            chat_id, f"❌ Caption terlalu panjang (maks {max_caption_len} karakter)."
        )
        return

    label, hashtag = CATEGORIES[category_key]

    try:
        post_number = save_post(user_id, entry["username"], "album", body, category_key)
        caption_html = format_menfess(post_number, body, hashtag)

        media_list = []
        for index, (kind, file_id) in enumerate(entry["items"]):
            # Caption cuma dipasang di item pertama, sisanya polos (perilaku
            # standar album Telegram: caption tampil di bawah item pertama).
            is_first = index == 0
            if kind == "photo":
                media_list.append(
                    InputMediaPhoto(
                        file_id,
                        caption=caption_html if is_first else None,
                        parse_mode=ParseMode.HTML if is_first else None,
                    )
                )
            else:
                media_list.append(
                    InputMediaVideo(
                        file_id,
                        caption=caption_html if is_first else None,
                        parse_mode=ParseMode.HTML if is_first else None,
                    )
                )

        sent_messages = await context.bot.send_media_group(chat_id=CHANNEL_ID, media=media_list)
        set_telegram_message_id(post_number, sent_messages[0].message_id)

        # Reset kategori milik user yang bersangkutan (bukan user_data lokal job ini).
        context.application.user_data[user_id].pop("category", None)

        await context.bot.send_message(
            chat_id,
            f"🚀 Menfess #{post_number} (album {len(media_list)} item - {label}) "
            "berhasil terbit di channel!",
        )
    except Forbidden:
        await context.bot.send_message(chat_id, "❌ Gagal posting: bot belum jadi admin di channel.")
    except TelegramError as e:
        logger.error("TelegramError saat posting album: %s", e)
        await context.bot.send_message(chat_id, "❌ Gagal posting. Coba lagi nanti.")
        await notify_admins(context, f"⚠️ Gagal posting album menfess dari user {user_id}: {e}")


# ============================================================
# HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_session(context)
    await update.message.reply_text(
        "👋 Selamat datang di bot Menfess!\n\n"
        "Pilih dulu kategori menfess-mu di bawah ini, lalu kirim isi pesannya "
        "(teks, foto, lagu/audio, atau video) — tanpa perlu ketik hashtag apa pun.\n\n"
        f"⏳ Ada jeda {get_setting('cooldown_seconds')} detik antar-pengiriman untuk mencegah spam.\n"
        "Ketik /help untuk bantuan lebih lanjut.",
        parse_mode=ParseMode.HTML,
        reply_markup=build_category_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category_list = "\n".join(f"• {label}" for label, _ in CATEGORIES.values())
    text = (
        "📖 <b>Cara pakai:</b>\n"
        "1. Ketik /start, lalu pilih salah satu kategori:\n"
        f"{html.escape(category_list)}\n"
        "2. Kirim isi menfess-mu (teks, foto, lagu/audio, atau video) — langsung saja, "
        "tanpa hashtag atau keyword apa pun.\n"
        "3. Bot otomatis menambahkan hashtag kategori dan memposting secara anonim ke channel.\n"
        "4. Sedang di tengah proses dan berubah pikiran? Tekan tombol ❌ Batal.\n\n"
        "Identitas kamu <b>tidak</b> ditampilkan di channel, tapi tetap tercatat "
        "di sistem untuk keperluan moderasi bila ada penyalahgunaan."
    )
    if is_admin(update.effective_user.id):
        text += (
            "\n\n🛠 <b>Khusus admin:</b>\n"
            "• /stats — lihat statistik menfess\n"
            "• /hapus &lt;nomor&gt; — hapus menfess dari channel\n"
            "• /pengaturan — ubah cooldown & batas panjang pesan"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


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
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Perintah ini khusus admin.")
        return
    total, today_count = get_stats()
    await update.message.reply_text(
        f"📊 Statistik Menfess\nTotal: {total}\nHari ini: {today_count}"
    )


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
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


# ------------------------------------------------------------
# PENGATURAN (admin only)
# ------------------------------------------------------------
async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Perintah ini khusus admin.")
        return
    context.user_data.pop("editing_setting", None)
    await update.message.reply_text(
        "⚙️ <b>Pengaturan Bot</b>\n\n"
        "Pilih pengaturan yang mau kamu ubah. Nilai saat ini ditampilkan di tombol.",
        parse_mode=ParseMode.HTML,
        reply_markup=build_settings_keyboard(),
    )


async def settings_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Cek admin di level callback juga — jangan cuma andalkan command handler,
    # supaya orang lain nggak bisa "warisin" tombol admin dari chat lama.
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Khusus admin.", show_alert=True)
        return

    await query.answer()
    key = query.data[len("setting_") :]
    if key not in SETTINGS_META:
        await query.edit_message_text("❌ Pengaturan tidak dikenali.")
        return

    meta = SETTINGS_META[key]
    context.user_data["editing_setting"] = key
    await query.edit_message_text(
        f"✏️ Ubah <b>{html.escape(meta['label'])}</b>\n"
        f"Nilai saat ini: <code>{get_setting(key)}</code>\n"
        f"Rentang valid: {meta['min']} – {meta['max']}\n\n"
        "Kirim angka baru sebagai pesan biasa, atau ketik /batalpengaturan untuk membatalkan.",
        parse_mode=ParseMode.HTML,
    )


async def cancel_setting_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Perintah ini khusus admin.")
        return
    if context.user_data.pop("editing_setting", None) is not None:
        await update.message.reply_text("🚫 Perubahan pengaturan dibatalkan.")
    else:
        await update.message.reply_text("Tidak ada pengaturan yang sedang diubah.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text or ""
    user = update.effective_user

    # --- Jalur khusus admin: sedang mengisi nilai pengaturan baru ---
    editing_key = context.user_data.get("editing_setting")
    if editing_key is not None:
        if not is_admin(user.id):
            # Seharusnya nggak mungkin kejadian (flag ini cuma diset lewat menu admin),
            # tapi dijaga tetap aman kalau ada state nyasar.
            context.user_data.pop("editing_setting", None)
        else:
            ok, msg = set_setting(editing_key, text.strip())
            if ok:
                context.user_data.pop("editing_setting", None)
            await message.reply_text(msg)
            return

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
    max_text_len = get_setting("max_text_len")
    if len(body) > max_text_len:
        await message.reply_text(f"❌ Pesan terlalu panjang (maks {max_text_len} karakter).")
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

    # Kalau ini bagian dari album (kirim banyak foto/video sekaligus),
    # tampung dulu lewat buffer, jangan diproses satu-satu.
    if message.media_group_id is not None:
        await handle_media_group_item(update, context, "photo")
        return

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
    max_caption_len = get_setting("max_caption_len")
    if len(body) > max_caption_len:
        await message.reply_text(f"❌ Caption terlalu panjang (maks {max_caption_len} karakter).")
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
    max_caption_len = get_setting("max_caption_len")
    if len(body) > max_caption_len:
        await message.reply_text(f"❌ Caption terlalu panjang (maks {max_caption_len} karakter).")
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

    # Kalau ini bagian dari album (kirim banyak foto/video sekaligus),
    # tampung dulu lewat buffer, jangan diproses satu-satu.
    if message.media_group_id is not None:
        await handle_media_group_item(update, context, "video")
        return

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
    max_caption_len = get_setting("max_caption_len")
    if len(body) > max_caption_len:
        await message.reply_text(f"❌ Caption terlalu panjang (maks {max_caption_len} karakter).")
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
    app.add_handler(CommandHandler("pengaturan", settings_command))
    app.add_handler(CommandHandler("batalpengaturan", cancel_setting_command))
    app.add_handler(CallbackQueryHandler(category_selected, pattern=r"^cat_"))
    app.add_handler(CallbackQueryHandler(cancel_selected, pattern=r"^cancel$"))
    app.add_handler(CallbackQueryHandler(settings_menu_callback, pattern=r"^setting_"))
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