from wanikani_mcp.text import check_text, extract_kanji


def test_extract_kanji_ignores_kana_punctuation_and_repeat_mark():
    assert extract_kanji("人々は静かに、ゆっくり歩く。ABC１２３") == ["人", "静", "歩"]


def test_text_with_only_known_kanji_passes(synced_db):
    result = check_text(synced_db, "静かな労働。情は清い。")
    assert result["ok"] is True
    assert result["unknown"] == []
    assert result["kanji_total"] == 5
    assert result["unique_kanji"] == 5


def test_kana_only_text_passes(synced_db):
    result = check_text(synced_db, "しかし、きょうは　いい　てんき　です。")
    assert result == {**result, "ok": True, "kanji_total": 0, "unknown_count": 0}


def test_flags_kanji_outside_the_cache(synced_db):
    result = check_text(synced_db, "静かな部屋で労働する。部屋は暗い。")
    assert result["ok"] is False
    assert [u["kanji"] for u in result["unknown"]] == ["部", "屋"]
    assert result["unknown"][0] == {"kanji": "部", "occurrences": 2, "in_wanikani": False}
    assert result["advice"]


def test_min_srs_stage_raises_the_bar(synced_db):
    # 静 is only Apprentice II in the recorded data, so it fails a Guru-only check.
    result = check_text(synced_db, "静かな労働", min_srs_stage=5)
    assert [u["kanji"] for u in result["unknown"]] == ["静"]
    assert result["unknown"][0] == {
        "kanji": "静", "occurrences": 1, "in_wanikani": True,
        "wanikani_level": 18, "meaning": "Quiet", "my_srs_stage": "Apprentice II",
    }
