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


def _drop_self(text: str, known=()) -> str:
    """Убрать упоминания самого Гремлина и ботов, которые сидят в чате."""
    for name in filter(None, (SELF_USERNAME, *known)):
        text = re.sub(rf"@{re.escape(name)}\b", " ", text, flags=re.IGNORECASE)
    return text


# (чат, бот), уже записанные в базу: ответы на сообщения одного бота идут
# потоком, и писать в базу на каждый незачем
_noted: set[tuple[int, int]] = set()


async def note_bot(chat_id: int, user) -> None:
    """Запомнить бота, который точно есть в чате."""
    from .. import db
    if (chat_id, user.id) in _noted:
        return
    await db.chat_bot_add(chat_id, user.id, user.username)
    _noted.add((chat_id, user.id))


async def forget_bot(chat_id: int, bot_id: int) -> None:
    from .. import db
    _noted.discard((chat_id, bot_id))
    await db.chat_bot_remove(chat_id, bot_id)


async def known_bots(bot, chat_id: int, text: str) -> set[str]:
    """Юзернеймы ботов чата — только если в тексте вообще упомянут бот.

    Инлайн-ботов сюда не берём ни по via_bot, ни по белому списку инлайна:
    спамеры как раз ими и ходят.
    """
    if not text or not _BOT_MENTION.search(text):
        return set()
    from .. import db
    from . import adm_cache
    await adm_cache.chat_admin_ids(bot, chat_id)
    return await db.chat_bot_names(chat_id) | adm_cache._admin_bots.get(chat_id, set())

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

# Причины, которые сами по себе что-то значат: их бот показывает даже тогда,
# когда косметика не считается.
_HARD_NAMES = {
    "невидимые символы в имени", "ссылка/упоминание в имени",
    "реклама профиля в имени", "telegra.ph-ссылка", "невидимые символы",
    "упоминание бота", "ссылка через сокращатель",
    "кривой текст вместе со ссылкой",
}

# Повторная карточка подозрения — только если счёт заметно вырос.
RECARD_STEP = 20


def profile_parts(first_name: str | None, last_name: str | None,
                  username: str | None) -> tuple[int, int, list[str]]:
    """Разобрать профиль на (тревожные очки, косметика, причины).

    Тревожное — то, что делают ради рекламы: ссылка в имени, невидимые
    символы, прямой призыв писать в личку. Косметика — необычный алфавит,
    эмодзи, цифры: у живых людей такое сплошь и рядом, поэтому сама по себе
    она ничего не значит и учитывается только рядом с тревожным.
    """
    name = " ".join(x for x in (first_name, last_name) if x)
    if not name:
        return 0, 0, []
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

    return score, min(cosmetic, COSMETIC_CAP), reasons


def score_profile(first_name: str | None, last_name: str | None,
                  username: str | None) -> tuple[int, list[str]]:
    """Суммарная оценка профиля — для мест, где разбирать по частям незачем."""
    hard, cosmetic, reasons = profile_parts(first_name, last_name, username)
    return hard + cosmetic, reasons


def message_parts(text: str, known=()) -> tuple[int, int, list[str]]:
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
    # «@GremlinModBot, привет» — это к нам, не спам; свои боты чата — тоже
    text = _drop_self(text, known)
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


