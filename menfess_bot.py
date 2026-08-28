"""
Bot Menfess (Confession Bot) - Telegram
----------------------------------------
Bot menerima pesan pribadi dari user, memvalidasi kata kunci trigger,
mengirim ke admin untuk disetujui, lalu memposting pesan tersebut
secara anonim ke channel publik.

Fitur:
- Konfigurasi via environment variable / file .env (token TIDAK hardcode lagi)
- Mendukung teks & foto/audio/video (dengan caption)
- Nomor urut otomatis untuk tiap menfess yang APPROVED (tersimpan di SQLite)
- Cooldown anti-spam per user
- Escaping otomatis agar teks user tidak merusak format pesan (fix bug Markdown)
- Perintah /stats khusus admin
- Error yang lebih informatif + notifikasi ke admin bila gagal posting
- Hanya bisa dipakai lewat chat pribadi (bukan grup)
- BARU: Sistem approval sebelum posting (admin approve/reject via tombol)
- BARU: Fitur balas menfess lain, format: "#fess to/123 isi balasan"
- BARU: Tombol "↪️ Balas menfess ini" di tiap post channel (deep link, tanpa perlu ketik to/123 manual)
"""

import html
import logging
import os
import re
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
        "ADMIN_IDS belum diset. Sistem approval butuh minimal 1 admin. "
        "Isi ADMIN_IDS=123456789,987654321 di .env"
    )

CHANNEL_ID = int(CHANNEL_ID_RAW)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("menfess_bot")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

REPLY_PATTERN = re.compile(r"^to/(\d+)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)


# ============================================================
# DATABASE (SQLite)
# ============================================================
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_number INTEGER,
                user_id INTEGER NOT NULL,
                username TEXT,
                content_type TEXT NOT NULL,
                message TEXT,
                file_id TEXT,
                reply_to INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                decided_at TEXT
            )
            """
        )
        # Migrasi ringan buat DB lama (sebelum fitur approval/reply ada)
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(posts)")}
        migrations = {
            "post_number": "ALTER TABLE posts ADD COLUMN post_number INTEGER",
            "file_id": "ALTER TABLE posts ADD COLUMN file_id TEXT",
            "reply_to": "ALTER TABLE posts ADD COLUMN reply_to INTEGER",
            "status": "ALTER TABLE posts ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'",
            "decided_at": "ALTER TABLE posts ADD COLUMN decided_at TEXT",
        }
        for col, ddl in migrations.items():
            if col not in existing_cols:
                conn.execute(ddl)
        conn.commit()


def get_last_post_time(user_id: int) -> Optional[float]:
    """Waktu submit terakhir user (pending/approved/rejected semua dihitung, buat cooldown)."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT created_at FROM posts WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return datetime.fromisoformat(row[0]).timestamp()


def save_pending_post(
    user_id: int,
    username: str,
    content_type: str,
    message: str,
    file_id: Optional[str],
    reply_to: Optional[int],
) -> int:
    """Simpan menfess sebagai draft pending. Return internal row id (BUKAN nomor urut)."""
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """
            INSERT INTO posts
                (post_number, user_id, username, content_type, message, file_id, reply_to, status, created_at)
            VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                content_type,
                message,
                file_id,
                reply_to,
                STATUS_PENDING,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        return cur.lastrowid


def get_post(row_id: int) -> Optional[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM posts WHERE id = ?", (row_id,)).fetchone()


def get_approved_post_by_number(post_number: int) -> Optional[sqlite3.Row]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM posts WHERE post_number = ? AND status = ?",
            (post_number, STATUS_APPROVED),
        ).fetchone()


def approve_post(row_id: int) -> Optional[int]:
    """Set status approved & kasih nomor urut baru. Return nomor urut, atau None kalau row tidak pending."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT status FROM posts WHERE id = ?", (row_id,)
        ).fetchone()
        if not row or row[0] != STATUS_PENDING:
            return None
        next_number = conn.execute(
            "SELECT COALESCE(MAX(post_number), 0) + 1 FROM posts"
        ).fetchone()[0]
        conn.execute(
            "UPDATE posts SET status = ?, post_number = ?, decided_at = ? WHERE id = ?",
            (STATUS_APPROVED, next_number, datetime.now().isoformat(), row_id),
        )
        conn.commit()
        return next_number


