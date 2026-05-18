import os
import re
import sqlite3
import logging
import asyncio
import secrets
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from aiohttp import web

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

# ============================================================
# Sexy Prime Ads Bot
# Bot de anúncios com mídia, botão URL, agendamento, fixação,
# controle por dono/admin e destinos aprovados.
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
AGENCY_NAME = os.getenv("AGENCY_NAME", "Sexy Prime").strip()
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/SXP_suporte").strip()
TIMEZONE_NAME = os.getenv("TIMEZONE", "America/Sao_Paulo").strip()
DB_PATH = os.getenv("DB_PATH", "data/sexy_prime_ads.db").strip()

# Render/Webhook
# RUN_MODE=polling para rodar localmente. RUN_MODE=webhook para Render.
RUN_MODE = os.getenv("RUN_MODE", "polling").strip().lower()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "").strip()
PORT = int(os.getenv("PORT", "10000") or 10000)

if not WEBHOOK_SECRET and BOT_TOKEN:
    WEBHOOK_SECRET = secrets.token_urlsafe(24)

if not WEBHOOK_PATH:
    WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"
elif not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

TZ = ZoneInfo(TIMEZONE_NAME)

Path("logs").mkdir(exist_ok=True)
Path("data").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("sexy-prime-ads")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today_prefix() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("t.me/"):
        return "https://" + url
    if url.startswith("@"):
        return "https://t.me/" + url[1:]
    return url


def is_valid_url(url: str) -> bool:
    url = normalize_url(url)
    return bool(re.match(r"^https?://[^\s]+\.[^\s]+", url) or url.startswith("https://t.me/"))


