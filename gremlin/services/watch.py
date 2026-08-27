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
# настоящая ссылка, без @упоминаний: обращение к человеку — не признак рекламы
_REAL_LINK = re.compile(r"(t\.me/|tg://|https?://|www\.|\w+\.(?:com|ru|net|org|io)/)",
                        re.IGNORECASE)
# сокращатели: спам прячет за ними и telegra.ph, и чужие каналы
_SHORTENER = re.compile(
    r"\b(bit\.ly|vk\.cc|clck\.ru|goo\.su|tinyurl\.com|is\.gd|cutt\.ly|surl\.li|"
    r"t\.co|rb\.gy|shorturl\.at|u\.to|qps\.ru|gg\.gg)\b", re.IGNORECASE)
_TELEGRAPH = re.compile(r"(telegra\.ph|graph\.org)/", re.IGNORECASE)
_BOT_MENTION = re.compile(r"@\w*bot\b", re.IGNORECASE)

# Юзернейм самого Гремлина. Его упоминание — обращение к нам, а не признак
# спама: люди зовут бота по имени, и штрафовать их за это глупо.
SELF_USERNAME = ""


def set_self(username: str | None) -> None:
    global SELF_USERNAME
    SELF_USERNAME = (username or "").lower()


def _drop_self(text: str) -> str:
    if not SELF_USERNAME:
        return text
    return re.sub(rf"@{re.escape(SELF_USERNAME)}\b", " ", text, flags=re.IGNORECASE)

# кириллица и латиница внутри одного слова = гомоглифы
_HOMOGLYPH_WORD = re.compile(r"\w*(?:[а-яё][a-z]|[a-z][а-яё])\w*", re.IGNORECASE)
# валютные/декоративные символы внутри слов (Д€ᛠርKO€)
_SYMBOL_IN_WORD = re.compile(r"[а-яёa-z][€₽$£♱♰卐]|[€₽$£♱♰卐][а-яёa-z]", re.IGNORECASE)
# цифра вместо буквы внутри слова: п0рн0, каз1но, прив3т
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_DIGIT_INSIDE = re.compile(r"[а-яёa-z]\d+[а-яёa-z]", re.IGNORECASE)


def _digit_in_word(text: str) -> bool:
    """Цифра, вклиненная между букв, в слове хотя бы из четырёх знаков.

    Проверяем именно так, а не одной регуляркой на «цифра рядом с буквой»:
    иначе в кривопись записывались «3D», «1С», «5G», «A4», «COVID19» и прочая
    обычная техническая речь. У них цифра либо с краю, либо слово короткое.
    """
    return any(len(w) >= 4 and _DIGIT_INSIDE.search(w) for w in _WORD.findall(text))


# слово, растащенное знаками: к.а.з.и.н.о, с-л-и-в. Пробела в списке нет
# намеренно — с ним под правило попадала любая живая фраза с короткими словами
# («него я в твиттере» читалось как «о я в т»)
_SPLIT_WORD = re.compile(r"(?:[а-яёa-z][.\-_·•*|/]){3,}[а-яёa-z]", re.IGNORECASE)
# то же самое, но через пробелы: «п о р н о». Тут строже — нужен длинный ряд
# одиночных букв, и среди них хотя бы три согласных, иначе ловились «а я и т д»
_VOWELS = set("аеёиоуыэюяaeiouy")


def _spaced_word(text: str) -> bool:
    run = []
    for token in text.split():
        letter = token.strip(".,!?;:()«»\"'")
        if len(letter) == 1 and letter.isalpha():
            run.append(letter.lower())
            if len(run) >= 5 and sum(c not in _VOWELS for c in run) >= 3:
                return True
        else:
            run = []
    return False


