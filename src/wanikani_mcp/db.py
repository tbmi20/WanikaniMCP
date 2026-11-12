"""Local SQLite cache and evidence log.

The cache holds the user's WaniKani data so tools answer without hitting the API.
The evidence tables (sync_runs, *_snapshots, tool_calls, check_text_runs) record
usage for the ``stats`` command. Nothing here ever stores the API token, and the
only user field kept is the level.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

DB_ENV_VAR = "WANIKANI_MCP_DB"
DEFAULT_PATH = Path.home() / ".wanikani-mcp" / "cache.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY,
    object TEXT NOT NULL,
    level INTEGER,
    characters TEXT,
    slug TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subjects_characters ON subjects(characters);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL,
    subject_type TEXT NOT NULL,
    srs_stage INTEGER NOT NULL,
    unlocked_at TEXT,
    started_at TEXT,
    passed_at TEXT,
    burned_at TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assignments_subject ON assignments(subject_id);

CREATE TABLE IF NOT EXISTS review_statistics (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL,
    subject_type TEXT NOT NULL,
    meaning_correct INTEGER NOT NULL,
    meaning_incorrect INTEGER NOT NULL,
    meaning_current_streak INTEGER NOT NULL,
    meaning_max_streak INTEGER NOT NULL,
    reading_correct INTEGER NOT NULL,
    reading_incorrect INTEGER NOT NULL,
    reading_current_streak INTEGER NOT NULL,
    reading_max_streak INTEGER NOT NULL,
    percentage_correct INTEGER NOT NULL,
    hidden INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_statistics_subject ON review_statistics(subject_id);

CREATE TABLE IF NOT EXISTS study_materials (
    id INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL UNIQUE,
    meaning_note TEXT,
    reading_note TEXT,
    meaning_synonyms TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    level INTEGER NOT NULL,
    max_level_granted INTEGER,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_state (
    endpoint TEXT PRIMARY KEY,
    last_updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    full_sync INTEGER NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS leech_snapshots (
    sync_run_id INTEGER NOT NULL REFERENCES sync_runs(id),
    taken_at TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    subject_type TEXT NOT NULL,
    characters TEXT,
    meaning_score REAL NOT NULL,
    reading_score REAL,
    score REAL NOT NULL,
    PRIMARY KEY (sync_run_id, subject_id)
);

CREATE TABLE IF NOT EXISTS accuracy_snapshots (
    sync_run_id INTEGER PRIMARY KEY REFERENCES sync_runs(id),
    taken_at TEXT NOT NULL,
    items INTEGER NOT NULL,
    correct INTEGER NOT NULL,
    incorrect INTEGER NOT NULL,
    leech_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    client TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    called_at TEXT NOT NULL,
    duration_ms REAL NOT NULL,
    item_count INTEGER,
    ok INTEGER NOT NULL,
    error_type TEXT
);

CREATE TABLE IF NOT EXISTS check_text_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    kanji_total INTEGER NOT NULL,
    unique_kanji INTEGER NOT NULL,
    unknown_count INTEGER NOT NULL,
    min_srs_stage INTEGER NOT NULL
);
"""


def default_path() -> Path:
    return Path(os.environ.get(DB_ENV_VAR) or DEFAULT_PATH).expanduser()


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        if path is None:
            path = default_path()
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.one(sql, params)
        return row[0] if row else None

    # ----- cache upserts -------------------------------------------------

    def existing_ids(self, table: str) -> set[int]:
        return {row[0] for row in self.query(f"SELECT id FROM {table}")}  # noqa: S608 - fixed table names

    def upsert_subjects(self, records: list[dict[str, Any]]) -> None:
        self.conn.executemany(
            """INSERT OR REPLACE INTO subjects (id, object, level, characters, slug, hidden, data, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    r["id"], r["object"], r["data"].get("level"), r["data"].get("characters"),
                    r["data"].get("slug"), int(r["data"].get("hidden_at") is not None),
                    json.dumps(r["data"], ensure_ascii=False), r["data_updated_at"],
                )
                for r in records
            ],
        )

    def upsert_assignments(self, records: list[dict[str, Any]]) -> None:
        self.conn.executemany(
            """INSERT OR REPLACE INTO assignments
               (id, subject_id, subject_type, srs_stage, unlocked_at, started_at, passed_at, burned_at, hidden, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    r["id"], d["subject_id"], d["subject_type"], d["srs_stage"], d.get("unlocked_at"),
                    d.get("started_at"), d.get("passed_at"), d.get("burned_at"), int(d.get("hidden", False)),
                    r["data_updated_at"],
                )
                for r in records
                for d in [r["data"]]
            ],
        )

    def upsert_review_statistics(self, records: list[dict[str, Any]]) -> None:
        self.conn.executemany(
            """INSERT OR REPLACE INTO review_statistics
               (id, subject_id, subject_type, meaning_correct, meaning_incorrect, meaning_current_streak,
                meaning_max_streak, reading_correct, reading_incorrect, reading_current_streak,
                reading_max_streak, percentage_correct, hidden, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    r["id"], d["subject_id"], d["subject_type"], d["meaning_correct"], d["meaning_incorrect"],
                    d["meaning_current_streak"], d["meaning_max_streak"], d["reading_correct"],
                    d["reading_incorrect"], d["reading_current_streak"], d["reading_max_streak"],
                    d["percentage_correct"], int(d.get("hidden", False)), r["data_updated_at"],
                )
                for r in records
                for d in [r["data"]]
            ],
        )

    def upsert_study_materials(self, records: list[dict[str, Any]]) -> None:
        self.conn.executemany(
            """INSERT OR REPLACE INTO study_materials
               (id, subject_id, meaning_note, reading_note, meaning_synonyms, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    r["id"], d["subject_id"], d.get("meaning_note"), d.get("reading_note"),
                    json.dumps(d.get("meaning_synonyms") or [], ensure_ascii=False), r["data_updated_at"],
                )
                for r in records
                for d in [r["data"]]
            ],
        )

    def set_user(self, level: int, max_level_granted: int | None, updated_at: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO user_profile (id, level, max_level_granted, updated_at) VALUES (1, ?, ?, ?)",
            (level, max_level_granted, updated_at),
        )

    def get_sync_cursor(self, endpoint: str) -> str | None:
        return self.scalar("SELECT last_updated_at FROM sync_state WHERE endpoint = ?", (endpoint,))

    def set_sync_cursor(self, endpoint: str, updated_at: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sync_state (endpoint, last_updated_at) VALUES (?, ?)", (endpoint, updated_at)
        )

    # ----- lookups -------------------------------------------------------

    def subject(self, subject_id: int) -> dict[str, Any] | None:
        row = self.one("SELECT id, object, data FROM subjects WHERE id = ?", (subject_id,))
        return _subject_from_row(row) if row else None

    def subjects_by_characters(self, characters: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT id, object, data FROM subjects WHERE characters = ? ORDER BY hidden, "
            "CASE object WHEN 'kanji' THEN 0 WHEN 'vocabulary' THEN 1 WHEN 'kana_vocabulary' THEN 2 ELSE 3 END",
            (characters,),
        )
        return [_subject_from_row(r) for r in rows]

    def subjects_by_ids(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.query(f"SELECT id, object, data FROM subjects WHERE id IN ({marks})", tuple(ids))  # noqa: S608
        return {r["id"]: _subject_from_row(r) for r in rows}

    def has_synced(self) -> bool:
        return self.scalar("SELECT COUNT(*) FROM sync_state") > 0


def _subject_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "object": row["object"], **json.loads(row["data"])}
