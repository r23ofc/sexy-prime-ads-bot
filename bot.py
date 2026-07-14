from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
AGENCY_NAME = os.getenv("AGENCY_NAME", "Sexy Prime").strip() or "Sexy Prime"
SITE_API_URL = os.getenv("SITE_API_URL", "https://sxyprime.com/api/bot_ads_gateway.php").strip()
SITE_API_SECRET = os.getenv("SITE_API_SECRET", "").strip()
SITE_PANEL_URL = os.getenv("SITE_PANEL_URL", "https://sxyprime.com/editar_perfil_modelo.php#telegram-bot-ads").strip()
SITE_ADMIN_URL = os.getenv("SITE_ADMIN_URL", "https://sxyprime.com/admin/bot_ads.php").strip()
RUN_MODE = os.getenv("RUN_MODE", "polling").strip().lower()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "").strip().strip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
PORT = int(os.getenv("PORT", "10000") or 10000)
DB_PATH = os.getenv("DB_PATH", "data/sexy_prime_ads_runtime.db").strip()
JOB_INTERVAL_SECONDS = max(15, int(os.getenv("JOB_INTERVAL_SECONDS", "30") or 30))
MAX_BUTTONS = 5

if not WEBHOOK_PATH:
    WEBHOOK_PATH = "telegram/" + secrets.token_urlsafe(18)
if not WEBHOOK_SECRET:
    WEBHOOK_SECRET = secrets.token_urlsafe(24)

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("logs/bot.log", encoding="utf-8")],
)
logger = logging.getLogger("sexy-prime-bot-ads")

UNLINKED_MESSAGE = "Vincule seu perfil ao seu painel para liberar o bot."
STATUS_LABELS = {
    "pending": "Aguardando aprovação",
    "approved": "Aprovado",
    "rejected": "Recusado",
    "scheduled": "Agendado",
    "completed": "Concluído",
    "draft": "Rascunho",
}


def validate_environment() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not SITE_API_URL:
        missing.append("SITE_API_URL")
    if not SITE_API_SECRET:
        missing.append("SITE_API_SECRET")
    if missing:
        raise RuntimeError("Variáveis obrigatórias ausentes: " + ", ".join(missing))


