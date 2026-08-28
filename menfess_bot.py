"""
Bot Menfess (Confession Bot) - Telegram
----------------------------------------
Bot menerima pesan pribadi dari user, memvalidasi kata kunci trigger,
lalu memposting pesan tersebut secara anonim ke channel publik.

Fitur:
- Konfigurasi via environment variable / file .env (token TIDAK hardcode lagi)
- Mendukung teks & foto/audio/video (dengan caption)
- Nomor urut otomatis untuk tiap menfess (tersimpan di SQLite)
- Cooldown anti-spam per user
- Escaping otomatis agar teks user tidak merusak format pesan (fix bug Markdown)
- Perintah /stats khusus admin
- Error yang lebih informatif + notifikasi ke admin bila gagal posting
- Hanya bisa dipakai lewat chat pribadi (bukan grup)
- Menfess langsung tayang begitu dikirim (TIDAK butuh approval admin)
- BARU: User bisa /hapus menfess miliknya sendiri, tapi permintaan hapus
  itu baru dieksekusi setelah admin approve
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
if not ADMIN_IDS:
    raise RuntimeError(
        "ADMIN_IDS belum diset. Fitur approval hapus butuh minimal 1 admin. "
        "Isi ADMIN_IDS=123456789,987654321 di .env"
    )

CHANNEL_ID = int(CHANNEL_ID_RAW)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("menfess_bot")

DELETE_PENDING = "pending"


# ============================================================
# DATABASE (SQLite) - nomor urut menfess, cooldown, & status hapus
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
                created_at TEXT NOT NULL,
                channel_message_id INTEGER,
                deleted INTEGER NOT NULL DEFAULT 0,
                delete_request_status TEXT
            )
            """
        )
        # Migrasi ringan buat DB lama (sebelum fitur hapus ada)
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        migrations = {
            "channel_message_id": "ALTER TABLE posts ADD COLUMN channel_message_id INTEGER",
            "deleted": "ALTER TABLE posts ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0",
            "delete_request_status": "ALTER TABLE posts ADD COLUMN delete_request_status TEXT",
        }
        for col, ddl in migrations.items():
            if col not in existing_cols:
                conn.execute(ddl)
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


def set_channel_message_id(post_id: int, channel_message_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE posts SET channel_message_id = ? WHERE id = ?",
            (channel_message_id, post_id),
        )
        conn.commit()