def short(text: str, limit: int = 45) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def caption_limit(text: str) -> str:
    # Telegram limita legenda de foto/vídeo em 1024 caracteres.
    text = str(text or "").strip()
    if len(text) <= 1024:
        return text
    return text[:1000].rstrip() + "\n..."


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def conn(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def init_db(self):
        with self.conn() as con:
            cur = con.cursor()

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    name TEXT,
                    role TEXT DEFAULT 'admin',
                    active INTEGER DEFAULT 1,
                    created_at TEXT
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    media_file_id TEXT NOT NULL,
                    description TEXT NOT NULL,
                    button_text TEXT,
                    button_url TEXT,
                    pin_message INTEGER DEFAULT 1,
                    delete_previous INTEGER DEFAULT 1,
                    active INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER UNIQUE NOT NULL,
                    chat_title TEXT,
                    chat_type TEXT,
                    approved INTEGER DEFAULT 0,
                    active INTEGER DEFAULT 1,
                    can_pin INTEGER DEFAULT 0,
                    added_at TEXT,
                    updated_at TEXT
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ad_id INTEGER NOT NULL,
                    hour INTEGER NOT NULL,
                    minute INTEGER NOT NULL,
                    days TEXT DEFAULT '0,1,2,3,4,5,6',
                    active INTEGER DEFAULT 1,
                    created_at TEXT,
                    FOREIGN KEY(ad_id) REFERENCES ads(id)
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS interval_schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ad_id INTEGER NOT NULL,
                    interval_hours INTEGER NOT NULL,
                    active INTEGER DEFAULT 1,
                    created_at TEXT,
                    updated_at TEXT,
                    last_run_at TEXT,
                    FOREIGN KEY(ad_id) REFERENCES ads(id)
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS post_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ad_id INTEGER,
                    chat_id INTEGER,
                    message_id INTEGER,
                    status TEXT,
                    error_message TEXT,
                    created_at TEXT
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS last_posts (
                    chat_id INTEGER PRIMARY KEY,
                    message_id INTEGER,
                    ad_id INTEGER,
                    posted_at TEXT
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )

            if OWNER_ID:
                cur.execute(
                    """
                    INSERT OR IGNORE INTO admins (user_id, name, role, active, created_at)
                    VALUES (?, ?, 'owner', 1, ?)
                    """,
                    (OWNER_ID, "Dono", now_iso()),
                )

            con.commit()

    # ---------- Admins ----------
    def is_admin(self, user_id: int) -> bool:
        if user_id == OWNER_ID:
            return True
        with self.conn() as con:
            row = con.execute(
                "SELECT active FROM admins WHERE user_id=? AND active=1",
                (user_id,),
            ).fetchone()
            return bool(row)

    def add_admin(self, user_id: int, name: str = ""):
        with self.conn() as con:
            con.execute(
                """
                INSERT INTO admins (user_id, name, role, active, created_at)
                VALUES (?, ?, 'admin', 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET active=1, name=excluded.name
                """,
                (user_id, name, now_iso()),
            )
            con.commit()

    def remove_admin(self, user_id: int):
        with self.conn() as con:
            con.execute("UPDATE admins SET active=0 WHERE user_id=?", (user_id,))
            con.commit()

    def list_admins(self):
        with self.conn() as con:
            return con.execute(
                "SELECT * FROM admins WHERE active=1 ORDER BY role DESC, user_id ASC"
            ).fetchall()

    # ---------- Ads ----------
    def create_ad(self, data: dict) -> int:
        with self.conn() as con:
            cur = con.execute(
                """
                INSERT INTO ads (
                    title, media_type, media_file_id, description,
                    button_text, button_url, pin_message, delete_previous,
                    active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    data["title"],
                    data["media_type"],
                    data["media_file_id"],
                    data["description"],
                    data.get("button_text") or "",
                    data.get("button_url") or "",
                    int(data.get("pin_message", 1)),
                    int(data.get("delete_previous", 1)),
                    now_iso(),
                    now_iso(),
                ),
            )
            con.commit()
            return int(cur.lastrowid)

    def update_ad_field(self, ad_id: int, field: str, value):
        allowed = {
            "title",
            "media_type",
            "media_file_id",
            "description",
            "button_text",
            "button_url",
            "pin_message",
            "delete_previous",
            "active",
        }
        if field not in allowed:
            raise ValueError("Campo inválido.")
        with self.conn() as con:
            con.execute(
                f"UPDATE ads SET {field}=?, updated_at=? WHERE id=?",
                (value, now_iso(), ad_id),
            )
            con.commit()

    def get_ad(self, ad_id: int):
        with self.conn() as con:
            return con.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()

    def list_ads(self, active_only: bool = False, limit: int = 20, offset: int = 0):
        with self.conn() as con:
            where = "WHERE active=1" if active_only else ""
            return con.execute(
                f"SELECT * FROM ads {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()

    def count_ads(self):
        with self.conn() as con:
            return con.execute("SELECT COUNT(*) AS c FROM ads").fetchone()["c"]

    # ---------- Targets ----------
    def upsert_target(self, chat_id: int, title: str, chat_type: str, can_pin: bool):
        with self.conn() as con:
            con.execute(
                """
                INSERT INTO targets (
                    chat_id, chat_title, chat_type, approved, active, can_pin,
                    added_at, updated_at
                )
                VALUES (?, ?, ?, 0, 1, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    chat_title=excluded.chat_title,
                    chat_type=excluded.chat_type,
                    active=1,
                    can_pin=excluded.can_pin,
                    updated_at=excluded.updated_at
                """,
                (chat_id, title, chat_type, int(can_pin), now_iso(), now_iso()),
            )
            con.commit()

    def set_target_approved(self, chat_id: int, approved: bool):
        with self.conn() as con:
            con.execute(
                "UPDATE targets SET approved=?, active=1, updated_at=? WHERE chat_id=?",
                (int(approved), now_iso(), chat_id),
            )
            con.commit()

    def set_target_active(self, chat_id: int, active: bool):
        with self.conn() as con:
            con.execute(
                "UPDATE targets SET active=?, updated_at=? WHERE chat_id=?",
                (int(active), now_iso(), chat_id),
            )
            con.commit()

    def mark_target_inactive(self, chat_id: int):
        self.set_target_active(chat_id, False)

    def get_target(self, chat_id: int):
        with self.conn() as con:
            return con.execute("SELECT * FROM targets WHERE chat_id=?", (chat_id,)).fetchone()

    def list_targets(self, approved=None, active=None, limit: int = 30):
        clauses = []
        params = []
        if approved is not None:
            clauses.append("approved=?")
            params.append(int(approved))
        if active is not None:
            clauses.append("active=?")
            params.append(int(active))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.conn() as con:
            return con.execute(
                f"SELECT * FROM targets {where} ORDER BY updated_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()

    def count_targets(self, approved=None, active=None):
        clauses = []
        params = []
        if approved is not None:
            clauses.append("approved=?")
            params.append(int(approved))
        if active is not None:
            clauses.append("active=?")
            params.append(int(active))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.conn() as con:
            return con.execute(f"SELECT COUNT(*) AS c FROM targets {where}", params).fetchone()["c"]

    # ---------- Schedules ----------
    def create_schedule(self, ad_id: int, hour: int, minute: int, days: str = "0,1,2,3,4,5,6") -> int:
        with self.conn() as con:
            cur = con.execute(
                """
                INSERT INTO schedules (ad_id, hour, minute, days, active, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (ad_id, hour, minute, days, now_iso()),
            )
            con.commit()
            return int(cur.lastrowid)

    def get_schedule(self, schedule_id: int):
        with self.conn() as con:
            return con.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()

    def list_schedules(self, active_only: bool = False):
        where = "WHERE s.active=1" if active_only else ""
        with self.conn() as con:
            return con.execute(
                f"""
                SELECT s.*, a.title AS ad_title
                FROM schedules s
                LEFT JOIN ads a ON a.id=s.ad_id
                {where}
                ORDER BY s.hour ASC, s.minute ASC
                """
            ).fetchall()

    def set_schedule_active(self, schedule_id: int, active: bool):
        with self.conn() as con:
            con.execute(
                "UPDATE schedules SET active=? WHERE id=?",
                (int(active), schedule_id),
            )
            con.commit()

    def count_schedules(self, active=True):
        with self.conn() as con:
            return con.execute(
                "SELECT COUNT(*) AS c FROM schedules WHERE active=?",
                (int(active),),
            ).fetchone()["c"]

    # ---------- Interval schedules ----------
    def create_interval_schedule(self, ad_id: int, interval_hours: int) -> int:
        interval_hours = int(interval_hours)
        if interval_hours < 1 or interval_hours > 24:
            raise ValueError("Intervalo inválido. Use de 1 a 24 horas.")
        with self.conn() as con:
            cur = con.execute(
                """
                INSERT INTO interval_schedules (
                    ad_id, interval_hours, active, created_at, updated_at, last_run_at
                )
                VALUES (?, ?, 1, ?, ?, NULL)
                """,
                (ad_id, interval_hours, now_iso(), now_iso()),
            )
            con.commit()
            return int(cur.lastrowid)

    def get_interval_schedule(self, interval_id: int):
        with self.conn() as con:
            return con.execute("SELECT * FROM interval_schedules WHERE id=?", (interval_id,)).fetchone()

    def list_interval_schedules(self, active_only: bool = False):
        where = "WHERE i.active=1" if active_only else ""
        with self.conn() as con:
            return con.execute(
                f"""
                SELECT i.*, a.title AS ad_title
                FROM interval_schedules i
                LEFT JOIN ads a ON a.id=i.ad_id
                {where}
                ORDER BY i.id DESC
                """
            ).fetchall()

    def set_interval_schedule_active(self, interval_id: int, active: bool):
        with self.conn() as con:
            con.execute(
                "UPDATE interval_schedules SET active=?, updated_at=? WHERE id=?",
                (int(active), now_iso(), interval_id),
            )
            con.commit()

    def disable_active_intervals_for_ad(self, ad_id: int) -> list[int]:
        with self.conn() as con:
            rows = con.execute(
                "SELECT id FROM interval_schedules WHERE ad_id=? AND active=1",
                (ad_id,),
            ).fetchall()
            ids = [int(r["id"]) for r in rows]
            con.execute(
                "UPDATE interval_schedules SET active=0, updated_at=? WHERE ad_id=? AND active=1",
                (now_iso(), ad_id),
            )
            con.commit()
            return ids

    def mark_interval_ran(self, interval_id: int):
        with self.conn() as con:
            con.execute(
                "UPDATE interval_schedules SET last_run_at=?, updated_at=? WHERE id=?",
                (now_iso(), now_iso(), interval_id),
            )
            con.commit()

    def count_interval_schedules(self, active=True):
        with self.conn() as con:
            return con.execute(
                "SELECT COUNT(*) AS c FROM interval_schedules WHERE active=?",
                (int(active),),
            ).fetchone()["c"]

    # ---------- Logs ----------
    def add_log(self, ad_id, chat_id, message_id, status, error_message=""):
        with self.conn() as con:
            con.execute(
                """
                INSERT INTO post_logs (ad_id, chat_id, message_id, status, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (ad_id, chat_id, message_id, status, error_message or "", now_iso()),
            )
            con.commit()

    def get_last_post(self, chat_id: int):
        with self.conn() as con:
            return con.execute(
                "SELECT * FROM last_posts WHERE chat_id=?",
                (chat_id,),
            ).fetchone()

    def set_last_post(self, chat_id: int, message_id: int, ad_id: int):
        with self.conn() as con:
            con.execute(
                """
                INSERT INTO last_posts (chat_id, message_id, ad_id, posted_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    message_id=excluded.message_id,
                    ad_id=excluded.ad_id,
                    posted_at=excluded.posted_at
                """,
                (chat_id, message_id, ad_id, now_iso()),
            )
            con.commit()

    def stats_today(self):
        with self.conn() as con:
            total = con.execute(
                "SELECT COUNT(*) AS c FROM post_logs WHERE created_at LIKE ?",
                (today_prefix() + "%",),
            ).fetchone()["c"]
            ok = con.execute(
                "SELECT COUNT(*) AS c FROM post_logs WHERE status='success' AND created_at LIKE ?",
                (today_prefix() + "%",),
            ).fetchone()["c"]
            fail = con.execute(
                "SELECT COUNT(*) AS c FROM post_logs WHERE status='error' AND created_at LIKE ?",
                (today_prefix() + "%",),
            ).fetchone()["c"]
            return {"total": total, "success": ok, "error": fail}

    def recent_errors(self, limit: int = 8):
        with self.conn() as con:
            return con.execute(
                """
                SELECT l.*, t.chat_title
                FROM post_logs l
                LEFT JOIN targets t ON t.chat_id=l.chat_id
                WHERE l.status='error'
                ORDER BY l.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()


db = Database(DB_PATH)


# ============================================================
# Menus
# ============================================================

def support_keyboard():
    if SUPPORT_URL and is_valid_url(SUPPORT_URL):
        return InlineKeyboardMarkup([[InlineKeyboardButton("💬 Suporte Sexy Prime", url=SUPPORT_URL)]])
    return None


def main_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Criar anúncio", callback_data="ad:new")],
            [
                InlineKeyboardButton("📋 Meus anúncios", callback_data="ad:list"),
                InlineKeyboardButton("⏰ Agendamentos", callback_data="sched:list"),
            ],
            [InlineKeyboardButton("🔁 Postagem automática", callback_data="interval:list")],
            [
                InlineKeyboardButton("📍 Destinos pendentes", callback_data="tg:pending"),
                InlineKeyboardButton("✅ Destinos aprovados", callback_data="tg:approved"),
            ],
            [
                InlineKeyboardButton("📊 Estatísticas", callback_data="stats"),
                InlineKeyboardButton("⚙️ Configurações", callback_data="settings"),
            ],
        ]
    )


def back_home():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao painel", callback_data="menu:home")]])


def yes_no_keyboard(prefix: str):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Sim", callback_data=f"{prefix}:1"),
                InlineKeyboardButton("❌ Não", callback_data=f"{prefix}:0"),
            ],
            [InlineKeyboardButton("Cancelar", callback_data="cancel")],
        ]
    )