def runtime_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS last_posts (
            destination_id INTEGER PRIMARY KEY,
            telegram_chat_id INTEGER NOT NULL,
            telegram_message_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    return conn


def save_last_post(destination_id: int, chat_id: int, message_id: int) -> None:
    with runtime_db() as conn:
        conn.execute(
            """
            INSERT INTO last_posts(destination_id, telegram_chat_id, telegram_message_id, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(destination_id) DO UPDATE SET
                telegram_chat_id=excluded.telegram_chat_id,
                telegram_message_id=excluded.telegram_message_id,
                updated_at=CURRENT_TIMESTAMP
            """,
            (destination_id, chat_id, message_id),
        )


def last_post(destination_id: int) -> tuple[int, int] | None:
    with runtime_db() as conn:
        row = conn.execute(
            "SELECT telegram_chat_id, telegram_message_id FROM last_posts WHERE destination_id = ?",
            (destination_id,),
        ).fetchone()
    if not row:
        return None
    return int(row["telegram_chat_id"]), int(row["telegram_message_id"])


def clear_last_post(destination_id: int) -> None:
    with runtime_db() as conn:
        conn.execute("DELETE FROM last_posts WHERE destination_id = ?", (destination_id,))


def valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except Exception:
        return False


async def api_request(action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(payload or {})
    data["action"] = action
    timeout = aiohttp.ClientTimeout(total=90)
    headers = {"X-SXP-Bot-Secret": SITE_API_SECRET}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(SITE_API_URL, json=data) as response:
            try:
                result = await response.json(content_type=None)
            except Exception:
                text = await response.text()
                raise RuntimeError(f"Resposta inválida do site ({response.status}): {text[:300]}")
            if not result.get("success"):
                raise RuntimeError(str(result.get("message") or "Falha ao comunicar com o site."))
            return result


async def api_submit_ad(payload: dict[str, Any], media_path: str | None, media_name: str, media_mime: str) -> dict[str, Any]:
    form = aiohttp.FormData()
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        form.add_field(key, str(value))

    file_handle = None
    try:
        if media_path and Path(media_path).is_file():
            file_handle = open(media_path, "rb")
            form.add_field(
                "media",
                file_handle,
                filename=media_name or Path(media_path).name,
                content_type=media_mime or "application/octet-stream",
            )
        timeout = aiohttp.ClientTimeout(total=180)
        headers = {"X-SXP-Bot-Secret": SITE_API_SECRET}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.post(SITE_API_URL, data=form) as response:
                result = await response.json(content_type=None)
                if not result.get("success"):
                    raise RuntimeError(str(result.get("message") or "Não foi possível enviar o anúncio."))
                return result
    finally:
        if file_handle:
            file_handle.close()


def linked_keyboard(is_owner: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("➕ Enviar anúncio", callback_data="ads:new")],
        [
            InlineKeyboardButton("💳 Meu saldo", callback_data="ads:balance"),
            InlineKeyboardButton("📋 Meus anúncios", callback_data="ads:mine"),
        ],
    ]
    if is_owner and SITE_ADMIN_URL:
        rows.append([InlineKeyboardButton("⚙️ Painel administrativo", url=SITE_ADMIN_URL)])
    return InlineKeyboardMarkup(rows)


def unlinked_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Vincular no painel", url=SITE_PANEL_URL)]])


async def profile_for(user_id: int) -> dict[str, Any] | None:
    try:
        result = await api_request("profile", {"telegram_user_id": user_id})
        return result
    except RuntimeError as exc:
        logger.warning("Falha ao consultar perfil %s: %s", user_id, exc)
        return None


async def show_home(update: Update, context: ContextTypes.DEFAULT_TYPE, notice: str = "") -> None:
    user = update.effective_user
    if not user:
        return
    result = await profile_for(user.id)
    target = update.callback_query.message if update.callback_query else update.effective_message
    if not target:
        return

    if not result:
        await target.reply_text("Não foi possível consultar seu perfil agora. Tente novamente.")
        return

    if not result.get("linked"):
        await target.reply_text(UNLINKED_MESSAGE, reply_markup=unlinked_keyboard())
        return

    model = result.get("model") or {}
    wallet = result.get("wallet") or {}
    text = (
        f"🔥 <b>{AGENCY_NAME} — Bot ADS</b>\n\n"
        f"Perfil: <b>{model.get('name', 'Modelo')}</b>\n"
        f"Créditos disponíveis: <b>{int(wallet.get('credits', 0))}</b>\n"
        f"Pontos no site: <b>{int(wallet.get('points', 0))}</b>"
    )
    if notice:
        text = notice + "\n\n" + text
    await target.reply_text(text, parse_mode="HTML", reply_markup=linked_keyboard(user.id == OWNER_ID))


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_chat.type != ChatType.PRIVATE:
        return

    payload = context.args[0] if context.args else ""
    if payload.startswith("link_"):
        token = payload[5:]
        try:
            result = await api_request(
                "link",
                {
                    "token": token,
                    "telegram_user_id": update.effective_user.id,
                    "telegram_chat_id": update.effective_chat.id,
                    "telegram_username": update.effective_user.username or "",
                },
            )
            bonus = int(result.get("bonus", 0))
            bonus_text = f" Você recebeu <b>{bonus} créditos</b> de bônus." if bonus > 0 else ""
            await update.effective_message.reply_text(
                "✅ <b>Perfil vinculado com sucesso.</b>" + bonus_text,
                parse_mode="HTML",
            )
        except RuntimeError as exc:
            await update.effective_message.reply_text(f"❌ {exc}")
            return

    await show_home(update, context)


async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_home(update, context)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("ad_flow", None)
    await update.effective_message.reply_text("Envio cancelado.")
    await show_home(update, context)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not update.effective_user:
        return
    await query.answer()
    data = query.data or ""

    if data == "ads:home":
        context.user_data.pop("ad_flow", None)
        await show_home(update, context)
        return

    profile = await profile_for(update.effective_user.id)
    if not profile or not profile.get("linked"):
        await query.message.reply_text(UNLINKED_MESSAGE, reply_markup=unlinked_keyboard())
        return

    if data == "ads:balance":
        wallet = profile.get("wallet") or {}
        await query.message.reply_text(
            f"💳 Créditos ADS: <b>{int(wallet.get('credits', 0))}</b>\n"
            f"⭐ Pontos: <b>{int(wallet.get('points', 0))}</b>\n\n"
            "No painel da modelo, 200 pontos podem ser trocados por 5 créditos.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar", callback_data="ads:home")]]),
        )
        return

    if data == "ads:mine":
        try:
            result = await api_request("my_ads", {"telegram_user_id": update.effective_user.id})
            ads = result.get("ads") or []
            if not ads:
                text = "Você ainda não enviou anúncios."
            else:
                lines = ["📋 <b>Seus anúncios recentes</b>"]
                for ad in ads[:10]:
                    status = STATUS_LABELS.get(str(ad.get("status")), str(ad.get("status") or ""))
                    line = f"\n#{int(ad.get('id', 0))} — <b>{status}</b>\n{str(ad.get('title') or 'Anúncio')}"
                    if ad.get("rejection_reason"):
                        line += f"\nMotivo: {str(ad.get('rejection_reason'))[:180]}"
                    lines.append(line)
                text = "\n".join(lines)
            await query.message.reply_text(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Voltar", callback_data="ads:home")]]),
            )
        except RuntimeError as exc:
            await query.message.reply_text(f"❌ {exc}")
        return

    if data == "ads:new":
        if int((profile.get("wallet") or {}).get("credits", 0)) <= 0:
            await query.message.reply_text(
                "Você não possui créditos para enviar um anúncio. Resgate créditos no painel da modelo.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Abrir painel", url=SITE_PANEL_URL)]]),
            )
            return
        context.user_data["ad_flow"] = {
            "step": "media",
            "source_nonce": secrets.token_urlsafe(24),
            "buttons": [],
        }
        await query.message.reply_text(
            "Envie agora a <b>foto</b>, o <b>vídeo</b> ou o <b>texto</b> do anúncio.\n\nUse /cancelar para sair.",
            parse_mode="HTML",
        )
        return

    flow = context.user_data.get("ad_flow")
    if not isinstance(flow, dict):
        await query.message.reply_text("Este envio expirou. Comece novamente.")
        return

    if data == "ads:no_button":
        flow["buttons"] = []
        flow["step"] = "confirm"
        await send_preview(query.message, flow)
        return

    if data == "ads:confirm":
        await confirm_submission(query.message, context, update.effective_user.id, flow)
        return

    if data == "ads:cancel":
        context.user_data.pop("ad_flow", None)
        await query.message.reply_text("Envio cancelado.")
        return


