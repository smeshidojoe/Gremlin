"""Подготовка текста для нейрофильтра: регистр, обфускация, невидимки."""
import os

import pytest

from gremlin import config, utils
from gremlin.services import nn


@pytest.mark.parametrize("raw,want", [
    ("ДА НЕТ КОНЕЧНО", "да нет конечно"),
    ("Привет, как дела?", "привет, как дела?"),
    ("КAЗИНO", "казино"),                 # латиница внутри кириллицы
    ("ка​зино", "казино"),           # невидимый символ
    ("дааааа", "даа"),                    # растяжка
    ("cool story bro", "cool story bro"),  # английский не портим
    ("заработок 10000 рублей", "заработок 10000 рублей"),
    ("", ""),
])
def test_normalize_text(raw, want):
    assert utils.normalize_text(raw) == want


@pytest.mark.model
async def test_caps_no_longer_looks_like_caps_spam():
    """КАПС ломал сравнение: любые два капса «похожи» на 93%, о чём бы ни
    были. После приведения к строчным разговор отходит от рекламы."""
    tokenizer = os.path.join(config.NN_MODEL_DIR, "tokenizer.json")
    if not os.path.exists(tokenizer):
        pytest.skip("нет модели rubert-tiny2 в tools/models")
    if not await nn.ensure():
        pytest.skip(f"модель не поднялась: {nn.status()}")
    spam = ("!!СРOЧНO ВAЖНO СРOЧНO!!! НYЖНЫ ЛЮДИ НА OНЛAЙH ЗAРAБOТOК "
            "950-1500$ В НEДEЛЮ ПИСАТЬ В ЛС")
    chat = "ДА НЕТ КОНЕЧНО КАКИЕ НАХУЙ ЛИЦЕНЗИИ У КИНОПОИСКА В 2026 БЛЯТЬ"
    near = "срочно нужны люди на онлайн заработок, пишите в личные сообщения"
    vecs = await nn.embed([spam, chat, near])
    talk = float(vecs[0] @ vecs[1]) * 100
    same = float(vecs[0] @ vecs[2]) * 100
    assert talk < 70, f"разговор похож на спам на {talk:.0f}%"
    assert same > 70, f"спам не узнал спам: {same:.0f}%"
