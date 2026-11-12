"""Reviews in context: find WaniKani words in real text, grade answers against
WaniKani's accepted answers, and turn passed items into real WaniKani reviews.

Rules (chosen to protect the SRS from easy passes):
- Kanji and vocabulary count only when BOTH the meaning and the reading (in kana)
  are shown. Radicals and kana-only vocabulary need only the meaning.
- Only items that are due right now can be submitted (a WaniKani API rule).
  Everything else is logged as practice.
- Items with wrong answers are not submitted unless WANIKANI_MCP_SUBMIT_INCORRECT=1,
  in which case they are sent as incorrect reviews, as on the WaniKani site.
- Grades are recorded by the server; the model cannot mark an item as passed except
  through grade_answer, and meanings it judged itself are labelled in the preview.
"""

from __future__ import annotations

import difflib
import json
import math
import re
from datetime import UTC, datetime
from typing import Any

from .db import Database
from .items import resolve
from .leeches import SRS_STAGE_NAMES
from .text import KANJI

READING_REQUIRED = {"kanji", "vocabulary"}
_HIRAGANA = re.compile(r"^[ぁ-ゟー]+$")
_STRIP = re.compile(r"[\s　・.,!?'\"()（）、。！？]")


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def is_due(assignment: dict[str, Any] | None, now: str | None = None) -> bool:
    if not assignment or not assignment.get("available_at"):
        return False
    if assignment.get("burned_at") or not 1 <= assignment["srs_stage"] <= 8:
        return False
    return _parse(assignment["available_at"]) <= _parse(now or now_iso())


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ----- finding WaniKani words in text ----------------------------------------


def _patterns(db: Database) -> dict[str, list[tuple[str, bool, dict[str, Any]]]]:
    """first character -> [(pattern, needs_kana_after, subject)], longest first.

    Vocabulary is matched by its written form. Verbs and i-adjectives conjugate, so
    they are also matched by their stem (dropping the last kana) when kana follows:
    働く matches 働いた, 暗い matches 暗かった.
    """
    rows = db.query(
        """SELECT s.id, s.characters, s.data, a.srs_stage, a.available_at, a.burned_at
           FROM subjects s JOIN assignments a ON a.subject_id = s.id
           WHERE s.object = 'vocabulary' AND s.hidden = 0 AND a.srs_stage >= 1"""
    )
    index: dict[str, list[tuple[str, bool, dict[str, Any]]]] = {}
    for row in rows:
        chars = row["characters"]
        if not chars or not KANJI.search(chars):
            continue
        data = json.loads(row["data"])
        subject = {
            "subject_id": row["id"],
            "characters": chars,
            "meaning": next((m["meaning"] for m in data["meanings"] if m.get("primary")), None),
            "srs_stage": row["srs_stage"],
            "available_at": row["available_at"],
            "burned_at": row["burned_at"],
        }
        index.setdefault(chars[0], []).append((chars, False, subject))
        pos = " ".join(data.get("parts_of_speech", [])).lower()
        conjugates = "verb" in pos or "い adjective" in pos or "i adjective" in pos
        if conjugates and not chars.endswith("する") and _HIRAGANA.match(chars[-1]):
            stem = chars[:-1]
            if KANJI.search(stem):
                index.setdefault(stem[0], []).append((stem, True, subject))
    for entries in index.values():
        entries.sort(key=lambda e: -len(e[0]))
    return index


def find_wanikani_words(db: Database, text: str, now: str | None = None) -> dict[str, Any]:
    index = _patterns(db)
    found: dict[int, dict[str, Any]] = {}
    i = 0
    while i < len(text):
        match = None
        for pattern, needs_kana, subject in index.get(text[i], []):
            if not text.startswith(pattern, i):
                continue
            after = text[i + len(pattern): i + len(pattern) + 1]
            if needs_kana and not (after and "ぁ" <= after <= "ゟ"):
                continue
            match = (pattern, subject)
            break
        if not match:
            i += 1
            continue
        pattern, subject = match
        entry = found.setdefault(subject["subject_id"], {
            "subject_id": subject["subject_id"],
            "characters": subject["characters"],
            "meaning": subject["meaning"],
            "srs_stage": SRS_STAGE_NAMES.get(subject["srs_stage"]),
            "due_now": is_due(subject, now),
            "occurrences": 0,
            "first_seen_as": text[i: i + len(pattern) + 2],
        })
        entry["occurrences"] += 1
        i += len(pattern)

    words = sorted(found.values(), key=lambda w: (not w["due_now"], -w["occurrences"]))
    return {
        "vocabulary_found": len(words),
        "due_now": sum(1 for w in words if w["due_now"]),
        "words": words,
        "note": "Matching is by written form and verb/adjective stems; check each match in context. "
                "Due status is from the last sync; call due_items for live data.",
    }