def ad_keyboard(ad_id: int):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👁 Prévia", callback_data=f"ad:preview:{ad_id}"),
                InlineKeyboardButton("🚀 Postar agora", callback_data=f"ad:post:{ad_id}"),
            ],
            [
                InlineKeyboardButton("⏰ Agendar", callback_data=f"ad:schedule:{ad_id}"),
                InlineKeyboardButton("🔁 Automático", callback_data=f"ad:interval:{ad_id}"),
            ],
            [
                InlineKeyboardButton("✏️ Editar", callback_data=f"ad:edit:{ad_id}"),
                InlineKeyboardButton("🗑 Remover", callback_data=f"ad:delete:{ad_id}"),
            ],
            [
                InlineKeyboardButton("⬅️ Lista", callback_data="ad:list"),
            ],
        ]
    )


def ad_edit_keyboard(ad):
    pin = "✅ Fixar" if ad["pin_message"] else "❌ Fixar"
    delete = "✅ Apagar anterior" if ad["delete_previous"] else "❌ Apagar anterior"
    active = "✅ Ativo" if ad["active"] else "❌ Desativado"
    ad_id = ad["id"]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Título", callback_data=f"ad:editfield:{ad_id}:title"),
                InlineKeyboardButton("Descrição", callback_data=f"ad:editfield:{ad_id}:description"),
            ],
            [
                InlineKeyboardButton("Mídia", callback_data=f"ad:editmedia:{ad_id}"),
                InlineKeyboardButton("Botão", callback_data=f"ad:editfield:{ad_id}:button_text"),
            ],
            [
                InlineKeyboardButton("URL", callback_data=f"ad:editfield:{ad_id}:button_url"),
            ],
            [
                InlineKeyboardButton(pin, callback_data=f"ad:togglepin:{ad_id}"),
                InlineKeyboardButton(delete, callback_data=f"ad:toggledel:{ad_id}"),
            ],
            [
                InlineKeyboardButton(active, callback_data=f"ad:toggleactive:{ad_id}"),
            ],
            [InlineKeyboardButton("⬅️ Voltar", callback_data=f"ad:view:{ad_id}")],
        ]
    )


# ============================================================
# Helpers
# ============================================================

def user_id_from_update(update: Update) -> int:
    if update.effective_user:
        return int(update.effective_user.id)
    return 0


def is_admin(user_id: int) -> bool:
    return db.is_admin(user_id)


async def safe_edit(query, text: str, reply_markup=None):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup)
    except BadRequest:
        await query.message.reply_text(text=text, reply_markup=reply_markup)


async def require_admin_update(update: Update) -> bool:
    uid = user_id_from_update(update)
    if is_admin(uid):
        return True

    if update.message and update.effective_chat and update.effective_chat.type == "private":
        await update.message.reply_text(
            f"🔒 Este bot é de uso exclusivo da Agência {AGENCY_NAME}.\n\n"
            "Se você precisa de atendimento, fale com o suporte oficial.",
            reply_markup=support_keyboard(),
        )
    return False


async def require_admin_query(query) -> bool:
    uid = query.from_user.id if query and query.from_user else 0
    if is_admin(uid):
        return True
    await query.answer("Acesso negado. Bot exclusivo da agência.", show_alert=True)
    return False


def ad_text(ad) -> str:
    return (
        f"📌 Anúncio #{ad['id']}\n\n"
        f"Nome: {ad['title']}\n"
        f"Mídia: {ad['media_type']}\n"
        f"Botão: {ad['button_text'] or 'sem botão'}\n"
        f"Fixar: {'sim' if ad['pin_message'] else 'não'}\n"
        f"Apagar anterior: {'sim' if ad['delete_previous'] else 'não'}\n"
        f"Status: {'ativo' if ad['active'] else 'desativado'}\n\n"
        f"Descrição:\n{ad['description']}"
    )


async def send_ad_to_chat(bot, chat_id: int, ad, *, preview=False) -> tuple[bool, str, int | None]:
    """
    Envia anúncio para um chat.
    preview=True não apaga/fixa/loga e é usado só para o dono ver a prévia.
    """
    reply_markup = None
    button_text = (ad["button_text"] or "").strip()
    button_url = normalize_url(ad["button_url"] or "")

    if button_text and button_url and is_valid_url(button_url):
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(button_text, url=button_url)]])

    try:
        if not preview and int(ad["delete_previous"]):
            last = db.get_last_post(chat_id)
            if last and last["message_id"]:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=int(last["message_id"]))
                except TelegramError as e:
                    logger.warning("Não consegui apagar postagem anterior em %s: %s", chat_id, e)

        caption = caption_limit(ad["description"])

        if ad["media_type"] == "photo":
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=ad["media_file_id"],
                caption=caption,
                reply_markup=reply_markup,
            )
        elif ad["media_type"] == "video":
            msg = await bot.send_video(
                chat_id=chat_id,
                video=ad["media_file_id"],
                caption=caption,
                supports_streaming=True,
                reply_markup=reply_markup,
            )
        else:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=caption,
                reply_markup=reply_markup,
            )

        if not preview and int(ad["pin_message"]):
            try:
                await bot.pin_chat_message(
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    disable_notification=True,
                )
            except TelegramError as e:
                logger.warning("Não consegui fixar em %s: %s", chat_id, e)

        if not preview:
            db.set_last_post(chat_id, msg.message_id, int(ad["id"]))
            db.add_log(int(ad["id"]), chat_id, msg.message_id, "success")

        return True, "success", msg.message_id

    except Forbidden as e:
        if not preview:
            db.mark_target_inactive(chat_id)
            db.add_log(int(ad["id"]), chat_id, None, "error", f"Forbidden: {e}")
        return False, f"Sem permissão ou bot removido: {e}", None

    except TelegramError as e:
        if not preview:
            db.add_log(int(ad["id"]), chat_id, None, "error", str(e))
        return False, str(e), None