async def private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message or update.effective_chat.type != ChatType.PRIVATE:
        return
    flow = context.user_data.get("ad_flow")
    if not isinstance(flow, dict):
        return

    message = update.effective_message
    step = str(flow.get("step") or "")

    if step == "media":
        if message.photo:
            photo = message.photo[-1]
            flow.update(
                {
                    "media_type": "photo",
                    "telegram_file_id": photo.file_id,
                    "media_file_name": "anuncio.jpg",
                    "media_mime": "image/jpeg",
                    "body_text": (message.caption or "").strip(),
                    "body_entities": json.dumps([e.to_dict() for e in (message.caption_entities or [])], ensure_ascii=False),
                }
            )
        elif message.video:
            flow.update(
                {
                    "media_type": "video",
                    "telegram_file_id": message.video.file_id,
                    "media_file_name": message.video.file_name or "anuncio.mp4",
                    "media_mime": message.video.mime_type or "video/mp4",
                    "body_text": (message.caption or "").strip(),
                    "body_entities": json.dumps([e.to_dict() for e in (message.caption_entities or [])], ensure_ascii=False),
                }
            )
        elif message.text:
            flow.update(
                {
                    "media_type": "text",
                    "telegram_file_id": "",
                    "media_file_name": "",
                    "media_mime": "",
                    "body_text": message.text.strip(),
                    "body_entities": json.dumps([e.to_dict() for e in (message.entities or [])], ensure_ascii=False),
                }
            )
        else:
            await message.reply_text("Envie uma foto, um vídeo ou uma mensagem de texto.")
            return

        if flow.get("media_type") in {"photo", "video"} and not flow.get("body_text"):
            flow["step"] = "caption"
            await message.reply_text("Agora envie a legenda/texto do anúncio. Envie apenas - para deixar sem legenda.")
        else:
            await ask_button(message, flow)
        return

    if step == "caption":
        if not message.text:
            await message.reply_text("Envie a legenda em texto ou apenas - para deixar sem legenda.")
            return
        flow["body_text"] = "" if message.text.strip() == "-" else message.text.strip()
        flow["body_entities"] = json.dumps([e.to_dict() for e in (message.entities or [])], ensure_ascii=False)
        await ask_button(message, flow)
        return

    if step == "button_text":
        if not message.text:
            await message.reply_text("Envie o texto do botão.")
            return
        text = message.text.strip()
        if text.lower() in {"sem botão", "sem botao", "-"}:
            flow["buttons"] = []
            flow["step"] = "confirm"
            await send_preview(message, flow)
            return
        if len(text) > 100:
            await message.reply_text("O texto do botão deve ter no máximo 100 caracteres.")
            return
        flow["pending_button_text"] = text
        flow["step"] = "button_url"
        await message.reply_text("Envie a URL completa do botão, começando com https://")
        return

    if step == "button_url":
        if not message.text or not valid_url(message.text):
            await message.reply_text("URL inválida. Envie uma URL começando com http:// ou https://")
            return
        buttons = list(flow.get("buttons") or [])
        buttons.append({"text": flow.pop("pending_button_text", "Abrir"), "url": message.text.strip()})
        flow["buttons"] = buttons[:MAX_BUTTONS]
        flow["step"] = "confirm"
        await send_preview(message, flow)
        return


