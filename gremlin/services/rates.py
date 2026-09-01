"""Курсы валют для команды !курс.

Берём официальные курсы Центробанка: они бесплатны, без ключей и обновляются
раз в сутки. Для чата этого достаточно — команда отвечает на вопрос «сколько
сейчас доллар», а не обслуживает торговлю.

Курс кэшируем на час: ЦБ всё равно меняет его раз в день, а дёргать чужой
сервис на каждое сообщение незачем. Не ответил — отдаём последнее, что знали,
и говорим, когда оно получено: устаревший курс полезнее пустого ответа.
"""
import asyncio
import logging
import time

from .. import config

logger = logging.getLogger("gremlin.rates")

# что показываем и как это называется у человека
CURRENCIES = (
    ("USD", "$", "доллар"),
    ("EUR", "€", "евро"),
    ("CNY", "¥", "юань"),
)
RUB = "₽"

_cache: dict = {"ts": 0.0, "rates": {}, "date": None}
_lock = asyncio.Lock()


def _parse_cbr(raw: bytes) -> tuple[dict[str, float], str | None]:
    """Официальный XML ЦБ -> цена одной единицы валюты в рублях.

    Валюты идут с номиналом: юань указан за 10 единиц, и без деления курс
    вышел бы в десять раз больше. Числа в русском формате, через запятую.
    """
    import xml.etree.ElementTree as ET
    root = ET.fromstring(raw.decode("cp1251", errors="replace"))
    wanted = {code for code, _s, _n in CURRENCIES}
    out = {}
    for node in root.findall("Valute"):
        code = (node.findtext("CharCode") or "").upper()
        if code not in wanted:
            continue
        try:
            nominal = float((node.findtext("Nominal") or "1").replace(",", "."))
            value = float((node.findtext("Value") or "").replace(",", "."))
        except ValueError:
            continue
        if nominal > 0:
            out[code] = value / nominal
    date = root.get("Date")            # ДД.ММ.ГГГГ
    return out, date


def _parse_fallback(data: dict) -> dict[str, float]:
    """Запасной источник отдаёт, сколько валюты в одном рубле — переворачиваем."""
    rates = data.get("rates") or {}
    out = {}
    for code, _sym, _name in CURRENCIES:
        try:
            per_rub = float(rates[code])
        except (KeyError, TypeError, ValueError):
            continue
        if per_rub > 0:
            out[code] = 1 / per_rub
    return out


async def fetch() -> tuple[dict[str, float], str | None, bool]:
    """(курсы, дата ЦБ, свежие ли). Пустой словарь — не смогли и нечего отдать."""
    now = time.monotonic()
    if _cache["rates"] and now - _cache["ts"] < config.RATES_TTL:
        return _cache["rates"], _cache["date"], True

    async with _lock:
        if _cache["rates"] and time.monotonic() - _cache["ts"] < config.RATES_TTL:
            return _cache["rates"], _cache["date"], True
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=config.RATES_TIMEOUT)
        rates, date = {}, None
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                async with sess.get(config.RATES_URL) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"ответ {resp.status}")
                    rates, date = _parse_cbr(await resp.read())
        except Exception as e:
            logger.warning("ЦБ не ответил (%s): %s", config.RATES_URL, e)

        if not rates and config.RATES_FALLBACK:
            try:
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.get(config.RATES_FALLBACK) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"ответ {resp.status}")
                        rates = _parse_fallback(await resp.json(content_type=None))
                        date = None
            except Exception as e:
                logger.warning("запасной источник курсов молчит: %s", e)

        if not rates:
            # отдаём прошлые: устаревший курс лучше, чем никакого
            return _cache["rates"], _cache["date"], False
        _cache.update(ts=time.monotonic(), rates=rates, date=date)
        return rates, date, True


def _money(value: float) -> str:
    """Число по-человечески: 84,5 вместо 84.4972 и 8 638 вместо 8638.0."""
    if value >= 1000:
        text = f"{value:,.0f}".replace(",", " ")
    elif abs(value - round(value)) < 0.005:
        text = f"{round(value):d}"          # круглое показываем без хвоста
    elif value >= 100:
        text = f"{value:.1f}"
    else:
        text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def parse_amount(text: str) -> tuple[float, str] | None:
    """Разобрать «!курс 100$» или «!курс 5000». Вернуть (сумма, код валюты).

    Без значка считаем, что это рубли: чаще всего спрашивают именно так.
    """
    body = text.strip()
    code = "RUB"
    for cur, sym, name in CURRENCIES:
        low = body.lower()
        if sym in body or cur.lower() in low or name in low:
            code = cur
            break
    digits = "".join(ch if ch.isdigit() or ch in ",." else " "
                     for ch in body).replace(",", ".").split()
    for token in digits:
        try:
            amount = float(token)
        except ValueError:
            continue
        if amount > 0:
            return amount, code
    return None


async def board() -> str:
    """Табло курсов: сколько стоит каждая валюта в рублях."""
    rates, date, fresh = await fetch()
    if not rates:
        return "💱 Курс сейчас не получить — источник не отвечает."
    lines = ["💱 <b>Курс валют</b>"]
    for code, sym, _name in CURRENCIES:
        if code in rates:
            lines.append(f"{sym}1 = {_money(rates[code])} {RUB}")
    lines.append(_footer(date, fresh))
    return "\n".join(lines)


async def convert(amount: float, code: str) -> str:
    """Перевод суммы: рубли — во все валюты, валюту — в рубли и остальные."""
    rates, date, fresh = await fetch()
    if not rates:
        return "💱 Курс сейчас не получить — источник не отвечает."
    sym = dict((c, s) for c, s, _n in CURRENCIES).get(code, RUB)
    lines = [f"💱 <b>{_money(amount)} {sym}</b>"]
    if code == "RUB":
        for cur, csym, _name in CURRENCIES:
            if cur in rates:
                lines.append(f"{csym}{_money(amount / rates[cur])}")
    else:
        lines.append(f"{RUB}{_money(amount * rates[code])}")
        for cur, csym, _name in CURRENCIES:
            if cur != code and cur in rates:
                lines.append(f"{csym}{_money(amount * rates[code] / rates[cur])}")
    lines.append(_footer(date, fresh))
    return "\n".join(lines)


def _footer(date: str | None, fresh: bool) -> str:
    where = f"курс ЦБ на {date}" if date else "курс ЦБ"
    return f"<i>{where}{'' if fresh else ' · источник не отвечает, курс прошлый'}</i>"
