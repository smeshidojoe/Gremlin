"""Команды чата и выключенные медиа-фильтры.

Выключенная команда должна исчезать для бота целиком. Если бы обработчик
просто молча выходил, сообщение «!мут» не дошло бы до остальных правил, а
хуже того — не-админ получал бы мут за «чужую команду», которой в чате нет.
"""
from gremlin import config, db, schema
from gremlin.handlers import group
from gremlin.handlers import user_menu as um
from gremlin.services import transfer

from conftest import CHAT, OWNER, Msg

CMD_FIELDS = {
    group.cmd_mute: "cmd_mute_on",
    group.cmd_kick: "cmd_kick_on",
    group.cmd_ban: "cmd_ban_on",
    group.cmd_warn: "cmd_warn_on",
    group.cmd_lift: "cmd_lift_on",
    group.cmd_delete: "cmd_dm_on",
}


def buttons(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


async def test_commands_are_on_by_default(chat):
    s = await db.get_settings(chat)
    assert all(getattr(s, f) == 1 for f in CMD_FIELDS.values())


async def test_switched_off_command_does_not_match(chat):
    check = group._cmd_on("cmd_mute_on")
    msg = Msg("!мут 30м")
    assert await check(msg) is True
    await db.set_setting(chat, "cmd_mute_on", 0)
    assert await check(msg) is False
    # соседние команды не задело
    assert await group._cmd_on("cmd_ban_on")(msg) is True


def test_every_command_handler_has_its_switch():
    """Каждая команда модерации зарегистрирована со своим выключателем."""
    seen = {}
    for handler in group.router.message.handlers:
        if handler.callback in CMD_FIELDS:
            names = [getattr(f.callback, "__qualname__", "") for f in handler.filters]
            seen[handler.callback] = any(n.startswith("_cmd_on") for n in names)
    assert seen == {cb: True for cb in CMD_FIELDS}


async def test_section_everywhere(chat):
    keys = {f.key for f in next(s for s in schema.SECTIONS if s.key == "modcmds").fields}
    assert set(CMD_FIELDS.values()) | {"misuse_mute", "cmd_mute_min",
                                       "cmd_ban_min"} == keys
    # перенос настроек забирает раздел целиком
    assert set(transfer.GROUPS["modcmds"][1]) == keys
    _text, kb = await um.view_chat(CHAT, OWNER)
    data = buttons(kb)
    assert f"u:s:{CHAT}:modcmds" in data
    # заодно: жалобы и набеги в меню бота раньше не открывались вовсе
    assert f"u:s:{CHAT}:report" in data and f"u:s:{CHAT}:raid" in data


async def test_media_filters_hidden_but_kept(chat):
    assert config.MEDIA_FILTERS is False
    assert "media" in schema.hidden_sections()
    assert "media_on" not in {k for k, _ in schema.OVERVIEW}
    _text, kb = await um.view_chat(CHAT, OWNER)
    assert f"u:s:{CHAT}:media" not in buttons(kb)
    # сам раздел и настройки на месте: включить обратно — одна строка
    assert any(s.key == "media" for s in schema.SECTIONS)
    assert hasattr(await db.get_settings(chat), "media_mask")