async def ask_button(message, flow: dict[str, Any]) -> None:
    flow["step"] = "button_text"
    await message.reply_text(
        "Envie o texto do botão do anúncio.\nEnvie <b>sem botão</b> para continuar sem URL.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Continuar sem botão", callback_data="ads:no_button")]]),
    )


def ad_markup(flow: dict[str, Any], confirm: bool = False) -> InlineKeyboardMarkup | None:
    rows = []
    for button in list(flow.get("buttons") or [])[:MAX_BUTTONS]:
        if button.get("text") and button.get("url"):
            rows.append([InlineKeyboardButton(str(button["text"]), url=str(button["url"]))])
    if confirm:
        rows.append(
            [
                InlineKeyboardButton("✅ Enviar para aprovação", callback_data="ads:confirm"),
                InlineKeyboardButton("Cancelar", callback_data="ads:cancel"),
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else None


async def send_preview(message, flow: dict[str, Any]) -> None:
    text = str(flow.get("body_text") or "")
    markup = ad_markup(flow, confirm=False)
    media_type = flow.get("media_type")
    file_id = flow.get("telegram_file_id")
    await message.reply_text("👁 <b>Prévia do anúncio</b>", parse_mode="HTML")
    try:
        if media_type == "photo" and file_id:
            await message.reply_photo(file_id, caption=text[:1024] or None, reply_markup=markup)
        elif media_type == "video" and file_id:
            await message.reply_video(file_id, caption=text[:1024] or None, reply_markup=markup)
        else:
            await message.reply_text(text or "Anúncio sem texto", reply_markup=markup)
    except TelegramError as exc:
        logger.warning("Falha ao exibir prévia: %s", exc)
        await message.reply_text(text or "Mídia anexada ao anúncio.", reply_markup=markup)

    await message.reply_text(
        "O anúncio será enviado ao painel administrativo e só será publicado depois da aprovação.",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Enviar para aprovação", callback_data="ads:confirm"),
                InlineKeyboardButton("Cancelar", callback_data="ads:cancel"),
            ]]
        ),
    )


async def download_media(context: ContextTypes.DEFAULT_TYPE, flow: dict[str, Any]) -> str | None:
    file_id = str(flow.get("telegram_file_id") or "")
    if not file_id:
        return None
    suffix = Path(str(flow.get("media_file_name") or "media.bin")).suffix or ".bin"
    temp = tempfile.NamedTemporaryFile(prefix="sxp_ad_", suffix=suffix, delete=False)
    temp_path = temp.name
    temp.close()
    try:
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(custom_path=temp_path)
        return temp_path
    except Exception as exc:
        logger.warning("Não foi possível baixar mídia para o painel: %s", exc)
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        return None


