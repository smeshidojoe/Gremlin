"""Декларативное описание настроек чата — единый источник для меню бота и mini app.

Ни aiogram, ни БД тут не импортируются: чистые данные + помощники. И меню, и будущий
веб-API читают отсюда описание полей, их допустимые значения и подписи. Добавил поле —
правишь только здесь (+ колонку в db.Settings/схеме).
"""
from dataclasses import dataclass, field

from . import config, utils


@dataclass
class Field:
    key: str                       # имя колонки в settings
    kind: str                      # 'toggle' | 'cycle'
    label: str                     # короткая подпись
    values: list | None = None     # для cycle: допустимые значения по порядку
    value_labels: dict | None = None  # value -> подпись
    fmt: str | None = None         # 'minutes' | 'sec' | 'plain'
    show_if: tuple | None = None    # (other_key, value) — показывать только при условии


@dataclass
class Section:
    key: str
    title: str                     # эмодзи + название
    intro: str                     # пояснение
    fields: list = field(default_factory=list)
    widgets: list = field(default_factory=list)  # кастомные списки: words/wl/anon/logsel/cardbits


_PUNISH = list(config.PUNISH_VALUES)
_MUTE = list(config.MUTE_PRESETS)


def _punish(key: str) -> Field:
    return Field(key, "cycle", "Наказание", _PUNISH, config.PUNISH_LABELS)


def _mute(key: str, dep: str) -> Field:
    return Field(key, "cycle", "Мут", _MUTE, fmt="minutes", show_if=(dep, "mute"))