async def profile_check(bot, chat_id: int, user, settings,
                        data: dict | None = None) -> tuple[dict, list[str]] | None:
    """Найти рекламу в описании профиля и прикреплённом канале.

    Возвращает (данные профиля, все находки) или None. Смотрим всё, а не до
    первой находки: иначе спам-профиль банился «за стоп-слово 🔞», и ни в
    карточке, ни в вердикте не было видно, что он ещё и похож на забаненных. Ищем теми же способами,
    что и в сообщениях: буквальные стоп-слова, смысловые фразы и сравнение с
    профилями, за которые в этом чате уже банили. Ничего нового не изобретается
    — меняется источник текста.

    Последней смотрим аватарку: она считается около секунды, а половина таких
    аккаунтов в описании вообще ничего не пишет, и фото — единственное, за что
    можно зацепиться.
    """
    from . import filters as flt
    from . import nn
    from . import profile as prof_svc

    if data is None:
        data = await prof_svc.fetch(bot, user.id)
    if not data:
        return None
    # Имя и ник — такая же часть рекламы, как описание: «Green VPN 🌿» с
    # безобидным «о себе» проходил мимо, хотя слово vpn в списке было.
    face = prof_svc.face_text(user, data)

    # Смысловые фразы к профилю не применяем сознательно: они пишутся под
    # сообщения, а в описании тот же смысл живёт наоборот — «не переношу
    # тему X» ловится наравне с тем, кто X продаёт.
    found: list[str] = []
    if len(face) >= 4 and settings.prof_words:
        word = await flt.match_stopword(chat_id, face, "prof")
        if word:
            found.append(f"стоп-слово в профиле: «{word}»")

    # По всей строке, а не по описанию: у «Алина Доход с телефона» описания
    # нет вовсе, реклама вся в имени — и сравнение с базой раньше не шло.
    if settings.watch_nn and len(face) >= 4:
        # сравниваем личность целиком — тем же видом строки, каким и запоминаем
        got = await nn.face_score(chat_id, face)
        if nn.face_hit(got):
            found.append(f"профиль как у забаненных ({nn.face_note(got)})")

    return (data, found) if found else None


async def photo_points(bot, chat_id: int, user, settings,
                       data: dict | None = None) -> tuple[int, list[str]]:
    """Очки за откровенную аватарку. Наказывать по ней одной нельзя.

    Распознавание путает «эффектно» с «откровенно»: обычный портрет в платье
    получил 92% и стоил живому человеку бана. При этом сигнал не пустой — у
    настоящего рекламного аккаунта было 100%, у обычных людей 0-3%. Поэтому
    фото не решает ничего в одиночку, а складывается с остальным о человеке.
    """
    if not settings.prof_on or not settings.prof_photo:
        return 0, []
    from . import nsfw
    from . import profile as prof_svc
    if data is None:
        data = await prof_svc.fetch(bot, user.id)
    photo_id = (data or {}).get("photo_id")
    got = nsfw.cached(photo_id)
    if got == "нет":
        # считаем только незнакомую картинку: и скачивание, и сама модель
        # стоят дорого, а аватарка у человека одна на все его сообщения
        raw = await prof_svc.photo_bytes(bot, data)
        if not raw:
            return 0, []
        got = await nsfw.score(raw)
        nsfw.remember(photo_id, got)
    if got is None or got < settings.prof_photo_min:
        return 0, []
    return int(settings.prof_photo_score), [f"откровенная аватарка ({got}%)"]


async def _profile_punish(bot, chat, user, settings, message, data, why) -> bool:
    """Режим «наказывать»: удалить сообщение и выдать наказание. True — выдали."""
    from .. import config, db, utils
    from . import moderation, nn

    kind = settings.prof_punish
    if message is not None:
        from . import deleting
        await deleting.one(message.delete, message.chat.id)
    body = moderation.message_body(message)
    pid = None
    if kind != "delete":
        pid = await moderation.apply_punishment(
            bot, chat.id, user, kind, settings.prof_mute_min,
            f"профиль: {why}", None)
    who = utils.mention(user.id, user.full_name, user.username)
    from . import profile as prof_svc
    head = {"delete": "🗑 <b>Удалено (профиль)</b>",
            "mute": "🔇 <b>Мут (профиль)</b>",
            "ban": "⛔ <b>Бан (профиль)</b>"}[kind]
    card = (
        f"{head} · {utils.esc(chat.title)}\n"
        f"👤 {who} (<code>{user.id}</code>)\n"
        f"📎 Причина: {utils.esc(why)}\n"
        f"{utils.esc(prof_svc.describe(data))}\n"
        f"🤖 Кем: Gremlin (автомод)" + body
    )
    await db.add_event(chat.id, "watch",
                       f"профиль: {user.full_name} ({user.id}) — {why}")
    if kind == "ban" and settings.watch_nn:
        # такой профиль пригодится: следующий похожий узнается сразу
        await nn.remember_face(chat.id, user.id,
                               prof_svc.face_text(user, data), "spam")
    await moderation.send_card(bot, chat.id, config.BIT_WATCH, card, pid,
                               kind if kind != "delete" else None, user.id)
    return True