# ----- grading ------------------------------------------------------------------


def to_hiragana(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def _norm_meaning(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"^(to|a|an|the)\s+", "", text)
    return re.sub(r"[^\w\s]", "", text).strip()


def grade_meaning(subject: dict[str, Any], answer: str, synonyms: list[str]) -> str:
    """correct | incorrect | needs_judgement (the answer may still be right, e.g. Japanese)."""
    given = _norm_meaning(answer)
    if not given:
        return "needs_judgement"
    blacklist = {_norm_meaning(a["meaning"]) for a in subject.get("auxiliary_meanings", []) if a["type"] == "blacklist"}
    if given in blacklist:
        return "incorrect"
    accepted = [m["meaning"] for m in subject.get("meanings", []) if m.get("accepted_answer")]
    accepted += [a["meaning"] for a in subject.get("auxiliary_meanings", []) if a["type"] == "whitelist"]
    accepted += synonyms
    targets = {_norm_meaning(a) for a in accepted}
    if given in targets:
        return "correct"
    if len(given) >= 4 and any(difflib.SequenceMatcher(None, given, t).ratio() >= 0.85 for t in targets):
        return "correct"  # small typos, like WaniKani's own leniency
    return "needs_judgement"


def grade_reading(subject: dict[str, Any], answer: str) -> str:
    """correct | incorrect | not_kana | other_reading (a valid reading WaniKani isn't asking for)."""
    given = to_hiragana(_STRIP.sub("", answer))
    if not given or not _HIRAGANA.match(given):
        return "not_kana"
    readings = subject.get("readings", [])
    if given in {to_hiragana(r["reading"]) for r in readings if r.get("accepted_answer")}:
        return "correct"
    if given in {to_hiragana(r["reading"]) for r in readings}:
        return "other_reading"
    return "incorrect"


def _open_row(db: Database, session_id: str, subject: dict[str, Any]) -> dict[str, Any]:
    row = db.one(
        "SELECT * FROM context_reviews WHERE session_id = ? AND subject_id = ? AND status = 'open'",
        (session_id, subject["id"]),
    )
    if row:
        return dict(row)
    db.conn.execute(
        "INSERT INTO context_reviews (session_id, subject_id, subject_type, created_at, reading_required) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, subject["id"], subject["object"], now_iso(), int(subject["object"] in READING_REQUIRED)),
    )
    return _open_row(db, session_id, subject)


def grade_answer(
    db: Database,
    session_id: str,
    item: str,
    meaning_answer: str | None = None,
    reading_answer: str | None = None,
    model_judgement: bool | None = None,
) -> dict[str, Any]:
    subject, _ = resolve(db, item)
    row = _open_row(db, session_id, subject)
    notes = db.one("SELECT meaning_synonyms FROM study_materials WHERE subject_id = ?", (subject["id"],))
    synonyms = json.loads(notes["meaning_synonyms"]) if notes else []
    result: dict[str, Any] = {"subject_id": subject["id"], "characters": subject.get("characters")}

    if meaning_answer is not None and not row["meaning_passed"]:
        verdict = grade_meaning(subject, meaning_answer, synonyms)
        if verdict == "needs_judgement" and model_judgement is not None:
            verdict = "correct" if model_judgement else "incorrect"
            row["meaning_judged_by_model"] = 1
            result["meaning_judged_by_model"] = True
        if verdict == "correct":
            row["meaning_passed"] = 1
        elif verdict == "incorrect":
            row["incorrect_meaning"] += 1
        result["meaning"] = verdict
        if verdict == "needs_judgement":
            result["meaning_hint"] = (
                "Not an exact match for WaniKani's answers. Decide whether the learner showed they understood "
                "the meaning, then call grade_answer again with model_judgement=true or false."
            )
    if reading_answer is not None and row["reading_required"] and not row["reading_passed"]:
        verdict = grade_reading(subject, reading_answer)
        if verdict == "correct":
            row["reading_passed"] = 1
        elif verdict == "incorrect":
            row["incorrect_reading"] += 1
        result["reading"] = verdict
        if verdict == "not_kana":
            result["reading_hint"] = "Ask the learner to type the reading in kana (hiragana or katakana)."
        elif verdict == "other_reading":
            result["reading_hint"] = "That is a valid reading, but not the one WaniKani teaches here. Ask again."

    db.conn.execute(
        """UPDATE context_reviews SET meaning_passed = ?, reading_passed = ?, incorrect_meaning = ?,
           incorrect_reading = ?, meaning_judged_by_model = ? WHERE id = ?""",
        (row["meaning_passed"], row["reading_passed"], row["incorrect_meaning"], row["incorrect_reading"],
         row["meaning_judged_by_model"], row["id"]),
    )
    db.conn.commit()

    complete = bool(row["meaning_passed"]) and (bool(row["reading_passed"]) or not row["reading_required"])
    result["item_complete"] = complete
    result["incorrect_so_far"] = {"meaning": row["incorrect_meaning"], "reading": row["incorrect_reading"]}
    if not complete:
        missing = [side for side, done in (("meaning", row["meaning_passed"]),
                   ("reading", row["reading_passed"] or not row["reading_required"])) if not done]
        result["still_needed"] = missing
    if result.get("meaning") in ("correct", "incorrect") or result.get("reading") in ("correct", "incorrect"):
        result["accepted_answers"] = {
            "meanings": [m["meaning"] for m in subject.get("meanings", []) if m.get("accepted_answer")],
            "readings": [r["reading"] for r in subject.get("readings", []) if r.get("accepted_answer")],
        }
    return result


# ----- submitting ---------------------------------------------------------------


def expected_stage(current: int, incorrect: int) -> int:
    """WaniKani's SRS rule: +1 if no mistakes, otherwise drop by ceil(incorrect / 2),
    doubled from Guru up, never below Apprentice I."""
    if incorrect == 0:
        return min(current + 1, 9)
    penalty = 2 if current >= 5 else 1
    return max(1, current - math.ceil(incorrect / 2) * penalty)


def plan_reviews(
    db: Database, session_id: str, live: dict[int, dict[str, Any]], submit_incorrect: bool, now: str | None = None
) -> list[dict[str, Any]]:
    """Decide what happens to each open context review. ``live`` maps subject_id to the
    current assignment data fetched from the API."""
    plan = []
    for row in db.query("SELECT * FROM context_reviews WHERE session_id = ? AND status = 'open' ORDER BY id",
                        (session_id,)):
        row = dict(row)
        subject = db.subject(row["subject_id"]) or {}
        assignment = live.get(row["subject_id"])
        incorrect = row["incorrect_meaning"] + row["incorrect_reading"]
        complete = row["meaning_passed"] and (row["reading_passed"] or not row["reading_required"])
        entry = {
            "context_review_id": row["id"],
            "subject_id": row["subject_id"],
            "characters": subject.get("characters"),
            "type": row["subject_type"],
            "incorrect_meaning_answers": row["incorrect_meaning"],
            "incorrect_reading_answers": row["incorrect_reading"],
            "meaning_judged_by_model": bool(row["meaning_judged_by_model"]),
        }
        if not complete:
            entry.update(action="practice", reason="not finished: needs " + (
                "meaning and reading" if not row["meaning_passed"] and row["reading_required"]
                and not row["reading_passed"] else "meaning" if not row["meaning_passed"] else "reading"))
        elif not is_due(assignment, now):
            entry.update(action="practice", reason="not due for review on WaniKani right now")
        elif incorrect and not submit_incorrect:
            entry.update(action="practice", reason="had wrong answers; incorrect reviews are not submitted "
                                                   "(set WANIKANI_MCP_SUBMIT_INCORRECT=1 to change)")
        else:
            stage = assignment["srs_stage"]
            entry.update(
                action="submit_incorrect" if incorrect else "submit_correct",
                assignment_id=assignment["id"],
                srs_change=f"{SRS_STAGE_NAMES[stage]} -> {SRS_STAGE_NAMES[expected_stage(stage, incorrect)]} (expected)",
            )
        plan.append(entry)
    return plan


def mark(db: Database, context_review_id: int, status: str, outcome: str) -> None:
    db.conn.execute(
        "UPDATE context_reviews SET status = ?, outcome = ?, submitted_at = ? WHERE id = ?",
        (status, outcome, now_iso() if status == "submitted" else None, context_review_id),
    )
