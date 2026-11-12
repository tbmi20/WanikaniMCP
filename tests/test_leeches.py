import pytest

from wanikani_mcp.leeches import LeechConfig, current_leeches, is_leech, leech_score, score_item


def stats(subject_type="kanji", mi=0, ms=1, ri=0, rs=1):
    return {
        "subject_type": subject_type,
        "meaning_incorrect": mi, "meaning_current_streak": ms,
        "reading_incorrect": ri, "reading_current_streak": rs,
    }


@pytest.mark.parametrize(
    ("incorrect", "streak", "expected"),
    [
        (8, 4, 1.0),       # 8 / 4^1.5 = 8 / 8
        (16, 1, 16.0),
        (0, 10, 0.0),
        (5, 0, 5.0),       # streak 0 (just missed) is treated as 1, no division by zero
        (27, 9, 1.0),      # 27 / 27
    ],
)
def test_leech_score_formula(incorrect, streak, expected):
    assert leech_score(incorrect, streak) == pytest.approx(expected)


def test_threshold_is_inclusive():
    assert is_leech(score_item(stats(mi=8, ms=4)))          # exactly 1.0
    assert not is_leech(score_item(stats(mi=7, ms=4)))      # 0.875


def test_meaning_and_reading_scored_separately_worst_wins():
    score = score_item(stats(mi=1, ms=9, ri=10, rs=1))
    assert score.meaning == pytest.approx(1 / 27)
    assert score.reading == pytest.approx(10.0)
    assert score.worst == pytest.approx(10.0)
    assert score.weak_side == "reading"


@pytest.mark.parametrize("subject_type", ["radical", "kana_vocabulary"])
def test_types_without_readings_ignore_reading_fields(subject_type):
    score = score_item(stats(subject_type, mi=0, ms=5, ri=50, rs=1))
    assert score.reading is None
    assert not is_leech(score)


def test_formula_is_configurable():
    gentle = LeechConfig(exponent=1.0, threshold=2.0)
    assert leech_score(8, 4, gentle) == pytest.approx(2.0)
    assert is_leech(score_item(stats(mi=8, ms=4), gentle), gentle)
    assert not is_leech(score_item(stats(mi=7, ms=4), gentle), gentle)


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("WANIKANI_LEECH_EXPONENT", "2")
    monkeypatch.setenv("WANIKANI_LEECH_THRESHOLD", "0.5")
    assert LeechConfig.from_env() == LeechConfig(exponent=2.0, threshold=0.5)


def test_current_leeches_from_recorded_data(synced_db):
    leeches = current_leeches(synced_db)
    assert [item["subject_id"] for item in leeches] == [4446, 3270, 1052, 1354, 2319, 188, 788]
    worst = leeches[0]
    assert worst["characters"] == "評論"
    assert worst["score"] == 24.0
    assert worst["weak_side"] == "meaning"
    deal_with = next(item for item in leeches if item["subject_id"] == 1354)
    assert deal_with["reading"] == {"score": 0.68, "incorrect": 10, "current_streak": 6}


def test_current_leeches_filters_by_type(synced_db):
    kanji = current_leeches(synced_db, item_type="kanji")
    assert {item["type"] for item in kanji} == {"kanji"}
    assert [item["characters"] for item in kanji] == ["静", "処", "靖", "労"]


def test_burned_items_are_excluded_unless_requested(synced_db):
    strict = LeechConfig(threshold=0.8)
    ids = {item["subject_id"] for item in current_leeches(synced_db, strict)}
    assert 809 not in ids  # burned 働, score 0.89
    with_burned = {item["subject_id"] for item in current_leeches(synced_db, strict, include_burned=True)}
    assert 809 in with_burned
