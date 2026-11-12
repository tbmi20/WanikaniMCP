"""Evidence logging: every tool call is recorded locally (name, time, duration, item
count, success). Arguments, results, API responses and the token are never logged.
check_text records only counts, never the text itself."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import median
from typing import Any

from .db import Database

# Rough minutes a tool call saves compared with doing the same thing by hand on the
# WaniKani site. These are estimates for the "time saved" metric; change them freely.
MINUTES_SAVED = {
    "find_leeches": 5.0,      # scanning item pages for bad accuracy/streaks
    "explain_item": 3.0,      # opening the item, its radicals and similar kanji
    "known_items": 2.0,       # working out which kanji you know
    "check_text": 2.0,        # checking every kanji in a text by hand
    "save_mnemonic": 1.0,
    "sync": 0.0,
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Call:
    items: int | None = None


class Recorder:
    def __init__(self, db: Database, client: str | None = None) -> None:
        self.db = db
        self.session_id = uuid.uuid4().hex
        self._client = client
        self._session_saved = False

    def _ensure_session(self) -> None:
        if not self._session_saved:
            self.db.conn.execute(
                "INSERT OR IGNORE INTO sessions (id, started_at, client) VALUES (?, ?, ?)",
                (self.session_id, _now(), self._client),
            )
            self._session_saved = True

    @contextmanager
    def track(self, tool: str) -> Iterator[Call]:
        """Use as ``with recorder.track("find_leeches") as call: ...; call.items = n``."""
        call = Call()
        start = time.perf_counter()
        error_type: str | None = None
        try:
            yield call
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            self._ensure_session()
            self.db.conn.execute(
                "INSERT INTO tool_calls (session_id, tool, called_at, duration_ms, item_count, ok, error_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.session_id, tool, _now(), round((time.perf_counter() - start) * 1000, 2), call.items,
                 int(error_type is None), error_type),
            )
            self.db.conn.commit()

    def log_check_text(self, result: dict[str, Any], min_srs_stage: int) -> None:
        self._ensure_session()
        self.db.conn.execute(
            "INSERT INTO check_text_runs (session_id, checked_at, kanji_total, unique_kanji, unknown_count, min_srs_stage) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.session_id, _now(), result["kanji_total"], result["unique_kanji"], result["unknown_count"], min_srs_stage),
        )
        self.db.conn.commit()


def stats(db: Database) -> dict[str, Any]:
    sessions = db.one("SELECT COUNT(*) AS n, MIN(started_at) AS first, MAX(started_at) AS last FROM sessions")
    calls = db.query("SELECT tool, duration_ms, ok, item_count FROM tool_calls")
    by_tool: dict[str, dict[str, Any]] = {}
    for c in calls:
        entry = by_tool.setdefault(c["tool"], {"calls": 0, "errors": 0, "durations": []})
        entry["calls"] += 1
        entry["errors"] += 0 if c["ok"] else 1
        entry["durations"].append(c["duration_ms"])
    tools = {
        name: {"calls": e["calls"], "errors": e["errors"], "median_ms": round(median(e["durations"]), 1)}
        for name, e in sorted(by_tool.items())
    }
    total_calls = len(calls)
    errors = sum(1 for c in calls if not c["ok"])

    leeches_surfaced = db.scalar(
        "SELECT COALESCE(SUM(item_count), 0) FROM tool_calls WHERE tool = 'find_leeches' AND ok = 1"
    )
    distinct_leeches = db.scalar("SELECT COUNT(DISTINCT subject_id) FROM leech_snapshots")

    snaps = db.query(
        "SELECT taken_at, items, correct, incorrect, leech_count FROM accuracy_snapshots ORDER BY sync_run_id"
    )
    progress = None
    if snaps:
        first, latest = snaps[0], snaps[-1]

        def acc(row: Any) -> float | None:
            total = row["correct"] + row["incorrect"]
            return round(100 * row["correct"] / total, 2) if total else None

        first_ids = {r[0] for r in db.query(
            "SELECT subject_id FROM leech_snapshots WHERE sync_run_id = (SELECT MIN(sync_run_id) FROM accuracy_snapshots)")}
        latest_ids = {r[0] for r in db.query(
            "SELECT subject_id FROM leech_snapshots WHERE sync_run_id = (SELECT MAX(sync_run_id) FROM accuracy_snapshots)")}
        progress = {
            "syncs": len(snaps),
            "first_sync": first["taken_at"],
            "latest_sync": latest["taken_at"],
            "leech_count": {"first": first["leech_count"], "latest": latest["leech_count"],
                            "change": latest["leech_count"] - first["leech_count"]},
            "leeches_cured_since_first_sync": len(first_ids - latest_ids),
            "new_leeches_since_first_sync": len(latest_ids - first_ids),
            "accuracy_percent": {"first": acc(first), "latest": acc(latest)},
            "reviews_since_first_sync": (latest["correct"] + latest["incorrect"]) - (first["correct"] + first["incorrect"]),
        }

    ct = db.one(
        "SELECT COUNT(*) AS runs, COALESCE(SUM(unknown_count > 0), 0) AS flagged, "
        "COALESCE(SUM(unknown_count), 0) AS unknown, COALESCE(SUM(kanji_total), 0) AS kanji FROM check_text_runs"
    )
    minutes = sum(MINUTES_SAVED.get(c["tool"], 0.0) for c in calls if c["ok"])

    return {
        "sessions": {"count": sessions["n"], "first": sessions["first"], "last": sessions["last"]},
        "tool_calls": {
            "total": total_calls,
            "errors": errors,
            "error_rate_percent": round(100 * errors / total_calls, 1) if total_calls else None,
            "by_tool": tools,
        },
        "leeches": {
            "surfaced_by_find_leeches": leeches_surfaced,
            "distinct_items_ever_flagged": distinct_leeches,
        },
        "progress": progress,
        "check_text": {
            "runs": ct["runs"],
            "runs_with_unknown_kanji": ct["flagged"],
            "flag_rate_percent": round(100 * ct["flagged"] / ct["runs"], 1) if ct["runs"] else None,
            "unknown_kanji_flagged": ct["unknown"],
            "kanji_checked": ct["kanji"],
        },
        "estimated_minutes_saved": round(minutes, 1),
    }


def format_stats(data: dict[str, Any]) -> str:
    s, t, l, c = data["sessions"], data["tool_calls"], data["leeches"], data["check_text"]
    lines = [
        "WaniKani MCP - usage evidence",
        "=" * 30,
        f"Sessions:            {s['count']}" + (f"  ({s['first'][:10]} -> {s['last'][:10]})" if s["count"] else ""),
        f"Tool calls:          {t['total']}  (errors: {t['errors']}"
        + (f", {t['error_rate_percent']}%)" if t["total"] else ")"),
    ]
    for name, e in t["by_tool"].items():
        lines.append(f"  {name:<18} {e['calls']:>4} calls  {e['errors']:>2} errors  median {e['median_ms']} ms")
    lines += [
        f"Leeches surfaced:    {l['surfaced_by_find_leeches']} (distinct items ever flagged: {l['distinct_items_ever_flagged']})",
    ]
    p = data["progress"]
    if p:
        lc, acc = p["leech_count"], p["accuracy_percent"]
        lines += [
            f"Syncs:               {p['syncs']}  ({p['first_sync'][:10]} -> {p['latest_sync'][:10]})",
            f"Leech count:         {lc['first']} -> {lc['latest']}  ({lc['change']:+d}; "
            f"{p['leeches_cured_since_first_sync']} cured, {p['new_leeches_since_first_sync']} new)",
            f"Accuracy:            {acc['first']}% -> {acc['latest']}%  over {p['reviews_since_first_sync']} answers",
        ]
    else:
        lines.append("Progress:            no syncs yet - run `wanikani-mcp sync`")
    lines += [
        f"check_text runs:     {c['runs']}  (flagged unknown kanji in {c['runs_with_unknown_kanji']}"
        + (f", {c['flag_rate_percent']}%)" if c["runs"] else ")"),
        f"Unknown kanji found: {c['unknown_kanji_flagged']} of {c['kanji_checked']} kanji checked",
        f"Time saved (est.):   {data['estimated_minutes_saved']} min",
    ]
    return "\n".join(lines)