SECTIONS: list[Section] = [
    Section(
        "inline", "🤖 Инлайн-боты",
        "<i>Зачем:</i> спамеры вызывают чужих ботов (@pic и подобные), чтобы протащить рекламу.\n"
        "<i>Как:</i> Telegram не даёт запретить сам вызов на клиенте, поэтому сообщение "
        "удаляется мгновенно, а автор получает наказание. Ниже — список ботов, которым "
        "в этом чате можно: их вызовы бот не трогает.\n"
        "<i>Бан при спам-сигналах</i> — планка для содержимого. Обычный вызов (гифка, "
        "картинка, трек) получает наказание сверху, а если в сообщении набралось "
        "столько очков спама, автор сразу улетает в бан. Очки: кнопки со ссылками — 20, "
        "кнопка на telegra.ph или чат — 30, telegra.ph в тексте — 45, обфускация — 25.",
        fields=[
            Field("inline_on", "toggle", "Статус"),
            _punish("inline_punish"),
            _mute("inline_mute_min", "inline_punish"),
            Field("inline_spam", "cycle", "Бан при спам-сигналах",
                  list(config.INLINE_SPAM_PRESETS), config.INLINE_SPAM_LABELS),
        ],
        widgets=["inline_wl"],
    ),
    Section(
        "links", "🔗 Ссылки",
        "<i>Зачем:</i> защита от увода аудитории в чужие чаты и от рекламных ссылок.\n"
        "<i>Как:</i> «Телеграм-ссылки» ловят t.me / telegram.me / tg:// на сторонние чаты, "
        "каналы и истории. «Внешние ссылки» — любые остальные (сайты, YouTube и т.п.). "
        "Никогда не трогаются: ссылки на сообщения этого чата, на привязанный канал, "
        "на разрешённых отправителей из раздела «Анонимы» и упоминания людей.\n"
        "«Проверка @упоминаний» — бот смотрит, что стоит за каждым @ником: если это "
        "канал или группа, а не человек, считает это рекламой. Стоит денег в скорости "
        "(запрос на каждый ник), поэтому выключено по умолчанию.\n"
        "«Блок пересылок» убирает любые пересланные сообщения — из каналов, чужих "
        "чатов и от людей. Свои же сообщения из этого чата и пересылки от тех, кто "
        "в вайтлисте, не трогаются.\n"
        "Наказание общее для обоих типов ссылок.",
        fields=[
            Field("links_on", "toggle", "Телеграм-ссылки"),
            Field("extlinks_on", "toggle", "Внешние ссылки (любые сайты)"),
            Field("mentions_check", "toggle", "Проверка @упоминаний каналов"),
            Field("forwards_on", "toggle", "Блок пересылок"),
            _punish("links_punish"),
            _mute("links_mute_min", "links_punish"),
        ],
        widgets=["link_wl"],
    ),
    Section(
        "anon", "📛 Сообщения от имени групп/каналов",
        "<i>Зачем:</i> спамеры пишут «от имени канала», чтобы их нельзя было забанить "
        "как обычного юзера.\n"
        "<i>Как:</i> сообщение удаляется, канал-отправитель банится в этом чате. Анонимные "
        "админы чата и привязанный канал не трогаются. Чтобы разрешить конкретный канал — "
        "добавьте его в «🕊 Вайтлист» (уровень «сообщения от имени канала» или «полный игнор»).",
        fields=[Field("anon_on", "toggle", "Статус")],
        widgets=["anon"],
    ),
    Section(
        "words", "🧨 Стоп-слова",
        "<i>Зачем:</i> свой список запрещённого — мат, реклама, скам-фразы.\n"
        "<i>Как:</i> слово со звёздочкой (слово*) ловит и разные окончания, "
        "без звёздочки — только точное совпадение.\n"
        "<i>Только для гостей:</i> проверяются лишь те, кто пишет, не состоя в чате — "
        "обычно это комментаторы под постами привязанного канала. Участники чата "
        "фильтр не задевает.",
        fields=[
            Field("words_on", "toggle", "Статус"),
            _punish("words_punish"),
            _mute("words_mute_min", "words_punish"),
            Field("words_guests", "toggle", "Только для гостей"),
        ],
        widgets=["words"],
    ),
    Section(
        "flood", "🌊 Антифлуд",
        "<i>Зачем:</i> против тех, кто заваливает чат сообщениями подряд.\n"
        "<i>Как:</i> больше N сообщений за M секунд — мут на заданное время.",
        fields=[
            Field("flood_on", "toggle", "Статус"),
            Field("flood_msgs", "cycle", "Сообщений", list(config.FLOOD_MSGS_PRESETS), fmt="plain"),
            Field("flood_window", "cycle", "Окно, сек", list(config.FLOOD_WINDOW_PRESETS), fmt="sec"),
            Field("flood_mute_min", "cycle", "Мут", _MUTE, fmt="minutes"),
        ],
    ),
    Section(
        "captcha", "🤖 Капча новичкам",
        "<i>Зачем:</i> отсекает спам-ботов, которые заходят и сразу пишут.\n"
        "<i>Как:</i> новичок получает мут и кнопку «Я не бот». Не нажал за отведённое "
        "время — кик (сможет вернуться). Юзеров из вайтлиста капча пропускает.",
        fields=[
            Field("captcha_on", "toggle", "Статус"),
            Field("captcha_timeout", "cycle", "Время, сек", list(config.CAPTCHA_TIMEOUT_PRESETS), fmt="sec"),
        ],
    ),
    Section(
        "watch", "👁 Наблюдение за профилями",
        "<i>Зачем:</i> ловит спам-аккаунты по виду профиля ещё до того, как они начнут писать.\n"
        "<i>Как:</i> за подозрительные признаки начисляются очки — невидимые символы, "
        "подмена букв, telegra.ph-ссылки, рекламные имена. Выше порога подозрения — карточка "
        "в лог-чат с кнопками Заблокировать/Не трогать; выше порога бана — автобан. "
        "Смена профиля перепроверяется. Ботов, добавленных не-админом, банит сразу.",
        fields=[
            Field("watch_on", "toggle", "Статус"),
            Field("watch_bots", "toggle", "Бан чужих ботов"),
            Field("watch_suspect", "cycle", "Порог подозрения",
                  list(config.WATCH_SUSPECT_PRESETS), fmt="plain"),
            Field("watch_ban", "cycle", "Порог автобана",
                  list(config.WATCH_BAN_PRESETS),
                  value_labels={0: "выключен", 60: "60", 80: "80", 100: "100"}),
        ],
    ),
    Section(
        "welcome", "👋 Приветствие",
        "<i>Зачем:</i> встретить новичка, чтобы чат не выглядел безлюдным.\n"
        "<i>Как:</i> приветствие шлётся при входе, <code>{name}</code> заменяется на имя "
        "новичка. Прошлое приветствие удаляется, чтобы они не копились в ленте.",
        fields=[Field("welcome_on", "toggle", "Приветствие")],
        widgets=["welcome_text"],
    ),
    Section(
        "media", "🖼 Медиа-фильтры",
        "<i>Зачем:</i> убрать из чата типы контента, которые вам не нужны.\n"
        "<i>Как:</i> отмеченные 🗑 типы удаляются сразу, без наказания автора. "
        "Админов чата не касается.",
        fields=[Field("media_on", "toggle", "Статус")],
        widgets=["mediabits"],
    ),
    Section(
        "triggers", "🎯 Триггеры",
        "<i>Зачем:</i> авто-ответы на частые вопросы, без участия админов.\n"
        "<i>Как:</i> бот отвечает на ключевую фразу (ищется внутри сообщения, регистр неважен) "
        "текстом или медиа. Кулдаун 30 сек на триггер, чтобы не спамил.",
        fields=[Field("trig_on", "toggle", "Статус")],
        widgets=["trigs"],
    ),
    Section(
        "cmds", "🔢 Счётчики",
        "<i>Зачем:</i> развлекательные команды со счётом — свои приколы в каждом чате.\n"
        "<i>Как:</i> задаёте команду (например <code>!черви</code>) и заготовку ответа "
        "(например <code>кузнечики</code>). Любой участник пишет команду — бот отвечает "
        "реплаем <code>кузнечики [1]</code>, где число это счётчик вызовов в этом чате: "
        "он растёт с каждым ответом и переживает перезапуск. Скобки со счётчиком бот "
        "дописывает сам — в заготовке их писать не нужно. У каждого счётчика свой кулдаун: "
        "пока он идёт, бот на команду не отвечает и вызов не засчитывает.\n"
        "<i>Кулдаун для гостей</i> — для тех, кто в чате не состоит (обычно это "
        "комментаторы под постами канала). Он общий на всех гостей: один вызвал "
        "команду — остальным она недоступна до конца паузы, их сообщения удаляются. "
        "«Как у всех» — считать их наравне с участниками.",
        fields=[
            Field("cmds_on", "toggle", "Статус"),
            Field("cmds_guest_cd", "cycle", "Кулдаун для гостей",
                  list(config.CMD_GUEST_CD_PRESETS), config.CMD_GUEST_CD_LABELS),
        ],
        widgets=["cmds"],
    ),
    Section(
        "digest", "📊 Недельная сводка",
        "<i>Зачем:</i> раз в неделю видеть, кто в чате живой, а кто молчит.\n"
        "<i>Как:</i> каждое воскресенье выбранному человеку уходит выжимка за текущую "
        "календарную неделю (с понедельника): сколько сообщений, топ-3 активных "
        "и список тех, кто не написал ничего. "
        "Кнопкой в сообщении можно догрузить полный HTML-отчёт. Получатель не указан — "
        "сводка не отправляется.\n"
        "<i>Только для профильного чата:</i> подробную базу (участники, история, "
        "HTML-отчёт) юзербот ведёт для одного чата. В остальных чатах есть базовая "
        "статистика бота — раздел «📈 Статистика».",
        widgets=["digest_to"],
    ),
    Section(
        "service", "🧹 Системные сообщения",
        "<i>Зачем:</i> служебные строки «X вошёл в группу» захламляют чат, особенно "
        "в живых сообществах.\n"
        "<i>Как:</i> бот удаляет отмеченные типы служебных сообщений сразу после появления. "
        "На самих участников это никак не влияет — чистится только текст в ленте.",
        fields=[
            Field("service_join", "toggle", "Уведомления о входе"),
            Field("service_leave", "toggle", "Уведомления о выходе"),
            Field("service_other", "toggle", "Закреп, смена названия и фото"),
        ],
    ),
    Section(
        "wl", "🕊 Вайтлист",
        "<i>Зачем:</i> доверенные люди и свои каналы не должны попадать под фильтры.\n"
        "<i>Как:</i> один список на всех — и на юзеров, и на каналы. <b>Кого добавили, "
        "того бот не проверяет по отмеченным пунктам.</b> Новый попадает в список с "
        "полным игнором; откройте его карточку и снимите галочки с того, что проверять "
        "всё-таки нужно. Добавить можно по id, @username или пересылкой сообщения.\n"
        "Если канал уже был забанен за сообщения от своего имени, бот снимет бан, как "
        "только у него отмечено «сообщения от имени канала» (в т.ч. через полный игнор).",
        widgets=["wl"],
    ),
    Section(
        "cards", "🪪 Карточки и лог",
        "<i>Зачем:</i> видеть всё, что бот делает в чате, и отменять решения одной кнопкой.\n"
        "<i>Как:</i> события летят карточками в отдельный лог-чат — что случилось, с кем, "
        "причина, кнопки «Разблокировать / Так ему и надо». Ниже выбирается лог-чат "
        "и типы событий.",
        fields=[Field("cards_on", "toggle", "Статус")],
        widgets=["logsel", "cardbits"],
    ),
]

