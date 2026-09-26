"""Обученная оценка: чему она учится и чему не должна.

Всё здесь ломается молча. Признак «какое правило сработало» дал бы модели
сто процентов на замере — она просто повторяла бы правила. Свежий автобан,
взятый слабой меткой «спам» раньше, чем его успели снять, учит на ошибках
бота. Сто банов одного аккаунта — это сто одинаковых примеров. А веса,
которые после записи в базу считают по-другому, тихо портят оценку.
"""
import time

from gremlin import db
from gremlin.services import cases, score

from conftest import make_user


def test_decision_source_does_not_leak():
    """Правило, причина, очки наблюдения не меняют вектор — модель их не видит."""
    base = {"days": 3, "photo": True, "text": 80}
    noisy = dict(base, rule="стоп-слово", why="18+ в профиле",
                 watch={"total": 90}, buttons=["t.me/x"])
    assert score.vector(base) == score.vector(noisy)
    assert not {"rule", "why", "watch"} & set(score.NAMES)


async def add_case(chat, uid, label, *, days, human=True, age=10 * 86400):
    case = await cases.record(None, chat, make_user(uid), msg_label=label,
                              prof_label=None, origin="card" if human else "auto",
                              feature="тест", text=f"сообщение номер {uid} для проверки",
                              labeled_by=1 if human else None, facts={"days": days})
    await db._db.execute("UPDATE samples SET ts = ? WHERE case_id = ?",
                         (int(time.time()) - age, case))
    return case


async def test_dataset_skips_fresh_autoban_and_repeats(chat):
    await add_case(chat, 1, "spam", days=0, human=False, age=3600)   # могут ещё снять
    await add_case(chat, 2, "spam", days=0, human=False)             # не сняли за 10 дней
    await add_case(chat, 3, "ok", days=50)
    await add_case(chat, 3, "ok", days=51)                           # тот же человек
    got = {(r["user"], r["label"]) for r in await score.dataset()}
    assert got == {(2, "spam"), (3, "ok")}


async def test_retrain_waits_for_enough_and_weights_survive_storage(chat):
    score._cached = (0.0, None)
    for uid in range(10):
        await add_case(chat, 100 + uid, "spam", days=0)
        await add_case(chat, 200 + uid, "ok", days=300)
    assert (await score.retrain()).startswith("рано")
    assert await score.predict({"days": 0}) is None

    for uid in range(10, score.MIN_EACH):
        await add_case(chat, 100 + uid, "spam", days=0)
        await add_case(chat, 200 + uid, "ok", days=300)
    assert (await score.retrain()).startswith("обучена")
    stored = score._cached[1]
    score._cached = (0.0, None)                       # читать из базы, не из памяти
    newbie, veteran = {"days": 0}, {"days": 300}
    assert await score.predict(newbie) == score.score_with(stored, newbie)
    assert await score.predict(newbie) > await score.predict(veteran)
