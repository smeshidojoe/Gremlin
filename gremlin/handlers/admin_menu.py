"""Служебные разделы меню: состояние, логи, ошибки, действия по чату."""
import logging
from aiogram import F, Router
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import config, db, utils
from ..services import errorlog, health

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
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data="a:health")
    b.button(text="⬅️ Назад", callback_data="a:home")
    b.adjust(1)
    try:
        await cb.message.edit_text(await health.report(), reply_markup=b.as_markup())
    except Exception:
        pass          # нажали «Обновить», а с прошлого раза ничего не поменялось
    await cb.answer()


# ---------- действия по чату (кнопки живут в дашборде чата) ----------

# ---------- лог событий ----------

@router.callback_query(F.data == "a:log")
async def cb_log(cb: CallbackQuery) -> None:
    rows = await db.recent_events(20)
    titles = {c["chat_id"]: c["title"] for c in await db.all_chats()}
    lines = ["<b>📜 Последние события</b>\n"]
    lines += [
        utils.event_line(r["kind"], await db.names_in(r["text"]), r["ts"],
                         titles.get(r["chat_id"]))
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