SECTION_BY_KEY = {s.key: s for s in SECTIONS}

# производные множества для валидации/обработчиков (единый источник — SECTIONS)
TOGGLE_FIELDS = {f.key for s in SECTIONS for f in s.fields if f.kind == "toggle"}
CYCLE_FIELDS = {f.key: f.values for s in SECTIONS for f in s.fields if f.kind == "cycle"}

# поле -> ключ раздела (для перерисовки после изменения)
FIELD_SECTION = {f.key: s.key for s in SECTIONS for f in s.fields}

# поля, попадающие в сводку на карточке чата (обзор)
OVERVIEW = [
    ("inline_on", "Инлайн"), ("links_on", "Ссылки"), ("anon_on", "Анонимы"),
    ("words_on", "Слова"), ("flood_on", "Антифлуд"), ("captcha_on", "Капча"),
    ("watch_on", "Наблюдение"), ("welcome_on", "Привет"), ("media_on", "Медиа"),
    ("trig_on", "Триггеры"), ("cmds_on", "Счётчики"), ("cards_on", "Карточки"),
]


def value_label(f: Field, val) -> str:
    """Человеческая подпись значения поля."""
    if f.kind == "toggle":
        return "✅ Включено" if val else "🚫 Выключено"
    if f.value_labels:
        return f.value_labels.get(val, str(val))
    if f.fmt == "minutes":
        return utils.fmt_minutes(val)
    if f.fmt == "sec":
        return f"{val} сек"
    return str(val)


