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
    back: str | None = None        # ключ родительского раздела: для подстраниц


_PUNISH = list(config.PUNISH_VALUES)
_MUTE = list(config.MUTE_PRESETS)


def _punish(key: str) -> Field:
    return Field(key, "cycle", "Наказание", _PUNISH, config.PUNISH_LABELS)


def _mute(key: str, dep: str) -> Field:
    return Field(key, "cycle", "Мут", _MUTE, fmt="minutes", show_if=(dep, "mute"))


SECTIONS: list[Section] = [
    Section(
        "inline", "🤖 Инлайн-боты",
        "Спамеры пользуются чужими ботами (@pic и подобными), чтобы "
        "протащить рекламу.\n"
        "Бот удаляет такое сообщение сразу и наказывает того, кто вызвал.\n"
        "Ниже — список ботов, которым в этом чате можно: их не трогаем.\n"
        "«Бан при спам-сигналах» — если в сообщении слишком много рекламных "
        "признаков (кнопки со ссылками, ссылки на telegra.ph, подменённые "
        "буквы), автор улетает в бан сразу, без мута.",
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
        "Защита от увода людей в чужие чаты и от рекламных ссылок.\n"
        "«Телеграм-ссылки» — ссылки на сторонние чаты, каналы и истории.\n"
        "«Внешние ссылки» — любые сайты.\n"
        "Не трогаются: ссылки на этот чат, на привязанный канал, на "
        "разрешённых в вайтлисте и упоминания людей.\n"
        "«Проверка упоминаний» — бот смотрит, что стоит за каждым @ником, и "
        "считает рекламой упоминание чужого канала. Работает медленнее, "
        "поэтому выключено по умолчанию.\n"
        "«Блок пересылок» убирает пересланное из других чатов и каналов.\n"
        "Наказание задаётся отдельно для участников и для тех, кто в чате "
        "не состоит, — кнопки ниже.",
        fields=[
            Field("links_on", "toggle", "Телеграм-ссылки"),
            Field("extlinks_on", "toggle", "Внешние ссылки (любые сайты)"),
            Field("mentions_check", "toggle", "Проверка @упоминаний каналов"),
            Field("forwards_on", "toggle", "Блок пересылок"),
        ],
        widgets=["links_pun", "link_wl"],
    ),
    Section(
        "links_member", "⚖️ Наказания для участников",
        "Что делать с теми, кто в чате состоит.\n"
        "У каждого типа ссылок своё наказание: например, ссылку на чужой "
        "канал удалять, а за пересылку мутить.",
        fields=[
            Field("lp_tg", "cycle", "ТГ-ссылки", _PUNISH, config.PUNISH_LABELS),
            _mute("lm_tg", "lp_tg"),
            Field("lp_ext", "cycle", "Внешние", _PUNISH, config.PUNISH_LABELS),
            _mute("lm_ext", "lp_ext"),
            Field("lp_men", "cycle", "Упоминания", _PUNISH, config.PUNISH_LABELS),
            _mute("lm_men", "lp_men"),
            Field("lp_fwd", "cycle", "Пересылки", _PUNISH, config.PUNISH_LABELS),
            _mute("lm_fwd", "lp_fwd"),
        ],
        back="links",
    ),
    Section(
        "links_guest", "⚖️ Наказания для не участников",
        "Для тех, кто в чате не состоит, — обычно это комментаторы под "
        "постами канала.\n"
        "Оттуда идёт почти вся реклама, поэтому наказания тут обычно "
        "строже.",
        fields=[
            Field("gp_tg", "cycle", "ТГ-ссылки", _PUNISH, config.PUNISH_LABELS),
            _mute("gm_tg", "gp_tg"),
            Field("gp_ext", "cycle", "Внешние", _PUNISH, config.PUNISH_LABELS),
            _mute("gm_ext", "gp_ext"),
            Field("gp_men", "cycle", "Упоминания", _PUNISH, config.PUNISH_LABELS),
            _mute("gm_men", "gp_men"),
            Field("gp_fwd", "cycle", "Пересылки", _PUNISH, config.PUNISH_LABELS),
            _mute("gm_fwd", "gp_fwd"),
        ],
        back="links",
    ),
    Section(
        "anon", "📛 Сообщения от имени групп/каналов",
        "Спамеры пишут «от имени канала», чтобы их нельзя было забанить как "
        "обычного человека.\n"
        "Бот удаляет такое сообщение и запрещает этому каналу писать в чат.\n"
        "Анонимных админов чата и привязанный канал не трогает.\n"
        "Разрешить конкретный канал — добавьте его в вайтлист.",
        fields=[Field("anon_on", "toggle", "Статус")],
        widgets=["anon"],
    ),
    Section(
        "words", "🧨 Стоп-слова",
        "Свой список запрещённого: мат, реклама, обманные фразы.\n"
        "<code>слово</code> — точное совпадение, <code>слово*</code> — с "
        "любыми окончаниями.\n"
        "«Только для гостей» — проверяются лишь те, кто в чате не состоит; "
        "участников фильтр не задевает.",
        fields=[
            Field("words_on", "toggle", "Статус"),
            _punish("words_punish"),
            _mute("words_mute_min", "words_punish"),
            Field("words_guests", "toggle", "Только для гостей"),
        ],
        widgets=["words"],
    ),
    Section(
        "sem", "🧠 Смысловые стоп-слова",
        "Обычный список ловит буквы, а рекламу переписывают быстрее, чем "
        "список пополняют: «казино» в списке — и мимо проходит «игровая "
        "площадка с приветственным бонусом».\n"
        "Здесь вы добавляете не слово, а фразу-образец: бот ловит похожее "
        "по смыслу, даже если ни одно слово не совпало.\n"
        "«Похожесть» — насколько близко должно быть, чтобы сработало. 75% "
        "ловит переформулировки и не задевает обычный разговор, 90% — "
        "только почти дословные повторы.\n"
        "Работает, когда включён нейрофильтр.",
        fields=[
            Field("sem_on", "toggle", "Статус"),
            Field("sem_threshold", "cycle", "Похожесть, %",
                  list(config.SEM_PRESETS), fmt="plain"),
            _punish("sem_punish"),
            _mute("sem_mute_min", "sem_punish"),
            Field("sem_guests", "toggle", "Только для гостей"),
        ],
        widgets=["phrases"],
    ),
    Section(
        "burst", "📡 Рассылки",
        "Набег: несколько аккаунтов пишут одно и то же разными словами.\n"
        "По отдельности каждое сообщение выглядит безобидно, поэтому "
        "обычные правила такое не ловят.\n"
        "Бот сравнивает новые сообщения между собой и срабатывает, когда "
        "одно и то же сказали несколько разных людей за пять минут.\n"
        "Работает, когда включён нейрофильтр.",
        fields=[
            Field("burst_on", "toggle", "Статус"),
            Field("burst_users", "cycle", "Сколько человек",
                  list(config.BURST_USERS_PRESETS), fmt="plain"),
            _punish("burst_punish"),
            _mute("burst_mute_min", "burst_punish"),
        ],
    ),
    Section(
        "flood", "🌊 Антифлуд",
        "Против тех, кто заваливает чат сообщениями подряд.\n"
        "Больше N сообщений за M секунд — мут, а сами сообщения залпа "
        "удаляются.",
        fields=[
            Field("flood_on", "toggle", "Статус"),
            Field("flood_msgs", "cycle", "Сообщений", list(config.FLOOD_MSGS_PRESETS), fmt="plain"),
            Field("flood_window", "cycle", "Окно, сек", list(config.FLOOD_WINDOW_PRESETS), fmt="sec"),
            Field("flood_mute_min", "cycle", "Мут", _MUTE, fmt="minutes"),
        ],
    ),
    Section(
        "captcha", "🤖 Капча новичкам",
        "Отсекает ботов, которые заходят в чат и сразу пишут.\n"
        "Новичок получает мут и кнопку «Я не бот». Нажал — пишет как "
        "обычно.\n"
        "Не нажал за отведённое время — бот его выгоняет; вернуться по "
        "ссылке человек сможет.",
        fields=[
            Field("captcha_on", "toggle", "Статус"),
            Field("captcha_timeout", "cycle", "Время, сек", list(config.CAPTCHA_TIMEOUT_PRESETS), fmt="sec"),
        ],
    ),
    Section(
        "watch", "👁 Наблюдение за профилями",
        "Бот присматривается к профилям и сообщениям и считает "
        "подозрительные признаки: невидимые символы, подменённые буквы, "
        "рекламные имена, ссылки на telegra.ph, упоминания ботов.\n"
        "Набралось на «подозрение» — карточка в лог-чат с кнопками "
        "«Заблокировать» и «Не трогать». Набралось на «бан» — бот банит "
        "сам.\n"
        "«Сравнивать профили с забаненными» — имя и ник нового человека "
        "сверяются с теми, кого в этом чате уже забанили: рекламные профили "
        "похожи между собой, даже когда написаны разными словами.\n"
        "«Проверять тех, кто ставит реакции» — рекламные аккаунты часто "
        "не пишут вовсе, а только ставят реакции, чтобы засветиться в чате. "
        "Бот проверит их профиль так же, как профиль написавшего; каждого "
        "человека — не чаще раза в сутки.\n"
        "Ботов, которых добавил не админ, бот банит сразу.\n"
        "Держите порог бана не ниже порога подозрения, иначе карточка "
        "«подозрение» никогда не появится.",
        fields=[
            Field("watch_on", "toggle", "Статус"),
            Field("watch_nn", "toggle", "Сравнивать профили с забаненными"),
            Field("watch_react", "toggle", "Проверять тех, кто ставит реакции"),
            Field("watch_bots", "toggle", "Бан ботов, добавленных в чат"),
            Field("watch_suspect", "cycle", "Порог подозрения",
                  list(config.WATCH_SUSPECT_PRESETS), fmt="plain"),
            Field("watch_ban", "cycle", "Порог автобана",
                  list(config.WATCH_BAN_PRESETS),
                  value_labels={0: "выключен", 60: "60", 80: "80", 100: "100"}),
        ],
    ),
    Section(
        "welcome", "👋 Приветствие",
        "Приветствие новичкам: текст с обращением по имени.\n"
        "Прошлое приветствие бот удаляет, чтобы чат не зарастал ими.",
        fields=[Field("welcome_on", "toggle", "Приветствие")],
        widgets=["welcome_text"],
    ),
    Section(
        "media", "🖼 Медиа-фильтры",
        "Удаление выбранных типов сообщений: стикеры, гифки, голосовые, "
        "кружки, фото, видео, файлы, музыка.\n"
        "Без наказания — сообщение просто исчезает.",
        fields=[Field("media_on", "toggle", "Статус")],
        widgets=["mediabits"],
    ),
    Section(
        "triggers", "🎯 Триггеры",
        "Ключевая фраза в сообщении — бот отвечает заготовкой: текстом или "
        "медиа.\n"
        "Работает для всех, включая админов.\n"
        "У каждого триггера своя пауза между срабатываниями.",
        fields=[Field("trig_on", "toggle", "Статус")],
        widgets=["trigs"],
    ),
    Section(
        "cmds", "🔢 Счётчики",
        "Развлекательные команды со счётом — свои приколы в каждом чате.\n"
        "Задаёте команду (например <code>!черви</code>) и ответ (например "
        "<code>кузнечики</code>). Бот отвечает «кузнечики [1]», «кузнечики "
        "[2]» — число растёт с каждым вызовом и переживает перезапуск.\n"
        "«Ловить в любом месте» — команда сработает и в середине фразы.\n"
        "«Работать и без «!»» — отзовётся и на голое слово, как триггер.\n"
        "«Кулдаун для гостей» — общая пауза для тех, кто в чате не состоит: "
        "вызвал один — остальным команда недоступна до конца паузы.",
        fields=[
            Field("cmds_on", "toggle", "Статус"),
            Field("cmds_anywhere", "toggle", "Ловить в любом месте"),
            Field("cmds_bare", "toggle", "Работать и без «!»"),
            Field("cmds_guest_cd", "cycle", "Кулдаун для гостей",
                  list(config.CMD_GUEST_CD_PRESETS), config.CMD_GUEST_CD_LABELS),
        ],
        widgets=["cmds"],
    ),
    Section(
        "rates", "💱 Курс валют",
        "Команда <code>!курс</code> показывает, сколько стоят доллар, евро "
        "и юань в рублях.\n"
        "<code>!курс 100$</code> переведёт сумму в рубли и в остальные валюты, "
        "а <code>!курс 5000</code> — рубли в валюту.\n"
        "Курс официальный, от Центробанка, обновляется раз в сутки.\n"
        "«Пауза» — сколько ждать между вызовами, чтобы командой не заваливали "
        "чат.",
        fields=[
            Field("rates_on", "toggle", "Статус"),
            Field("rates_cd", "cycle", "Пауза", list(config.RATES_CD_PRESETS),
                  fmt="sec"),
        ],
    ),
    Section(
        "digest", "📊 Недельная сводка",
        "Раз в неделю бот присылает сводку по чату: кто сколько писал, кто "
        "пришёл и ушёл, кто давно молчит.\n"
        "Отправляется в личку тому, кого вы укажете.",
        widgets=["digest_to"],
    ),
    Section(
        "trust", "🎖 Доверие",
        "Одно и то же нарушение от новичка и от старожила — разные вещи.\n"
        "Бот делит людей на новичков, своих и гостей (кто в чате не "
        "состоит) и смягчает наказание тем, кто давно с вами.\n"
        "Ниже выбирается, с какого возраста и количества сообщений человек "
        "считается своим и что именно ему прощать.",
        fields=[
            Field("trust_on", "toggle", "Статус"),
            Field("trust_soften", "toggle", "Смягчать наказания своим"),
            Field("trust_days", "cycle", "Дней до уровня «свой»",
                  list(config.TRUST_DAYS_PRESETS), fmt="plain"),
            Field("trust_msgs", "cycle", "Сообщений до уровня «свой»",
                  list(config.TRUST_MSGS_PRESETS), fmt="plain"),
        ],
        widgets=["trustsoft"],
    ),
    Section(
        "trust_soft", "🎖 Что смягчать",
        "Какие правила смягчать своим людям.\n"
        "Отмеченное превращается для них в удаление вместо мута или бана.",
        fields=[],
        widgets=["trustbits"],
        back="trust",
    ),
    Section(
        "warns", "⚠️ Варны",
        "Предупреждения с накоплением: <code>!warn причина</code> ответом "
        "на сообщение.\n"
        "Набрал лимит — получает наказание, счётчик обнуляется.",
        fields=[
            Field("warns_on", "toggle", "Статус"),
            Field("warns_limit", "cycle", "Варнов до наказания",
                  list(config.WARN_LIMIT_PRESETS), fmt="plain"),
            _punish("warns_punish"),
            _mute("warns_mute_min", "warns_punish"),
        ],
        widgets=["warnlist"],
    ),
    Section(
        "rules", "📜 Правила в постах",
        "Под каждым постом, который прилетает из привязанного канала, бот "
        "публикует правила чата.\n"
        "Заготовок можно завести несколько — бот выберет случайную.\n"
        "Правка поста и альбом из нескольких картинок считаются одним "
        "постом: правила отправятся один раз.",
        fields=[Field("rules_on", "toggle", "Статус")],
        widgets=["rules_text"],
    ),
    Section(
        "punish_cfg", "⚙️ Настройки наказаний",
        "Общие мелочи наказаний.\n"
        "«Мут за чужие команды» — что делать, если команду модерации "
        "написал обычный участник: сообщение удаляется, а сам он получает "
        "мут на это время.\n"
        "«Мут оставляет реакции» — замученный не пишет, но может ставить "
        "реакции.",
        fields=[
            Field("misuse_mute", "cycle", "Мут за чужие команды",
                  list(config.MISUSE_MUTE_PRESETS), config.MISUSE_MUTE_LABELS),
            Field("mute_reactions", "toggle", "Мут оставляет реакции"),
        ],
        back="u:p:{cid}:0",
    ),
    Section(
        "service", "🧹 Системные сообщения",
        "Чистка служебных сообщений Telegram: «вошёл в чат», «вышел из "
        "чата» и прочих вроде закрепа или смены названия.",
        fields=[
            Field("service_join", "toggle", "Уведомления о входе"),
            Field("service_leave", "toggle", "Уведомления о выходе"),
            Field("service_other", "toggle", "Закреп, смена названия и фото"),
        ],
    ),
    Section(
        "wl", "🕊 Вайтлист",
        "Для этих людей и каналов проверки не работают.\n"
        "Нажмите на запись, чтобы выбрать, что именно прощать: всё сразу "
        "или только ссылки, стоп-слова, инлайн-ботов, флуд.\n"
        "Добавить можно по id, @нику или пересылкой сообщения.",
        widgets=["wl"],
    ),
    Section(
        "read", "🔍 Распознавание медиа",
        "Половина рекламы приходит картинкой или голосовым. Для правил "
        "такое сообщение выглядит пустым, и мимо проходят и стоп-слова, и "
        "ссылки.\n"
        "Бот достаёт текст из картинки или записи и проверяет его обычными "
        "правилами. Само распознавание никого не наказывает — бот просто "
        "начинает видеть больше.\n"
        "Распознанное попадает в карточку отдельной строкой: видно, за что "
        "наказали.\n"
        "Картинки бот читает сам. Голосовые — только если подключена служба "
        "расшифровки; без неё переключатель ничего не делает.\n"
        "Записи длиннее заданного пропускаются: длинную речь разбирать "
        "долго.",
        fields=[
            Field("ocr_on", "toggle", "Читать текст с картинок"),
            Field("ocr_langs", "cycle", "Языки", list(config.OCR_LANG_PRESETS),
                  config.OCR_LANG_LABELS, show_if=("ocr_on", 1)),
            Field("asr_on", "toggle", "Расшифровывать голосовые"),
            Field("asr_max_sec", "cycle", "Максимум записи",
                  list(config.ASR_MAX_SEC_PRESETS), fmt="sec",
                  show_if=("asr_on", 1)),
        ],
        widgets=["read_stats"],
    ),
    Section(
        "nn", "🧪 Нейрофильтр",
        "Правила ловят то, что описано словами, а рекламу постоянно "
        "переписывают. Бот сравнивает новое сообщение с теми, за которые "
        "уже наказывали, и узнаёт то же самое, сказанное иначе.\n"
        "«Сбор данных» — бот только копит примеры: что удалили правила, что "
        "вы подтвердили или отменили кнопкой на карточке, плюс редкие "
        "обычные сообщения как образец нормы. Наказания, выданные руками "
        "через <code>!mute</code> и <code>!ban</code>, в сравнении не "
        "участвуют: причины у людей свои и к тексту часто отношения не "
        "имеют.\n"
        "«Теневой режим» — бот выносит решение и записывает его в файл, но "
        "ничего не делает. Так видно, ошибался бы он или нет, ещё до того, "
        "как ему что-то доверили.\n"
        "«Порог» — насколько похоже должно быть, чтобы бот счёл сообщение "
        "рекламой. Ниже есть рекомендованный: он подобран по вашим же "
        "примерам так, чтобы обычные сообщения под него не попадали.\n"
        "«Учиться на уликах всей сетки» — чаты одной сетки делятся "
        "примерами между собой, и новый чат начинает работать сразу.\n"
        "«Стартовый набор» — готовые примеры спама, загруженные владельцем "
        "бота. Ими чат пользуется, пока не накопит собственных: свои примеры "
        "точнее, поэтому набор отключается сам.",
        fields=[
            Field("nn_mode", "cycle", "Режим", list(config.NN_MODES),
                  config.NN_MODE_LABELS),
            Field("nn_threshold", "cycle", "Порог сходства, %",
                  list(config.NN_THRESHOLD_PRESETS), fmt="plain"),
            Field("nn_net", "toggle", "Учиться на уликах всей сетки"),
            Field("nn_seed", "toggle", "Пользоваться стартовым набором"),
        ],
        widgets=["nn_stats", "nn_clusters"],
    ),
    Section(
        "cards", "🪪 Карточки и лог",
        "Всё, что бот делает в чате, приходит карточкой в отдельный "
        "лог-чат: кто, что, причина и кнопки, чтобы отменить решение.\n"
        "Ниже выбирается лог-чат и то, о каких событиях сообщать.",
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
    ("trig_on", "Триггеры"), ("cmds_on", "Счётчики"), ("warns_on", "Варны"),
    ("rules_on", "Правила"), ("trust_on", "Доверие"),
    ("cards_on", "Карточки"),
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


def field_dict(f: Field, settings_obj) -> dict:
    """Одно поле для панели: значение плюс готовые подписи вариантов.

    Подписи считаем здесь, а не в браузере: правила вроде «1440 = сутки»
    живут в utils, и дублировать их на javascript значит однажды разойтись.
    """
    val = getattr(settings_obj, f.key)
    return {
        "key": f.key, "kind": f.kind, "label": f.label,
        "value": val, "value_label": value_label(f, val),
        "options": [{"value": v, "label": value_label(f, v)} for v in (f.values or [])],
        "fmt": f.fmt, "visible": visible(f, settings_obj),
    }


def section_dict(s: Section, settings_obj) -> dict:
    """Раздел целиком. main — главный тумблер, по нему рисуется точка в списке."""
    main = next((f.key for f in s.fields if f.kind == "toggle"), None)
    return {
        "key": s.key, "title": s.title, "intro": s.intro,
        "widgets": s.widgets, "back": s.back, "main": main,
        "on": bool(getattr(settings_obj, main)) if main else None,
        "fields": [field_dict(f, settings_obj) for f in s.fields],
    }
