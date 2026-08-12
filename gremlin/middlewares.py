"""Мидлвари: трекинг юзеров в личке + контроль доступа к боту."""
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from . import config, db


_DENIED = (
    "🔒 Доступ к боту закрыт.\n\n"
    "Бот работает по списку допущенных. Обратитесь к владельцу за доступом."
)


class TrackingMiddleware(BaseMiddleware):
    """Личка: пишем юзера в базу, отсекаем забаненных и не допущенных к боту."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = None
        private = False
        if isinstance(event, Message):
            user = event.from_user
            private = event.chat.type == "private"
        elif isinstance(event, CallbackQuery):
            user = event.from_user
            private = event.message is not None and event.message.chat.type == "private"
        if user is not None and private and not user.is_bot:
            if user.id not in config.ADMIN_IDS:
                if await db.is_bot_banned(user.id):
                    if isinstance(event, CallbackQuery):
                        await event.answer("Доступ к боту закрыт.", show_alert=True)
                    return None
                # список допуска: пустой = бот открыт всем (не мешаем первому запуску)
                if await db.access_list() and not await db.access_allowed(
                    user.id, user.username
                ):
                    if isinstance(event, CallbackQuery):
                        await event.answer("Доступ к боту закрыт.", show_alert=True)
                    elif isinstance(event, Message):
                        await event.answer(_DENIED)
                    return None
            await db.track_user(user.id, user.username, user.first_name)
        return await handler(event, data)