# растянутые буквы: пооооорно, сliiiiв. Нужно четыре повтора и больше — «эээ»,
# «нуууу» и «ааааа» это обычная живая речь, а не попытка обойти фильтр
_CHAR_RUN = re.compile(r"([а-яёa-z])\1{4,}", re.IGNORECASE)
# ссылки из проверки на кривопись выкидываем: в любом адресе полно цифр рядом
# с буквами (deezload2bot, track/2610505422), и это не обход фильтра
_URL_TOKEN = re.compile(r"\S*(?:https?://|t\.me/|www\.|\.(?:com|ru|org|net|ph|io|me)\b)\S*",
                        re.IGNORECASE)


def _wordy_part(text: str) -> str:
    """Текст без ссылок — в нём и ищем кривое написание слов."""
    return _URL_TOKEN.sub(" ", text or "")


_PROFILE_ADS = re.compile(r"(смотри|ссылк\w+ в|в профил|проф\w* 👆|check bio)", re.IGNORECASE)


# Косметика: странное написание ника. Живые люди украшают имена постоянно —
# латиница вперемешку с кириллицей, «€» вместо буквы, иероглифы, готические
# буквы. Само по себе это не спам, поэтому вся косметика вместе не может
# перевесить порог подозрения: её сумма ограничена COSMETIC_CAP.
COSMETIC_CAP = 30

# У текста потолок свой и выше: признаков там пять, и упираться в тридцатку
# после первого же из них — значит не различать «нуууу» и «к.а.з.и.н.о п0рн0».
TEXT_COSMETIC_CAP = 45

# Кривое написание рядом со ссылкой — уже не случайность, а обход фильтра.
# Столько добавляем, когда косметика и настоящая ссылка встретились вместе.
OBFUSCATION_BOOST = 25

# Столько добавляем тому, кто в чате не состоит, если очки уже есть.
GUEST_BOOST = 15

# Копилка сообщений: спам выгодно дробить на мелкие порции, поэтому очки за
# сообщения складываются, пока человек не замолчит на сутки.
SCORE_WINDOW = 86400
SCORE_MAX = 200          # выше копить бессмысленно: бан наступит раньше

# Повторная карточка подозрения — только если счёт заметно вырос.
RECARD_STEP = 20


def score_profile(first_name: str | None, last_name: str | None,
                  username: str | None) -> tuple[int, list[str]]:
    """Оценка профиля. Вернуть (очки, причины)."""
    name = " ".join(x for x in (first_name, last_name) if x)
    if not name:
        return 0, []
    score, reasons = 0, []
    cosmetic = 0

    # тревожные признаки: так оформляют профиль ради рекламы и обхода фильтров
    if _INVISIBLE.search(_visible_part(name)):
        score += 40; reasons.append("невидимые символы в имени")
    if _URLISH.search(name):
        score += 40; reasons.append("ссылка/упоминание в имени")
    if _PROFILE_ADS.search(name):
        score += 45; reasons.append("реклама профиля в имени")

    # косметические: считаем, но общий вклад режем
    if _RARE_SCRIPT.search(name):
        cosmetic += 35; reasons.append("нетипичный алфавит в имени")
    if _HOMOGLYPH_WORD.search(name):
        cosmetic += 25; reasons.append("гомоглифы в имени")
    if _SYMBOL_IN_WORD.search(name):
        cosmetic += 25; reasons.append("символы-заменители в имени")
    if _CJK.search(name) and not _RARE_SCRIPT.search(name):
        cosmetic += 15; reasons.append("CJK-имя")   # иначе один признак дважды
    if len(_EMOJI.findall(name)) >= 4:   # пара эмодзи в нике — обычное дело
        cosmetic += 10; reasons.append("эмодзи-спам в имени")
    if re.search(r"[а-яёa-z]\d{2,}|[一-鿿぀-ヿ]\d", name, re.IGNORECASE):
        cosmetic += 10; reasons.append("цифры в имени")

    return score + min(cosmetic, COSMETIC_CAP), reasons


