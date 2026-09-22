"""Единая оценка: один вердикт на сообщение вместо десятка отдельных судей.

Зачем. Раньше каждое правило решало само: стоп-слово нашло слово — забанило,
профиль нашёл рекламу — забанил, аватарка набрала 60 очков из 80 — почти
забанила. Друг о друге они не знали, поэтому обычный человек улетал в бан за
одно слово в разговоре, а одна и та же фраза считалась трижды — стоп-словами,
смысловыми фразами и нейрофильтром.

Здесь правила больше не судят. Они только докладывают, что увидели, а решение
принимается один раз и по всей картине.

Что сюда НЕ входит. Инлайн-боты, ссылки с пересылками, антифлуд, сообщения от
имени канала, медиа-фильтры, капча и вход по подписке остаются отдельными
модулями со своей мерой: там проверяемый факт и конкретное нарушение, гадать
не о чем. Распознавание картинок и голоса — тоже не сигнал, а подготовка:
достаёт текст, чтобы остальные его увидели.

Четыре семьи признаков:
    содержание  — стоп-слова, смысловые фразы, нейрофильтр
    профиль     — имя, ник, «о себе», канал, аватарка
    поведение   — рассылка, первое сообщение, не участник, реакции
    репутация   — CAS, наказания в сетке, история здесь

Главные правила, ради которых всё затевалось:
    * внутри семьи сигналы не складываются — берём сильнейший плюс четверть
      от второго. Одна фраза, найденная тремя способами, остаётся одной уликой;
    * наказание требует двух разных семей. Одной, какой бы сильной ни была,
      хватает только на удаление;
    * из одних догадок наказание не складывается — нужен проверяемый факт;
    * аргументы за человека (давно в чате, ответил реплаем) не просто снижают
      оценку, а снимают бан со стола.
"""
import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

from .. import config

logger = logging.getLogger("gremlin.verdict")

# Семьи. Порядок важен только для показа.
FAMILIES = ("content", "profile", "behavior", "reputation")
FAMILY_NAMES = {
    "content": "содержание",
    "profile": "профиль",
    "behavior": "поведение",
    "reputation": "репутация",
}

# Что делаем по итогу. Порядок — по возрастанию строгости.
ACTIONS = ("clean", "delete", "mute", "ban")


@dataclass
class Signal:
    """Одна улика: откуда, что именно и насколько это тянет.

    guess — догадка модели (нейрофильтр, смысловая близость, аватарка) или
    проверяемый факт (слово нашлось, аккаунт в списке спамеров). Разница
    решает: на одних догадках наказывать нельзя, они ошибаются молча.
    """
    family: str
    name: str
    score: int
    guess: bool = False


@dataclass
class Verdict:
    total: int = 0
    action: str = "clean"
    families: dict[str, int] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    excuses: list[str] = field(default_factory=list)   # аргументы за человека
    notes: list[str] = field(default_factory=list)     # поправки за обстоятельства
    limits: list[str] = field(default_factory=list)    # что ограничило меру
    raw: int = 0                                       # до множителей

    @property
    def reasons(self) -> list[str]:
        return [s.name for s in self.signals if s.score > 0]

    def line(self) -> str:
        """Короткая строка разбора — для теневого лога и калибровки."""
        fam = " ".join(f"{FAMILY_NAMES[k]}={v}" for k, v in self.families.items() if v)
        out = f"{self.action} {self.total} (сырое {self.raw}) · {fam}"
        if self.notes:
            out += " · поправки: " + ", ".join(self.notes)
        if self.excuses:
            out += " · за человека: " + ", ".join(self.excuses)
        if self.limits:
            out += " · мера ограничена: " + ", ".join(self.limits)
        return out


def family_score(signals: list[Signal]) -> int:
    """Оценка семьи: сильнейший сигнал плюс четверть второго.

    Не сумма: стоп-слово, смысловая фраза и нейрофильтр смотрят на один и тот
    же текст, и складывать их — считать одну улику трижды. Но и не чистый
    максимум: они смотрят по-разному, и совпавшее второе мнение чего-то стоит.
    """
    if not signals:
        return 0
    vals = sorted((s.score for s in signals), reverse=True)
    total = vals[0]
    if len(vals) > 1:
        total += vals[1] // 4
    return min(total, 100)


