"""Наблюдение за профилями: скоринг подозрительности (вдохновлено Casper).

Два порога: suspect (карточка «подозрительный» в лог-чат) и ban (автобан).
Сигналы взвешены так, чтобы одиночный слабый признак (CJK-имя, эмодзи) не давал
ложных срабатываний — банят только комбинации или явную обфускацию.
"""
import logging
import re

logger = logging.getLogger("gremlin.watch")

# Невидимые символы — но только те, которым нечего делать в обычном тексте.
# Намеренно НЕ трогаем U+200D (склейка эмодзи) и U+200E/U+200F (метки направления):
# Telegram сам подставляет их вокруг эмодзи и флагов, и на них ловились живые люди.
_INVISIBLE = re.compile(
    "["
    "​‌"          # zero-width space / non-joiner
    "⁠-⁤"         # word joiner и невидимые операторы
    "‪-‮"         # bidi-встраивание и подмена направления
    "᠎﻿"          # монгольский разделитель, BOM
    "]"
)

# Эмодзи-последовательность целиком: сами эмодзи, флаги, модификаторы и склейки.
# Нужна, чтобы вырезать её перед поиском невидимых — иначе ZWJ внутри 👨‍👩‍👧
# читался бы как попытка спрятать символы.
_EMOJI_SEQ = re.compile(
    "[\U0001f000-\U0001faff☀-➿⬀-⯿"
    "\U0001f1e6-\U0001f1ff️‍\U0001f3fb-\U0001f3ff]+"
)


def _visible_part(text: str) -> str:
    """Текст без эмодзи-последовательностей — в нём и ищем спрятанные символы."""
    return _EMOJI_SEQ.sub(" ", text or "")


# редкие алфавиты, в норме не встречающиеся в именах: руны, глаголица, эфиопский,
# математические стилизованные буквы
_RARE_SCRIPT = re.compile(
    r"[ᚠ-᛿Ⰰ-ⱟሀ-፿\U0001d400-\U0001d7ff]"
)
_CJK = re.compile(r"[一-鿿぀-ヿ]")
# Считаем только «настоящие» эмодзи. Блок Dingbats (✧ ✦ ❀ ✿ ★) намеренно исключён:
# это обычное украшение ника, а не признак спама.
_EMOJI = re.compile(
    "[\U0001F300-\U0001F6FF\U0001F900-\U0001F9FF\U0001FA70-\U0001FAFF]"
)
_URLISH = re.compile(r"(t\.me/|https?://|@\w{4,})", re.IGNORECASE)
_TELEGRAPH = re.compile(r"(telegra\.ph|graph\.org)/", re.IGNORECASE)
_BOT_MENTION = re.compile(r"@\w*bot\b", re.IGNORECASE)
# кириллица и латиница внутри одного слова = гомоглифы
_HOMOGLYPH_WORD = re.compile(r"\w*(?:[а-яё][a-z]|[a-z][а-яё])\w*", re.IGNORECASE)
# валютные/декоративные символы внутри слов (Д€ᛠርKO€)
_SYMBOL_IN_WORD = re.compile(r"[а-яёa-z][€₽$£♱♰卐]|[€₽$£♱♰卐][а-яёa-z]", re.IGNORECASE)
_PROFILE_ADS = re.compile(r"(смотри|ссылк\w+ в|в профил|проф\w* 👆|check bio)", re.IGNORECASE)


def score_profile(first_name: str | None, last_name: str | None,
                  username: str | None) -> tuple[int, list[str]]:
    """Оценка профиля. Вернуть (очки, причины)."""
    name = " ".join(x for x in (first_name, last_name) if x)
    score, reasons = 0, []
    if not name:
        return 0, []
    if _INVISIBLE.search(_visible_part(name)):
        score += 40; reasons.append("невидимые символы в имени")
    if _RARE_SCRIPT.search(name):
        score += 35; reasons.append("нетипичный алфавит в имени")
    if _URLISH.search(name):
        score += 30; reasons.append("ссылка/упоминание в имени")
    if _PROFILE_ADS.search(name):
        score += 35; reasons.append("реклама профиля в имени")
    if _HOMOGLYPH_WORD.search(name):
        score += 25; reasons.append("гомоглифы в имени")
    if _SYMBOL_IN_WORD.search(name):
        score += 25; reasons.append("символы-заменители в имени")
    if _CJK.search(name):
        score += 15; reasons.append("CJK-имя")
    if len(_EMOJI.findall(name)) >= 4:   # пара эмодзи в нике — обычное дело
        score += 10; reasons.append("эмодзи-спам в имени")
    if re.search(r"[а-яёa-z]\d{2,}|[一-鿿぀-ヿ]\d", name, re.IGNORECASE):
        score += 10; reasons.append("цифры в имени")
    return score, reasons


def score_message(text: str) -> tuple[int, list[str]]:
    """Оценка текста сообщения."""
    score, reasons = 0, []
    if not text:
        return 0, []
    if _TELEGRAPH.search(text):
        score += 45; reasons.append("telegra.ph-ссылка")
    if _INVISIBLE.search(_visible_part(text)):
        score += 30; reasons.append("невидимые символы")
    if _BOT_MENTION.search(text):
        score += 25; reasons.append("упоминание бота")
    if _RARE_SCRIPT.search(text) or _SYMBOL_IN_WORD.search(text):
        score += 25; reasons.append("обфускация текста")
    elif _HOMOGLYPH_WORD.search(text):
        score += 15; reasons.append("гомоглифы в тексте")
    return score, reasons