def message_parts(text: str) -> tuple[int, int, list[str]]:
    """Разобрать текст на (тревожные очки, косметика, причины).

    Тревожные (telegra.ph, невидимки, сокращатели) и усилитель за кривопись
    рядом со ссылкой — это уже поведение спамера, такое имеет смысл копить.
    Косметика сама по себе — просто манера письма («эээ», «нууу», «прив3т»);
    она живёт ровно одно сообщение и в копилку не идёт, иначе за день любой
    разговорчивый человек набирает на карточку.
    """
    hard, reasons = 0, []
    if not text:
        return 0, 0, []
    text = _drop_self(text)      # «@GremlinModBot, привет» — это к нам, не спам
    if _TELEGRAPH.search(text):
        hard += 45; reasons.append("telegra.ph-ссылка")
    if _INVISIBLE.search(_visible_part(text)):
        hard += 30; reasons.append("невидимые символы")
    if _BOT_MENTION.search(text):
        hard += 25; reasons.append("упоминание бота")
    if _SHORTENER.search(text):
        hard += 20; reasons.append("ссылка через сокращатель")

    words = _wordy_part(text)
    cosmetic = 0
    if _RARE_SCRIPT.search(words) or _SYMBOL_IN_WORD.search(words):
        cosmetic += 25; reasons.append("обфускация текста")
    elif _HOMOGLYPH_WORD.search(words):
        cosmetic += 15; reasons.append("гомоглифы в тексте")
    if _SPLIT_WORD.search(words) or _spaced_word(words):
        cosmetic += 20; reasons.append("слово через разделители")
    if _digit_in_word(words):
        cosmetic += 15; reasons.append("цифры вместо букв")
    if _CHAR_RUN.search(words):
        cosmetic += 10; reasons.append("растянутые буквы")
    cosmetic = min(cosmetic, TEXT_COSMETIC_CAP)

    if cosmetic and _REAL_LINK.search(text):
        hard += OBFUSCATION_BOOST
        reasons.append("кривой текст вместе со ссылкой")
    return hard, cosmetic, reasons


def score_message(text: str) -> tuple[int, list[str]]:
    """Общая оценка текста: тревожное плюс косметика этого сообщения."""
    hard, cosmetic, reasons = message_parts(text)
    return hard + cosmetic, reasons


# Ссылка вида t.me/<кто-то>[/что-то][?start=…]. Нужна, чтобы отличать инвайт
# в чужой чат от безобидной дип-ссылки бота на самого себя.
_TG_URL = re.compile(r"(?:https?://)?(?:t|telegram)\.me/([^/?#\s]+)([^\s]*)", re.I)


def _button_kind(url: str, self_bot: str | None) -> str | None:
    """Что за ссылка на кнопке: graph | invite | chat | deeplink | self | None."""
    if re.search(r"(telegra\.ph|graph\.org)/", url, re.I):
        return "graph"
    m = _TG_URL.search(url)
    if not m:
        return None
    name, tail = m.group(1), m.group(2) or ""
    if name.startswith("+") or name.lower() == "joinchat":
        return "invite"
    if "start=" in tail.lower():
        # инлайн-боты вешают на кнопку ссылку в личку самих себя («Please wait…»)
        # — это их обычная работа, а не увод аудитории
        return "self" if self_bot and name.lower() == self_bot.lstrip("@").lower() else "deeplink"
    return "chat"


# вес и подпись для каждого вида кнопки
_BUTTON_WEIGHTS = {
    "graph": (30, "кнопка на telegra.ph"),
    "invite": (25, "кнопка-инвайт в чат"),
    "chat": (25, "кнопка на чат/канал"),
    "deeplink": (5, "кнопка в личку бота"),
    "self": (0, ""),
}


def score_buttons(urls: list[str], self_bot: str | None = None) -> tuple[int, list[str]]:
    """Оценка кнопок под сообщением: в рекламе вся суть висит именно на них.

    self_bot — юзернейм бота, который это сообщение и отдал: ссылку на самого
    себя ему не засчитываем.
    """
    score, reasons = 0, []
    if urls:
        score += 20
        reasons.append("кнопки со ссылками")
    seen = set()
    for url in urls:
        kind = _button_kind(url, self_bot)
        if kind is None or kind in seen:
            continue
        seen.add(kind)
        weight, label = _BUTTON_WEIGHTS[kind]
        if weight:
            score += weight
            reasons.append(label)
    return score, reasons


