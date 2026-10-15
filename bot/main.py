"""Бот продаж и мониторинга для панели SharX.

Покупатель: пробный период, покупка/продление (Telegram Stars, USDT через CryptoBot), «Моя подписка».
Администратор: уведомления о сбоях, ежедневная резервная копия базы панели, выдача доступа вручную.
Интерфейс: русский, английский, испанский (texts.py).
"""
import asyncio
import html
import json
import logging
import os
import shutil
import secrets
import ssl
import string
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, LabeledPrice, LinkPreviewOptions, Message, PreCheckoutQuery)

from texts import LANGS, pick_lang, plan_title, tr

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sharx-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ.get("ADMIN_ID") or 0)
PANEL_URL = os.environ["PANEL_URL"].rstrip("/")
PANEL_API_TOKEN = os.environ["PANEL_API_TOKEN"]
CRYPTOBOT_TOKEN = os.environ.get("CRYPTOBOT_TOKEN", "").strip()
BUNDLE_ID = int(os.environ.get("BUNDLE_ID", "1"))
TRIAL_DAYS = int(os.environ.get("TRIAL_DAYS", "7"))
MAX_DEVICES = int(os.environ.get("MAX_DEVICES", "3"))
PANEL_DB_DSN = os.environ.get("PANEL_DB_DSN", "").strip()
SUPPORT_URL = os.environ.get("SUPPORT_URL", "").strip()
BRAND = os.environ.get("BRAND", "VPN")
REF_BONUS_DAYS = int(os.environ.get("REF_BONUS_DAYS", "7"))

HAPP_ANDROID = "https://play.google.com/store/apps/details?id=com.happproxy"
HAPP_IOS = "https://apps.apple.com/app/happ-proxy-utility/id6504287215"
HAPP_DESKTOP = "https://www.happ.su/main/ru"

DATA = Path("/data")
DB_PATH = DATA / "bot.db"
PLANS = {p["id"]: p for p in json.loads(Path("/app/tariffs.json").read_text(encoding="utf-8"))["plans"]}
MSK = timezone(timedelta(hours=3))
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

router = Router()
BOT_USERNAME = ""
user_locks: dict[int, asyncio.Lock] = {}


def lock_for(tg_id: int) -> asyncio.Lock:
    """Один замок на пользователя: исключает двойную выдачу при быстрых повторных нажатиях."""
    return user_locks.setdefault(tg_id, asyncio.Lock())


# создаются в main() при запуске
bot: Bot = None  # type: ignore[assignment]
db: aiosqlite.Connection = None  # type: ignore[assignment]
http: aiohttp.ClientSession = None  # type: ignore[assignment]


# ───────────────────────── база бота ─────────────────────────

async def init_db() -> None:
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        create table if not exists users(
            tg_id integer primary key, username text, full_name text,
            client_id integer, sub_url text, trial_used integer not null default 0,
            created_at integer not null, notified_for integer not null default 0);
        create table if not exists payments(
            id integer primary key autoincrement, tg_id integer not null, plan text not null,
            method text not null, amount text not null, currency text not null,
            status text not null default 'pending', ext_id text, created_at integer not null, paid_at integer);
        create index if not exists idx_payments_pending on payments(status, method);
        create table if not exists kv(k text primary key, v text);
        """
    )
    cols = {r["name"] for r in await (await db.execute("pragma table_info(users)")).fetchall()}
    if "lang" not in cols:
        await db.execute("alter table users add column lang text")
    if "unlimited" not in cols:
        await db.execute("alter table users add column unlimited integer not null default 0")
    if "ref_by" not in cols:
        await db.execute("alter table users add column ref_by integer")
        await db.execute("alter table users add column ref_rewarded integer not null default 0")
    await db.commit()


async def get_user(tg_id: int):
    cur = await db.execute("select * from users where tg_id=?", (tg_id,))
    return await cur.fetchone()


async def ensure_user(ev) -> aiosqlite.Row:
    """Регистрирует пользователя; язык берётся из Telegram при первом обращении."""
    u = ev.from_user
    await db.execute(
        "insert into users(tg_id, username, full_name, created_at, lang) values(?,?,?,?,?) "
        "on conflict(tg_id) do update set username=excluded.username, full_name=excluded.full_name, "
        "lang=coalesce(users.lang, excluded.lang)",
        (u.id, u.username or "", u.full_name or "", int(time.time()), pick_lang(u.language_code)),
    )
    await db.commit()
    return await get_user(u.id)


def lang_of(user) -> str:
    return (user["lang"] if user and user["lang"] else None) or "ru"


# ───────────────────────── API панели ─────────────────────────

class PanelError(Exception):
    pass


async def panel(method: str, path: str, body: dict | None = None):
    async with http.request(method, PANEL_URL + path, json=body,
                            headers={"Authorization": f"Bearer {PANEL_API_TOKEN}"},
                            timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False) as r:
        if r.status != 200:
            raise PanelError(f"{path}: HTTP {r.status}")
        d = await r.json(content_type=None)
        if not d.get("success"):
            raise PanelError(f"{path}: {d.get('msg')}")
        return d.get("obj")


async def panel_client(client_id: int) -> dict | None:
    try:
        return await panel("GET", f"/panel/client/get/{client_id}")
    except PanelError:
        return None


async def grant_days(tg_id: int, days: int, comment: str) -> tuple[dict, bool]:
    """Создаёт клиента или продлевает существующего. Возвращает (клиент, создан_ли)."""
    user = await get_user(tg_id)
    now_ms = int(time.time() * 1000)
    client = await panel_client(user["client_id"]) if user and user["client_id"] else None
    if client:
        if int(client.get("expiryTime") or 0) != 0:           # 0 = бессрочная подписка, её срок не меняем
            base = max(now_ms, int(client["expiryTime"]))
            await panel("POST", f"/panel/client/update/{client['id']}",
                        {"expiryTime": base + days * 86_400_000, "enable": True})
            client = await panel_client(client["id"])
        created = False
    else:
        name = f"tg{tg_id}"
        await panel("POST", "/panel/client/add", {
            "name": name, "email": name, "enable": True, "tgId": tg_id,
            "expiryTime": now_ms + days * 86_400_000,
            "hwidEnabled": MAX_DEVICES > 0, "maxHwid": MAX_DEVICES,
            "comment": comment[:200], "bundleIds": [BUNDLE_ID]})
        lst = await panel("GET", "/panel/client/list")
        client = next(c for c in lst if c.get("name") == name)
        client = await panel_client(client["id"])
        created = True
    await db.execute("update users set client_id=?, sub_url=?, notified_for=0 where tg_id=?",
                     (client["id"], client.get("subscriptionUrl", ""), tg_id))
    await db.commit()
    return client, created


# ───────────────────────── оформление ─────────────────────────

def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, **a) for t, a in row] for row in rows])


async def show(ev, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Редактирует сообщение при нажатии кнопки, отправляет новое при команде."""
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                await ev.message.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)
        await ev.answer()
    else:
        await ev.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


def main_menu(user) -> InlineKeyboardMarkup:
    L = lang_of(user)
    rows = []
    if not user["trial_used"] and not user["client_id"]:
        rows.append([(tr(L, "btn_trial", days=TRIAL_DAYS), {"callback_data": "trial"})])
    if not user["unlimited"]:
        rows.append([(tr(L, "btn_renew" if user["client_id"] else "btn_buy"), {"callback_data": "buy"})])
    rows.append([(tr(L, "btn_my"), {"callback_data": "my"}), (tr(L, "btn_help"), {"callback_data": "help"})])
    if REF_BONUS_DAYS > 0:
        rows.append([(tr(L, "btn_invite"), {"callback_data": "ref"})])
    last = [(tr(L, "btn_lang"), {"callback_data": "lang"})]
    if SUPPORT_URL:
        last.insert(0, (tr(L, "btn_support"), {"url": SUPPORT_URL}))
    rows.append(last)
    return kb(rows)