async def confirm_submission(message, context: ContextTypes.DEFAULT_TYPE, telegram_user_id: int, flow: dict[str, Any]) -> None:
    await message.reply_text("Enviando anúncio para análise...")
    media_path = None
    try:
        media_path = await download_media(context, flow)
        result = await api_submit_ad(
            {
                "action": "submit_ad",
                "telegram_user_id": telegram_user_id,
                "title": "Anúncio enviado pelo Bot ADS",
                "media_type": flow.get("media_type", "text"),
                "telegram_file_id": flow.get("telegram_file_id", ""),
                "body_text": flow.get("body_text", ""),
                "body_entities": flow.get("body_entities", ""),
                "source_nonce": flow.get("source_nonce", secrets.token_urlsafe(24)),
                "buttons_json": flow.get("buttons", []),
            },
            media_path,
            str(flow.get("media_file_name") or "media"),
            str(flow.get("media_mime") or "application/octet-stream"),
        )
        wallet = result.get("wallet") or {}
        ad = result.get("ad") or {}
        await message.reply_text(
            "✅ <b>Anúncio enviado para aprovação.</b>\n\n"
            f"Protocolo: <b>#{int(ad.get('id', 0))}</b>\n"
            f"Crédito reservado: <b>{int(ad.get('credits_reserved', 0))}</b>\n"
            f"Saldo disponível: <b>{int(wallet.get('credits', 0))}</b>",
            parse_mode="HTML",
            reply_markup=linked_keyboard(telegram_user_id == OWNER_ID),
        )
        context.user_data.pop("ad_flow", None)
    except RuntimeError as exc:
        await message.reply_text(f"❌ {exc}")
    except Exception as exc:
        logger.exception("Falha ao enviar anúncio")
        await message.reply_text("❌ Não foi possível enviar o anúncio agora. Tente novamente.")
    finally:
        if media_path:
            try:
                os.unlink(media_path)
            except OSError:
                pass


async def register_chat(chat, bot) -> None:
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}:
        return
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
        can_pin = bool(
            getattr(me, "can_pin_messages", False)
            or getattr(me, "can_manage_chat", False)
            or getattr(me, "can_edit_messages", False)
        )
        await api_request(
            "register_destination",
            {
                "chat_id": chat.id,
                "title": chat.title or str(chat.id),
                "type": chat.type,
                "username": chat.username or "",
                "can_pin": can_pin,
            },
        )
        logger.info("Destino registrado: %s (%s)", chat.title, chat.id)
    except Exception as exc:
        logger.warning("Falha ao registrar destino %s: %s", chat.id, exc)


async def registrar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL}:
        await update.effective_message.reply_text("Use este comando no grupo ou canal que será cadastrado.")
        return
    await register_chat(update.effective_chat, context.bot)
    try:
        await update.effective_message.reply_text("Destino enviado para aprovação no painel administrativo.")
    except TelegramError:
        pass


async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    changed = update.my_chat_member
    if not changed:
        return
    old_status = changed.old_chat_member.status
    new_status = changed.new_chat_member.status
    active = new_status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
    was_active = old_status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}
    if active and not was_active:
        await register_chat(changed.chat, context.bot)


def destination_keyboard(buttons: list[dict[str, Any]]) -> InlineKeyboardMarkup | None:
    rows = []
    for button in buttons[:MAX_BUTTONS]:
        text = str(button.get("button_text") or button.get("text") or "").strip()
        url = str(button.get("button_url") or button.get("url") or "").strip()
        style = str(button.get("button_style") or button.get("style") or "").strip().lower()
        if text and valid_url(url):
            if style in {"primary", "success", "danger"}:
                rows.append([InlineKeyboardButton(text, url=url, api_kwargs={"style": style})])
            else:
                rows.append([InlineKeyboardButton(text, url=url)])
    return InlineKeyboardMarkup(rows) if rows else None