async def post_ad_to_all(bot, ad) -> dict:
    targets = db.list_targets(approved=True, active=True, limit=500)
    result = {"success": 0, "error": 0, "total": len(targets)}

    for target in targets:
        ok, err, _message_id = await send_ad_to_chat(bot, int(target["chat_id"]), ad, preview=False)
        if ok:
            result["success"] += 1
        else:
            result["error"] += 1
            logger.warning("Falha ao postar em %s: %s", target["chat_id"], err)

    return result


# ============================================================
# Agendamentos
# ============================================================

def schedule_job(application: Application, schedule_row):
    if not application.job_queue:
        logger.error("JobQueue indisponível. Instale: pip install \"python-telegram-bot[job-queue]\"")
        return

    name = f"schedule_{schedule_row['id']}"

    for job in application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()

    days = tuple(int(d) for d in str(schedule_row["days"]).split(",") if d.strip().isdigit())
    run_time = time(
        hour=int(schedule_row["hour"]),
        minute=int(schedule_row["minute"]),
        second=0,
        tzinfo=TZ,
    )

    application.job_queue.run_daily(
        scheduled_post_job,
        time=run_time,
        days=days,
        data={"schedule_id": int(schedule_row["id"])},
        name=name,
    )

    logger.info("Agendamento carregado: %s às %02d:%02d", name, schedule_row["hour"], schedule_row["minute"])


def remove_schedule_job(application: Application, schedule_id: int):
    if not application.job_queue:
        return
    name = f"schedule_{schedule_id}"
    for job in application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()


def schedule_interval_job(application: Application, interval_row):
    if not application.job_queue:
        logger.error('JobQueue indisponível. Instale: pip install "python-telegram-bot[job-queue]"')
        return

    name = f"interval_{interval_row['id']}"

    for job in application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()

    interval_hours = int(interval_row["interval_hours"])
    interval_seconds = interval_hours * 60 * 60

    application.job_queue.run_repeating(
        interval_post_job,
        interval=interval_seconds,
        first=interval_seconds,
        data={"interval_id": int(interval_row["id"])},
        name=name,
    )

    logger.info(
        "Postagem automática carregada: %s a cada %sh",
        name,
        interval_hours,
    )


def remove_interval_job(application: Application, interval_id: int):
    if not application.job_queue:
        return
    name = f"interval_{interval_id}"
    for job in application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()


def load_schedules(application: Application):
    for sched in db.list_schedules(active_only=True):
        schedule_job(application, sched)
    for interval in db.list_interval_schedules(active_only=True):
        schedule_interval_job(application, interval)


async def scheduled_post_job(context: ContextTypes.DEFAULT_TYPE):
    schedule_id = int(context.job.data["schedule_id"])
    sched = db.get_schedule(schedule_id)
    if not sched or not sched["active"]:
        return

    ad = db.get_ad(int(sched["ad_id"]))
    if not ad or not ad["active"]:
        logger.warning("Agendamento %s ignorado: anúncio ausente ou desativado.", schedule_id)
        return

    logger.info("Executando agendamento #%s do anúncio #%s", schedule_id, ad["id"])
    result = await post_ad_to_all(context.bot, ad)

    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"⏰ Agendamento executado\n\n"
                f"Anúncio: #{ad['id']} - {ad['title']}\n"
                f"Destinos: {result['total']}\n"
                f"Enviados: {result['success']}\n"
                f"Falhas: {result['error']}"
            ),
        )
    except TelegramError:
        pass


async def interval_post_job(context: ContextTypes.DEFAULT_TYPE):
    interval_id = int(context.job.data["interval_id"])
    interval = db.get_interval_schedule(interval_id)
    if not interval or not interval["active"]:
        return

    ad = db.get_ad(int(interval["ad_id"]))
    if not ad or not ad["active"]:
        logger.warning("Postagem automática %s ignorada: anúncio ausente ou desativado.", interval_id)
        return

    logger.info(
        "Executando postagem automática #%s do anúncio #%s a cada %sh",
        interval_id,
        ad["id"],
        interval["interval_hours"],
    )
    result = await post_ad_to_all(context.bot, ad)
    db.mark_interval_ran(interval_id)

    try:
        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                f"🔁 Postagem automática executada\n\n"
                f"Anúncio: #{ad['id']} - {ad['title']}\n"
                f"Intervalo: a cada {interval['interval_hours']}h\n"
                f"Destinos: {result['total']}\n"
                f"Enviados: {result['success']}\n"
                f"Falhas: {result['error']}"
            ),
        )
    except TelegramError:
        pass


async def post_init(application: Application):
    load_schedules(application)
    try:
        await application.bot.set_my_commands(
            [
                ("start", "Abrir o painel"),
                ("panel", "Abrir o painel admin"),
                ("id", "Ver seu ID do Telegram"),
                ("help", "Ajuda rápida"),
                ("backup", "Baixar backup do banco"),
            ]
        )
    except TelegramError:
        pass


# ============================================================
# Commands
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_update(update):
        return

    await update.message.reply_text(
        f"🔥 Painel {AGENCY_NAME} Ads\n\n"
        "Escolha uma opção abaixo:",
        reply_markup=main_menu(),
    )


