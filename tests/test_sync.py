from conftest import FakeWaniKani
from wanikani_mcp.db import Database
from wanikani_mcp.sync import sync


def _bump(fake: FakeWaniKani, collection: str, subject_id: int, **fields):
    for record in fake.data[collection]["data"]:
        if record["data"]["subject_id"] == subject_id:
            record["data"].update(fields)
            record["data_updated_at"] = "2026-11-12T09:00:00.000000Z"
            return
    raise AssertionError(f"no {collection} record for {subject_id}")


async def test_first_sync_is_full_and_caches_everything(synced_db: Database, first_sync_summary):
    summary = first_sync_summary
    assert summary["full_sync"] is True
    assert summary["level"] == 60
    assert summary["changes"]["subjects"] == {"new": 26, "updated": 0}
    assert summary["changes"]["study_materials"] == {"new": 2, "updated": 0}
    assert summary["leeches"]["count"] == 7
    assert synced_db.scalar("SELECT COUNT(*) FROM subjects") == 26
    assert synced_db.subjects_by_characters("静")[0]["meanings"][0]["meaning"] == "Quiet"


async def test_second_sync_only_fetches_changes(fake_api: FakeWaniKani, client, synced_db: Database):
    _bump(fake_api, "review_statistics", 788, meaning_current_streak=4)   # 6/8 = 0.75, no longer a leech
    _bump(fake_api, "assignments", 1052, srs_stage=3)                      # moved up
    fake_api.requests.clear()

    summary = await sync(client, synced_db)

    collection_requests = [r for r in fake_api.requests if not r.url.path.endswith("/user")]
    assert all("updated_after" in r.url.params for r in collection_requests)
    assert summary["full_sync"] is False
    assert summary["changes"]["review_statistics"] == {"new": 0, "updated": 1}
    assert summary["changes"]["subjects"] == {"new": 0, "updated": 0}
    assert summary["srs_movement"] == {"up": 1, "down": 0, "burned": 0, "newly_started": 0}
    assert summary["leeches"] == {"count": 6, "new_since_last_sync": 0, "resolved_since_last_sync": 1}


async def test_each_sync_saves_a_dated_snapshot(client, synced_db: Database):
    await sync(client, synced_db)
    runs = synced_db.query("SELECT sync_run_id, COUNT(*) FROM leech_snapshots GROUP BY sync_run_id")
    assert [tuple(r) for r in runs] == [(1, 7), (2, 7)]
    acc = synced_db.query("SELECT leech_count, correct, incorrect FROM accuracy_snapshots ORDER BY sync_run_id")
    assert len(acc) == 2 and acc[0]["correct"] > 0


async def test_user_profile_keeps_only_level(synced_db: Database):
    columns = [r[1] for r in synced_db.query("PRAGMA table_info(user_profile)")]
    assert "username" not in columns
    assert synced_db.scalar("SELECT level FROM user_profile") == 60


def test_old_cache_is_migrated_and_assignments_refetched(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE assignments (id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL, subject_type TEXT NOT NULL,
            srs_stage INTEGER NOT NULL, unlocked_at TEXT, started_at TEXT, passed_at TEXT, burned_at TEXT,
            hidden INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        CREATE TABLE sync_state (endpoint TEXT PRIMARY KEY, last_updated_at TEXT NOT NULL);
        INSERT INTO sync_state VALUES ('assignments', '2026-11-01'), ('subjects', '2026-11-01');
    """)
    old.commit()
    old.close()

    db = Database(path)
    columns = {r[1] for r in db.query("PRAGMA table_info(assignments)")}
    assert "available_at" in columns
    assert db.get_sync_cursor("assignments") is None      # full refetch next sync
    assert db.get_sync_cursor("subjects") == "2026-11-01"  # others untouched
