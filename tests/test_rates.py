"""Курс валют: разбор ответа ЦБ (с номиналами), тенге, гривна, тугрик, оформление."""
import time

import pytest

from gremlin.services import rates

XML = """<?xml version="1.0" encoding="windows-1251"?>
<ValCurs Date="08.09.2026" name="Foreign Currency Market">
<Valute><CharCode>USD</CharCode><Nominal>1</Nominal><Value>84,4972</Value></Valute>
<Valute><CharCode>EUR</CharCode><Nominal>1</Nominal><Value>98,7654</Value></Valute>
<Valute><CharCode>CNY</CharCode><Nominal>10</Nominal><Value>118,32</Value></Valute>
<Valute><CharCode>KZT</CharCode><Nominal>100</Nominal><Value>15,6789</Value></Valute>
<Valute><CharCode>UAH</CharCode><Nominal>10</Nominal><Value>18,8088</Value></Valute>
<Valute><CharCode>MNT</CharCode><Nominal>1000</Nominal><Value>23,4571</Value></Valute>
</ValCurs>""".encode("cp1251")


@pytest.fixture
def cached(monkeypatch):
    got, date = rates._parse_cbr(XML)
    monkeypatch.setattr(rates, "_cache",
                        {"ts": time.monotonic(), "rates": got, "date": date})
    return got


def test_nominal_is_divided():
    got, date = rates._parse_cbr(XML)
    assert date == "08.09.2026"
    assert got["CNY"] == pytest.approx(11.832)
    assert got["KZT"] == pytest.approx(0.156789)


async def test_board_shows_main_and_blank_lines(cached):
    lines = (await rates.board()).split("\n")
    assert lines[0] == "💱 <b>Курс валют</b>"
    assert lines[1] == ""                      # абзац после заголовка
    assert lines[2:4] == ["$1 = 84,5 ₽", "€1 = 98,77 ₽"]
    assert lines[4] == ""                      # остальных без спроса нет
    assert "курс ЦБ на 08.09.2026" in lines[-1]


async def test_board_adds_named_currency(cached):
    assert "₸1 = 0,16 ₽" in (await rates.board(rates.mentioned("тенге"))).split("\n")
    assert "₴1 = 1,88 ₽" in (await rates.board(rates.mentioned("гривна"))).split("\n")
    # тугрик за тысячу: «₮1 = 0,02 ₽» ничего не говорит
    assert "₮1 000 = 23,46 ₽" in (await rates.board(rates.mentioned("тугрик"))).split("\n")
    assert rates.mentioned("что там") is None


async def test_convert_lists_all(cached):
    text = await rates.convert(100, "USD")
    for sym in ("₽", "€", "¥", "₸", "₴", "₮"):
        assert sym in text


async def test_convert_tenge(cached):
    text = await rates.convert(10000, "KZT")
    assert "10 000 ₸" in text
    assert "₽1 568" in text


@pytest.mark.parametrize("raw,want", [
    ("1000 тенге", (1000.0, "KZT")),
    ("500₸", (500.0, "KZT")),
    ("300 kzt", (300.0, "KZT")),
    ("100$", (100.0, "USD")),
    ("5000", (5000.0, "RUB")),
    ("200 гривен", (200.0, "UAH")),
    ("50 гривны", (50.0, "UAH")),
    ("10000 тугриков", (10000.0, "MNT")),
    ("300₮", (300.0, "MNT")),
])
def test_parse_amount(raw, want):
    assert rates.parse_amount(raw) == want


def test_money_format():
    assert rates._money(8638.0) == "8 638"
    assert rates._money(100.0) == "100"
    assert rates._money(84.4972) == "84,5"
