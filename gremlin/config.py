import os

from dotenv import load_dotenv

load_dotenv()

# корень проекта (родитель пакета gremlin/)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BOT_TOKEN = os.getenv("BOT_TOKEN")

DB_PATH = os.getenv("DB_PATH", os.path.join(BASE_DIR, "gremlin.sqlite3"))
LOG_PATH = os.getenv("LOG_PATH", os.path.join(BASE_DIR, "bot.log"))

# Админы бота (доступ к /admin). Через запятую в env ADMIN_IDS.
try:
    ADMIN_IDS = {
        int(x) for x in os.getenv("ADMIN_IDS", "424211817").replace(" ", "").split(",") if x
    }
except ValueError as e:
    raise RuntimeError(f"ADMIN_IDS must be comma-separated integers: {e}") from e

# Часовой пояс чата, смещение от UTC в часах. По нему считаются сутки и недели
# в статистике: иначе «сегодня» начиналось бы в 03:00 по Москве.
TZ_OFFSET = int(os.getenv("TZ_OFFSET") or 3)

# Кэш списка админов чата, сек.
ADMIN_CACHE_TTL = 300

# Кэш результатов get_chat для проверки @упоминаний, сек.
MENTION_CACHE_TTL = 600

# Сколько последних ошибок держать в памяти для админ-меню.
ERROR_LOG_SIZE = 50

# Пресеты длительности мута, минуты (селектор ◀ ▶). 0 = навсегда.
MUTE_PRESETS = (10, 30, 60, 180, 360, 1440, 4320, 10080, 0)

# Пресеты антифлуда.
FLOOD_MSGS_PRESETS = (3, 5, 8, 12, 20)
FLOOD_WINDOW_PRESETS = (5, 10, 20, 30, 60)

# Пресеты капчи.
CAPTCHA_TIMEOUT_PRESETS = (60, 120, 300, 600)

# Сообщение не трогаем, если оно старше стольких секунд (защита от бэклога).
MSG_MAX_AGE = 120

# Mini app. WEBAPP_URL — публичный https-адрес (Telegram требует https). Пусто = аппка выкл.
# `or` вместо дефолта getenv: пустая переменная в .env (WEBAPP_PORT=) иначе перебила бы дефолт.
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").rstrip("/")
WEBAPP_HOST = os.getenv("WEBAPP_HOST") or "0.0.0.0"
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT") or "8080")

# Варианты наказаний (селектор).
PUNISH_VALUES = ("delete", "mute", "ban")
PUNISH_LABELS = {"delete": "только удаление", "mute": "мут", "ban": "бан"}

# Биты card_mask: какие карточки слать в лог-чат.
BIT_BAN = 1       # баны (командой и вручную)
BIT_MUTE = 2      # муты
BIT_INLINE = 4    # инлайн-боты
BIT_LINKS = 8     # ссылки
BIT_WORDS = 16    # стоп-слова
BIT_ANON = 32     # сообщения от имени групп/каналов
BIT_ADMIN = 64    # действия админов чата вручную
BIT_FLOOD = 128   # антифлуд
BIT_CAPTCHA = 512 # капча
BIT_WATCH = 1024  # наблюдение за профилями

CARD_BITS = (
    (BIT_BAN, "Баны"),
    (BIT_MUTE, "Муты"),
    (BIT_INLINE, "Инлайн-боты"),
    (BIT_LINKS, "Ссылки"),
    (BIT_WORDS, "Стоп-слова"),
    (BIT_ANON, "Анонимы"),
    (BIT_ADMIN, "Действия админов"),
    (BIT_FLOOD, "Антифлуд"),
    (BIT_CAPTCHA, "Капча"),
    (BIT_WATCH, "Наблюдение"),
)

CARD_MASK_ALL = 4095  # все биты включены (дефолт)

# Медиа-фильтры: биты media_mask — какие типы сообщений удалять.
MEDIA_BITS = (
    (1, "sticker", "Стикеры"),
    (2, "animation", "Гифки"),
    (4, "voice", "Голосовые"),
    (8, "video_note", "Кружки"),
    (16, "photo", "Фото"),
    (32, "video", "Видео"),
    (64, "document", "Файлы"),
    (128, "audio", "Музыка"),
)

# Триггеры: лимит на чат и кулдаун срабатывания, сек.
TRIG_LIMIT = 20
TRIG_COOLDOWN = 30

# Счётчики (команды со счётом): лимит на чат и пресеты кулдауна, сек (0 = без кулдауна).
CMD_LIMIT = 30
CMD_COOLDOWN_PRESETS = (0, 5, 10, 30, 60, 180, 600)
# Медиа-ответы триггеров лежат тут (файл скачивается один раз при создании).
TRIG_DIR = os.getenv("TRIG_DIR") or os.path.join(BASE_DIR, "data", "triggers")

# Юзербот-наблюдатель (Telethon): видит сообщения других ботов, чего Bot API не умеет.
# Ключи берём те же, что у скраппера (my.telegram.org). Пусто = юзербот выключен.
TG_API_ID = os.getenv("TG_API_ID") or ""
TG_API_HASH = os.getenv("TG_API_HASH") or ""
TG_SESSION = os.getenv("TG_SESSION") or os.path.join(BASE_DIR, "data", "audit_session")
USERBOT_ON = (os.getenv("USERBOT_ON") or "1") not in ("0", "false", "no")

# Возврат после разбана: сколько часов живёт пригласительная ссылка и «пропуск»,
# по которому бот сам одобряет заявку на вступление от разбаненного.
UNBAN_PASS_HOURS = int(os.getenv("UNBAN_PASS_HOURS") or 48)

# База статистики от скраппера (TGChatScrapper) — источник недельных сводок.
STATS_DB = os.getenv("STATS_DB") or os.path.join(BASE_DIR, "data", "chat_stats.db")

# Пороги наблюдения (очки скоринга). 0 в ban-пороге = автобан выключен.
WATCH_SUSPECT_PRESETS = (25, 40, 55, 70)
WATCH_BAN_PRESETS = (60, 80, 100, 0)

# Уровни игнора для вайтлиста.
WL_SCOPES = ("all", "inline", "links", "words", "flood", "watch", "anon")
WL_SCOPE_LABELS = {
    "all": "полный игнор",
    "inline": "инлайн-боты",
    "links": "ссылки",
    "words": "стоп-слова",
    "flood": "антифлуд",
    "watch": "наблюдение",
    "anon": "сообщения от имени канала",
}