async def send_job_to_destination(context: ContextTypes.DEFAULT_TYPE, job: dict[str, Any], destination: dict[str, Any]) -> dict[str, Any]:
    schedule = job.get("schedule") or {}
    ad = job.get("ad") or {}
    destination_id = int(destination.get("id") or 0)
    chat_id = int(destination.get("telegram_chat_id") or 0)
    result: dict[str, Any] = {"destination_id": destination_id, "chat_id": chat_id, "ok": False}

    try:
        if bool(int(schedule.get("delete_previous") or 0)):
            server_previous_id = int(destination.get("previous_message_id") or 0)
            previous = (chat_id, server_previous_id) if server_previous_id > 0 else last_post(destination_id)
            if previous:
                try:
                    await context.bot.delete_message(chat_id=previous[0], message_id=previous[1])
                except (BadRequest, Forbidden):
                    pass
                clear_last_post(destination_id)

        text = str(ad.get("body_text") or "")
        media_type = str(ad.get("media_type") or "text")
        telegram_file_id = str(ad.get("telegram_file_id") or "")
        media_url = str(ad.get("media_url") or "")
        media_source = telegram_file_id or media_url
        markup = destination_keyboard(list(ad.get("buttons") or []))

        if media_type == "photo" and media_source:
            sent = await context.bot.send_photo(chat_id=chat_id, photo=media_source, caption=text[:1024] or None, reply_markup=markup)
        elif media_type == "video" and media_source:
            sent = await context.bot.send_video(chat_id=chat_id, video=media_source, caption=text[:1024] or None, reply_markup=markup, supports_streaming=True)
        else:
            if not text:
                raise RuntimeError("Anúncio sem texto ou mídia.")
            sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, disable_web_page_preview=False)

        save_last_post(destination_id, chat_id, sent.message_id)
        if bool(int(schedule.get("pin_message") or 0)) and bool(int(destination.get("can_pin") or 0)):
            try:
                await context.bot.pin_chat_message(chat_id=chat_id, message_id=sent.message_id, disable_notification=True)
            except (BadRequest, Forbidden) as exc:
                logger.warning("Não foi possível fixar em %s: %s", chat_id, exc)

        result.update({"ok": True, "message_id": sent.message_id})
    except Exception as exc:
        logger.warning("Falha no destino %s: %s", chat_id, exc)
        result["error"] = str(exc)[:1000]
    return result


async def job_worker(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        response = await api_request("next_job")
        job = response.get("job")
        if not job:
            return
        destinations = list(job.get("destinations") or [])
        schedule = job.get("schedule") or {}
        schedule_id = int(schedule.get("id") or 0)
        lease_token = str(job.get("lease_token") or "")
        run_number = int(job.get("run_number") or (int(schedule.get("runs_completed") or 0) + 1))
        existing = {
            int(item.get("destination_id") or 0): {
                "destination_id": int(item.get("destination_id") or 0),
                "chat_id": int(item.get("chat_id") or 0),
                "message_id": int(item.get("message_id") or 0),
                "ok": True,
            }
            for item in list(job.get("existing_results") or [])
            if int(item.get("destination_id") or 0) > 0
        }
        results = []
        for destination in destinations:
            destination_id = int(destination.get("id") or 0)
            if destination_id in existing:
                results.append(existing[destination_id])
                continue
            result = await send_job_to_destination(context, job, destination)
            results.append(result)
            try:
                await api_request(
                    "checkpoint_job",
                    {
                        "schedule_id": schedule_id,
                        "lease_token": lease_token,
                        "run_number": run_number,
                        "result": result,
                    },
                )
            except Exception as checkpoint_error:
                logger.warning("Falha ao registrar checkpoint do destino %s: %s", destination_id, checkpoint_error)
            await asyncio.sleep(0.15)
        await api_request(
            "complete_job",
            {
                "schedule_id": schedule_id,
                "lease_token": lease_token,
                "results": results,
            },
        )
    except Exception as exc:
        logger.exception("Worker de anúncios falhou: %s", exc)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Erro não tratado no bot", exc_info=context.error)


def build_application() -> Application:
    validate_environment()
    runtime_db().close()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("painel", panel_command))
    app.add_handler(CommandHandler("cancelar", cancel_command))
    app.add_handler(CommandHandler("registrar", registrar_command))
    app.add_handler(CallbackQueryHandler(callback_handler, pattern=r"^ads:"))
    app.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.VIDEO | filters.TEXT) & ~filters.COMMAND, private_message_handler))
    app.add_error_handler(error_handler)
    app.job_queue.run_repeating(job_worker, interval=JOB_INTERVAL_SECONDS, first=10, name="bot-ads-worker")
    return app


def main() -> None:
    app = build_application()
    if RUN_MODE == "webhook":
        if not WEBHOOK_URL:
            raise RuntimeError("WEBHOOK_URL é obrigatório quando RUN_MODE=webhook.")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH,
            webhook_url=f"{WEBHOOK_URL}/{WEBHOOK_PATH}",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
    else:
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