async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id_from_update(update)
    chat_id = update.effective_chat.id if update.effective_chat else ""
    await update.message.reply_text(
        f"🆔 Seu user_id: {uid}\n"
        f"💬 Chat ID atual: {chat_id}"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_update(update):
        return
    await update.message.reply_text(
        "Ajuda rápida:\n\n"
        "/start ou /panel - abrir painel\n"
        "/id - ver seu ID\n"
        "/addadmin ID - adicionar admin extra, só dono\n"
        "/removeadmin ID - remover admin extra, só dono\n"
        "/backup - baixar backup do banco\n\n"
        "Para cadastrar destino: adicione o bot como admin no grupo/canal. "
        "Depois aprove em Destinos pendentes."
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Operação cancelada.", reply_markup=main_menu())


async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id_from_update(update)
    if uid != OWNER_ID:
        await update.message.reply_text("Apenas o dono principal pode adicionar admins.")
        return

    if not context.args:
        await update.message.reply_text("Use assim: /addadmin 123456789")
        return

    try:
        new_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID inválido.")
        return

    db.add_admin(new_id, "Admin extra")
    await update.message.reply_text(f"✅ Admin adicionado: {new_id}")


async def remove_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id_from_update(update)
    if uid != OWNER_ID:
        await update.message.reply_text("Apenas o dono principal pode remover admins.")
        return

    if not context.args:
        await update.message.reply_text("Use assim: /removeadmin 123456789")
        return

    try:
        old_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID inválido.")
        return

    if old_id == OWNER_ID:
        await update.message.reply_text("Não dá para remover o dono principal.")
        return

    db.remove_admin(old_id)
    await update.message.reply_text(f"✅ Admin removido: {old_id}")


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = user_id_from_update(update)
    if uid != OWNER_ID:
        await update.message.reply_text("Apenas o dono principal pode baixar backup.")
        return

    db_file = Path(DB_PATH)
    if not db_file.exists():
        await update.message.reply_text("Banco ainda não encontrado.")
        return

    with db_file.open("rb") as f:
        await update.message.reply_document(
            document=f,
            filename=f"backup_sexy_prime_ads_{datetime.now(TZ).strftime('%Y%m%d_%H%M')}.db",
            caption="Backup do banco SQLite do bot.",
        )


# ============================================================
# Fluxos por mensagem
# ============================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin_update(update):
        return

    flow = context.user_data.get("flow")
    if not flow:
        if update.effective_chat and update.effective_chat.type == "private":
            await update.message.reply_text("Use o painel para escolher uma ação.", reply_markup=main_menu())
        return

    name = flow.get("name")

    if name == "new_ad":
        await handle_new_ad_flow(update, context, flow)
    elif name == "schedule_ad":
        await handle_schedule_flow(update, context, flow)
    elif name == "edit_ad":
        await handle_edit_ad_flow(update, context, flow)


async def handle_new_ad_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict):
    step = flow.get("step")
    data = flow.setdefault("data", {})
    msg = update.message

    if step == "title":
        if not msg.text:
            await msg.reply_text("Envie um título em texto.")
            return
        data["title"] = msg.text.strip()[:80]
        flow["step"] = "media"
        await msg.reply_text(
            "Agora envie a mídia do anúncio.\n\n"
            "Pode ser uma FOTO ou um VÍDEO."
        )
        return

    if step == "media":
        if msg.photo:
            data["media_type"] = "photo"
            data["media_file_id"] = msg.photo[-1].file_id
        elif msg.video:
            data["media_type"] = "video"
            data["media_file_id"] = msg.video.file_id
        else:
            await msg.reply_text("Envie uma foto ou vídeo válido.")
            return

        flow["step"] = "description"
        await msg.reply_text(
            "Mídia recebida ✅\n\n"
            "Agora envie a descrição/legenda do anúncio.\n"
            "Limite recomendado: até 1024 caracteres."
        )
        return

    if step == "description":
        text = (msg.text or msg.caption or "").strip()
        if not text:
            await msg.reply_text("Envie a descrição em texto.")
            return
        if len(text) > 1024:
            await msg.reply_text("A descrição passou de 1024 caracteres. Envie uma versão menor.")
            return
        data["description"] = text
        flow["step"] = "button_text"
        await msg.reply_text(
            "Agora envie o texto do botão.\n\n"
            "Exemplos:\n"
            "Ver modelo\n"
            "Entrar no VIP\n"
            "Falar com suporte\n\n"
            "Ou envie: sem botão"
        )
        return

    if step == "button_text":
        text = (msg.text or "").strip()
        if not text:
            await msg.reply_text("Envie o texto do botão ou 'sem botão'.")
            return

        if text.lower() in {"sem botão", "sem botao", "pular", "não", "nao"}:
            data["button_text"] = ""
            data["button_url"] = ""
            flow["step"] = "pin"
            await msg.reply_text("Deseja fixar o anúncio depois de postar?", reply_markup=yes_no_keyboard("new:pin"))
            return

        data["button_text"] = text[:50]
        flow["step"] = "button_url"
        await msg.reply_text(
            "Agora envie o link do botão.\n\n"
            "Exemplo:\n"
            "https://t.me/seulink\n"
            "https://sxyprime.com"
        )
        return

    if step == "button_url":
        url = normalize_url((msg.text or "").strip())
        if not is_valid_url(url):
            await msg.reply_text("Link inválido. Envie um link começando com https:// ou http://")
            return

        data["button_url"] = url
        flow["step"] = "pin"
        await msg.reply_text("Deseja fixar o anúncio depois de postar?", reply_markup=yes_no_keyboard("new:pin"))
        return


async def handle_schedule_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict):
    msg = update.message
    text = (msg.text or "").strip()

    match = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", text)
    if not match:
        await msg.reply_text("Horário inválido. Envie no formato HH:MM. Exemplo: 18:30")
        return

    hour = int(match.group(1))
    minute = int(match.group(2))
    ad_id = int(flow["ad_id"])

    schedule_id = db.create_schedule(ad_id, hour, minute)
    sched = db.get_schedule(schedule_id)
    schedule_job(context.application, sched)

    context.user_data.clear()

    await msg.reply_text(
        f"✅ Agendamento criado.\n\n"
        f"Anúncio #{ad_id}\n"
        f"Horário: {hour:02d}:{minute:02d}\n"
        f"Dias: todos os dias\n"
        f"Fuso: {TIMEZONE_NAME}",
        reply_markup=main_menu(),
    )


async def handle_edit_ad_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: dict):
    msg = update.message
    ad_id = int(flow["ad_id"])
    field = flow["field"]

    ad = db.get_ad(ad_id)
    if not ad:
        context.user_data.clear()
        await msg.reply_text("Anúncio não encontrado.", reply_markup=main_menu())
        return

    if field == "media":
        if msg.photo:
            db.update_ad_field(ad_id, "media_type", "photo")
            db.update_ad_field(ad_id, "media_file_id", msg.photo[-1].file_id)
        elif msg.video:
            db.update_ad_field(ad_id, "media_type", "video")
            db.update_ad_field(ad_id, "media_file_id", msg.video.file_id)
        else:
            await msg.reply_text("Envie uma foto ou vídeo válido.")
            return

        context.user_data.clear()
        await msg.reply_text("✅ Mídia atualizada.", reply_markup=ad_keyboard(ad_id))
        return

    text = (msg.text or "").strip()
    if not text and field != "button_text":
        await msg.reply_text("Envie um texto válido.")
        return

    if field == "title":
        db.update_ad_field(ad_id, "title", text[:80])
    elif field == "description":
        if len(text) > 1024:
            await msg.reply_text("A descrição passou de 1024 caracteres. Envie uma versão menor.")
            return
        db.update_ad_field(ad_id, "description", text)
    elif field == "button_text":
        if text.lower() in {"sem botão", "sem botao", "pular", "remover"}:
            db.update_ad_field(ad_id, "button_text", "")
            db.update_ad_field(ad_id, "button_url", "")
        else:
            db.update_ad_field(ad_id, "button_text", text[:50])
    elif field == "button_url":
        url = normalize_url(text)
        if text.lower() in {"remover", "pular", "sem botão", "sem botao"}:
            db.update_ad_field(ad_id, "button_url", "")
        elif not is_valid_url(url):
            await msg.reply_text("URL inválida. Envie começando com https:// ou http://")
            return
        else:
            db.update_ad_field(ad_id, "button_url", url)

    context.user_data.clear()
    await msg.reply_text("✅ Anúncio atualizado.", reply_markup=ad_keyboard(ad_id))