def profile_sig(first_name: str | None, last_name: str | None, username: str | None) -> str:
    """Подпись профиля для отслеживания изменений."""
    return f"{first_name or ''}|{last_name or ''}|{username or ''}"


async def check_user(bot, chat, user, settings, message=None) -> None:
    """Полный цикл наблюдения: скоринг профиля (+сообщения), бан или карточка.

    Профиль скорится один раз на версию профиля (изменился — пересчёт).
    Сообщение скорится каждый раз, очки складываются.
    """
    from .. import config, db, utils
    from . import moderation

    sig = profile_sig(user.first_name, user.last_name, user.username)
    row = await db.watch_get(chat.id, user.id)
    profile_changed = row is None or row["sig"] != sig

    p_score, p_reasons = score_profile(user.first_name, user.last_name, user.username)
    m_score, m_reasons = (0, [])
    if message is not None:
        m_score, m_reasons = score_message(message.text or message.caption or "")

    total = p_score + m_score
    reasons = p_reasons + m_reasons
    # текст сообщения — только если человек что-то писал: на входе в чат его нет
    body = moderation.message_body(message)
    if total < settings.watch_suspect:
        if profile_changed:
            await db.watch_set(chat.id, user.id, sig, False)
        return

    who = utils.mention(user.id, user.full_name, user.username)
    why = ", ".join(reasons)

    # автобан по порогу
    if settings.watch_ban and total >= settings.watch_ban:
        if message is not None:
            try:
                await message.delete()
            except Exception:
                pass
        pid = await moderation.apply_punishment(
            bot, chat.id, user, "ban", 0, f"наблюдение: {why} ({total})", None
        )
        await db.watch_set(chat.id, user.id, sig, True)
        card = (
            f"⛔ <b>Бан (наблюдение)</b> · {utils.esc(chat.title)}\n"
            f"👤 {who} (<code>{user.id}</code>)\n"
            f"📎 Сигналы: {utils.esc(why)} — <b>{total} очков</b>\n"
            f"🤖 Кем: Gremlin (автомод)" + body
        )
        await db.add_event(chat.id, "watch", f"ban: {user.full_name} ({user.id}) — {why} ({total})")
        await moderation.send_card(bot, chat.id, config.BIT_WATCH, card, pid, "ban", user.id)
        return

    # подозрение: карточка один раз на версию профиля
    if row is not None and row["flagged"] and not profile_changed:
        return
    await db.watch_set(chat.id, user.id, sig, True)
    card = (
        f"👁 <b>Подозрительный аккаунт</b> · {utils.esc(chat.title)}\n"
        f"👤 {who} (<code>{user.id}</code>)\n"
        f"📎 Сигналы: {utils.esc(why)} — <b>{total} очков</b>"
        + ("\n🔄 Профиль изменился" if row is not None and profile_changed else "")
        + body
    )
    await db.add_event(chat.id, "watch", f"suspect: {user.full_name} ({user.id}) — {why} ({total})")
    s = await db.get_settings(chat.id)
    if s.cards_on and s.log_chat_id and (s.card_mask & config.BIT_WATCH):
        from aiogram.utils.keyboard import InlineKeyboardBuilder
        b = InlineKeyboardBuilder()
        b.button(text="⛔ Забанить", callback_data=f"k:ban:{chat.id}:{user.id}")
        b.button(text="🕊 Не трогать", callback_data="k:wok")
        b.adjust(2)
        try:
            await bot.send_message(s.log_chat_id, card, reply_markup=b.as_markup())
        except Exception:
            logger.warning("suspect card failed for chat %s", chat.id, exc_info=True)


if __name__ == "__main__":
    # самопроверка на реальном примере из спам-атаки
    s, r = score_profile("木子淼淼1", None, "saribnak")
    assert s >= 25, (s, r)                       # CJK + цифры = подозрительный
    s, r = score_message("Purchasery @neuxieksbot ecad‌")
    assert s >= 55, (s, r)                       # бот-упоминание + невидимый символ
    s, r = score_message("🤫 Д€ᛠርKO€ ᛖΘΛΘԿK0 ᛠYᛠ 👇 https://telegra.ph/AKTUALNAYA-07-10")
    assert s >= 70, (s, r)                       # telegra.ph + руны = бан
    s, r = score_profile("Иван Петров", None, "ivan")
    assert s == 0, (s, r)                        # обычный профиль чист
    s, r = score_message("привет, как дела? посмотри код на github")
    assert s == 0, (s, r)

    # живые ники, на которых бот раньше ошибался: декоративные ✧ считались
    # эмодзи-спамом, а служебные символы вокруг эмодзи — «невидимками»
    for name in ("✧Yutohai✧ 📖", "Лягушенька 🐸", "Аня 💐🌸", "👨‍👩‍👧 Семья"):
        s, r = score_profile(name, None, "user")
        assert s == 0, (name, s, r)
    # а спрятанный символ в обычном тексте — по-прежнему сигнал
    s, _ = score_message("текст‮со скрытым разворотом")
    assert s >= 30
    print("watch self-check OK")
