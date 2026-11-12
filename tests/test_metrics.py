import pytest

from wanikani_mcp.metrics import Recorder, format_stats, stats
from wanikani_mcp.sync import sync


def test_stats_on_empty_database(db):
    data = stats(db)
    assert data["sessions"]["count"] == 0
    assert data["progress"] is None
    assert "no syncs yet" in format_stats(data)


def test_recorder_logs_success_and_failure(db):
    recorder = Recorder(db)
    with recorder.track("find_leeches") as call:
        call.items = 7
    with pytest.raises(ValueError), recorder.track("explain_item"):
        raise ValueError("boom")
    data = stats(db)
    assert data["sessions"]["count"] == 1
    assert data["tool_calls"]["total"] == 2
    assert data["tool_calls"]["error_rate_percent"] == 50.0
    assert data["leeches"]["surfaced_by_find_leeches"] == 7
    assert data["estimated_minutes_saved"] == 5.0  # failed calls save nothing


def test_check_text_flag_rate(db):
    recorder = Recorder(db)
    recorder.log_check_text({"kanji_total": 10, "unique_kanji": 8, "unknown_count": 2}, 1)
    recorder.log_check_text({"kanji_total": 12, "unique_kanji": 9, "unknown_count": 0}, 1)
    ct = stats(db)["check_text"]
    assert ct == {"runs": 2, "runs_with_unknown_kanji": 1, "flag_rate_percent": 50.0,
                  "unknown_kanji_flagged": 2, "kanji_checked": 22}


async def test_progress_compares_first_and_latest_sync(fake_api, client, synced_db):
    for record in fake_api.data["review_statistics"]["data"]:
        if record["data"]["subject_id"] == 788:  # 労: cure the leech with 1 more correct answer
            record["data"].update(meaning_current_streak=4, meaning_correct=record["data"]["meaning_correct"] + 1)
            record["data_updated_at"] = "2026-11-12T09:00:00.000000Z"
    await sync(client, synced_db)
    progress = stats(synced_db)["progress"]
    assert progress["syncs"] == 2
    assert progress["leech_count"] == {"first": 7, "latest": 6, "change": -1}
    assert progress["leeches_cured_since_first_sync"] == 1
    assert progress["reviews_since_first_sync"] == 1
    assert progress["accuracy_percent"]["latest"] > progress["accuracy_percent"]["first"]
    assert "7 -> 6" in format_stats(stats(synced_db))
