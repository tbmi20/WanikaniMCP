import pytest

from wanikani_mcp.items import ItemNotFound, explain_item, known_items, plain, resolve


def test_resolve_by_characters_id_and_radical_slug(synced_db):
    assert resolve(synced_db, "静")[0]["id"] == 1052
    assert resolve(synced_db, "1052")[0]["id"] == 1052
    assert resolve(synced_db, "heavy")[0]["id"] == 188
    with pytest.raises(ItemNotFound):
        resolve(synced_db, "猫")


def test_explain_kanji_has_everything_for_root_cause_analysis(synced_db):
    info = explain_item(synced_db, "静")
    assert info["meanings"]["primary"] == "Quiet"
    assert info["readings"]["taught_reading_type"] in {"onyomi", "kunyomi"}
    assert any(r.startswith("せい") for r in info["readings"]["onyomi"])
    assert [c["characters"] for c in info["components"]] == ["青", "争"]
    similar = {s["characters"] for s in info["visually_similar_kanji"]}
    assert {"清", "精", "情", "請"} <= similar
    assert all("my_srs_stage" in s and "my_leech_score" in s for s in info["visually_similar_kanji"])
    mine = info["my_stats"]
    assert mine["srs_stage"] == "Apprentice II"
    assert mine["meaning"]["incorrect"] == 16
    assert mine["leech_score"]["is_leech"] is True
    assert mine["leech_score"]["weak_side"] == "meaning"
    assert info["mnemonics"]["meaning"] and "<radical>" not in info["mnemonics"]["meaning"]


def test_explain_vocabulary_lists_kanji_components_and_context(synced_db):
    info = explain_item(synced_db, "労働")
    assert [c["characters"] for c in info["components"]] == ["労", "働"]
    assert isinstance(info["readings"], list) and "ろうどう" in info["readings"]
    assert info["context_sentences"]


def test_explain_includes_personal_synonyms(synced_db):
    assert explain_item(synced_db, "労")["my_notes"]["synonyms"] == ["hard work"]


def test_plain_strips_wanikani_markup():
    assert plain("The <radical>moon</radical> is <reading>げつ</reading>") == "The moon is げつ"


def test_known_items_by_stage(synced_db):
    started = known_items(synced_db, min_srs_stage=1)
    assert started["kanji_count"] == 15
    assert "静" in started["kanji"]
    guru = known_items(synced_db, min_srs_stage=5)
    assert "静" not in guru["kanji"]
    assert "労働 (Manual Labor)" not in guru["vocabulary"]  # Apprentice III
    assert "しかし (However)" in guru["vocabulary"]


def test_known_items_caps_vocabulary(synced_db):
    result = known_items(synced_db, item_type="vocabulary", vocabulary_limit=1)
    assert "kanji" not in result
    assert result["vocabulary_count"] == 3
    assert len(result["vocabulary"]) == 1 and result["vocabulary_truncated"] is True