_PERCENT = re.compile(r"\((\d{1,3})%\)")


def _percent(reason: str, default: int = 100) -> int:
    """Вытащить процент из причины вида «откровенная аватарка (97%)»."""
    m = _PERCENT.search(reason or "")
    return int(m.group(1)) if m else default


def _clean_find(reason: str) -> str:
    """«стоп-слово в профиле: «18+»» -> «18+»: в логе хватает самого слова."""
    head, sep, tail = (reason or "").partition(": ")
    return (tail if sep else head).strip("«»") or reason


async def _uni_shadow(bot, chat, user, settings, message, text, *,
                      p_hard, p_reasons, hard, cosmetic, m_reasons,
                      prof_pts, prof_reasons, cas_pts, nn_hit, lvl,
                      face_sim, total, suspect, ban_at, event,
                      pdata: dict | None = None, was: str | None = None) -> None:
    """Перевести улики наблюдения на общую шкалу и записать вердикт.

    Заново ничего не считаем и в Telegram не ходим: берём то, что наблюдение
    уже собрало. Иначе теневой прогон удвоил бы работу на каждом сообщении.
    """
    from . import adm_cache, moderation, verdict as vd

    buttons = moderation.button_urls(message) if message is not None else []
    outward = vd.has_outward(text, buttons)
    signals = vd.content_signals(
        # наблюдение знает только «сработал или нет», поэтому берём порог
        # чата: врать точным процентом в логе, по которому будем калиброваться,
        # нельзя
        nn_score=settings.nn_threshold if nn_hit else None,
        text_hard=hard, text_cosmetic=cosmetic, text_why=m_reasons,
        outward=outward)
    # Находка в профиле приходит уже очками и текстом причины — переводим
    # обратно в признаки. Проценты вытаскиваем настоящие: по этому логу потом
    # подбираются веса, и выдуманные числа в нём хуже, чем их отсутствие.
    word = photo = None
    face = face_sim
    for reason in prof_reasons if prof_pts else []:
        if "аватарка" in reason:
            photo = _percent(reason)
        elif "как у забаненных" in reason:
            # сравнение с копилкой спам-профилей (туда же подмешан стартовый
            # набор) — это догадка модели, и записывать её проверяемым фактом
            # нельзя: на одних догадках наказание не складывается
            face = max(face or 0, _percent(reason))
        elif word is None:
            word = _clean_find(reason)
    from . import filters as flt
    word_weight = (await flt.stopword_weight(chat.id, word, "prof")
                   if word else None)
    signals += vd.profile_signals(word=word, word_weight=word_weight, face=face,
                                  name_hard=p_hard, name_why=p_reasons,
                                  photo=photo)
    signals += vd.behavior_signals(reaction_only=event == "reaction",
                                   first_message=event == "join")

    # только то, что уже знаем: лишний getChatMember ради теневой
    # записи не оправдан
    known = adm_cache.member_cached(chat.id, user.id)
    guest = known is False
    ctx = await vd.context(chat.id, user, message, lvl=lvl, guest=guest,
                           text=text, buttons=buttons)
    # Реклама в профиле ведёт наружу сама: канал в профиле и есть выход.
    # Считаем его только при улике в профиле — канал есть у многих обычных
    # людей, и за сообщение без ссылок он отвечать не должен
    if (not ctx["outward"] and any(s.family == "profile" for s in signals)
            and vd.profile_outward(pdata)):
        ctx["outward"] = True
        ctx["outward_note"] = "выход наружу через профиль"
    # факты о человеке лежат в ctx: спрашивать их второй раз — три лишних
    # запроса к базе на каждое подозрительное сообщение
    signals += vd.reputation_signals(cas=bool(cas_pts),
                                     punished=ctx["facts"]["pun"])
    # Пишем состояние, а не отправку: карточку наблюдение при повторе
    # придерживает (RECARD_STEP), и метка «карточка» врала бы — сравнивали
    # бы вердикт с тем, чего не было.
    if was is None:
        was = "ничего"
        if ban_at and total >= ban_at:
            was = "наблюдение/ban"
        elif total >= suspect:
            was = "наблюдение/подозрение"
    await vd.shadow(chat, user, settings, signals=signals, ctx=ctx, text=text,
                    was=was)