def score_content(text: str, urls: list[str] | None = None,
                  self_bot: str | None = None) -> tuple[int, list[str]]:
    """Текст плюс кнопки — общая оценка содержимого сообщения."""
    urls = urls or []
    score, reasons = score_message(text + " " + " ".join(urls))
    b_score, b_reasons = score_buttons(urls, self_bot)
    return score + b_score, reasons + b_reasons


def profile_sig(first_name: str | None, last_name: str | None, username: str | None) -> str:
    """Подпись профиля для отслеживания изменений."""
    return f"{first_name or ''}|{last_name or ''}|{username or ''}"


async def check_user(bot, chat, user, settings, message=None, lvl=None) -> None:
    """Полный цикл наблюдения: скоринг профиля (+сообщения), бан или карточка.

    Профиль скорится один раз на версию профиля (изменился — пересчёт).
    Очки за сообщения складываются в копилку: спам выгодно дробить на порции
    ниже порога, и без накопления такая рассылка проходила бы насквозь.
    Копилка обнуляется, если человек сутки не давал поводов.
    """
    import time

    from .. import config, db, utils
    from . import moderation

    p_score, p_reasons = score_profile(user.first_name, user.last_name, user.username)
    hard, cosmetic, m_reasons = (0, 0, [])
    if message is not None:
        hard, cosmetic, m_reasons = message_parts(message.text or message.caption or "")
    # чисто? тогда и в базу лезть незачем — на каждое сообщение это лишний запрос
    if not p_score and not hard and not cosmetic:
        return

    sig = profile_sig(user.first_name, user.last_name, user.username)
    row = await db.watch_get(chat.id, user.id)
    profile_changed = row is None or row["sig"] != sig

    # пороги зависят от доверия: гостю строже, ветерану мягче
    from . import trust as trust_svc
    suspect, ban_at = trust_svc.watch_thresholds(settings, lvl if lvl is not None else 1)

    now = int(time.time())
    saved = 0
    if row is not None and now - (row["score_ts"] or 0) <= SCORE_WINDOW:
        saved = row["score"] or 0
    # копим только тревожное: манера письма к спаму отношения не имеет, и без
    # этого «эээ» да «нууу» за день набирали человеку на карточку
    pot = min(saved + hard, SCORE_MAX)

    total = p_score + pot + cosmetic
    reasons = p_reasons + m_reasons
    # Имя и ник — тоже текст. «Анна | 18+ ЛС» и «Кристина ❤️ пиши в лс» для
    # эвристик разные, для модели — одно и то же, поэтому сравниваем профиль
    # с теми, за кого в этом чате уже банили.
    if settings.watch_nn and total:
        from . import nn
        face = (f"{user.full_name} @{user.username}" if user.username
                else user.full_name)
        sim = await nn.face_score(chat.id, face)
        if sim is not None and sim >= config.PROFILE_SIM:
            total += config.PROFILE_POINTS
            reasons.append(f"имя как у забаненных профилей ({sim}%)")
    if saved:
        reasons.append(f"копилка за сутки: {saved}")
    # Не участник чата (комментатор под постом канала) — тот же текст от него
    # весит больше: почти вся реклама приходит именно оттуда. Спрашиваем статус
    # только когда очки уже набежали, иначе это запрос на каждое сообщение.
    if lvl is None and 0 < total < suspect:
        # при включённом доверии надбавка не нужна: гостю уже занижен порог,
        # и статус в чате там спрошен один раз на десять минут
        from . import adm_cache
        if not await adm_cache.is_member(bot, chat.id, user.id):
            total += GUEST_BOOST
            reasons.append("автор не состоит в чате")
    if lvl is not None:
        reasons.append(f"доверие: {trust_svc.label(lvl)}")
    # текст сообщения — только если человек что-то писал: на входе в чат его нет.
    # Ссылку даём: при подозрении сообщение остаётся в чате, его можно открыть.
    body = moderation.message_body(message, with_link=True)
    body_gone = moderation.message_body(message)   # для автобана: сообщение удалим
    if total < suspect:
        await db.watch_set(chat.id, user.id, sig,
                           bool(row["flagged"]) if row is not None and not profile_changed
                           else False,
                           score=pot)
        return

    who = utils.mention(user.id, user.full_name, user.username)
    why = ", ".join(reasons)

    # автобан по порогу
    if ban_at and total >= ban_at:
        if message is not None:
            try:
                await message.delete()
            except Exception:
                pass
        pid = await moderation.apply_punishment(
            bot, chat.id, user, "ban", 0, f"наблюдение: {why} ({total})", None
        )
        await db.watch_set(chat.id, user.id, sig, True, score=0, card_score=total)
        card = (
            f"⛔ <b>Бан (наблюдение)</b> · {utils.esc(chat.title)}\n"
            f"👤 {who} (<code>{user.id}</code>)\n"
            f"📎 Сигналы: {utils.esc(why)} — <b>{total} очков</b>\n"
            f"🤖 Кем: Gremlin (автомод)" + body_gone
        )
        await db.add_event(chat.id, "watch", f"ban: {user.full_name} ({user.id}) — {why} ({total})")
        if settings.watch_nn:
            from . import nn
            face = (f"{user.full_name} @{user.username}" if user.username
                    else user.full_name)
            await nn.remember_face(chat.id, user.id, face, "spam")
        await moderation.send_card(bot, chat.id, config.BIT_WATCH, card, pid, "ban", user.id)
        return

    # Подозрение: карточку не повторяем на каждое сообщение, но и не замолкаем
    # навсегда — если счёт заметно вырос, шлём ещё одну. Иначе рост 40 -> 75
    # оставался бы незамеченным до самого бана.
    seen = (row["card_score"] or 0) if row is not None else 0
    if (row is not None and row["flagged"] and not profile_changed
            and total < seen + RECARD_STEP):
        await db.watch_set(chat.id, user.id, sig, True, score=pot)
        return
    await db.watch_set(chat.id, user.id, sig, True, score=pot, card_score=total)
    card = (
        f"👁 <b>Подозрительный аккаунт</b> · {utils.esc(chat.title)}\n"
        f"👤 {who} (<code>{user.id}</code>)\n"
        f"📎 Сигналы: {utils.esc(why)} — <b>{total} очков</b>"
        + ("\n🔄 Профиль изменился" if row is not None and profile_changed else "")
        + body
    )
    await db.add_event(chat.id, "watch", f"suspect: {user.full_name} ({user.id}) — {why} ({total})")
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    b = InlineKeyboardBuilder()
    b.button(text="⛔ Забанить", callback_data=f"k:ban:{chat.id}:{user.id}")
    b.button(text="🕊 Не трогать", callback_data="k:wok")
    b.adjust(2)
    # через send_card: он сам сверится с настройками карточек, отправит копию
    # в глобальный лог и свяжет обе, чтобы кнопки гасли разом
    await moderation.send_card(bot, chat.id, config.BIT_WATCH, card,
                               markup=b.as_markup())


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

    # украшенные ники живых людей: косметика не должна дотягивать до карточки
    for name in ("~×°KåPå€ь в KeDåX°×~ o_O", "邋望Walen—神", "𝔻𝕒𝕣𝕜 𝕃𝕠𝕣𝕕 Д€КО"):
        s, r = score_profile(name, None, "user")
        assert s <= COSMETIC_CAP, (name, s, r)
    # а ссылка или реклама в имени — сама по себе повод для карточки
    for name in ("Аня t.me/spamchat", "смотри ссылку в профиле"):
        s, r = score_profile(name, None, "user")
        assert s >= 40, (name, s, r)
    # а спрятанный символ в обычном тексте — по-прежнему сигнал
    s, _ = score_message("текст‮со скрытым разворотом")
    assert s >= 30
    # кнопки: дип-ссылка инлайн-бота на самого себя — не спам
    s, r = score_content("https://www.deezer.com/track/2610505422",
                         ["https://t.me/deezload2bot?start=deezertrack2610505422"],
                         self_bot="deezload2bot")
    assert s == 20, (s, r)                       # только «кнопки со ссылками»
    s, r = score_content("", ["https://t.me/somechat"])
    assert s == 45, (s, r)                       # ссылка на чужой чат — сигнал
    s, r = score_content("", ["https://t.me/+AbCdEf"])
    assert s == 45, (s, r)                       # инвайт тоже
    s, r = score_content("", ["https://telegra.ph/AKTUAL-07-10"])
    assert s >= 50, (s, r)                       # telegra.ph на кнопке — бан
    s, r = score_content("", ["https://t.me/otherbot?start=x"], self_bot="deezload2bot")
    assert s == 25, (s, r)                       # чужой бот — слабый сигнал

    # кривопись: сама по себе до порога подозрения (40) не дотягивает
    for text in ("прив3т, как дела", "нуууу такое", "с-п-а-с-и-б-о всем",
                 "ставка 2-0 в пользу наших", "заказ №12345 приехал"):
        s, r = score_message(text)
        assert s < 40, (text, s, r)
    # а рядом со ссылкой та же кривопись — уже обход фильтра
    for text in ("каzино с бонусом, переходи t.me/luckyplay",
                 "к.а.з.и.н.о бонус https://bit.ly/x",
                 "п0рн0 архив тут https://t.me/+AbCd"):
        s, r = score_message(text)
        assert s >= 40, (text, s, r)
    # цифры и точки внутри адреса — не кривопись, ссылки из проверки выкидываем
    for text in ("смотри https://www.deezer.com/track/2610505422",
                 "видео на youtube.com/watch?v=dQw4w9WgXcQ глянь"):
        s, r = score_message(text)
        assert s == 0, (text, s, r)
    # техническая речь: цифра с краю слова или короткое слово — не обфускация
    for text in ("смотри 3D модель https://sketchfab.com/x",
                 "в 1С провёл, вот выгрузка https://disk.yandex.ru/y",
                 "купил Wi-Fi 6E роутер, обзор тут https://dns-shop.ru/z",
                 "COVID19 статистика https://who.int/a"):
        s, r = score_message(text)
        assert s == 0, (text, s, r)
    # манера письма живого человека: в копилку такое попадать не должно
    for text in ("Типа эээ душу или как жанр или есть какая-то штука нишевая?",
                 "нуууу такое, не зашло", "ааааа что это было"):
        h, c, r = message_parts(text)
        assert h == 0, (text, h, c, r)
    # живая речь с короткими словами — не «слово через разделители»
    for text in ("только про него я в твиттере шум видел после киберлика.",
                 "он у нас в с ним поехал", "и т. п. дальше по списку",
                 "с 1 по 5 я в отпуске"):
        s, r = score_message(text)
        assert s == 0, (text, s, r)
    # а вот растащенное слово — да
    for text in ("п о р н о бесплатно", "к.а.з.и.н.о бонус", "с-л-и-в базы"):
        s, r = score_message(text)
        assert "слово через разделители" in r, (text, s, r)
    # @упоминание человека — не ссылка, усилитель включать нельзя
    for text in ("прив3т @vasya_petrov, ты где", "нуууу @kolya сегодня не смогу"):
        s, r = score_message(text)
        assert s < 40 and "кривой текст вместе со ссылкой" not in r, (text, s, r)
    # сокращатели — самостоятельный сигнал
    s, r = score_message("бонус тут https://bit.ly/xyz")
    assert s == 20, (s, r)
    s, r = score_message("сliiiв базы, жми https://vk.cc/x")
    assert s >= 60, (s, r)                       # сокращатель + кривопись + ссылка
    print("watch self-check OK")