def visible(f: Field, settings_obj) -> bool:
    if f.show_if is None:
        return True
    dep_key, dep_val = f.show_if
    return getattr(settings_obj, dep_key) == dep_val


def validate(field_key: str, value):
    """Проверка значения для API. Вернуть приведённое значение или бросить ValueError."""
    if field_key in TOGGLE_FIELDS:
        return 1 if value in (1, True, "1", "true", "on") else 0
    if field_key in CYCLE_FIELDS:
        try:
            iv = int(value)
        except (TypeError, ValueError):
            iv = value
        if iv not in CYCLE_FIELDS[field_key]:
            raise ValueError(f"{field_key}: {value} не из {CYCLE_FIELDS[field_key]}")
        return iv
    raise ValueError(f"неизвестное поле {field_key}")


def to_dict(settings_obj) -> dict:
    """JSON-описание всех разделов с текущими значениями — для mini app."""
    out = []
    for s in SECTIONS:
        fields = []
        for f in s.fields:
            fields.append({
                "key": f.key, "kind": f.kind, "label": f.label,
                "value": getattr(settings_obj, f.key),
                "values": f.values, "value_labels": f.value_labels,
                "fmt": f.fmt, "visible": visible(f, settings_obj),
            })
        out.append({"key": s.key, "title": s.title, "intro": s.intro,
                    "fields": fields, "widgets": s.widgets})
    return {"sections": out}