# ============================================================
# Callback Query
# ============================================================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await require_admin_query(query):
        return

    data = query.data or ""

    if data == "cancel":
        context.user_data.clear()
        await safe_edit(query, "Operação cancelada.", reply_markup=main_menu())
        return

    if data == "menu:home":
        context.user_data.clear()
        await safe_edit(
            query,
            f"🔥 Painel {AGENCY_NAME} Ads\n\nEscolha uma opção abaixo:",
            reply_markup=main_menu(),
        )
        return

    # ---------- criação: callbacks sim/não ----------
    if data.startswith("new:pin:"):
        flow = context.user_data.get("flow")
        if not flow or flow.get("name") != "new_ad":
            await safe_edit(query, "Fluxo expirado. Comece novamente.", reply_markup=main_menu())
            return

        flow["data"]["pin_message"] = int(data.split(":")[-1])
        flow["step"] = "delete_previous"
        await safe_edit(
            query,
            "Quando postar este anúncio, deseja apagar a última postagem anterior do bot no destino?",
            reply_markup=yes_no_keyboard("new:delprev"),
        )
        return

    if data.startswith("new:delprev:"):
        flow = context.user_data.get("flow")
        if not flow or flow.get("name") != "new_ad":
            await safe_edit(query, "Fluxo expirado. Comece novamente.", reply_markup=main_menu())
            return

        flow["data"]["delete_previous"] = int(data.split(":")[-1])
        ad_id = db.create_ad(flow["data"])
        context.user_data.clear()
        ad = db.get_ad(ad_id)

        await safe_edit(
            query,
            f"✅ Anúncio criado com sucesso.\n\n{ad_text(ad)}",
            reply_markup=ad_keyboard(ad_id),
        )
        return

    # ---------- menu ----------
    if data == "ad:new":
        context.user_data["flow"] = {
            "name": "new_ad",
            "step": "title",
            "data": {},
        }
        await safe_edit(
            query,
            "➕ Criar anúncio\n\n"
            "Envie o título interno do anúncio.\n"
            "Exemplo: Modelo Ana - VIP de hoje",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="cancel")]]),
        )
        return

    if data == "ad:list":
        ads = db.list_ads(limit=20)
        if not ads:
            await safe_edit(query, "Nenhum anúncio criado ainda.", reply_markup=back_home())
            return

        rows = []
        for ad in ads:
            status = "✅" if ad["active"] else "❌"
            rows.append([InlineKeyboardButton(f"{status} #{ad['id']} - {short(ad['title'], 30)}", callback_data=f"ad:view:{ad['id']}")])
        rows.append([InlineKeyboardButton("⬅️ Voltar ao painel", callback_data="menu:home")])
        await safe_edit(query, "📋 Meus anúncios:", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("ad:view:"):
        ad_id = int(data.split(":")[-1])
        ad = db.get_ad(ad_id)
        if not ad:
            await safe_edit(query, "Anúncio não encontrado.", reply_markup=back_home())
            return
        await safe_edit(query, ad_text(ad), reply_markup=ad_keyboard(ad_id))
        return

    if data.startswith("ad:preview:"):
        ad_id = int(data.split(":")[-1])
        ad = db.get_ad(ad_id)
        if not ad:
            await safe_edit(query, "Anúncio não encontrado.", reply_markup=back_home())
            return

        await send_ad_to_chat(context.bot, query.message.chat_id, ad, preview=True)
        await query.message.reply_text("👆 Prévia do anúncio acima.", reply_markup=ad_keyboard(ad_id))
        return

    if data.startswith("ad:post:"):
        ad_id = int(data.split(":")[-1])
        ad = db.get_ad(ad_id)
        if not ad:
            await safe_edit(query, "Anúncio não encontrado.", reply_markup=back_home())
            return

        approved = db.count_targets(approved=True, active=True)
        if approved <= 0:
            await safe_edit(
                query,
                "Nenhum destino aprovado ainda.\n\n"
                "Adicione o bot como admin em grupos/canais e depois aprove em Destinos pendentes.",
                reply_markup=back_home(),
            )
            return

        await safe_edit(
            query,
            f"🚀 Confirmar postagem?\n\n"
            f"Anúncio: #{ad_id} - {ad['title']}\n"
            f"Destinos aprovados ativos: {approved}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Confirmar postagem", callback_data=f"ad:postconfirm:{ad_id}")],
                    [InlineKeyboardButton("⬅️ Cancelar", callback_data=f"ad:view:{ad_id}")],
                ]
            ),
        )
        return

    if data.startswith("ad:postconfirm:"):
        ad_id = int(data.split(":")[-1])
        ad = db.get_ad(ad_id)
        if not ad:
            await safe_edit(query, "Anúncio não encontrado.", reply_markup=back_home())
            return

        await safe_edit(query, "🚀 Postando anúncio nos destinos aprovados...")
        result = await post_ad_to_all(context.bot, ad)

        await query.message.reply_text(
            f"✅ Postagem finalizada.\n\n"
            f"Anúncio: #{ad_id} - {ad['title']}\n"
            f"Destinos: {result['total']}\n"
            f"Enviados: {result['success']}\n"
            f"Falhas: {result['error']}",
            reply_markup=ad_keyboard(ad_id),
        )
        return

    if data.startswith("ad:schedule:"):
        ad_id = int(data.split(":")[-1])
        ad = db.get_ad(ad_id)
        if not ad:
            await safe_edit(query, "Anúncio não encontrado.", reply_markup=back_home())
            return

        context.user_data["flow"] = {"name": "schedule_ad", "ad_id": ad_id}
        await safe_edit(
            query,
            f"⏰ Agendar anúncio #{ad_id}\n\n"
            "Envie o horário no formato HH:MM.\n"
            "Exemplo: 18:30\n\n"
            f"Fuso usado: {TIMEZONE_NAME}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="cancel")]]),
        )
        return

    if data.startswith("ad:interval:"):
        ad_id = int(data.split(":")[-1])
        ad = db.get_ad(ad_id)
        if not ad:
            await safe_edit(query, "Anúncio não encontrado.", reply_markup=back_home())
            return

        await safe_edit(
            query,
            f"🔁 Postagem automática do anúncio #{ad_id}\n\n"
            "Escolha de quanto em quanto tempo o bot deve postar este anúncio.\n\n"
            "Importante: ao ativar, a primeira postagem automática acontece depois do intervalo escolhido. "
            "Se quiser postar agora, use o botão 🚀 Postar agora.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("1 em 1 hora", callback_data=f"interval:create:{ad_id}:1"),
                        InlineKeyboardButton("2 em 2 horas", callback_data=f"interval:create:{ad_id}:2"),
                    ],
                    [
                        InlineKeyboardButton("3 em 3 horas", callback_data=f"interval:create:{ad_id}:3"),
                        InlineKeyboardButton("4 em 4 horas", callback_data=f"interval:create:{ad_id}:4"),
                    ],
                    [
                        InlineKeyboardButton("6 em 6 horas", callback_data=f"interval:create:{ad_id}:6"),
                        InlineKeyboardButton("12 em 12 horas", callback_data=f"interval:create:{ad_id}:12"),
                    ],
                    [InlineKeyboardButton("⬅️ Voltar", callback_data=f"ad:view:{ad_id}")],
                ]
            ),
        )
        return

    if data.startswith("interval:create:"):
        parts = data.split(":")
        ad_id = int(parts[2])
        interval_hours = int(parts[3])
        ad = db.get_ad(ad_id)
        if not ad:
            await safe_edit(query, "Anúncio não encontrado.", reply_markup=back_home())
            return

        old_interval_ids = db.disable_active_intervals_for_ad(ad_id)
        for old_interval_id in old_interval_ids:
            remove_interval_job(context.application, old_interval_id)

        interval_id = db.create_interval_schedule(ad_id, interval_hours)
        interval = db.get_interval_schedule(interval_id)
        schedule_interval_job(context.application, interval)

        await safe_edit(
            query,
            f"✅ Postagem automática ativada.\n\n"
            f"Anúncio: #{ad_id} - {ad['title']}\n"
            f"Intervalo: a cada {interval_hours} hora(s)\n\n"
            "O bot vai postar nos destinos aprovados ativos. "
            "A primeira postagem automática acontece depois desse intervalo.",
            reply_markup=ad_keyboard(ad_id),
        )
        return

    if data.startswith("ad:edit:"):
        ad_id = int(data.split(":")[-1])
        ad = db.get_ad(ad_id)
        if not ad:
            await safe_edit(query, "Anúncio não encontrado.", reply_markup=back_home())
            return
        await safe_edit(query, f"✏️ Editar anúncio #{ad_id}\n\nEscolha o que deseja alterar:", reply_markup=ad_edit_keyboard(ad))
        return

    if data.startswith("ad:editfield:"):
        parts = data.split(":")
        ad_id = int(parts[2])
        field = parts[3]

        labels = {
            "title": "novo título",
            "description": "nova descrição",
            "button_text": "novo texto do botão. Envie 'remover' para tirar o botão",
            "button_url": "nova URL do botão. Envie 'remover' para tirar a URL",
        }

        context.user_data["flow"] = {"name": "edit_ad", "ad_id": ad_id, "field": field}
        await safe_edit(
            query,
            f"Envie o {labels.get(field, 'novo valor')} do anúncio #{ad_id}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="cancel")]]),
        )
        return

    if data.startswith("ad:editmedia:"):
        ad_id = int(data.split(":")[-1])
        context.user_data["flow"] = {"name": "edit_ad", "ad_id": ad_id, "field": "media"}
        await safe_edit(
            query,
            f"Envie a nova foto ou vídeo do anúncio #{ad_id}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Cancelar", callback_data="cancel")]]),
        )
        return

    if data.startswith("ad:togglepin:"):
        ad_id = int(data.split(":")[-1])
        ad = db.get_ad(ad_id)
        if not ad:
            await safe_edit(query, "Anúncio não encontrado.", reply_markup=back_home())
            return
        db.update_ad_field(ad_id, "pin_message", 0 if ad["pin_message"] else 1)
        ad = db.get_ad(ad_id)
        await safe_edit(query, f"✏️ Editar anúncio #{ad_id}\n\nOpção atualizada.", reply_markup=ad_edit_keyboard(ad))
        return

    if data.startswith("ad:toggledel:"):
        ad_id = int(data.split(":")[-1])
        ad = db.get_ad(ad_id)
        if not ad:
            await safe_edit(query, "Anúncio não encontrado.", reply_markup=back_home())
            return
        db.update_ad_field(ad_id, "delete_previous", 0 if ad["delete_previous"] else 1)
        ad = db.get_ad(ad_id)
        await safe_edit(query, f"✏️ Editar anúncio #{ad_id}\n\nOpção atualizada.", reply_markup=ad_edit_keyboard(ad))
        return

    if data.startswith("ad:toggleactive:"):
        ad_id = int(data.split(":")[-1])
        ad = db.get_ad(ad_id)
        if not ad:
            await safe_edit(query, "Anúncio não encontrado.", reply_markup=back_home())
            return
        db.update_ad_field(ad_id, "active", 0 if ad["active"] else 1)
        ad = db.get_ad(ad_id)
        await safe_edit(query, f"✏️ Editar anúncio #{ad_id}\n\nStatus atualizado.", reply_markup=ad_edit_keyboard(ad))
        return

    if data.startswith("ad:delete:"):
        ad_id = int(data.split(":")[-1])
        ad = db.get_ad(ad_id)
        if not ad:
            await safe_edit(query, "Anúncio não encontrado.", reply_markup=back_home())
            return
        await safe_edit(
            query,
            f"🗑 Deseja remover/desativar o anúncio #{ad_id}?\n\n{ad['title']}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Sim, remover", callback_data=f"ad:deleteconfirm:{ad_id}")],
                    [InlineKeyboardButton("⬅️ Cancelar", callback_data=f"ad:view:{ad_id}")],
                ]
            ),
        )
        return

    if data.startswith("ad:deleteconfirm:"):
        ad_id = int(data.split(":")[-1])
        db.update_ad_field(ad_id, "active", 0)
        await safe_edit(query, f"✅ Anúncio #{ad_id} desativado.", reply_markup=back_home())
        return

    # ---------- targets ----------
    if data == "tg:pending":
        targets = db.list_targets(approved=False, active=True, limit=20)
        if not targets:
            await safe_edit(query, "Nenhum destino pendente.", reply_markup=back_home())
            return

        text = "📍 Destinos pendentes:\n\n"
        rows = []
        for t in targets:
            text += f"• {t['chat_title']} ({t['chat_type']})\nID: {t['chat_id']}\n\n"
            rows.append(
                [
                    InlineKeyboardButton(f"✅ Aprovar {short(t['chat_title'], 18)}", callback_data=f"tg:ok:{t['chat_id']}"),
                    InlineKeyboardButton("❌ Rejeitar", callback_data=f"tg:no:{t['chat_id']}"),
                ]
            )
        rows.append([InlineKeyboardButton("⬅️ Voltar", callback_data="menu:home")])
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "tg:approved":
        targets = db.list_targets(approved=True, active=True, limit=30)
        if not targets:
            await safe_edit(query, "Nenhum destino aprovado ativo.", reply_markup=back_home())
            return

        text = "✅ Destinos aprovados ativos:\n\n"
        rows = []
        for t in targets:
            text += f"• {t['chat_title']} ({t['chat_type']})\nID: {t['chat_id']}\n\n"
            rows.append([InlineKeyboardButton(f"⛔ Desativar {short(t['chat_title'], 24)}", callback_data=f"tg:disable:{t['chat_id']}")])
        rows.append([InlineKeyboardButton("⬅️ Voltar", callback_data="menu:home")])
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("tg:ok:"):
        chat_id = int(data.split(":")[-1])
        db.set_target_approved(chat_id, True)
        await safe_edit(query, f"✅ Destino aprovado:\n{chat_id}", reply_markup=back_home())
        return

    if data.startswith("tg:no:"):
        chat_id = int(data.split(":")[-1])
        db.set_target_approved(chat_id, False)
        db.set_target_active(chat_id, False)
        await safe_edit(query, f"❌ Destino rejeitado/desativado:\n{chat_id}", reply_markup=back_home())
        return

    if data.startswith("tg:disable:"):
        chat_id = int(data.split(":")[-1])
        db.set_target_active(chat_id, False)
        await safe_edit(query, f"⛔ Destino desativado:\n{chat_id}", reply_markup=back_home())
        return

    # ---------- schedules ----------
    if data == "sched:list":
        schedules = db.list_schedules(active_only=False)
        if not schedules:
            await safe_edit(query, "Nenhum agendamento criado ainda.", reply_markup=back_home())
            return

        text = "⏰ Agendamentos:\n\n"
        rows = []
        for s in schedules:
            status = "✅" if s["active"] else "❌"
            text += (
                f"{status} #{s['id']} - {s['hour']:02d}:{s['minute']:02d}\n"
                f"Anúncio: #{s['ad_id']} - {s['ad_title'] or 'removido'}\n\n"
            )
            if s["active"]:
                rows.append([InlineKeyboardButton(f"⛔ Desativar agendamento #{s['id']}", callback_data=f"sched:disable:{s['id']}")])
        rows.append([InlineKeyboardButton("⬅️ Voltar", callback_data="menu:home")])
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("sched:disable:"):
        schedule_id = int(data.split(":")[-1])
        db.set_schedule_active(schedule_id, False)
        remove_schedule_job(context.application, schedule_id)
        await safe_edit(query, f"✅ Agendamento #{schedule_id} desativado.", reply_markup=back_home())
        return

    # ---------- intervalos automáticos ----------
    if data == "interval:list":
        intervals = db.list_interval_schedules(active_only=False)
        if not intervals:
            await safe_edit(
                query,
                "🔁 Nenhuma postagem automática criada ainda.\n\n"
                "Para ativar: Meus anúncios > escolha o anúncio > 🔁 Automático.",
                reply_markup=back_home(),
            )
            return

        text = "🔁 Postagens automáticas:\n\n"
        rows = []
        for i in intervals:
            status = "✅" if i["active"] else "❌"
            last_run = i["last_run_at"] or "ainda não executou"
            text += (
                f"{status} #{i['id']} - a cada {i['interval_hours']}h\n"
                f"Anúncio: #{i['ad_id']} - {i['ad_title'] or 'removido'}\n"
                f"Última execução: {last_run}\n\n"
            )
            if i["active"]:
                rows.append([InlineKeyboardButton(f"⛔ Parar automático #{i['id']}", callback_data=f"interval:disable:{i['id']}")])
        rows.append([InlineKeyboardButton("⬅️ Voltar", callback_data="menu:home")])
        await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("interval:disable:"):
        interval_id = int(data.split(":")[-1])
        db.set_interval_schedule_active(interval_id, False)
        remove_interval_job(context.application, interval_id)
        await safe_edit(query, f"✅ Postagem automática #{interval_id} parada.", reply_markup=back_home())
        return

    # ---------- stats / settings ----------
    if data == "stats":
        st = db.stats_today()
        text = (
            "📊 Estatísticas\n\n"
            f"Anúncios cadastrados: {db.count_ads()}\n"
            f"Destinos pendentes: {db.count_targets(approved=False, active=True)}\n"
            f"Destinos aprovados ativos: {db.count_targets(approved=True, active=True)}\n"
            f"Agendamentos ativos: {db.count_schedules(active=True)}\n"
            f"Postagens automáticas ativas: {db.count_interval_schedules(active=True)}\n\n"
            f"Postagens hoje: {st['total']}\n"
            f"Enviadas hoje: {st['success']}\n"
            f"Falhas hoje: {st['error']}\n"
        )

        errors = db.recent_errors(limit=5)
        if errors:
            text += "\nÚltimas falhas:\n"
            for e in errors:
                text += f"• {short(e['chat_title'] or e['chat_id'], 25)}: {short(e['error_message'], 60)}\n"

        await safe_edit(query, text, reply_markup=back_home())
        return

    if data == "settings":
        admins = db.list_admins()
        admins_text = "\n".join([f"• {a['user_id']} - {a['role']}" for a in admins]) or "Nenhum"
        text = (
            "⚙️ Configurações atuais\n\n"
            f"Agência: {AGENCY_NAME}\n"
            f"Dono: {OWNER_ID}\n"
            f"Fuso: {TIMEZONE_NAME}\n"
            f"Suporte: {SUPPORT_URL}\n"
            f"Banco: {DB_PATH}\n\n"
            f"Admins ativos:\n{admins_text}\n\n"
            "Comandos:\n"
            "/addadmin ID\n"
            "/removeadmin ID\n"
            "/backup"
        )
        await safe_edit(query, text, reply_markup=back_home())
        return

    await safe_edit(query, "Ação não reconhecida.", reply_markup=main_menu())