def _multiplier(ctx: dict) -> tuple[float, list[str]]:
    """Поправка к сумме за то, кто и как пишет. Возвращает (множитель, что учли)."""
    mult, notes = 1.0, []
    lvl = ctx.get("trust")
    if lvl is not None:
        # ветеран чата и гость с одинаковым текстом — разные истории
        step = config.UNI_TRUST_MULT.get(lvl)
        if step and step != 1.0:
            mult *= step
            notes.append(f"доверие ×{step}")
    if ctx.get("guest"):
        mult *= config.UNI_GUEST_MULT
        notes.append(f"не в чате ×{config.UNI_GUEST_MULT}")
    if ctx.get("reply_to_other"):
        mult *= config.UNI_REPLY_MULT
        notes.append(f"ответ человеку ×{config.UNI_REPLY_MULT}")
    if not ctx.get("outward"):
        # Реклама всегда даёт способ связи: ссылку, ник, канал, кнопку,
        # «пиши в лс». Без этого текст рекламой быть почти не может — но
        # «почти»: вербовка вида «отвечу в комментариях» контактов не даёт,
        # поэтому это сильный минус, а не потолок.
        mult *= config.UNI_NO_EXIT_MULT
        notes.append(f"нет выхода наружу ×{config.UNI_NO_EXIT_MULT}")
    elif ctx.get("outward_note"):
        notes.append(ctx["outward_note"])
    return mult, notes


def decide(signals: list[Signal], ctx: dict, suspect: int, punish: int) -> Verdict:
    """Собрать вердикт из улик и обстоятельств.

    ctx: trust (уровень или None), guest, reply_to_other, outward (есть ли
    в сообщении выход наружу), excuses (список аргументов за человека).
    """
    v = Verdict(signals=[s for s in signals if s.score > 0])
    by_family: dict[str, list[Signal]] = {}
    for s in v.signals:
        by_family.setdefault(s.family, []).append(s)
    v.families = {f: family_score(by_family[f]) for f in FAMILIES if f in by_family}
    v.raw = sum(v.families.values())

    # множители и ограничения меры — разные вещи: «не в чате ×1.25» оценку
    # поднимает, а «сработала одна семья» опускает приговор. В одной куче
    # лог читался наоборот
    mult, notes = _multiplier(ctx)
    v.notes += notes
    v.total = max(0, min(int(round(v.raw * mult)), 100))

    v.excuses = list(ctx.get("excuses") or [])
    hard = [s for s in v.signals if not s.guess]
    families_hit = sum(1 for score in v.families.values() if score >= config.UNI_FAMILY_MIN)

    # Мера по очкам, дальше её опускают правила. Ниже порога подозрения —
    # «чисто»: очки полежат в копилке. Удалять сообщение за один слабый
    # признак нельзя, удаление человек замечает не меньше мута.
    if v.total >= punish:
        v.action = "ban"
    elif v.total >= suspect:
        v.action = "mute"
    else:
        return v

    cap = "ban"
    if families_hit < 2:
        cap = "delete"
        v.limits.append("сработала одна семья")
    if not hard:
        cap = _softer(cap, "delete")
        v.limits.append("только догадки моделей")
    if not ctx.get("outward"):
        cap = _softer(cap, "mute")
        v.limits.append("без выхода наружу бан не выдаём")
    if v.excuses:
        cap = _softer(cap, "mute")
    v.action = _softer(v.action, cap)
    return v


def _softer(a: str, b: str) -> str:
    """Мягчайшее из двух действий."""
    return ACTIONS[min(ACTIONS.index(a), ACTIONS.index(b))]

# ---------- сбор улик ----------
#
# Правила считают своё и так, по ходу модерации. Здесь мы не считаем заново,
# а переводим уже посчитанное на общую шкалу: каждый сигнал получает свой
# потолок и метку «факт или догадка». Второй раз в Telegram не ходим — иначе
# теневой прогон удвоил бы нагрузку на ровном месте.


def _cap(value: int, top: int) -> int:
    """Перевести чужие очки в наш потолок, не превысив его."""
    return max(0, min(int(value), top))