def fmt_date(ms: int, L: str = "ru") -> str:
    if L == "ru":
        return datetime.fromtimestamp(ms / 1000, MSK).strftime("%d.%m.%Y %H:%M МСК")
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def days_left(ms: int) -> int:
    return max(0, int((ms / 1000 - time.time()) // 86400))


COUNTRY_OK = {"ru": True, "nl": True}          # обновляет monitor()


def status_line(L: str) -> str:
    dot = lambda ok: "🟢" if ok else "🔴"
    return tr(L, "status_line", ru=dot(COUNTRY_OK["ru"]), ru_name=tr(L, "c_ru"),
              nl=dot(COUNTRY_OK["nl"]), nl_name=tr(L, "c_nl"))


def welcome_text(L: str) -> str:
    return tr(L, "welcome", brand=BRAND, devices=MAX_DEVICES) + f"\n\n<i>{status_line(L)}</i>"


def fmt_bytes(b: int, L: str = "ru") -> str:
    gb, mb = ("ГБ", "МБ") if L == "ru" else ("GB", "MB")
    return f"{b / 1024 ** 3:.1f} {gb}" if b >= 1024 ** 3 else f"{b / 1024 ** 2:.0f} {mb}"


def is_active(client: dict) -> bool:
    exp = int(client.get("expiryTime") or 0)
    return bool(client.get("enable")) and (exp == 0 or exp > time.time() * 1000)


def sub_card(client: dict, L: str) -> str:
    exp = int(client.get("expiryTime") or 0)
    lines = [f"{tr(L, 'row_status')}: <b>{tr(L, 'st_active' if is_active(client) else 'st_expired')}</b>"]
    if exp == 0:
        lines.append(f"{tr(L, 'row_until')}: <b>{tr(L, 'forever')}</b>")
    else:
        lines.append(f"{tr(L, 'row_until')}: <b>{fmt_date(exp, L)}</b>")
        if is_active(client):
            lines.append(f"{tr(L, 'row_left')}: <b>{tr(L, 'days_n', n=days_left(exp))}</b>")
    dev = (tr(L, "dev_of", a=client.get("activeHwidCount", 0), b=client.get("maxHwid"))
           if client.get("hwidEnabled") else tr(L, "unlimited"))
    lines.append(f"{tr(L, 'row_devices')}: <b>{dev}</b>")
    lines.append(f"{tr(L, 'row_traffic')}: <b>{tr(L, 'traffic_val', v=fmt_bytes(int(client.get('allTime') or 0), L))}</b>")
    last = int(client.get("lastOnline") or 0)
    seen = (tr(L, "online_now") if client.get("isOnline") else
            tr(L, "last_seen", date=fmt_date(last, L)) if last else tr(L, "never"))
    lines.append(f"{tr(L, 'row_online')}: <b>{seen}</b>")
    return (f"{tr(L, 'sub_title')}\n\n<blockquote>" + "\n".join(lines) + "</blockquote>\n\n"
            f"{tr(L, 'link_hint')}\n<pre>{html.escape(client.get('subscriptionUrl', ''))}</pre>")


def sub_buttons(client: dict, L: str, back: bool = True) -> InlineKeyboardMarkup:
    rows = []
    page = client.get("subscriptionPageUrl") or client.get("subscriptionUrl")
    if page:
        rows.append([(tr(L, "btn_connect"), {"url": page})])
    first = [(tr(L, "btn_help"), {"callback_data": "help"})]
    if int(client.get("expiryTime") or 0) != 0:
        first.insert(0, (tr(L, "btn_renew"), {"callback_data": "buy"}))
    rows.append(first)
    rows.append([(tr(L, "btn_devices"), {"callback_data": "dev"})])
    if back:
        rows.append([(tr(L, "btn_menu"), {"callback_data": "menu"})])
    return kb(rows)


def plan_price(plan: dict, L: str) -> str:
    return f"{plan['rub']} ₽" if L == "ru" else f"{plan['stars']} ⭐️"


def plan_discount(plan: dict) -> str:
    base = next((p for p in PLANS.values() if p["days"] == 30), None)
    if not base or plan["days"] <= 30:
        return ""
    full = base["rub"] * plan["days"] / 30
    pct = round((1 - plan["rub"] / full) * 100)
    return f"  −{pct}%" if pct > 0 else ""


# ───────────────────────── обработчики покупателя ─────────────────────────

@router.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    is_new = await get_user(m.from_user.id) is None
    user = await ensure_user(m)
    L = lang_of(user)
    arg = (command.args or "").strip()
    if is_new and arg.startswith("r") and arg[1:].isdigit() and int(arg[1:]) != m.from_user.id:
        if await get_user(int(arg[1:])):                      # приглашение засчитывается только новым пользователям
            await db.execute("update users set ref_by=? where tg_id=? and ref_by is null", (int(arg[1:]), m.from_user.id))
            await db.commit()
    extra = f"\n\n<i>Telegram ID: <code>{m.from_user.id}</code></i>" if not ADMIN_ID else ""
    await m.answer(welcome_text(L) + extra, reply_markup=main_menu(user),
                   link_preview_options=NO_PREVIEW)


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    user = await ensure_user(c)
    await show(c, welcome_text(lang_of(user)), main_menu(user))


@router.message(Command("lang"))
@router.callback_query(F.data == "lang")
async def cb_lang(ev):
    user = await ensure_user(ev)
    L = lang_of(user)
    rows = [[(("✓ " if code == L else "") + title, {"callback_data": f"setlang:{code}"})] for code, title in LANGS.items()]
    rows.append([(tr(L, "btn_back"), {"callback_data": "menu"})])
    await show(ev, tr(L, "lang_choose"), kb(rows))


@router.callback_query(F.data.startswith("setlang:"))
async def cb_setlang(c: CallbackQuery):
    code = c.data.split(":", 1)[1]
    if code in LANGS:
        await db.execute("update users set lang=? where tg_id=?", (code, c.from_user.id))
        await db.commit()
    user = await ensure_user(c)
    L = lang_of(user)
    await c.answer(tr(L, "lang_set"))
    await show(c, welcome_text(L), main_menu(user))


@router.message(Command("help"))
@router.callback_query(F.data == "help")
async def cb_help(ev):
    L = lang_of(await ensure_user(ev))
    await show(ev, tr(L, "help"), kb([
        [(tr(L, "btn_android"), {"url": HAPP_ANDROID}), (tr(L, "btn_ios"), {"url": HAPP_IOS})],
        [(tr(L, "btn_desktop"), {"url": HAPP_DESKTOP})],
        [(tr(L, "btn_my"), {"callback_data": "my"}), (tr(L, "btn_trouble"), {"callback_data": "trouble"})],
        [(tr(L, "btn_menu"), {"callback_data": "menu"})]]))


@router.callback_query(F.data == "trouble")
async def cb_trouble(c: CallbackQuery):
    user = await ensure_user(c)
    L = lang_of(user)
    rows = [[(tr(L, "btn_devices"), {"callback_data": "dev"})]]
    if SUPPORT_URL:
        rows[0].append((tr(L, "btn_support"), {"url": SUPPORT_URL}))
    rows.append([(tr(L, "btn_back"), {"callback_data": "help"})])
    await show(c, tr(L, "trouble", status=status_line(L)), kb(rows))


@router.callback_query(F.data == "trial")
async def cb_trial(c: CallbackQuery):
    user = await ensure_user(c)
    L = lang_of(user)
    if user["trial_used"] or user["client_id"]:
        await c.answer(tr(L, "trial_used"), show_alert=True)
        return
    await c.answer(tr(L, "wait"))
    try:
        async with lock_for(c.from_user.id):
            user = await get_user(c.from_user.id)
            if user["trial_used"] or user["client_id"]:
                return
            client, _ = await grant_days(c.from_user.id, TRIAL_DAYS, f"trial @{c.from_user.username or ''}")
            await db.execute("update users set trial_used=1 where tg_id=?", (c.from_user.id,))
            await db.commit()
    except Exception as e:
        log.exception("trial failed")
        await notify_admin(f"⚠️ Не удалось выдать пробный период {c.from_user.id}: {html.escape(str(e))}")
        await c.message.answer(tr(L, "error"))
        return
    await c.message.edit_text(tr(L, "trial_ok", days=TRIAL_DAYS) + "\n\n" + sub_card(client, L),
                              reply_markup=sub_buttons(client, L), link_preview_options=NO_PREVIEW)
    await notify_admin(f"🎁 Пробный период: {mention(c.from_user)}")


@router.message(Command("my"))
@router.callback_query(F.data == "my")
async def cb_my(ev):
    user = await ensure_user(ev)
    L = lang_of(user)
    client = await panel_client(user["client_id"]) if user["client_id"] else None
    if not client:
        await show(ev, tr(L, "no_sub"), main_menu(user))
        return
    await show(ev, sub_card(client, L), sub_buttons(client, L))


def device_name(d: dict) -> str:
    parts = [str(d.get("deviceOs") or "").strip(), str(d.get("deviceModel") or "").strip()]
    name = " · ".join(x for x in parts if x) or str(d.get("deviceName") or "Device")
    return name[:40]


async def all_devices(user) -> list[dict]:
    """Все устройства только этого пользователя — список берётся по его собственному клиенту."""
    if not user["client_id"]:
        return []
    try:
        return await panel("GET", f"/panel/client/hwid/list/{user['client_id']}") or []
    except PanelError:
        return []


async def my_devices(user) -> list[dict]:
    return [d for d in await all_devices(user) if d.get("isActive", True) and not d.get("blocked")]


async def blocked_devices(user) -> list[dict]:
    return [d for d in await all_devices(user) if d.get("blocked")]


async def block_device(device_id: int, blocked: bool = True) -> None:
    """Блокировка, а не удаление: заблокированное устройство не получит протоколы, даже если добавит
    подписку заново, а его место в лимите освобождается."""
    await panel("POST", f"/panel/client/hwid/block/{int(device_id)}", {"blocked": blocked})


async def rotate_key(client_id: int) -> None:
    """Новый ключ клиента: старый перестаёт работать сразу, в том числе в сохранённых конфигах.
    Меняются оба секрета: UUID (VLESS) и пароль (Hysteria2)."""
    await panel("POST", f"/panel/client/update/{int(client_id)}",
                {"uuid": str(uuid.uuid4()), "password": secrets.token_hex(16)})


async def show_devices(c: CallbackQuery, user, L: str) -> None:
    client = await panel_client(user["client_id"]) if user["client_id"] else None
    if not client:
        await show(c, tr(L, "no_sub"), main_menu(user))
        return
    devs = await my_devices(user)
    limit = client.get("maxHwid") if client.get("hwidEnabled") and client.get("maxHwid") else "∞"
    text = tr(L, "dev_title", a=len(devs), b=limit)
    rows = []
    if devs:
        lines = []
        for n, d in enumerate(devs, 1):
            seen = fmt_date(int(d.get("lastSeenAt") or 0) * 1000, L) if d.get("lastSeenAt") else "—"
            lines.append(f"<b>{n}. {html.escape(device_name(d))}</b>\n     {tr(L, 'dev_seen', date=seen)}")
            rows.append([(f"✕ {n}. {device_name(d)}", {"callback_data": f"dev:ask:{d['id']}"})])
        text += "\n\n<blockquote>" + "\n".join(lines) + "</blockquote>\n\n" + tr(L, "dev_hint")
        if len(devs) > 1:
            rows.append([(tr(L, "btn_dev_all"), {"callback_data": "dev:ask:all"})])
    else:
        text += "\n\n<blockquote>" + tr(L, "dev_empty") + "</blockquote>"
    blocked = await blocked_devices(user)
    if blocked:
        rows.append([(tr(L, "btn_blocked", n=len(blocked)), {"callback_data": "dev:blocked"})])
    rows.append([(tr(L, "btn_newlink"), {"callback_data": "newlink"})])
    rows.append([(tr(L, "btn_back"), {"callback_data": "my"})])
    await show(c, text, kb(rows))


@router.callback_query(F.data == "dev")
async def cb_devices(c: CallbackQuery):
    user = await ensure_user(c)
    await show_devices(c, user, lang_of(user))


@router.callback_query(F.data.startswith("dev:ask:"))
async def cb_device_ask(c: CallbackQuery):
    user = await ensure_user(c)
    L = lang_of(user)
    target = c.data.split(":", 2)[2]
    if target == "all":
        await show(c, tr(L, "dev_confirm_all"), kb([[(tr(L, "btn_dev_yes"), {"callback_data": "dev:del:all"})],
                                                     [(tr(L, "btn_back"), {"callback_data": "dev"})]]))
        return
    d = next((x for x in await my_devices(user) if str(x.get("id")) == target), None)
    if not d:                                              # чужое или уже удалённое устройство
        await show_devices(c, user, L)
        return
    await show(c, tr(L, "dev_confirm", name=html.escape(device_name(d))),
               kb([[(tr(L, "btn_dev_yes"), {"callback_data": f"dev:del:{d['id']}"})],
                   [(tr(L, "btn_dev_rotate"), {"callback_data": f"dev:rot:{d['id']}"})],
                   [(tr(L, "btn_back"), {"callback_data": "dev"})]]))


@router.callback_query(F.data.startswith("dev:del:") | F.data.startswith("dev:rot:"))
async def cb_device_del(c: CallbackQuery):
    user = await ensure_user(c)
    L = lang_of(user)
    _, action, target = c.data.split(":", 2)
    rotated = False
    try:
        async with lock_for(c.from_user.id):
            devs = await my_devices(user)
            if target == "all":
                for d in devs:
                    await block_device(d["id"])
                if user["client_id"]:
                    await rotate_key(user["client_id"])
                    rotated = True
            elif any(str(x.get("id")) == target for x in devs):            # только своё устройство
                await block_device(int(target))
                if action == "rot":
                    await rotate_key(user["client_id"])
                    rotated = True
        await c.answer(tr(L, "dev_rotated" if rotated else "dev_removed"), show_alert=rotated)
    except Exception:
        log.exception("device removal failed")
        await c.answer(tr(L, "error"), show_alert=True)
    await show_devices(c, user, L)


@router.callback_query(F.data == "newlink")
async def cb_newlink(c: CallbackQuery):
    L = lang_of(await ensure_user(c))
    await show(c, tr(L, "newlink_confirm"), kb([[(tr(L, "btn_newlink_yes"), {"callback_data": "newlink:yes"})],
                                                 [(tr(L, "btn_back"), {"callback_data": "dev"})]]))


@router.callback_query(F.data == "newlink:yes")
async def cb_newlink_yes(c: CallbackQuery):
    """Новая ссылка + новые секреты + сброс устройств: утёкшая ссылка и сохранённые конфиги перестают работать."""
    user = await ensure_user(c)
    L = lang_of(user)
    if not user["client_id"]:
        await show(c, tr(L, "no_sub"), main_menu(user))
        return
    try:
        async with lock_for(c.from_user.id):
            sub_id = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
            await panel("POST", f"/panel/client/update/{user['client_id']}",
                        {"subId": sub_id, "uuid": str(uuid.uuid4()), "password": secrets.token_hex(16)})
            await panel("POST", f"/panel/client/clearHwid/{user['client_id']}")
            client = await panel_client(user["client_id"])
            await db.execute("update users set sub_url=? where tg_id=?", (client.get("subscriptionUrl", ""), c.from_user.id))
            await db.execute("insert or replace into kv(k, v) values(?, '[]')", (f"seen:{user['client_id']}",))
            await db.commit()
    except Exception:
        log.exception("new link failed")
        await c.answer(tr(L, "error"), show_alert=True)
        return
    await c.answer()
    await show(c, tr(L, "newlink_done") + "\n\n" + sub_card(client, L), sub_buttons(client, L))
    await notify_admin(f"🔄 Новая ссылка подписки: {mention(c.from_user)}")


@router.callback_query(F.data == "dev:blocked")
async def cb_device_blocked(c: CallbackQuery):
    user = await ensure_user(c)
    L = lang_of(user)
    blocked = await blocked_devices(user)
    if not blocked:
        await show_devices(c, user, L)
        return
    rows = [[(tr(L, "btn_restore", name=device_name(d)), {"callback_data": f"dev:unb:{d['id']}"})] for d in blocked]
    rows.append([(tr(L, "btn_back"), {"callback_data": "dev"})])
    await show(c, tr(L, "blocked_title"), kb(rows))


@router.callback_query(F.data.startswith("dev:unb:"))
async def cb_device_restore(c: CallbackQuery):
    user = await ensure_user(c)
    L = lang_of(user)
    target = c.data.split(":", 2)[2]
    try:
        async with lock_for(c.from_user.id):
            if not any(str(x.get("id")) == target for x in await blocked_devices(user)):   # только своё
                await show_devices(c, user, L)
                return
            client = await panel_client(user["client_id"])
            limit = int(client.get("maxHwid") or 0) if client and client.get("hwidEnabled") else 0
            if limit and len(await my_devices(user)) >= limit:
                await c.answer(tr(L, "restore_full"), show_alert=True)
                return
            await block_device(int(target), blocked=False)
        await c.answer(tr(L, "restore_ok"), show_alert=True)
    except Exception:
        log.exception("device restore failed")
        await c.answer(tr(L, "error"), show_alert=True)
    await show_devices(c, user, L)


@router.callback_query(F.data == "ref")
async def cb_ref(c: CallbackQuery):
    user = await ensure_user(c)
    L = lang_of(user)
    n = (await (await db.execute("select count(*) c from users where ref_by=?", (c.from_user.id,))).fetchone())["c"]
    m = (await (await db.execute("select count(*) c from users where ref_by=? and ref_rewarded=1",
                                 (c.from_user.id,))).fetchone())["c"]
    link = f"https://t.me/{BOT_USERNAME}?start=r{c.from_user.id}"
    await show(c, tr(L, "ref_text", bonus=REF_BONUS_DAYS, n=n, m=m) + f"\n<pre>{link}</pre>",
               kb([[(tr(L, "btn_menu"), {"callback_data": "menu"})]]))


@router.message(Command("buy"))
@router.callback_query(F.data == "buy")
async def cb_buy(ev):
    user = await ensure_user(ev)
    L = lang_of(user)
    if user["unlimited"]:
        await show(ev, tr(L, "unlimited_note"), kb([[(tr(L, "btn_my"), {"callback_data": "my"})],
                                                    [(tr(L, "btn_menu"), {"callback_data": "menu"})]]))
        return
    rows = [[(tr(L, "plan_btn", title=plan_title(L, p), price=plan_price(p, L), disc=plan_discount(p)),
              {"callback_data": f"plan:{p['id']}"})] for p in PLANS.values()]
    rows.append([(tr(L, "btn_back"), {"callback_data": "menu"})])
    await show(ev, tr(L, "choose_plan"), kb(rows))


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(c: CallbackQuery):
    L = lang_of(await ensure_user(c))
    p = PLANS.get(c.data.split(":", 1)[1])
    if not p:
        await c.answer()
        return
    rows = [[(tr(L, "btn_stars", n=p["stars"]), {"callback_data": f"pay:stars:{p['id']}"})]]
    if CRYPTOBOT_TOKEN:
        rows.append([(tr(L, "btn_crypto", n=p["usdt"]), {"callback_data": f"pay:crypto:{p['id']}"})])
    rows.append([(tr(L, "btn_back"), {"callback_data": "buy"})])
    await show(c, tr(L, "choose_method", title=plan_title(L, p), days=p["days"], devices=MAX_DEVICES), kb(rows))


async def new_payment(tg_id: int, plan: dict, method: str, amount, currency: str) -> int:
    cur = await db.execute("insert into payments(tg_id, plan, method, amount, currency, created_at) values(?,?,?,?,?,?)",
                           (tg_id, plan["id"], method, str(amount), currency, int(time.time())))
    await db.commit()
    return cur.lastrowid


@router.callback_query(F.data.startswith("pay:stars:"))
async def cb_pay_stars(c: CallbackQuery):
    L = lang_of(await ensure_user(c))
    p = PLANS.get(c.data.split(":")[2])
    if not p:
        await c.answer()
        return
    pid = await new_payment(c.from_user.id, p, "stars", p["stars"], "XTR")
    title = plan_title(L, p)
    await bot.send_invoice(c.from_user.id, title=tr(L, "inv_title", brand=BRAND, title=title)[:32],
                           description=tr(L, "inv_desc", days=p["days"], devices=MAX_DEVICES),
                           payload=f"pay:{pid}", currency="XTR",
                           prices=[LabeledPrice(label=title, amount=int(p["stars"]))], provider_token="")
    await c.answer()


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = False
    if q.invoice_payload.startswith("pay:"):
        cur = await db.execute("select status from payments where id=?", (q.invoice_payload[4:],))
        row = await cur.fetchone()
        ok = bool(row and row["status"] == "pending")
    payer = await get_user(q.from_user.id)
    if ok and payer and payer["unlimited"]:
        ok = False
    if ok:
        row = await (await db.execute("select * from payments where id=?", (q.invoice_payload[4:],))).fetchone()
        plan = PLANS.get(row["plan"])
        ok = bool(plan and row["tg_id"] == q.from_user.id and row["method"] == "stars"
                  and q.currency == "XTR" and q.total_amount == int(plan["stars"]))
    L = lang_of(await get_user(q.from_user.id))
    await q.answer(ok=ok, error_message=None if ok else tr(L, "inv_old"))


@router.message(F.successful_payment)
async def on_stars_paid(m: Message):
    sp = m.successful_payment
    if not sp.invoice_payload.startswith("pay:") or not sp.invoice_payload[4:].isdigit():
        return
    row = await (await db.execute("select * from payments where id=?", (sp.invoice_payload[4:],))).fetchone()
    plan = PLANS.get(row["plan"]) if row else None
    if not (plan and row["tg_id"] == m.from_user.id and sp.currency == "XTR" and sp.total_amount == int(plan["stars"])):
        await notify_admin(f"🚨 Подозрительная оплата Stars от {m.from_user.id}: payload={html.escape(sp.invoice_payload)}, "
                           f"сумма {sp.total_amount} {sp.currency}. Доступ не выдан.")
        return
    await complete_payment(row["id"], ext_id=sp.telegram_payment_charge_id)


@router.callback_query(F.data.startswith("pay:crypto:"))
async def cb_pay_crypto(c: CallbackQuery):
    L = lang_of(await ensure_user(c))
    p = PLANS.get(c.data.split(":")[2])
    if not p or not CRYPTOBOT_TOKEN:
        await c.answer()
        return
    pid = await new_payment(c.from_user.id, p, "crypto", p["usdt"], "USDT")
    title = plan_title(L, p)
    try:
        inv = await cryptopay("createInvoice", asset="USDT", amount=str(p["usdt"]),
                              description=tr(L, "inv_title", brand=BRAND, title=title),
                              payload=f"pay:{pid}", expires_in=3600)
    except Exception:
        log.exception("cryptobot invoice failed")
        await db.execute("update payments set status='error' where id=?", (pid,))
        await db.commit()
        await c.answer(tr(L, "crypto_fail"), show_alert=True)
        return
    await db.execute("update payments set ext_id=? where id=?", (str(inv["invoice_id"]), pid))
    await db.commit()
    url = inv.get("bot_invoice_url") or inv.get("pay_url") or inv.get("mini_app_invoice_url")
    await show(c, tr(L, "crypto_text", title=title, n=p["usdt"]),
               kb([[(tr(L, "btn_pay_crypto"), {"url": url})], [(tr(L, "btn_menu"), {"callback_data": "menu"})]]))


async def cryptopay(method: str, **params):
    async with http.get(f"https://pay.crypt.bot/api/{method}", params=params,
                        headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
                        timeout=aiohttp.ClientTimeout(total=20)) as r:
        d = await r.json(content_type=None)
        if not d.get("ok"):
            raise RuntimeError(f"CryptoBot {method}: {d.get('error')}")
        return d["result"]


async def complete_payment(pid: int, ext_id: str | None = None) -> None:
    """Отмечает платёж оплаченным ровно один раз и выдаёт дни."""
    cur = await db.execute("update payments set status='paid', paid_at=?, ext_id=coalesce(?, ext_id) "
                           "where id=? and status='pending'", (int(time.time()), ext_id, pid))
    await db.commit()
    if cur.rowcount != 1:
        return
    row = await (await db.execute("select * from payments where id=?", (pid,))).fetchone()
    plan = PLANS[row["plan"]]
    user = await get_user(row["tg_id"])
    L = lang_of(user)
    who = f"@{user['username']}" if user and user["username"] else str(row["tg_id"])
    try:
        async with lock_for(row["tg_id"]):
            client, created = await grant_days(row["tg_id"], plan["days"], f"paid {who}")
    except Exception as e:
        log.exception("grant after payment failed")
        await db.execute("update payments set status='paid_not_granted' where id=?", (pid,))
        await db.commit()
        await notify_admin(f"🚨 Оплата #{pid} от {who} получена ({row['amount']} {row['currency']}), "
                           f"но доступ не выдан: {html.escape(str(e))}\nВыдайте вручную: /grant {row['tg_id']} {plan['days']}")
        await bot.send_message(row["tg_id"], tr(L, "paid_manual"))
        return
    await db.execute("update users set trial_used=1 where tg_id=?", (row["tg_id"],))
    await db.commit()
    await bot.send_message(row["tg_id"], tr(L, "paid_new" if created else "paid_renew") + "\n\n" + sub_card(client, L),
                           reply_markup=sub_buttons(client, L), link_preview_options=NO_PREVIEW)
    await notify_admin(f"💰 Оплата: {html.escape(who)} — {plan_title('ru', plan)}, {row['amount']} {row['currency']} ({row['method']})")
    await reward_referrer(row["tg_id"])


async def reward_referrer(tg_id: int) -> None:
    """Бонус пригласившему — один раз, после первой оплаты приглашённого."""
    if REF_BONUS_DAYS <= 0:
        return
    cur = await db.execute("update users set ref_rewarded=1 where tg_id=? and ref_by is not null and ref_rewarded=0", (tg_id,))
    await db.commit()
    if cur.rowcount != 1:
        return
    inviter = (await get_user(tg_id))["ref_by"]
    if not await get_user(inviter):
        return
    try:
        async with lock_for(inviter):
            await grant_days(inviter, REF_BONUS_DAYS, "referral bonus")
        await bot.send_message(inviter, tr(lang_of(await get_user(inviter)), "ref_bonus", bonus=REF_BONUS_DAYS))
        await notify_admin(f"🎁 Реферальный бонус {REF_BONUS_DAYS} дн.: {inviter} за приглашённого {tg_id}")
    except Exception:
        log.exception("referral bonus failed")


# ───────────────────────── администратор ─────────────────────────

def mention(u) -> str:
    return html.escape(f"@{u.username}" if u.username else f"{u.full_name} ({u.id})")


async def notify_admin(text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        await bot.send_message(ADMIN_ID, text, link_preview_options=NO_PREVIEW)
    except Exception:
        log.exception("admin notify failed")


def is_admin(m: Message) -> bool:
    return bool(ADMIN_ID) and m.from_user.id == ADMIN_ID


@router.message(Command("grant"))
async def cmd_grant(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    try:
        tg_id, days = (int(x) for x in (command.args or "").split()[:2])
    except Exception:
        await m.answer("Формат: /grant TELEGRAM_ID ДНЕЙ")
        return
    await db.execute("insert or ignore into users(tg_id, username, full_name, created_at) values(?,?,?,?)",
                     (tg_id, "", "", int(time.time())))
    await db.commit()
    client, created = await grant_days(tg_id, days, "granted by admin")
    await m.answer(("Создан" if created else "Продлён") + f" клиент tg{tg_id} до {fmt_date(int(client['expiryTime']))}\n"
                   f"<pre>{html.escape(client.get('subscriptionUrl', ''))}</pre>")
    L = lang_of(await get_user(tg_id))
    try:
        await bot.send_message(tg_id, tr(L, "granted", days=days) + "\n\n" + sub_card(client, L),
                               reply_markup=sub_buttons(client, L), link_preview_options=NO_PREVIEW)
    except Exception:
        await m.answer("Пользователь ещё не писал боту — сообщение ему не доставлено.")


async def find_panel_client(name: str) -> dict | None:
    lst = await panel("GET", "/panel/client/list")
    name = name.strip().lower()
    return next((c for c in lst if str(c.get("name", "")).lower() == name), None)


@router.message(Command("clients"))
async def cmd_clients(m: Message):
    """Клиенты панели и к каким Telegram-аккаунтам они привязаны."""
    if not is_admin(m):
        return
    lst = await panel("GET", "/panel/client/list")
    linked = {r["client_id"]: r for r in await (await db.execute(
        "select client_id, tg_id, username from users where client_id is not null")).fetchall()}
    lines = []
    for c in sorted(lst, key=lambda x: x["id"]):
        exp = int(c.get("expiryTime") or 0)
        term = "♾" if exp == 0 else ("до " + fmt_date(exp)[:10] if exp > time.time() * 1000 else "истекла")
        u = linked.get(c["id"])
        who = f"→ {html.escape('@' + u['username'] if u['username'] else str(u['tg_id']))}" if u else "— не привязан"
        lines.append(f"<b>{html.escape(str(c.get('name')))}</b> · {term} {who}")
    await m.answer("👥 <b>Клиенты панели</b>\n\n<blockquote>" + "\n".join(lines) + "</blockquote>\n\n"
                   "Привязать: <code>/link TELEGRAM_ID имя</code>\nОтвязать: <code>/unlink TELEGRAM_ID</code>")


@router.message(Command("link"))
async def cmd_link(m: Message, command: CommandObject):
    """Привязывает существующего клиента панели к Telegram-аккаунту, срок и лимиты не меняет."""
    if not is_admin(m):
        return
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        await m.answer("Формат: <code>/link TELEGRAM_ID имя_клиента</code>\nСписок имён: /clients")
        return
    tg_id, name = int(parts[0]), parts[1]
    client = await find_panel_client(name)
    if not client:
        await m.answer(f"Клиент «{html.escape(name)}» в панели не найден. Список: /clients")
        return
    if client["name"] in SERVICE_CLIENTS:
        await m.answer("Это служебный клиент моста — привязывать его нельзя.")
        return
    busy = await (await db.execute("select tg_id from users where client_id=? and tg_id<>?", (client["id"], tg_id))).fetchone()
    if busy:
        await m.answer(f"Этот клиент уже привязан к {busy['tg_id']}. Сначала: /unlink {busy['tg_id']}")
        return
    other = await get_user(tg_id)
    if other and other["client_id"] and other["client_id"] != client["id"]:
        await m.answer(f"У {tg_id} уже есть другая подписка в боте (клиент id {other['client_id']}). Сначала: /unlink {tg_id}")
        return
    client = await panel_client(client["id"])
    unlimited = int(int(client.get("expiryTime") or 0) == 0)
    await db.execute("insert or ignore into users(tg_id, username, full_name, created_at) values(?,?,?,?)",
                     (tg_id, "", "", int(time.time())))
    await db.execute("update users set client_id=?, sub_url=?, trial_used=1, unlimited=?, notified_for=0 where tg_id=?",
                     (client["id"], client.get("subscriptionUrl", ""), unlimited, tg_id))
    await db.commit()
    try:
        await panel("POST", f"/panel/client/update/{client['id']}", {"tgId": tg_id})
    except PanelError:
        log.exception("tgId update failed")
    term = "бессрочная" if unlimited else "до " + fmt_date(int(client["expiryTime"]))
    await m.answer(f"✅ Клиент «{html.escape(client['name'])}» ({term}) привязан к {tg_id}.")
    L = lang_of(await get_user(tg_id))
    try:
        await bot.send_message(tg_id, tr(L, "linked") + "\n\n" + sub_card(client, L),
                               reply_markup=sub_buttons(client, L), link_preview_options=NO_PREVIEW)
    except Exception:
        await m.answer("Пользователь ещё не писал боту. Когда он нажмёт Start, подписка у него уже будет.")


@router.message(Command("unlink"))
async def cmd_unlink(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await m.answer("Формат: <code>/unlink TELEGRAM_ID</code>")
        return
    cur = await db.execute("update users set client_id=null, sub_url=null, unlimited=0 where tg_id=?", (int(arg),))
    await db.commit()
    await m.answer("Отвязано. Клиент в панели не тронут." if cur.rowcount else "Такого пользователя в боте нет.")


pending_broadcast: dict[int, str] = {}


@router.message(Command("broadcast"))
async def cmd_broadcast(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    text = (command.args or "").strip()
    if not text:
        await m.answer("Формат: <code>/broadcast текст сообщения</code>\nСообщение уйдёт всем пользователям бота как обычный текст.")
        return
    n = (await (await db.execute("select count(*) c from users")).fetchone())["c"]
    pending_broadcast[m.from_user.id] = text
    await m.answer(f"📣 <b>Предпросмотр</b> — получателей: {n}\n\n<blockquote>{html.escape(text)}</blockquote>",
                   reply_markup=kb([[("✅ Отправить всем", {"callback_data": "bc:send"}), ("✕ Отмена", {"callback_data": "bc:cancel"})]]))


@router.callback_query(F.data.startswith("bc:"))
async def cb_broadcast(c: CallbackQuery):
    if not (ADMIN_ID and c.from_user.id == ADMIN_ID):
        await c.answer()
        return
    text = pending_broadcast.pop(c.from_user.id, None)
    if c.data == "bc:cancel" or not text:
        await c.message.edit_text("Рассылка отменена.")
        await c.answer()
        return
    await c.message.edit_text("📣 Отправляю…")
    await c.answer()
    ok = fail = 0
    for r in await (await db.execute("select tg_id from users")).fetchall():
        try:
            await bot.send_message(r["tg_id"], text, parse_mode=None)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await c.message.edit_text(f"📣 Рассылка завершена: доставлено {ok}, не доставлено {fail}.")


@router.message(Command("user"))
async def cmd_user(m: Message, command: CommandObject):
    """Карточка пользователя по Telegram ID, @нику или имени клиента в панели."""
    if not is_admin(m):
        return
    q = (command.args or "").strip()
    if not q:
        await m.answer("Формат: <code>/user 123456789</code>, <code>/user @ник</code> или <code>/user имя_клиента</code>")
        return
    row = None
    if q.lstrip("@").isdigit():
        row = await get_user(int(q.lstrip("@")))
    if not row and q.startswith("@"):
        row = await (await db.execute("select * from users where lower(username)=lower(?)", (q[1:],))).fetchone()
    client = None
    if not row:
        try:
            client = await find_panel_client(q)
        except PanelError:
            client = None
        if client:
            row = await (await db.execute("select * from users where client_id=?", (client["id"],))).fetchone()
    if not row and not client:
        await m.answer("Не найден ни в боте, ни среди клиентов панели.")
        return
    if row and row["client_id"]:
        client = await panel_client(row["client_id"])
    elif client:
        client = await panel_client(client["id"])
    lines = []
    if row:
        who = f"@{row['username']}" if row["username"] else "—"
        lines.append(f"Telegram: <code>{row['tg_id']}</code> · {html.escape(who)} · {html.escape(row['full_name'] or '')}")
        lines.append(f"В боте с {fmt_date(int(row['created_at']) * 1000)[:10]}, язык {row['lang'] or 'ru'}")
        pays = await (await db.execute("select currency, count(*) n, sum(cast(amount as real)) s from payments "
                                       "where tg_id=? and status='paid' group by currency", (row["tg_id"],))).fetchall()
        lines.append("Оплаты: " + (", ".join(f"{p['n']} × {p['s']:g} {p['currency']}" for p in pays) or "нет"))
        inv = (await (await db.execute("select count(*) c from users where ref_by=?", (row["tg_id"],))).fetchone())["c"]
        lines.append(f"Пригласил: {inv}")
    else:
        lines.append("К Telegram не привязан")
    if client:
        exp = int(client.get("expiryTime") or 0)
        lines.append(f"Клиент: <b>{html.escape(str(client.get('name')))}</b> · "
                     + ("♾ бессрочно" if exp == 0 else ("до " if exp > time.time() * 1000 else "истекла ") + fmt_date(exp)))
        lines.append(f"Трафик: {fmt_bytes(int(client.get('allTime') or 0))} · "
                     + ("🟢 в сети" if client.get("isOnline") else
                        ("был " + fmt_date(int(client['lastOnline']))) if client.get("lastOnline") else "не подключался"))
        try:
            devs = await panel("GET", f"/panel/client/hwid/list/{client['id']}") or []
        except PanelError:
            devs = []
        act = [device_name(d) for d in devs if not d.get("blocked")]
        blk = [device_name(d) for d in devs if d.get("blocked")]
        lim = client.get("maxHwid") if client.get("hwidEnabled") else "∞"
        lines.append(f"Устройства {len(act)}/{lim}: " + (html.escape(", ".join(act)) or "нет")
                     + (f" · отключены: {html.escape(', '.join(blk))}" if blk else ""))
    await m.answer("👤 <b>Пользователь</b>\n\n<blockquote>" + "\n".join(lines) + "</blockquote>")


@router.message(Command("stats"))
async def cmd_stats(m: Message):
    if not is_admin(m):
        return
    q = lambda s, *a: db.execute(s, a)
    users = (await (await q("select count(*) c from users")).fetchone())["c"]
    with_sub = (await (await q("select count(*) c from users where client_id is not null")).fetchone())["c"]
    langs = await (await q("select coalesce(lang,'ru') l, count(*) n from users group by 1 order by 2 desc")).fetchall()
    rows = await (await q("select currency, count(*) n, sum(cast(amount as real)) s from payments "
                          "where status='paid' group by currency")).fetchall()
    month = int(time.time()) - 30 * 86400
    rows30 = await (await q("select currency, count(*) n, sum(cast(amount as real)) s from payments "
                            "where status='paid' and paid_at>? group by currency", month)).fetchall()
    f = lambda rs: ", ".join(f"{r['n']} шт. на {r['s']:g} {r['currency']}" for r in rs) or "нет"
    await m.answer(f"📊 <b>Статистика</b>\n\n<blockquote>Пользователей бота: <b>{users}</b>\nС подпиской: <b>{with_sub}</b>\n"
                   f"Языки: {', '.join(f'{r[0]} — {r[1]}' for r in langs)}\n"
                   f"Оплат за 30 дней: {f(rows30)}\nОплат всего: {f(rows)}</blockquote>")


@router.message(Command("status"))
async def cmd_status(m: Message):
    if not is_admin(m):
        return
    problems = await run_checks()
    await m.answer("✅ Всё в порядке." if not problems else "⚠️ Проблемы:\n" + "\n".join(f"• {v}" for v in problems.values()))


@router.message(Command("backup"))
async def cmd_backup(m: Message):
    if is_admin(m):
        await send_backup()


# ───────────────────────── фоновые задачи ─────────────────────────

async def crypto_poller():
    while True:
        await asyncio.sleep(20)
        if not CRYPTOBOT_TOKEN:
            continue
        try:
            rows = await (await db.execute("select id, ext_id, created_at from payments where status='pending' "
                                           "and method='crypto' and ext_id is not null")).fetchall()
            if not rows:
                continue
            res = await cryptopay("getInvoices", invoice_ids=",".join(r["ext_id"] for r in rows))
            st = {str(i["invoice_id"]): i for i in res.get("items", [])}
            for r in rows:
                inv = st.get(r["ext_id"]) or {}
                s = inv.get("status")
                if s == "paid":
                    pay = await (await db.execute("select * from payments where id=?", (r["id"],))).fetchone()
                    good = (inv.get("payload") == f"pay:{r['id']}" and inv.get("asset") == "USDT"
                            and float(inv.get("amount") or 0) >= float(pay["amount"]))
                    if good:
                        await complete_payment(r["id"])
                    else:
                        await db.execute("update payments set status='mismatch' where id=?", (r["id"],))
                        await db.commit()
                        await notify_admin(f"🚨 Счёт CryptoBot {r['ext_id']} оплачен, но данные не совпали с платежом #{r['id']}. Доступ не выдан.")
                elif s == "expired" or time.time() - r["created_at"] > 2 * 3600:
                    await db.execute("update payments set status='expired' where id=? and status='pending'", (r["id"],))
                    await db.commit()
        except Exception:
            log.exception("crypto poller")


async def expiry_notifier():
    """Напоминания за 3 дня и за 1 день до окончания."""
    while True:
        try:
            rows = await (await db.execute("select tg_id, client_id, notified_for, lang from users "
                                           "where client_id is not null")).fetchall()
            for u in rows:
                client = await panel_client(u["client_id"])
                if not client:
                    continue
                exp = int(client.get("expiryTime") or 0)
                if exp == 0:
                    continue
                left = exp / 1000 - time.time()
                stage = 3 if 86400 < left <= 3 * 86400 else 1 if 0 < left <= 86400 else 0
                if stage and u["notified_for"] != stage:
                    L = lang_of(u)
                    try:
                        await bot.send_message(u["tg_id"], tr(L, f"remind_{stage}", date=fmt_date(exp, L)),
                                               reply_markup=kb([[(tr(L, "btn_renew"), {"callback_data": "buy"})]]))
                    except Exception:
                        pass
                    await db.execute("update users set notified_for=? where tg_id=?", (stage, u["tg_id"]))
                    await db.commit()
        except Exception:
            log.exception("expiry notifier")
        await asyncio.sleep(3600)


# Что проверяет мониторинг: ноды панели по регионам и TCP-порты (monitoring.json, монтируется в контейнер).
# Регион считается недоступным, если его нода офлайн или закрыты все его critical-порты.
MONITORING = json.loads(Path("/app/monitoring.json").read_text(encoding="utf-8"))
REGION_NODES: dict[str, int] = MONITORING["nodes"]
CHECK_PORTS = [(c["name"], c["host"], int(c["port"])) for c in MONITORING["ports"]]
CRITICAL_PORTS = {r: [f"port:{c['host']}:{c['port']}" for c in MONITORING["ports"]
                      if c.get("region") == r and c.get("critical")] for r in REGION_NODES}


async def port_open(host: str, port: int) -> bool:
    for _ in range(2):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 6)
            w.close()
            return True
        except Exception:
            await asyncio.sleep(2)
    return False


def cert_days_left(path: str) -> int | None:
    try:
        d = ssl._ssl._test_decode_cert(path)
        exp = datetime.strptime(d["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return (exp - datetime.now(timezone.utc)).days
    except Exception:
        return None


async def run_checks() -> dict[str, str]:
    problems: dict[str, str] = {}
    try:
        for n in await panel("GET", "/panel/node/list"):
            if n.get("enable", True) and n.get("status") != "online":
                problems[f"node:{n.get('id')}"] = f"Нода «{n.get('name')}» недоступна (статус: {n.get('status')})"
    except Exception as e:
        problems["panel"] = f"Панель не отвечает: {html.escape(str(e))}"
    results = await asyncio.gather(*(port_open(h, p) for _, h, p in CHECK_PORTS))
    for (name, h, p), ok in zip(CHECK_PORTS, results):
        if not ok:
            problems[f"port:{h}:{p}"] = f"Порт закрыт: {name}"
    du = shutil.disk_usage(DATA)                             # том лежит на системном диске сервера
    if du.total:
        if du.used / du.total > 0.9:
            problems["disk"] = f"Диск NL-сервера заполнен на {du.used * 100 // du.total}%"
    left = cert_days_left("/cert/fullchain.pem")
    if left is not None and left < 10:
        problems["cert"] = f"Сертификат NL-сервера истекает через {left} дн."
    return problems


def country_down(problems: dict[str, str]) -> dict[str, bool]:
    """Регион недоступен, если его нода офлайн или закрыты все его critical-порты."""
    return {r: f"node:{node}" in problems or bool(CRITICAL_PORTS[r]) and all(k in problems for k in CRITICAL_PORTS[r])
            for r, node in REGION_NODES.items()}


async def broadcast_outage(country: str, recovered: bool) -> None:
    """Сообщение о сбое или восстановлении всем, у кого есть подписка."""
    rows = await (await db.execute("select tg_id, lang from users where client_id is not null")).fetchall()
    other = "nl" if country == "ru" else "ru"
    for r in rows:
        L = lang_of(r)
        if recovered:
            text = tr(L, "outage_ok", country=tr(L, f"c_{country}"))
        elif not COUNTRY_OK[other]:
            text = tr(L, "outage_both")
        else:
            text = tr(L, "outage", country=tr(L, f"c_{country}"), other=tr(L, f"c_{other}"))
        try:
            await bot.send_message(r["tg_id"], text)
        except Exception:
            pass
        await asyncio.sleep(0.05)


async def monitor():
    active: dict[str, str] = {}
    strikes: dict[str, int] = {}
    down_strikes = {"ru": 0, "nl": 0}
    announced = {"ru": False, "nl": False}
    await asyncio.sleep(30)
    while True:
        try:
            now = await run_checks()
            for country, down in country_down(now).items():
                down_strikes[country] = down_strikes[country] + 1 if down else 0
                COUNTRY_OK[country] = down_strikes[country] < 2
                if down_strikes[country] == 3 and not announced[country]:     # ~6 минут подряд — сообщаем людям
                    announced[country] = True
                    await broadcast_outage(country, recovered=False)
                elif not down and announced[country]:
                    announced[country] = False
                    await broadcast_outage(country, recovered=True)
            for k, v in now.items():
                strikes[k] = strikes.get(k, 0) + 1
                if strikes[k] == 2 and k not in active:      # проблема держится две проверки подряд
                    active[k] = v
                    await notify_admin(f"🚨 {v}")
            for k in list(strikes):
                if k not in now:
                    strikes.pop(k)
                    if k in active:
                        await notify_admin(f"✅ Восстановлено: {active.pop(k)}")
        except Exception:
            log.exception("monitor")
        await asyncio.sleep(120)


async def device_watcher():
    """Сообщает владельцу о каждом новом устройстве на его подписке."""
    await asyncio.sleep(45)
    while True:
        try:
            rows = await (await db.execute("select tg_id, client_id, lang from users where client_id is not null")).fetchall()
            for u in rows:
                try:
                    devs = await panel("GET", f"/panel/client/hwid/list/{u['client_id']}") or []
                except PanelError:
                    continue
                key = f"seen:{u['client_id']}"
                row = await (await db.execute("select v from kv where k=?", (key,))).fetchone()
                ids = {str(d["id"]) for d in devs}
                if row is not None:                                   # при первом проходе только запоминаем
                    seen = set(json.loads(row["v"]))
                    L = lang_of(u)
                    for d in devs:
                        if str(d["id"]) not in seen and d.get("isActive", True) and not d.get("blocked"):
                            try:
                                await bot.send_message(u["tg_id"], tr(L, "new_device", name=html.escape(device_name(d))),
                                                       reply_markup=kb([[(tr(L, "btn_not_me"), {"callback_data": f"dev:ask:{d['id']}"})],
                                                                        [(tr(L, "btn_devices"), {"callback_data": "dev"})]]))
                            except Exception:
                                pass
                    ids |= seen
                await db.execute("insert or replace into kv(k, v) values(?, ?)", (key, json.dumps(sorted(ids))))
                await db.commit()
        except Exception:
            log.exception("device watcher")
        await asyncio.sleep(120)


SERVICE_CLIENTS = {"ru-bridge", "ru-bridge-nl", "nl-bridge-ru"}      # клиенты мостов между нодами


async def limit_enforcer():
    """Клиентам основного пакета, созданным вручную в панели, включает лимит устройств, как у всех.
    Исключения: служебные клиенты мостов и клиенты с пометкой #nolimit в комментарии."""
    await asyncio.sleep(60)
    while True:
        try:
            linked = {r["client_id"] for r in await (await db.execute(
                "select client_id from users where client_id is not null")).fetchall()}
            for c in await panel("GET", "/panel/client/list") or []:
                if (c.get("hwidEnabled") or c.get("name") in SERVICE_CLIENTS
                        or "#nolimit" in str(c.get("comment") or "").lower()
                        or BUNDLE_ID not in (c.get("bundleIds") or [])):
                    continue
                await panel("POST", f"/panel/client/update/{c['id']}", {"hwidEnabled": True, "maxHwid": MAX_DEVICES})
                hint = "" if c["id"] in linked else f"\nПривязать к Telegram: <code>/link TELEGRAM_ID {html.escape(str(c['name']))}</code>"
                await notify_admin(f"🔒 Клиенту «{html.escape(str(c['name']))}» включён лимит {MAX_DEVICES} устройства "
                                   f"(чтобы оставить без лимита — добавьте #nolimit в комментарий в панели).{hint}")
        except Exception:
            log.exception("limit enforcer")
        await asyncio.sleep(300)


async def send_backup() -> None:
    if not (ADMIN_ID and PANEL_DB_DSN):
        return
    try:
        proc = await asyncio.create_subprocess_exec("pg_dump", "-Fc", PANEL_DB_DSN, stdout=subprocess.PIPE,
                                                    stderr=subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), 300)
        if proc.returncode != 0 or not out:
            raise RuntimeError(err.decode(errors="replace")[:300])
        name = f"sharx_panel_{datetime.now(MSK):%Y%m%d_%H%M}.dump"
        await bot.send_document(ADMIN_ID, BufferedInputFile(out, name),
                                caption=f"💾 Резервная копия базы панели ({len(out) // 1024} КБ)")
        if DB_PATH.exists():
            await bot.send_document(ADMIN_ID, BufferedInputFile(DB_PATH.read_bytes(), "bot.db"),
                                    caption="💾 База бота (платежи и пользователи)")
    except Exception as e:
        log.exception("backup failed")
        await notify_admin(f"⚠️ Резервная копия не создана: {html.escape(str(e))}")


async def backup_scheduler():
    while True:
        now = datetime.now(MSK)
        nxt = now.replace(hour=5, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        await send_backup()


async def setup_profile() -> None:
    """Меню команд и описание бота на трёх языках."""
    for code in LANGS:
        cmds = [BotCommand(command=c, description=tr(code, f"cmd_{c}")) for c in ("start", "my", "buy", "help", "lang")]
        lc = None if code == "en" else code          # английский — язык по умолчанию для остальных
        try:
            await bot.set_my_commands(cmds, language_code=lc)
            await bot.set_my_short_description(tr(code, "bot_about"), language_code=lc)
            await bot.set_my_description(tr(code, "bot_desc"), language_code=lc)
        except Exception:
            log.exception("profile setup failed for %s", code)


async def main():
    global bot, http
    DATA.mkdir(exist_ok=True)
    await init_db()
    http = aiohttp.ClientSession()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    log.info("bot @%s started, admin=%s, cryptobot=%s", me.username, ADMIN_ID or "-", bool(CRYPTOBOT_TOKEN))
    await setup_profile()
    tasks = [asyncio.create_task(t()) for t in (crypto_poller, expiry_notifier, monitor, backup_scheduler, device_watcher, limit_enforcer)]
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
        await http.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