# ============================================================
# Detectar quando o bot entra/sai de grupos/canais
# ============================================================

async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    event = update.my_chat_member
    if not event:
        return

    chat = event.chat
    new = event.new_chat_member

    status = new.status
    chat_id = int(chat.id)
    title = chat.title or chat.username or str(chat_id)
    chat_type = chat.type

    if status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}:
        can_pin = bool(getattr(new, "can_pin_messages", False))
        db.upsert_target(chat_id, title, chat_type, can_pin)

        text = (
            "📍 Novo destino detectado\n\n"
            f"Nome: {title}\n"
            f"Tipo: {chat_type}\n"
            f"ID: {chat_id}\n"
            f"Status do bot: {status}\n"
            f"Pode fixar: {'sim' if can_pin else 'não/indefinido'}\n\n"
            "Deseja aprovar para receber anúncios?"
        )

        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=text,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ Aprovar", callback_data=f"tg:ok:{chat_id}"),
                            InlineKeyboardButton("❌ Rejeitar", callback_data=f"tg:no:{chat_id}"),
                        ]
                    ]
                ),
            )
        except TelegramError as e:
            logger.warning("Não consegui avisar o dono sobre novo destino: %s", e)

    elif status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}:
        db.mark_target_inactive(chat_id)
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"⚠️ Bot removido ou bloqueado no destino:\n\n{title}\nID: {chat_id}",
            )
        except TelegramError:
            pass


