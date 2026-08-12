"""Служебные разделы меню: состояние, логи, ошибки, действия по чату."""
import logging
import platform

import aiogram
from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, runtime, utils
from ..services import errorlog

logger = logging.getLogger("gremlin.admin")

router = Router()
router.message.filter(F.from_user.id.in_(config.ADMIN_IDS), F.chat.type == "private")
router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))

def _back_kb():
    """Назад ведёт в единое главное меню (обрабатывается в user_menu)."""
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="a:home")
    return b.as_markup()


# ---------- статистика / состояние ----------

@router.callback_query(F.data == "a:health")
async def cb_health(cb: CallbackQuery) -> None:
    up = runtime.uptime_seconds()
    h, rem = divmod(up, 3600)
    m, sec = divmod(rem, 60)
    text = (
        "<b>⚙️ Состояние</b>\n\n"
        f"⏱ Аптайм: <b>{h}ч {m}м {sec}с</b>\n"
        f"🐍 Python: <b>{platform.python_version()}</b>\n"
        f"🤖 aiogram: <b>{aiogram.__version__}</b>"
    )
    await cb.message.edit_text(text, reply_markup=_back_kb())
    await cb.answer()


# ---------- действия по чату (кнопки живут в дашборде чата) ----------

@router.callback_query(F.data.startswith("a:clog:"))
async def cb_chat_log(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    ch = await db.get_chat(cid)
    rows = await db.recent_events(20, chat_id=cid)
    title = utils.esc(ch["title"] if ch else str(cid))
    lines = [f"<b>📜 Лог чата</b> · {title}\n"]
    lines += [utils.event_line(r["kind"], r["text"], r["ts"]) for r in rows] or ["Пока пусто."]
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=f"u:c:{cid}")
    await cb.message.edit_text(utils.chunk("\n".join(lines)), reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("a:leave:"))
async def cb_leave(cb: CallbackQuery) -> None:
    cid = int(cb.data.split(":")[2])
    b = InlineKeyboardBuilder()
    b.button(text="✅ Да, выйти", callback_data=f"a:leave_yes:{cid}")
    b.button(text="⬅️ Отмена", callback_data=f"u:c:{cid}")
    b.adjust(2)
    await cb.message.edit_text(
        f"Точно покинуть чат <code>{cid}</code>?", reply_markup=b.as_markup()
    )
    await cb.answer()


@router.callback_query(F.data.startswith("a:leave_yes:"))
async def cb_leave_yes(cb: CallbackQuery, bot: Bot) -> None:
    cid = int(cb.data.split(":")[2])
    try:
        await bot.leave_chat(cid)
    except Exception as e:
        await cb.answer(f"Не вышло: {e}", show_alert=True)
        return
    await db.set_chat_active(cid, False)
    await cb.message.edit_text("✅ Бот покинул чат.", reply_markup=_back_kb())
    await cb.answer()


# ---------- лог событий ----------

@router.callback_query(F.data == "a:log")
async def cb_log(cb: CallbackQuery) -> None:
    rows = await db.recent_events(20)
    titles = {c["chat_id"]: c["title"] for c in await db.all_chats()}
    lines = ["<b>📜 Последние события</b>\n"]
    lines += [
        utils.event_line(r["kind"], r["text"], r["ts"], titles.get(r["chat_id"]))
        for r in rows
    ] or ["Пока пусто."]
    await cb.message.edit_text(utils.chunk("\n".join(lines)), reply_markup=_back_kb())
    await cb.answer()


# ---------- ошибки ----------

@router.callback_query(F.data == "a:errors")
async def cb_errors(cb: CallbackQuery) -> None:
    recent = errorlog.recent(15)
    if not recent:
        text = "<b>🐞 Ошибки</b>\n\nОшибок нет."
    else:
        body = "\n\n".join(recent)[-3500:]
        text = f"<b>🐞 Последние ошибки</b>\n\n<pre>{utils.esc(body)}</pre>"
    await cb.message.edit_text(text, reply_markup=_back_kb())
    await cb.answer()
