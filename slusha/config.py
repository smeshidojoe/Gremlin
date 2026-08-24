"""Настройки бота-собеседника. Свой .env, свой токен, своя база.

Специально не тянем ничего из gremlin: это отдельный бот со своим процессом и
своими секретами. Общее у них только то, что лежит в одной папке проекта.
"""
import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.dirname(os.path.abspath(__file__))

# сначала свой .env рядом с пакетом, потом общий в корне — чтобы можно было
# держать оба бота на одном файле, если так удобнее
load_dotenv(os.path.join(PKG_DIR, ".env"))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Только свой токен: подхватить BOT_TOKEN модератора нельзя — два
# поллера на одном токене начнут отбирать апдейты друг у друга.
BOT_TOKEN = os.getenv("SLUSHA_BOT_TOKEN")

DB_PATH = os.getenv("SLUSHA_DB_PATH") or os.path.join(BASE_DIR, "data", "slusha.sqlite3")
LOG_PATH = os.getenv("SLUSHA_LOG_PATH") or os.path.join(BASE_DIR, "data", "slusha.log")

try:
    ADMIN_IDS = {
        int(x) for x in (os.getenv("SLUSHA_ADMIN_IDS") or os.getenv("ADMIN_IDS")
                         or "424211817").replace(" ", "").split(",") if x
    }
except ValueError as e:
    raise RuntimeError(f"ADMIN_IDS must be comma-separated integers: {e}") from e

TZ_OFFSET = int(os.getenv("TZ_OFFSET") or 3)

# Служебные аккаунты Telegram: это не люди, отвечать им незачем.
SERVICE_IDS = {777000, 1087968824, 136817688}

# Сообщение старше этого не трогаем: после простоя не отвечаем на вчерашнее.
MSG_MAX_AGE = 120

# ---------- модель ----------
#
# Провайдер: anthropic — родной SDK; openai — любой OpenAI-совместимый сервис
# (OpenRouter с Kimi и DeepSeek, Moonshot напрямую); ollama — локальная модель.
AI_PROVIDER = (os.getenv("AI_PROVIDER") or "anthropic").lower()
AI_KEY = os.getenv("ANTHROPIC_API_KEY") or ""
AI_BASE_URL = (os.getenv("AI_BASE_URL") or "").rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY") or ""
_DEFAULT_MODEL = {"anthropic": "claude-opus-5"}.get(AI_PROVIDER, "")
AI_MODEL = os.getenv("AI_MODEL") or _DEFAULT_MODEL
AI_MAX_TOKENS = int(os.getenv("AI_MAX_TOKENS") or 800)

# «Думающие» модели тратят токены на рассуждения и могут упереться в лимит,
# так и не начав отвечать. Просим их не думать вслух.
AI_NO_THINK = (os.getenv("AI_NO_THINK") or "1") not in ("0", "false", "no")
AI_THINKING_MODELS = ("qwen3", "qwq", "deepseek-r1", "magistral", "reasoning")

AI_MAX_CHARS = int(os.getenv("AI_MAX_CHARS") or 600)
AI_TIMEOUT = 40              # сек на запрос, дальше молчим
AI_COOLDOWN = 8              # пауза между ответами в одном чате, сек
AI_HISTORY = 60              # сколько сообщений чата держим в памяти
AI_PARTS = 3                 # на сколько сообщений максимум дробим ответ
AI_TYPING_CPM = 1200         # «скорость печати» для пауз между репликами
AI_PART_PAUSE_MAX = 5        # но ждать дольше этого не будем, сек

AI_CTX_PRESETS = (10, 20, 30, 50)
AI_RANDOM_PRESETS = (0, 1, 3, 5, 10, 20, 50, 100)
AI_DAILY_PRESETS = (20, 50, 100, 200, 500, 1000, 3000, 10000)

AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE") or 0.9)

# Ollama по умолчанию даёт модели 4096 токенов и молча режет остальное.
AI_NUM_CTX = int(os.getenv("AI_NUM_CTX") or 8192)
# Штраф за повторы: маленькие модели любят копировать свои же прошлые ответы.
AI_REPEAT_PENALTY = float(os.getenv("AI_REPEAT_PENALTY") or 1.2)

AI_LEN_PRESETS = (0, 1, 2)
AI_LEN_LABELS = {0: "коротко", 1: "средне", 2: "развёрнуто"}
AI_LEN_RULES = {
    0: ("одной-двумя короткими фразами", 400),
    1: ("двумя-тремя живыми фразами", 800),
    2: ("развёрнуто, 4–6 фраз, но без воды и списков", 1800),
}

AI_PERSONA_LIMIT = int(os.getenv("AI_PERSONA_LIMIT") or 4000)

LORE_BUDGET = int(os.getenv("LORE_BUDGET") or 1500)
LORE_LIMIT = 200

AI_PERSONA_DEFAULT = (
    "Ты — Слюша, ехидный обитатель чата. Отвечаешь коротко, по-русски, "
    "без морали и канцелярита. Можешь язвить, но не унижаешь людей и не лезешь "
    "в политику. Если сказать нечего — отвечай одной фразой."
)