# ============================================================
# Error handler
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Erro não tratado:", exc_info=context.error)
    try:
        if OWNER_ID:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"⚠️ Erro no bot:\n{type(context.error).__name__}: {context.error}",
            )
    except TelegramError:
        pass


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Configure BOT_TOKEN no arquivo .env ou nas Environment Variables do Render")
    if not OWNER_ID:
        raise RuntimeError("Configure OWNER_ID no arquivo .env ou nas Environment Variables do Render")

    defaults = Defaults(tzinfo=TZ)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .defaults(defaults)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("panel", panel))
    app.add_handler(CommandHandler("id", get_id))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("addadmin", add_admin_cmd))
    app.add_handler(CommandHandler("removeadmin", remove_admin_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))

    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(buttons))

    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.TEXT, handle_message))

    app.add_error_handler(error_handler)
    return app


def main_polling():
    app = build_application()
    logger.info("Bot iniciado em polling. Agência: %s | Dono: %s | Fuso: %s", AGENCY_NAME, OWNER_ID, TIMEZONE_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


async def run_webhook_server():
    if not WEBHOOK_URL:
        raise RuntimeError("Configure WEBHOOK_URL com a URL pública do Render. Ex: https://sexy-prime-ads.onrender.com")

    application = build_application()

    await application.initialize()
    await post_init(application)
    await application.start()

    full_webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    await application.bot.set_webhook(
        url=full_webhook_url,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )

    async def health(_request: web.Request):
        return web.json_response(
            {
                "ok": True,
                "bot": "Sexy Prime Ads",
                "mode": "webhook",
                "webhook_path": WEBHOOK_PATH,
                "time": now_iso(),
            }
        )

    async def telegram_webhook(request: web.Request):
        # GET serve para cron/monitoramento acordar o Render sem enviar update falso.
        if request.method == "GET":
            return await health(request)

        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)

        update = Update.de_json(payload, application.bot)
        await application.process_update(update)
        return web.json_response({"ok": True})

    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    web_app.router.add_get("/ping", health)
    web_app.router.add_route("*", WEBHOOK_PATH, telegram_webhook)

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info("Bot iniciado em webhook.")
    logger.info("Render URL: %s", WEBHOOK_URL)
    logger.info("Webhook Telegram: %s", full_webhook_url)
    logger.info("Ping/cron: %s%s", WEBHOOK_URL, WEBHOOK_PATH)
    logger.info("Porta: %s", PORT)

    try:
        await asyncio.Event().wait()
    finally:
        await application.bot.delete_webhook(drop_pending_updates=False)
        await application.stop()
        await application.shutdown()
        await runner.cleanup()


def main():
    if RUN_MODE == "webhook":
        asyncio.run(run_webhook_server())
    else:
        main_polling()


if __name__ == "__main__":
    main()