def get_post(post_id: int) -> Optional[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()


def get_active_posts_by_user(user_id: int, limit: int = 20):
    """Menfess milik user yang masih tayang (belum dihapus)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM posts WHERE user_id = ? AND deleted = 0 ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def request_delete(post_id: int, user_id: int) -> str:
    """Return kode hasil: 'ok', 'not_owner', 'already_deleted', 'already_pending'."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT user_id, deleted, delete_request_status FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if row is None:
            return "not_found"
        owner_id, deleted, status = row
        if owner_id != user_id:
            return "not_owner"
        if deleted:
            return "already_deleted"
        if status == DELETE_PENDING:
            return "already_pending"
        conn.execute(
            "UPDATE posts SET delete_request_status = ? WHERE id = ?",
            (DELETE_PENDING, post_id),
        )
        conn.commit()
        return "ok"


def approve_delete(post_id: int) -> Optional[sqlite3.Row]:
    """Tandai deleted=1 kalau memang lagi pending. Return row (sebelum diupdate) atau None."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if row is None or row["delete_request_status"] != DELETE_PENDING:
            return None
        conn.execute(
            "UPDATE posts SET deleted = 1, delete_request_status = NULL WHERE id = ?",
            (post_id,),
        )
        conn.commit()
        return row


def reject_delete(post_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT delete_request_status FROM posts WHERE id = ?", (post_id,)
        ).fetchone()
        if not row or row[0] != DELETE_PENDING:
            return False
        conn.execute(
            "UPDATE posts SET delete_request_status = NULL WHERE id = ?", (post_id,)
        )
        conn.commit()
        return True


def get_stats() -> Tuple[int, int, int]:
    """Return (total_posts_aktif, posts_today, delete_request_pending)."""
    today = datetime.now().date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM posts WHERE deleted = 0").fetchone()[0]
        today_count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE created_at LIKE ? AND deleted = 0", (f"{today}%",)
        ).fetchone()[0]
        pending_delete = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE delete_request_status = ?", (DELETE_PENDING,)
        ).fetchone()[0]
    return total, today_count, pending_delete


# ============================================================
# HELPER
# ============================================================
def strip_trigger(text: str) -> str:
    """Hapus kata kunci trigger di awal pesan (case-insensitive)."""
    return text[len(TRIGGER_WORD):].strip()


def format_menfess(post_number: int, body: str) -> str:
    safe_body = html.escape(body) if body else "<i>(tanpa teks)</i>"
    return f"💌 <b>Menfess #{post_number}</b>\n\n{safe_body}"


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str, keyboard: Optional[InlineKeyboardMarkup] = None) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard)
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


def delete_decision_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Setujui hapus", callback_data=f"mf_delapprove:{post_id}"),
                InlineKeyboardButton("❌ Tolak", callback_data=f"mf_delreject:{post_id}"),
            ]
        ]
    )


# ============================================================
# HANDLERS - perintah dasar
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Selamat datang di bot Menfess!\n\n"
        f"Kirim pesan (teks, foto, lagu/audio, atau video+caption) diawali kata kunci "
        f"<b>{html.escape(TRIGGER_WORD)}</b> untuk memposting secara anonim ke channel.\n\n"
        "Mau hapus menfess yang udah kamu kirim? Ketik /hapus — permintaan hapus akan "
        "ditinjau admin dulu sebelum benar-benar dihapus dari channel.\n\n"
        f"⏳ Ada jeda {COOLDOWN_SECONDS} detik antar-pengiriman untuk mencegah spam.\n"
        "Ketik /help untuk bantuan lebih lanjut.",
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Cara pakai:</b>\n"
        f"1. Ketik pesanmu (atau caption foto/lagu/video) diawali <b>{html.escape(TRIGGER_WORD)}</b>\n"
        "2. Kirim ke bot ini lewat chat pribadi (teks, foto, lagu/audio, atau video)\n"
        "3. Bot akan langsung memposting pesanmu secara anonim ke channel\n\n"
        "🗑️ <b>Mau hapus menfess yang sudah tayang?</b>\n"
        "Ketik /hapus — bot akan menampilkan menfess kamu yang masih tayang lengkap "
        "dengan tombol buat mengajukan hapus. Permintaan itu baru dieksekusi kalau "
        "disetujui admin.\n\n"
        "Identitas kamu <b>tidak</b> ditampilkan di channel, tapi tetap tercatat "
        "di sistem untuk keperluan moderasi bila ada penyalahgunaan.",
        parse_mode=ParseMode.HTML,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Perintah ini khusus admin.")
        return
    total, today_count, pending_delete = get_stats()
    await update.message.reply_text(
        f"📊 Statistik Menfess\nTotal tayang: {total}\nHari ini: {today_count}\n"
        f"Permintaan hapus menunggu: {pending_delete}"
    )


async def hapus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    posts = get_active_posts_by_user(user.id)

    if not posts:
        await update.message.reply_text("Kamu belum punya menfess yang masih tayang di channel.")
        return

    await update.message.reply_text(
        f"Kamu punya {len(posts)} menfess yang masih tayang. Tekan tombol di bawah "
        "menfess yang mau kamu ajukan hapus (perlu persetujuan admin dulu):"
    )
    for row in posts:
        preview_text = row["message"] or "(tanpa teks)"
        if len(preview_text) > 200:
            preview_text = preview_text[:200] + "..."
        label = f"Menfess #{row['id']} [{row['content_type']}]\n{preview_text}"

        if row["delete_request_status"] == DELETE_PENDING:
            await update.message.reply_text(
                html.escape(label) + "\n\n⏳ Permintaan hapus sedang menunggu admin."
            )
            continue

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🗑️ Ajukan hapus", callback_data=f"mf_delreq:{row['id']}")]]
        )
        await update.message.reply_text(html.escape(label), reply_markup=keyboard)


# ============================================================
# HANDLERS - submit menfess per tipe konten (langsung tayang)
# ============================================================
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
        sent = await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=format_menfess(post_number, body),
            parse_mode=ParseMode.HTML,
        )
        set_channel_message_id(post_number, sent.message_id)
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
        sent = await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=file_id,
            caption=format_menfess(post_number, body),
            parse_mode=ParseMode.HTML,
        )
        set_channel_message_id(post_number, sent.message_id)
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
        sent = await context.bot.send_audio(
            chat_id=CHANNEL_ID,
            audio=file_id,
            caption=format_menfess(post_number, body),
            parse_mode=ParseMode.HTML,
        )
        set_channel_message_id(post_number, sent.message_id)
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
        sent = await context.bot.send_video(
            chat_id=CHANNEL_ID,
            video=file_id,
            caption=format_menfess(post_number, body),
            parse_mode=ParseMode.HTML,
        )
        set_channel_message_id(post_number, sent.message_id)
        await message.reply_text(f"🚀 Menfess #{post_number} (video) berhasil terbit di channel!")
    except Forbidden:
        await message.reply_text("❌ Gagal posting: bot belum jadi admin di channel.")
    except TelegramError as e:
        logger.error("TelegramError saat posting video: %s", e)
        await message.reply_text("❌ Gagal posting. Coba lagi nanti.")
        await notify_admins(context, f"⚠️ Gagal posting video menfess dari user {user.id}: {e}")


# ============================================================
# HANDLER - user mengajukan hapus menfess miliknya sendiri
# ============================================================
async def handle_delete_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    _, _, post_id_str = query.data.partition(":")
    try:
        post_id = int(post_id_str)
    except ValueError:
        await query.answer("ID tidak valid.", show_alert=True)
        return

    result = request_delete(post_id, user.id)

    if result == "not_found":
        await query.answer("Menfess tidak ditemukan.", show_alert=True)
        return
    if result == "not_owner":
        await query.answer("Ini bukan menfess kamu.", show_alert=True)
        return
    if result == "already_deleted":
        await query.answer("Menfess ini sudah dihapus sebelumnya.", show_alert=True)
        return
    if result == "already_pending":
        await query.answer("Sudah ada permintaan hapus yang menunggu admin buat menfess ini.", show_alert=True)
        return

    # result == "ok"
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass
    await query.answer("Permintaan hapus dikirim ke admin.")
    await query.message.reply_text(
        "📨 Permintaan hapus sudah dikirim ke admin. Kamu akan dikabari begitu diputuskan."
    )

    row = get_post(post_id)
    preview_text = (row["message"] or "(tanpa teks)")[:300]
    who = f"@{row['username']}" if row["username"] else "(tanpa username)"
    text = (
        f"🗑️ <b>Permintaan hapus menfess #{post_id}</b>\n"
        f"Diajukan oleh: {who}\n\n"
        f"{html.escape(preview_text)}"
    )
    await notify_admins(context, text, keyboard=delete_decision_keyboard(post_id))


# ============================================================
# HANDLER - keputusan admin atas permintaan hapus
# ============================================================
async def handle_delete_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = update.effective_user

    if admin.id not in ADMIN_IDS:
        await query.answer("⛔ Kamu bukan admin.", show_alert=True)
        return

    action, _, post_id_str = query.data.partition(":")
    try:
        post_id = int(post_id_str)
    except ValueError:
        await query.answer("ID tidak valid.", show_alert=True)
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass

    admin_label = f"@{admin.username}" if admin.username else admin.first_name

    if action == "mf_delapprove":
        row = approve_delete(post_id)
        if row is None:
            await query.answer("Permintaan ini sudah tidak berlaku (mungkin sudah diproses).", show_alert=True)
            return

        deleted_ok = False
        if row["channel_message_id"]:
            try:
                await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=row["channel_message_id"])
                deleted_ok = True
            except Forbidden:
                await query.message.reply_text("❌ Gagal hapus: bot bukan admin di channel.")
            except TelegramError as e:
                logger.error("Gagal hapus pesan channel untuk post %s: %s", post_id, e)
                await query.message.reply_text(f"⚠️ Gagal hapus pesan di channel: {e}")
        else:
            await query.message.reply_text("⚠️ Tidak ada catatan message_id di channel untuk menfess ini.")

        await query.answer("Disetujui.")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ Permintaan hapus menfess #{post_id} disetujui oleh {admin_label}"
            + (" dan sudah dihapus dari channel." if deleted_ok else " (tapi gagal dihapus otomatis, cek manual)."),
            reply_to_message_id=query.message.message_id,
        )
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=f"✅ Menfess #{post_id} kamu sudah dihapus dari channel sesuai permintaanmu.",
            )
        except Forbidden:
            pass

    elif action == "mf_delreject":
        ok = reject_delete(post_id)
        if not ok:
            await query.answer("Permintaan ini sudah tidak berlaku (mungkin sudah diproses).", show_alert=True)
            return
        row = get_post(post_id)
        await query.answer("Ditolak.")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"❌ Permintaan hapus menfess #{post_id} ditolak oleh {admin_label}, menfess tetap tayang.",
            reply_to_message_id=query.message.message_id,
        )
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text=f"❌ Permintaan hapus menfess #{post_id} ditolak admin, jadi tetap tayang di channel.",
            )
        except Forbidden:
            pass
    else:
        await query.answer("Aksi tidak dikenali.", show_alert=True)


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
    app.add_handler(CommandHandler("hapus", hapus_command))
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
    app.add_handler(CallbackQueryHandler(handle_delete_request, pattern=r"^mf_delreq:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_delete_decision, pattern=r"^mf_del(approve|reject):\d+$"))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info("Bot menfess siap & jalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