def content_signals(*, stopword: str | None = None,
                    stopword_weight: int | None = None,
                    phrase: str | None = None,
                    nn_score: int | None = None, text_hard: int = 0,
                    text_cosmetic: int = 0, text_why: list | None = None,
                    outward: bool = False) -> list[Signal]:
    """Семья «содержание»: всё, что сказано в самом сообщении.

    stopword_weight — сколько это слово значит. У списка слов три сорта:
    «онлифанс» в живой речи не встречается, а «оплата» и «пиши» встречаются
    каждый день, и одинаковый вес делал из вторых мины.
    """
    out = []
    if stopword:
        out.append(Signal("content", f"стоп-слово: «{stopword}»",
                          _cap(stopword_weight if stopword_weight
                               else config.UNI_W_STOPWORD,
                               config.UNI_W_STOPWORD)))
    if phrase:
        out.append(Signal("content", "смысловое совпадение",
                          config.UNI_W_PHRASE, guess=True))
    if nn_score:
        # нейрофильтр отдаёт процент сходства — переводим в свой потолок
        out.append(Signal("content", f"нейрофильтр ({nn_score}%)",
                          _cap(nn_score * config.UNI_W_NN // 100,
                               config.UNI_W_NN), guess=True))
    if text_hard:
        why = ", ".join(text_why or []) or "признаки рекламы в тексте"
        out.append(Signal("content", why, _cap(text_hard, config.UNI_W_TEXT_HARD)))
    if text_cosmetic and (text_hard or outward):
        # Кривопись сама по себе — манера письма. Уликой она становится только
        # рядом с настоящим сигналом: «прив3т» пишут и обычные люди.
        out.append(Signal("content", "кривой текст",
                          _cap(text_cosmetic, config.UNI_W_TEXT_COSMETIC)))
    return out


def profile_signals(*, word: str | None = None, word_weight: int | None = None,
                    face: int | None = None,
                    name_hard: int = 0, name_why: list | None = None,
                    photo: int | None = None) -> list[Signal]:
    """Семья «профиль»: кто это по описанию, а не по сообщению."""
    out = []
    if word:
        out.append(Signal("profile", f"в профиле: «{word}»",
                          _cap(word_weight if word_weight
                               else config.UNI_W_PROF_WORD,
                               config.UNI_W_PROF_WORD)))
    if face:
        out.append(Signal("profile", f"похож на забаненных ({face}%)",
                          _cap(face * config.UNI_W_PROF_FACE // 100,
                               config.UNI_W_PROF_FACE), guess=True))
    if name_hard:
        why = ", ".join(name_why or []) or "реклама в имени"
        out.append(Signal("profile", why, _cap(name_hard, config.UNI_W_PROF_NAME)))
    if photo:
        # Аватарка не участвует в максимуме семьи наравне с текстом: она
        # ошибается чаще всех и годится только как добавка. Поэтому потолок
        # у неё маленький и метка «догадка».
        out.append(Signal("profile", f"откровенная аватарка ({photo}%)",
                          config.UNI_W_PROF_PHOTO, guess=True))
    return out


def behavior_signals(*, burst: bool = False, first_message: bool = False,
                     reaction_only: bool = False) -> list[Signal]:
    """Семья «поведение»: как человек себя ведёт, независимо от текста."""
    out = []
    if burst:
        out.append(Signal("behavior", "то же сообщение в других чатах",
                          config.UNI_W_BURST))
    if first_message:
        out.append(Signal("behavior", "первое сообщение в чате",
                          config.UNI_W_FIRST_MSG))
    if reaction_only:
        out.append(Signal("behavior", "только реакции, без сообщений",
                          config.UNI_W_REACTION))
    return out


def reputation_signals(*, cas: bool = False, net: bool = False,
                       punished: int = 0) -> list[Signal]:
    """Семья «репутация»: что о человеке известно помимо этого сообщения."""
    out = []
    if cas:
        out.append(Signal("reputation", "в общем списке спамеров",
                          config.UNI_W_CAS))
    if net:
        out.append(Signal("reputation", "наказан в соседнем чате сетки",
                          config.UNI_W_NET))
    if punished:
        out.append(Signal("reputation", f"наказаний здесь: {punished}",
                          _cap(punished * 12, config.UNI_W_HISTORY)))
    return out


def excuses_for(*, msgs: int = 0, days: int = 0, reply_to_other: bool = False,
                member: bool = True) -> list[str]:
    """Аргументы за человека. Они не уменьшают оценку — они запрещают бан.

    Смысл: ошибка фильтра на своём участнике дороже, чем пропущенная реклама.
    Пропущенное поймается на следующем сообщении, а несправедливый бан человек
    запоминает надолго.
    """
    out = []
    if member and msgs >= config.UNI_EXCUSE_MSGS:
        out.append(f"{msgs} своих сообщений в чате")
    if member and days >= config.UNI_EXCUSE_DAYS:
        out.append(f"в чате {days} дн.")
    if reply_to_other:
        out.append("ответ на чужое сообщение")
    return out

# ---------- обстоятельства ----------