def reject_post(row_id: int) -> bool:
    """Set status rejected. Return True kalau berhasil (sebelumnya pending)."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT status FROM posts WHERE id = ?", (row_id,)
        ).fetchone()
        if not row or row[0] != STATUS_PENDING:
            return False
        conn.execute(
            "UPDATE posts SET status = ?, decided_at = ? WHERE id = ?",
            (STATUS_REJECTED, datetime.now().isoformat(), row_id),
        )
        conn.commit()
        return True


def get_stats() -> Tuple[int, int, int]:
    """Return (total_approved, approved_today, pending_count)."""
    today = datetime.now().date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status = ?", (STATUS_APPROVED,)
        ).fetchone()[0]
        today_count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status = ? AND created_at LIKE ?",
            (STATUS_APPROVED, f"{today}%"),
        ).fetchone()[0]
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status = ?", (STATUS_PENDING,)
        ).fetchone()[0]
    return total, today_count, pending_count


# ============================================================
# HELPER
# ============================================================
def strip_trigger(text: str) -> str:
    """Hapus kata kunci trigger di awal pesan (case-insensitive)."""
    return text[len(TRIGGER_WORD):].strip()


def parse_reply(body: str) -> Tuple[str, Optional[int]]:
    """Deteksi format 'to/123 isi pesan'. Return (body_bersih, reply_to_atau_None)."""
    match = REPLY_PATTERN.match(body)
    if not match:
        return body, None
    reply_to = int(match.group(1))
    remaining = match.group(2).strip()
    return remaining, reply_to


def format_menfess(post_number: int, body: str, reply_to: Optional[int] = None) -> str:
    safe_body = html.escape(body) if body else "<i>(tanpa teks)</i>"
    header = f"💌 <b>Menfess #{post_number}</b>"
    if reply_to:
        header += f"\n↪️ Balasan untuk Menfess #{reply_to}"
    return f"{header}\n\n{safe_body}"


def format_preview(row_id: int, content_type: str, body: str, reply_to: Optional[int], username: str) -> str:
    safe_body = html.escape(body) if body else "<i>(tanpa teks)</i>"
    who = f"@{username}" if username else "(tanpa username)"
    lines = [
        f"🕵️ <b>Menfess baru menunggu persetujuan</b>",
        f"ID: <code>{row_id}</code> | Tipe: {content_type} | Dari: {who}",
    ]
    if reply_to:
        lines.append(f"↪️ Balasan untuk Menfess #{reply_to}")
    lines.append("")
    lines.append(safe_body)
    return "\n".join(lines)


def approval_keyboard(row_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Setujui", callback_data=f"mf_approve:{row_id}"),
                InlineKeyboardButton("❌ Tolak", callback_data=f"mf_reject:{row_id}"),
            ]
        ]
    )


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except TelegramError:
            logger.warning("Gagal mengirim notifikasi ke admin %s", admin_id)


async def send_for_approval(
    context: ContextTypes.DEFAULT_TYPE,
    row_id: int,
    content_type: str,
    body: str,
    file_id: Optional[str],
    reply_to: Optional[int],
    username: str,
) -> None:
    caption = format_preview(row_id, content_type, body, reply_to, username)
    keyboard = approval_keyboard(row_id)
    for admin_id in ADMIN_IDS:
        try:
            if content_type == "text":
                await context.bot.send_message(
                    chat_id=admin_id, text=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
            elif content_type == "photo":
                await context.bot.send_photo(
                    chat_id=admin_id, photo=file_id, caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
            elif content_type == "audio":
                await context.bot.send_audio(
                    chat_id=admin_id, audio=file_id, caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
            elif content_type == "video":
                await context.bot.send_video(
                    chat_id=admin_id, video=file_id, caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
        except TelegramError:
            logger.warning("Gagal kirim preview approval ke admin %s", admin_id)


def check_cooldown(user_id: int) -> float:
    """Return sisa detik cooldown (0 kalau boleh kirim)."""
    last_time = get_last_post_time(user_id)
    if last_time is None:
        return 0
    elapsed = time.time() - last_time
    remaining = COOLDOWN_SECONDS - elapsed
    return max(0.0, remaining)


def reply_button(bot_username: str, post_number: int) -> InlineKeyboardMarkup:
    """Tombol deep-link: klik -> buka chat pribadi bot, otomatis siap nerima balasan."""
    url = f"https://t.me/{bot_username}?start=reply_{post_number}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("↪️ Balas menfess ini", url=url)]])


async def publish_to_channel(
    context: ContextTypes.DEFAULT_TYPE, row, post_number: int
) -> None:
    text = format_menfess(post_number, row["message"] or "", row["reply_to"])
    content_type = row["content_type"]
    bot_username = (await context.bot.get_me()).username
    keyboard = reply_button(bot_username, post_number)
    if content_type == "text":
        await context.bot.send_message(chat_id=CHANNEL_ID, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif content_type == "photo":
        await context.bot.send_photo(chat_id=CHANNEL_ID, photo=row["file_id"], caption=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif content_type == "audio":
        await context.bot.send_audio(chat_id=CHANNEL_ID, audio=row["file_id"], caption=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif content_type == "video":
        await context.bot.send_video(chat_id=CHANNEL_ID, video=row["file_id"], caption=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# ============================================================
# ALUR SUBMIT (dipakai bareng oleh text/photo/audio/video)
# ============================================================
async def submit_menfess(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    content_type: str,
    raw_body: str,
    file_id: Optional[str],
    max_len: int,
    reject_label: str,
    forced_reply_to: Optional[int] = None,
) -> None:
    message = update.message
    user = update.effective_user

    remaining = check_cooldown(user.id)
    if remaining > 0:
        await message.reply_text(f"⏳ Tunggu {int(remaining)} detik lagi sebelum kirim menfess baru.")
        return

    if forced_reply_to is not None:
        # Datang dari tombol "Balas menfess ini" -> seluruh pesan dianggap isi balasan
        body, reply_to = raw_body, forced_reply_to
    else:
        body, reply_to = parse_reply(raw_body)

    if content_type == "text" and not body:
        await message.reply_text("❌ Isi menfess tidak boleh kosong.")
        return

    if len(body) > max_len:
        await message.reply_text(f"❌ {reject_label} terlalu panjang (maks {max_len} karakter).")
        return

    if reply_to is not None and get_approved_post_by_number(reply_to) is None:
        await message.reply_text(f"❌ Menfess #{reply_to} tidak ditemukan, jadi tidak bisa dibalas.")
        return

    row_id = save_pending_post(user.id, user.username or "", content_type, body, file_id, reply_to)

    await send_for_approval(context, row_id, content_type, body, file_id, reply_to, user.username or "")

    await message.reply_text(
        "📨 Menfess kamu sudah dikirim dan menunggu persetujuan admin. "
        "Kamu akan dikabari begitu diputuskan."
    )


# ============================================================
# HANDLERS - perintah dasar
# ============================================================
START_REPLY_PATTERN = re.compile(r"^reply_(\d+)$")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deep link dari tombol "↪️ Balas menfess ini" di channel: /start reply_12
    if context.args:
        match = START_REPLY_PATTERN.match(context.args[0])
        if match:
            post_number = int(match.group(1))
            if get_approved_post_by_number(post_number) is None:
                await update.message.reply_text(
                    f"❌ Menfess #{post_number} tidak ditemukan atau sudah tidak bisa dibalas."
                )
                return
            context.user_data["pending_reply_to"] = post_number
            await update.message.reply_text(
                f"✍️ Oke, sekarang kirim balasan kamu untuk <b>Menfess #{post_number}</b>.\n"
                "Boleh teks, foto, lagu/audio, atau video — langsung kirim aja, "
                "tidak perlu ketik trigger atau to/ lagi.\n\n"
                "Ketik /batal kalau berubah pikiran.",
                parse_mode=ParseMode.HTML,
            )
            return

    await update.message.reply_text(
        "👋 Selamat datang di bot Menfess!\n\n"
        f"Kirim pesan (teks, foto, lagu/audio, atau video+caption) diawali kata kunci "
        f"<b>{html.escape(TRIGGER_WORD)}</b> untuk mengirim menfess anonim.\n\n"
        "Menfess kamu akan ditinjau admin dulu sebelum tayang di channel.\n\n"
        f"↪️ Mau balas menfess lain? Paling gampang tinggal tekan tombol "
        f"\"↪️ Balas menfess ini\" di postingan channel. Atau manual, tambahkan "
        f"<code>to/&lt;nomor&gt;</code> setelah trigger, contoh:\n"
        f"<code>{html.escape(TRIGGER_WORD)} to/12 semangat ya!</code>\n\n"
        f"⏳ Ada jeda {COOLDOWN_SECONDS} detik antar-pengiriman untuk mencegah spam.\n"
        "Ketik /help untuk bantuan lebih lanjut.",
        parse_mode=ParseMode.HTML,
    )


async def cancel_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.pop("pending_reply_to", None) is not None:
        await update.message.reply_text("👌 Oke, dibatalkan. Kamu bisa kirim menfess baru kapan saja.")
    else:
        await update.message.reply_text("Tidak ada balasan yang sedang menunggu untuk dibatalkan.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Cara pakai:</b>\n"
        f"1. Ketik pesanmu (atau caption foto/lagu/video) diawali <b>{html.escape(TRIGGER_WORD)}</b>\n"
        "2. Kirim ke bot ini lewat chat pribadi (teks, foto, lagu/audio, atau video)\n"
        "3. Admin akan meninjau dulu sebelum menfess tayang di channel\n\n"
        "↪️ <b>Mau balas menfess orang lain?</b> Ada 2 cara:\n"
        "• Paling gampang: tekan tombol \"↪️ Balas menfess ini\" di postingan channel, "
        "lalu langsung kirim balasanmu (tanpa trigger/to/ lagi). Batalkan dengan /batal.\n"
        f"• Manual: tambahkan <code>to/&lt;nomor&gt;</code> setelah trigger, contoh:\n"
        f"  <code>{html.escape(TRIGGER_WORD)} to/12 isi balasan</code>\n\n"
        "Identitas kamu <b>tidak</b> ditampilkan di channel, tapi tetap tercatat "
        "di sistem untuk keperluan moderasi bila ada penyalahgunaan.",
        parse_mode=ParseMode.HTML,
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Perintah ini khusus admin.")
        return
    total, today_count, pending_count = get_stats()
    await update.message.reply_text(
        f"📊 Statistik Menfess\nTotal tayang: {total}\nTayang hari ini: {today_count}\nMenunggu approval: {pending_count}"
    )


# ============================================================
# HANDLERS - submit menfess per tipe konten
# ============================================================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text = message.text or ""

    pending_reply_to = context.user_data.pop("pending_reply_to", None)
    if pending_reply_to is not None:
        await submit_menfess(
            update, context, "text", text.strip(), None, MAX_TEXT_LEN, "Pesan",
            forced_reply_to=pending_reply_to,
        )
        return

    if not text.lower().startswith(TRIGGER_WORD):
        await message.reply_text(
            f'❌ Pesan ditolak. Harus diawali kata kunci "{TRIGGER_WORD}".'
        )
        return

    raw_body = strip_trigger(text)
    await submit_menfess(update, context, "text", raw_body, None, MAX_TEXT_LEN, "Pesan")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    caption = message.caption or ""
    file_id = message.photo[-1].file_id

    pending_reply_to = context.user_data.pop("pending_reply_to", None)
    if pending_reply_to is not None:
        await submit_menfess(
            update, context, "photo", caption.strip(), file_id, MAX_CAPTION_LEN, "Caption",
            forced_reply_to=pending_reply_to,
        )
        return

    if not caption.lower().startswith(TRIGGER_WORD):
        await message.reply_text(
            f'❌ Foto ditolak. Caption harus diawali kata kunci "{TRIGGER_WORD}".'
        )
        return

    raw_body = strip_trigger(caption)
    await submit_menfess(update, context, "photo", raw_body, file_id, MAX_CAPTION_LEN, "Caption")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    caption = message.caption or ""
    file_id = message.audio.file_id

    pending_reply_to = context.user_data.pop("pending_reply_to", None)
    if pending_reply_to is not None:
        await submit_menfess(
            update, context, "audio", caption.strip(), file_id, MAX_CAPTION_LEN, "Caption",
            forced_reply_to=pending_reply_to,
        )
        return

    if not caption.lower().startswith(TRIGGER_WORD):
        await message.reply_text(
            f'❌ Lagu ditolak. Caption harus diawali kata kunci "{TRIGGER_WORD}".'
        )
        return

    raw_body = strip_trigger(caption)
    await submit_menfess(update, context, "audio", raw_body, file_id, MAX_CAPTION_LEN, "Caption")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    caption = message.caption or ""
    file_id = message.video.file_id

    pending_reply_to = context.user_data.pop("pending_reply_to", None)
    if pending_reply_to is not None:
        await submit_menfess(
            update, context, "video", caption.strip(), file_id, MAX_CAPTION_LEN, "Caption",
            forced_reply_to=pending_reply_to,
        )
        return

    if not caption.lower().startswith(TRIGGER_WORD):
        await message.reply_text(
            f'❌ Video ditolak. Caption harus diawali kata kunci "{TRIGGER_WORD}".'
        )
        return

    raw_body = strip_trigger(caption)
    await submit_menfess(update, context, "video", raw_body, file_id, MAX_CAPTION_LEN, "Caption")


# ============================================================
# HANDLER - keputusan admin (approve/reject)
# ============================================================
async def handle_admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin = update.effective_user

    if admin.id not in ADMIN_IDS:
        await query.answer("⛔ Kamu bukan admin.", show_alert=True)
        return

    action, _, row_id_str = query.data.partition(":")
    try:
        row_id = int(row_id_str)
    except ValueError:
        await query.answer("ID tidak valid.", show_alert=True)
        return

    row = get_post(row_id)
    if row is None:
        await query.answer("Menfess tidak ditemukan.", show_alert=True)
        return

    if row["status"] != STATUS_PENDING:
        await query.answer("Menfess ini sudah diputuskan sebelumnya.", show_alert=True)
        return

    # Hilangkan tombol dulu supaya tidak diklik dobel oleh admin lain
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass

    admin_label = f"@{admin.username}" if admin.username else admin.first_name

    if action == "mf_approve":
        post_number = approve_post(row_id)
        if post_number is None:
            await query.answer("Sudah diproses admin lain.", show_alert=True)
            return
        row = get_post(row_id)  # refresh biar dapat post_number & status terbaru
        try:
            await publish_to_channel(context, row, post_number)
            await query.answer("Menfess disetujui & tayang.")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"✅ Disetujui oleh {admin_label} — tayang sebagai Menfess #{post_number}",
                reply_to_message_id=query.message.message_id,
            )
            try:
                await context.bot.send_message(
                    chat_id=row["user_id"],
                    text=f"✅ Menfess kamu disetujui dan sudah tayang sebagai Menfess #{post_number}!",
                )
            except Forbidden:
                pass  # user memblokir bot, tidak apa-apa
        except Forbidden:
            await notify_admins(context, "❌ Gagal posting: bot belum jadi admin di channel.")
        except TelegramError as e:
            logger.error("TelegramError saat publish menfess #%s: %s", row_id, e)
            await notify_admins(context, f"⚠️ Gagal posting menfess (id={row_id}): {e}")

    elif action == "mf_reject":
        ok = reject_post(row_id)
        if not ok:
            await query.answer("Sudah diproses admin lain.", show_alert=True)
            return
        await query.answer("Menfess ditolak.")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"❌ Ditolak oleh {admin_label}",
            reply_to_message_id=query.message.message_id,
        )
        try:
            await context.bot.send_message(
                chat_id=row["user_id"],
                text="❌ Maaf, menfess kamu tidak disetujui admin untuk tayang.",
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
    app.add_handler(CommandHandler("batal", cancel_reply))
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
    app.add_handler(CallbackQueryHandler(handle_admin_decision, pattern=r"^mf_(approve|reject):\d+$"))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info("Bot menfess siap & jalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
