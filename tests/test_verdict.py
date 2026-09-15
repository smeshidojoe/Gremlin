"""Единая оценка: правила, которые не дают банить своих и пропускать спам."""
from gremlin import config
from gremlin.services import verdict as v

SUSPECT, BAN = 45, 75


def run(signals, **ctx):
    ctx.setdefault("outward", True)
    return v.decide(signals, ctx, SUSPECT, BAN)


def test_one_phrase_found_three_ways_is_one_piece_of_evidence():
    triple = v.content_signals(stopword="в лс", phrase="да", nn_score=90,
                               outward=True)
    assert len(triple) == 3
    tops = sorted((s.score for s in triple), reverse=True)
    # сильнейший плюс четверть второго, а не сумма
    assert v.family_score(triple) == tops[0] + tops[1] // 4
    assert v.family_score(triple) < sum(tops)


def test_stopword_in_conversation_does_nothing():
    """Та самая жалоба: слово есть, но это разговор своего человека."""
    got = run(v.content_signals(stopword="малолетка"), trust=2,
              reply_to_other=True, outward=False,
              excuses=v.excuses_for(msgs=120, days=23, reply_to_other=True))
    assert got.action == "clean"
    assert got.total > 0


def test_below_suspect_is_clean_not_delete():
    got = run(v.content_signals(text_hard=20, text_why=["признак"]), trust=1)
    assert got.total < SUSPECT
    assert got.action == "clean"


def test_one_family_caps_at_delete():
    got = run(v.content_signals(stopword="казино", nn_score=100, text_hard=45,
                                text_why=["telegra.ph-ссылка"]),
              trust=0, guest=True)
    assert got.total >= BAN
    assert got.action == "delete"
    assert "сработала одна семья" in got.limits


def test_guesses_alone_never_punish():
    got = run(v.profile_signals(photo=99, face=100)
              + v.content_signals(nn_score=100), trust=0, guest=True)
    assert got.action in ("clean", "delete")
    assert "только догадки моделей" in got.limits


def test_avatar_alone_is_nothing():
    assert run(v.profile_signals(photo=99), trust=1).action == "clean"


def test_real_spammer_gets_banned():
    got = run(v.content_signals(stopword="заработок", nn_score=88, text_hard=45,
                                text_why=["telegra.ph-ссылка"], outward=True)
              + v.profile_signals(word="пиши в лс", photo=99)
              + v.behavior_signals(first_message=True),
              trust=0, guest=True, outward=True)
    assert got.action == "ban"
    assert len(got.families) == 3


def test_no_outward_exit_forbids_ban_but_still_punishes():
    got = run(v.content_signals(stopword="заработок", nn_score=88)
              + v.profile_signals(word="пиши в лс")
              + v.behavior_signals(first_message=True),
              trust=0, guest=True, outward=False)
    assert got.action == "mute"
    assert "без выхода наружу бан не выдаём" in got.limits


def test_excuses_take_ban_off_the_table():
    base = v.content_signals(stopword="казино") + v.profile_signals(word="в лс")
    assert run(base, trust=0, guest=True).action == "ban"
    got = run(base, trust=0, guest=True,
              excuses=v.excuses_for(msgs=200, days=60))
    assert got.action == "mute"


def test_veteran_scored_softer_than_guest():
    base = v.content_signals(stopword="казино") + v.profile_signals(word="в лс")
    assert run(base, trust=3).total < run(base, trust=0, guest=True).total


def test_cas_is_a_fact_not_a_guess():
    assert run(v.reputation_signals(cas=True) + v.content_signals(nn_score=80),
               trust=0, guest=True).action == "ban"


def test_cosmetic_text_only_counts_next_to_real_signal():
    assert v.content_signals(text_cosmetic=25, outward=False) == []
    assert len(v.content_signals(text_cosmetic=25, outward=True)) == 1


def test_score_never_exceeds_hundred():
    huge = (v.content_signals(stopword="а", nn_score=100, text_hard=99)
            + v.profile_signals(word="б", face=100, name_hard=99, photo=99)
            + v.behavior_signals(burst=True, first_message=True)
            + v.reputation_signals(cas=True, net=True, punished=9))
    got = run(huge, trust=0, guest=True)
    assert got.total == 100
    assert all(x <= 100 for x in got.families.values())


def test_word_weight_changes_contribution():
    strong = v.content_signals(stopword="onlyfans", stopword_weight=45)
    weak = v.content_signals(stopword="оплата", stopword_weight=15)
    assert strong[0].score == 45
    assert weak[0].score == 15
    # без веса — как было, выше потолка не пускаем
    assert v.content_signals(stopword="x")[0].score == config.UNI_W_STOPWORD
    assert v.content_signals(stopword="x", stopword_weight=999)[0].score \
        == config.UNI_W_STOPWORD
    ctx = {"outward": False, "trust": 2, "excuses": []}
    assert v.decide(weak, ctx, SUSPECT, BAN).total \
        < v.decide(strong, ctx, SUSPECT, BAN).total


def test_profile_similarity_is_a_guess():
    """Сходство с копилкой спам-профилей опирается на стартовый набор —
    это догадка модели, а не найденное слово."""
    sig = v.profile_signals(face=88)
    assert sig[0].guess is True
    assert sig[0].family == "profile"


def test_outward_detection():
    assert v.has_outward("зайди на https://t.me/x")
    assert v.has_outward("пиши @annadeals")
    assert v.has_outward("интересно? пиши в лс")
    assert v.has_outward("просто текст", ["https://x"])
    assert not v.has_outward("да нет конечно какая оплата")


def test_log_line_separates_adjustments_from_limits():
    got = run(v.content_signals(stopword="казино"), trust=0, guest=True,
              outward=False)
    line = got.line()
    assert "поправки:" in line
    assert "не в чате" in line
