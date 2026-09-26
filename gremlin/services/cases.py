"""Случай: что написали, кто написал и чем кончилось — одной записью.

Зачем. Каждое решение — бан правилом, наказание за профиль, карточка
подозрения, ручной бан — это разметка, которую бот раньше выбрасывал: из 286
наказаний 245 не оставили в копилке ничего. Улика писалась, только если в чате
включён нейрофильтр, профиль — только при бане и только при сравнении
профилей, сообщение при бане за профиль — никогда.

Теперь любое решение пишет случай целиком: сообщение с полями, профиль автора
и снимок признаков на момент решения — стаж, гость или участник, аватарка.
Метки у сообщения и профиля свои: бан за стоп-слово ничего не говорит о
профиле (аккаунт могли взломать), бан за профиль — о сообщении. Что не
доказано, пишется «unknown» и ждёт кнопки на карточке: снятие наказания
делает нормой весь случай, бан из карточки — спамом.

Запись не зависит от настроек нейрофильтра: записать — не значит наказать.
"""
import logging

from .. import db

logger = logging.getLogger("gremlin.cases")

# профиль нормы берём только у старожилов: у новичка ещё неизвестно, кто он
NORM_DAYS = 7


def _msg_data(message, text: str | None) -> dict:
    """Поля сообщения, из которых собрана строка для модели."""
    if message is None:
        return {"text": text or ""}
    from . import media, moderation
    kind = next((label for attr, label in moderation._MEDIA_LABELS.items()
                 if getattr(message, attr, None) is not None), None)
    return {"text": message.text or message.caption or "", "media": kind,
            "media_text": media.cached(message) or "",
            "buttons": moderation.button_urls(message)}


async def _features(bot, chat_id: int, user, pdata: dict | None,
                    pid: int | None, facts: dict | None,
                    body: str = "", face: str = "") -> dict:
    """Снимок признаков на момент решения.

    Всё, что меняет само наказание, берём как было до него: иначе признак
    выдаёт ответ. После бана человек «не в чате» и «наказан однажды» — у
    нормы таких не бывает, и модель выучила бы не спам, а факт бана.

    Набор одинаковый у любого случая, откуда бы он ни пришёл: признак, который
    есть только у банов наблюдения, выучился бы как «пришло из наблюдения —
    значит спам». Поэтому эвристики имени и текста считаем здесь для всех.
    """
    from . import nn, watch
    uid = getattr(user, "id", None)
    out = {"premium": getattr(user, "is_premium", None),
           "lang": getattr(user, "language_code", None) or ""}
    first = getattr(user, "first_name", None) or getattr(user, "full_name", None)
    out["name_hard"], out["name_cos"], _ = watch.profile_parts(
        first, getattr(user, "last_name", None), getattr(user, "username", None))
    out["msg_hard"], out["msg_cos"], _ = watch.message_parts(body or "")
    # оценки моделей — только если модель уже поднята: грузить её ради записи
    # случая незачем, в боте она и так загружена с первого сообщения
    if nn.status() == "ok":
        got = await nn.check(chat_id, body) if body else None
        out["text"] = got["score"] if got else None
        got = await nn.face_score(chat_id, face) if face else None
        out["face"] = list(got) if got else None
    # номер аккаунта: Telegram раздаёт их по порядку, так что чем больше id,
    # тем моложе аккаунт. Одноразовые спам-аккаунты почти всегда свежие
    if uid:
        out["id"] = uid
    if pdata is not None:
        out["photo"] = bool(pdata.get("photo_id"))
        # аватарка: у половины спам-профилей вся суть в ней. Байты берём из
        # кэша профиля — наблюдение их обычно уже скачало
        if pdata.get("photo_id") and bot is not None:
            from . import nsfw
            from . import profile as prof_svc
            out["nsfw"] = await nsfw.score(await prof_svc.photo_bytes(bot, pdata))
        out["channel"] = bool(pdata.get("channel_title"))
        out["bio"] = bool(pdata.get("bio"))
    if uid:
        got = await db.trust_facts(chat_id, uid)
        out.update(days=got["days"], msgs=got["msgs"],
                   pun=max(got["pun"] - (1 if pid else 0), 0))
        member = None
        if pid:
            row = await db.get_punishment(pid)
            if row is not None and "was_member" in row.keys():
                member = bool(row["was_member"])
        elif bot is not None:
            from . import adm_cache
            try:
                member = await adm_cache.is_member(bot, chat_id, uid)
            except Exception:
                pass
        out["member"] = member
    if facts:
        out.update(facts)
    return out