async def check_user(bot, chat, user, settings, message=None, lvl=None,
                     event: str = "message", nn_hit: bool = False) -> None:
    """Полный цикл наблюдения: скоринг профиля (+сообщения), бан или карточка.

    Профиль скорится один раз на версию профиля (изменился — пересчёт).
    Очки за сообщения складываются в копилку: спам выгодно дробить на порции
    ниже порога, и без накопления такая рассылка проходила бы насквозь.
    Копилка обнуляется, если человек сутки не давал поводов.

    event — откуда пришли: 'message', 'join' или 'reaction'. Нужен CAS: на входе
    в чат текста ещё нет, и проверка списка спамеров там единственное, что
    вообще может сработать. nn_hit — нейрофильтр счёл сообщение рекламой;
    для CAS это тоже повод спросить.
    """
    import time

    from .. import config, db, utils
    from . import cas as cas_svc
    from . import moderation

    p_hard, p_cos, p_reasons = profile_parts(user.first_name, user.last_name,
                                             user.username)
    hard, cosmetic, m_reasons = (0, 0, [])
    text = ""
    if message is not None:
        text = message.text or message.caption or ""
        hard, cosmetic, m_reasons = message_parts(
            text, await known_bots(bot, chat.id, text))

    # CAS на входе в чат: профиль у спамера обычно самый обычный, и всё
    # остальное здесь молчит — спрашиваем список до того, как он что-то написал
    cas_pts, cas_reasons = 0, []
    if settings.cas_on and settings.cas_join and event == "join":
        cas_pts, cas_reasons = await cas_svc.points(user.id, settings)

    # Описание профиля и прикреплённый канал. Спрашиваем не про всех: про
    # новичка на входе, про того, кто в чате не состоит (комментаторы под
    # постами — как раз они), и про того, кто уже чем-то насторожил.
    prof_pts, prof_reasons = 0, []
    pdata = None
    if settings.prof_on:
        from . import adm_cache
        # реакция — тоже повод: рекламные аккаунты часто ничего не пишут,
        # а ставят реакцию, чтобы засветиться. Их и так проверяем не чаще
        # раза в сутки на человека, лишних запросов не будет
        worth = (event in ("join", "reaction")
                 or p_hard or p_cos or hard or cosmetic or cas_pts or nn_hit
                 or not await adm_cache.is_member(bot, chat.id, user.id))
        # Своих можно и не трогать. Спам приходит от тех, кто в чат не
        # вступал — комментаторы под постами канала, — а у старожила
        # «18+» в описании его же канала это просто его канал.
        # Новичка на входе проверяем в любом случае: он для того и новичок.
        if (worth and not settings.prof_members and event != "join"
                and await adm_cache.is_member(bot, chat.id, user.id)):
            worth = False
        if worth:
            # профиль спрашиваем один раз и отдаём обеим проверкам
            from . import profile as prof_svc
            pdata = await prof_svc.fetch(bot, user.id)
            found = await profile_check(bot, chat.id, user, settings, pdata)
            # аватарка идёт очками всегда, даже в режиме «наказывать»:
            # одна она ничего не доказывает, но в причине бана её видно
            pts, why_photo = await photo_points(bot, chat.id, user, settings, pdata)
            if found is not None:
                data, finds = found
                if settings.prof_mode == "punish":
                    # Вердикт пишем и тут: раньше наказание за профиль выходило
                    # до теневой записи, и самые явные спам-аккаунты в журнал
                    # не попадали вовсе — калибровать было не на чем
                    if settings.uni_mode:
                        try:
                            await _uni_shadow(
                                bot, chat, user, settings, message, text,
                                p_hard=p_hard, p_reasons=p_reasons,
                                hard=hard, cosmetic=cosmetic, m_reasons=m_reasons,
                                prof_pts=int(settings.prof_score) + pts,
                                prof_reasons=finds + why_photo,
                                cas_pts=cas_pts, nn_hit=nn_hit, lvl=lvl,
                                face_sim=None, total=0, suspect=0, ban_at=0,
                                event=event, pdata=data,
                                was=f"профиль/{settings.prof_punish}")
                        except Exception:
                            logger.debug("единая оценка не посчиталась", exc_info=True)
                    await _profile_punish(bot, chat, user, settings, message,
                                          data, ", ".join(finds + why_photo))
                    return
                prof_pts = int(settings.prof_score)
                prof_reasons = list(finds)
            prof_pts += pts
            prof_reasons += why_photo

    # чисто? тогда и в базу лезть незачем — на каждое сообщение это лишний запрос
    if not (p_hard or p_cos or hard or cosmetic or cas_pts or nn_hit or prof_pts):
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

    # Косметика — растянутые буквы, эмодзи, необычный алфавит — у живых людей
    # встречается постоянно, и как самостоятельная улика она давала карточки
    # на обычных участников. Теперь она только усиливает: считается, если рядом
    # есть настоящий сигнал — тревожный признак, ссылка или похожий профиль.
    amplify = bool(p_hard or hard or (text and _REAL_LINK.search(text)))
    total = p_hard + pot + (p_cos + cosmetic if amplify else 0)
    # без усиления в причинах остаётся только тревожное: иначе в карточке
    # висело бы «растянутые буквы» как повод, которым оно не является
    reasons = (p_reasons + m_reasons if amplify
               else [r for r in p_reasons + m_reasons if r in _HARD_NAMES])
    # Имя и ник — тоже текст. «Анна | 18+ ЛС» и «Кристина ❤️ пиши в лс» для
    # эвристик разные, для модели — одно и то же, поэтому сравниваем профиль
    # с теми, за кого в этом чате уже банили.
    face_sim = None
    if settings.watch_nn and total:
        from . import nn
        face = (f"{user.full_name} @{user.username}" if user.username
                else user.full_name)
        got = await nn.face_score(chat.id, face)
        if nn.face_hit(got):
            face_sim = got[0]
            total += config.PROFILE_POINTS
            reasons.append(f"имя как у забаненных профилей ({nn.face_note(got)})")
    # CAS при подозрении: спрашиваем, только когда что-то уже набежало или
    # нейрофильтр показал на сообщение. Спрашивать про каждого — и лишний
    # запрос наружу, и чужому сервису знать всех подряд незачем.
    if (settings.cas_on and settings.cas_suspect and not cas_pts
            and (total > 0 or nn_hit)):
        cas_pts, cas_reasons = await cas_svc.points(user.id, settings)
    if prof_pts:
        total += prof_pts
        reasons += prof_reasons
    if cas_pts:
        total += cas_pts
        reasons += cas_reasons
    if nn_hit:
        reasons.append("нейрофильтр: похоже на рекламу")
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
    # Единая оценка по тем же уликам, что собрало наблюдение. Считаем до
    # решения и ничего по ней не делаем: сейчас она только записывается.
    if settings.uni_mode:
        try:
            await _uni_shadow(bot, chat, user, settings, message, text,
                              p_hard=p_hard, p_reasons=p_reasons,
                              hard=hard, cosmetic=cosmetic, m_reasons=m_reasons,
                              prof_pts=prof_pts, prof_reasons=prof_reasons,
                              cas_pts=cas_pts, nn_hit=nn_hit, lvl=lvl,
                              face_sim=face_sim,
                              total=total, suspect=suspect, ban_at=ban_at,
                              event=event, pdata=pdata)
        except Exception:
            logger.debug("единая оценка не посчиталась", exc_info=True)

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
            from . import deleting
            await deleting.one(message.delete, message.chat.id)
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