# Выход наружу: способ увести человека из чата. Реклама без него не работает,
# поэтому его отсутствие — сильный довод в пользу того, что это разговор.
_OUTWARD = re.compile(
    r"https?://|www\.|t\.me/|telegra\.ph"
    r"|@[A-Za-z][\w]{3,}"                       # чужой ник или канал
    r"|\bв\s*л[сc]\b|\bв\s*личк|\bпиши(?:те)?\s+в\s+л"
    r"|\bнапиши(?:те)?\s+мне\b"
    r"|\+7\s*\d|\b8\s?\(?9\d{2}",               # телефон
    re.IGNORECASE)


def has_outward(text: str, buttons: list | None = None) -> bool:
    """Есть ли в сообщении способ связаться или уйти по ссылке."""
    if buttons:
        return True
    return bool(_OUTWARD.search(text or ""))


def profile_outward(data: dict | None) -> bool:
    """Есть ли выход наружу в самом профиле: канал или контакт в описании.

    У спам-аккаунта сообщение бывает совсем пустым («Остался всего один…»),
    а реклама висит в профиле — канал и «кончи со мной» в его описании.
    Искать выход только в сообщении значило резать такой профиль вдвое.
    """
    if not data:
        return False
    if data.get("channel_title") or data.get("channel_username"):
        return True
    return bool(_OUTWARD.search(" ".join([data.get("bio") or "",
                                          data.get("channel_desc") or ""])))


async def context(chat_id: int, user, message=None, *, lvl=None,
                  guest: bool = False, text: str = "",
                  buttons: list | None = None) -> dict:
    """Собрать обстоятельства: кто пишет, кому и есть ли выход наружу."""
    from .. import db

    reply = getattr(message, "reply_to_message", None) if message else None
    reply_author = getattr(reply, "from_user", None) if reply else None
    # ответ самому себе разговором не считается: так дробят длинную рассылку
    reply_to_other = bool(reply_author and reply_author.id != user.id
                          and not getattr(reply_author, "is_bot", False))
    facts = await db.trust_facts(chat_id, user.id)
    return {
        "trust": lvl,
        "guest": guest,
        "reply_to_other": reply_to_other,
        "outward": has_outward(text, buttons),
        "excuses": excuses_for(msgs=facts["msgs"], days=facts["days"],
                               reply_to_other=reply_to_other, member=not guest),
        "facts": facts,
    }


# ---------- теневая запись ----------

def log_path(chat_id: int) -> str:
    os.makedirs(config.UNI_LOG_DIR, exist_ok=True)
    return os.path.join(config.UNI_LOG_DIR, f"{chat_id}.log")


def _append(chat_id: int, line: str) -> None:
    try:
        with open(log_path(chat_id), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        logger.warning("не записать вердикт по %s", chat_id, exc_info=True)


def _one_line(text: str | None, limit: int = 160) -> str:
    out = " ".join((text or "").split())
    return out[:limit] + ("…" if len(out) > limit else "")


async def shadow(chat, user, settings, *, signals, ctx, text: str = "",
                 was: str | None = None) -> "Verdict | None":
    """Посчитать вердикт и записать его, ничего не делая.

    was — что на самом деле сделали старые правила. Ради этой пары чисел всё
    и затевается: неделя таких строк покажет, где новая оценка расходится
    со старой, и по ним будем двигать пороги, а не гадать.
    """
    if settings.uni_mode < 1:
        return None
    v = decide(signals, ctx, settings.uni_suspect, settings.uni_ban)
    if v.action == "clean" and not v.signals:
        return v                      # чистое сообщение писать незачем
    stamp = time.strftime("%d.%m %H:%M:%S")
    lines = [
        f"[{stamp}] {v.line()}",
        f"    было: {was or 'ничего'} · автор {user.id}"
        + (f" · доверие {ctx.get('trust')}" if ctx.get("trust") is not None else "")
        + (" · не в чате" if ctx.get("guest") else "")
        + (" · бот" if getattr(user, "is_bot", False) else " · человек"),
    ]
    for s in v.signals:
        mark = "?" if s.guess else "!"
        lines.append(f"    {mark} [{FAMILY_NAMES[s.family]}] {s.name} = {s.score}")
    if text:
        lines.append(f"    > {_one_line(text)}")
    await asyncio.to_thread(_append, chat.id, "\n".join(lines) + "\n\n")

    from .. import db
    try:
        await db.verdict_add(
            chat.id, user.id, v.total, v.raw, v.action, was,
            json.dumps(v.families, ensure_ascii=False),
            json.dumps([[s.family, s.name, s.score, s.guess] for s in v.signals],
                       ensure_ascii=False),
            text)
    except Exception:
        logger.warning("вердикт не лёг в журнал", exc_info=True)
    return v