async def record(bot, chat_id: int, user, *, msg_label: str | None,
                 prof_label: str | None, origin: str, feature: str,
                 message=None, text: str | None = None, pid: int | None = None,
                 labeled_by: int | None = None, pdata: dict | None = None,
                 facts: dict | None = None) -> int | None:
    """Записать случай. Номер случая или None, если писать было нечего.

    msg_label / prof_label — 'spam', 'ok', 'unknown' или None (часть не пишем).
    origin — откуда сообщение: 'auto' решил бот, 'card' — человек кнопкой,
    'manual' — ручное наказание, 'random' — случайный образец нормы.
    facts — что знает вызывающий сверх общего: очки наблюдения, правило.
    Ошибка записи модерацию не ломает: случай потеряется, бан останется.
    """
    try:
        return await _record(bot, chat_id, user, msg_label, prof_label, origin,
                             feature, message, text, pid, labeled_by, pdata, facts)
    except Exception:
        logger.warning("случай не записался", exc_info=True)
        return None


async def _record(bot, chat_id, user, msg_label, prof_label, origin, feature,
                  message, text, pid, labeled_by, pdata, facts) -> int | None:
    from . import moderation
    from . import profile as prof_svc

    uid = getattr(user, "id", None)
    # профиль спрашиваем у любого случая, даже если его самого не пишем:
    # иначе «есть аватарка» знали бы только у спама, а у нормы — пусто
    if uid and pdata is None and bot is not None:
        pdata = await prof_svc.fetch(bot, uid)
    data = _msg_data(message, text)
    body = " ".join(filter(None, [data["text"], data.get("media_text")]))
    face = " ".join(prof_svc.face_text(user, pdata).split()) if uid else ""
    feats = await _features(bot, chat_id, user, pdata, pid, facts, body, face)

    ids = []
    if msg_label:
        data["features"] = feats
        extra = "\n".join(moderation._buttons(message)) if message is not None else ""
        ids.append(await db.sample_add(chat_id, uid, origin, msg_label, body,
                                       feature=feature, pid=pid, extra=extra or None,
                                       data=data, labeled_by=labeled_by))
    if prof_label and uid:
        fields = prof_svc.fields_of(user, pdata)
        fields["features"] = feats
        ids.append(await db.sample_add(chat_id, uid, "profile", prof_label, face,
                                       feature=feature, pid=pid, data=fields,
                                       labeled_by=labeled_by))
    ids = [i for i in ids if i]
    if not ids:
        return None
    case = min(ids)
    await db.case_link(case, ids)
    if uid:
        from . import score
        await db.verdict_attach(chat_id, uid, case, await score.predict(feats))
    # модель не сбрасываем: копилка перечитывается сама раз в несколько минут,
    # а сброс на каждый бан и каждый образец нормы гонял бы регрессию заново
    return case


async def settle(case_id: int | None, label: str, labeled_by: int | None,
                 chat_id: int | None = None) -> bool:
    """Исход случая от человека: весь случай — спам или норма."""
    if not case_id:
        return False
    moved = await db.case_label(case_id, label, labeled_by)
    if moved:
        from . import nn
        nn.invalidate(chat_id)
    return bool(moved)


async def norm_sample(bot, message) -> int | None:
    """Случайный образец нормы. Профиль — только у старожила чата.

    Профилей нормы в копилке меньше всего, а без них сравнению профилей не с
    чем сопоставить спам: всё похоже на плохое. Старожил, прошедший всю
    модерацию, — самый дешёвый честный пример.
    """
    user = message.from_user
    got = await db.trust_facts(message.chat.id, user.id)
    old = got["days"] >= NORM_DAYS
    return await record(bot, message.chat.id, user, msg_label="ok",
                        prof_label="ok" if old else None, origin="random",
                        feature=None, message=message, text=None)
