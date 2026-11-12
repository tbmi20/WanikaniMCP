"""Read-only lookups over the local cache: resolving items, explaining them, and
listing what the user already knows."""

from __future__ import annotations

import json
import re
from typing import Any

from .db import Database
from .leeches import SRS_STAGE_NAMES, LeechConfig, is_leech, score_item

_MARKUP = re.compile(r"</?(radical|kanji|vocabulary|reading|meaning|ja)>")


class ItemNotFound(LookupError):
    pass


def plain(text: str | None) -> str | None:
    """Strip WaniKani's highlight tags (<radical>, <kanji>, <reading>…) from mnemonic text."""
    return _MARKUP.sub("", text) if text else text


def resolve(db: Database, item: str | int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Find a subject by id or characters. Returns (best match, other matches).

    Characters like 人 exist as both a kanji and a vocabulary subject; kanji wins and
    the others are returned so the model can ask for them explicitly by id.
    """
    text = str(item).strip()
    if text.isdigit():
        subject = db.subject(int(text))
        if subject:
            return subject, []
    else:
        matches = db.subjects_by_characters(text)
        if not matches:  # radicals without a character are found by slug, e.g. "stick"
            rows = db.query("SELECT id FROM subjects WHERE slug = ?", (text.lower(),))
            matches = list(db.subjects_by_ids([r[0] for r in rows]).values())
        if matches:
            return matches[0], matches[1:]
    raise ItemNotFound(f"No WaniKani item matches {text!r}. Run `sync` first if the cache is empty.")


def _primary(entries: list[dict[str, Any]], key: str) -> str | None:
    for entry in entries:
        if entry.get("primary"):
            return entry[key]
    return entries[0][key] if entries else None


def _assignment(db: Database, subject_id: int) -> dict[str, Any] | None:
    row = db.one("SELECT srs_stage, unlocked_at, burned_at FROM assignments WHERE subject_id = ?", (subject_id,))
    return dict(row) if row else None


def _stats(db: Database, subject_id: int) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM review_statistics WHERE subject_id = ?", (subject_id,))
    return dict(row) if row else None


def _brief(db: Database, subject: dict[str, Any], config: LeechConfig) -> dict[str, Any]:
    out: dict[str, Any] = {
        "subject_id": subject["id"],
        "type": subject["object"],
        "characters": subject.get("characters") or subject.get("slug"),
        "meaning": _primary(subject.get("meanings", []), "meaning"),
    }
    if subject.get("readings"):
        out["reading"] = _primary(subject["readings"], "reading")
    assignment = _assignment(db, subject["id"])
    out["my_srs_stage"] = SRS_STAGE_NAMES.get(assignment["srs_stage"]) if assignment else "Locked"
    stats = _stats(db, subject["id"])
    if stats:
        out["my_leech_score"] = round(score_item(stats, config).worst, 2)
    return out


def explain_item(db: Database, item: str | int, config: LeechConfig = LeechConfig()) -> dict[str, Any]:
    subject, others = resolve(db, item)
    sid = subject["id"]
    result: dict[str, Any] = {
        "subject_id": sid,
        "type": subject["object"],
        "characters": subject.get("characters") or subject.get("slug"),
        "level": subject.get("level"),
        "meanings": {
            "primary": _primary(subject.get("meanings", []), "meaning"),
            "accepted": [m["meaning"] for m in subject.get("meanings", []) if m.get("accepted_answer")],
            "also_accepted": [a["meaning"] for a in subject.get("auxiliary_meanings", []) if a["type"] == "whitelist"],
            "explicitly_wrong": [a["meaning"] for a in subject.get("auxiliary_meanings", []) if a["type"] == "blacklist"],
        },
    }

    readings = subject.get("readings") or []
    if subject["object"] == "kanji":
        grouped: dict[str, list[str]] = {}
        for r in readings:
            label = r["reading"] + (" (primary)" if r.get("primary") else "")
            grouped.setdefault(r["type"], []).append(label)
        result["readings"] = {
            "onyomi": grouped.get("onyomi", []),
            "kunyomi": grouped.get("kunyomi", []),
            "nanori": grouped.get("nanori", []),
            "taught_reading_type": next((r["type"] for r in readings if r.get("primary")), None),
        }
    elif readings:
        result["readings"] = [r["reading"] for r in readings if r.get("accepted_answer")]

    related = db.subjects_by_ids(
        subject.get("component_subject_ids", []) + subject.get("visually_similar_subject_ids", [])
    )
    result["components"] = [
        _brief(db, related[i], config) for i in subject.get("component_subject_ids", []) if i in related
    ]
    if subject["object"] == "kanji":
        result["visually_similar_kanji"] = [
            _brief(db, related[i], config) for i in subject.get("visually_similar_subject_ids", []) if i in related
        ]
        result["vocabulary_using_this_kanji_i_know"] = _known_vocab_using(db, sid)

    assignment = _assignment(db, sid)
    stats = _stats(db, sid)
    mine: dict[str, Any] = {
        "srs_stage": SRS_STAGE_NAMES.get(assignment["srs_stage"]) if assignment else "Locked",
        "burned": bool(assignment and assignment["burned_at"]),
    }
    if stats:
        score = score_item(stats, config)
        mine["meaning"] = {k: stats[f"meaning_{k}"] for k in ("correct", "incorrect", "current_streak", "max_streak")}
        if score.reading is not None:
            mine["reading"] = {k: stats[f"reading_{k}"] for k in ("correct", "incorrect", "current_streak", "max_streak")}
        mine["percentage_correct"] = stats["percentage_correct"]
        mine["leech_score"] = {
            "meaning": round(score.meaning, 2),
            "reading": round(score.reading, 2) if score.reading is not None else None,
            "is_leech": is_leech(score, config),
            "weak_side": score.weak_side,
        }
    result["my_stats"] = mine

    result["mnemonics"] = {
        "meaning": plain(subject.get("meaning_mnemonic")),
        "meaning_hint": plain(subject.get("meaning_hint")),
        "reading": plain(subject.get("reading_mnemonic")),
        "reading_hint": plain(subject.get("reading_hint")),
    }
    notes = db.one("SELECT meaning_note, reading_note, meaning_synonyms FROM study_materials WHERE subject_id = ?", (sid,))
    if notes:
        result["my_notes"] = {
            "meaning_note": notes["meaning_note"] or None,
            "reading_note": notes["reading_note"] or None,
            "synonyms": json.loads(notes["meaning_synonyms"]),
        }
    if subject.get("context_sentences"):
        result["context_sentences"] = subject["context_sentences"][:2]
    if others:
        result["other_items_with_same_characters"] = [{"subject_id": o["id"], "type": o["object"]} for o in others]
    return result


def _known_vocab_using(db: Database, kanji_id: int, limit: int = 5) -> list[dict[str, Any]]:
    kanji = db.subject(kanji_id) or {}
    ids = kanji.get("amalgamation_subject_ids", [])
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = db.query(
        f"SELECT a.subject_id FROM assignments a WHERE a.subject_id IN ({marks}) AND a.srs_stage >= 5 "  # noqa: S608
        "ORDER BY a.srs_stage DESC LIMIT ?",
        (*ids, limit),
    )
    subjects = db.subjects_by_ids([r[0] for r in rows])
    return [
        {
            "characters": s["characters"],
            "meaning": _primary(s.get("meanings", []), "meaning"),
            "reading": _primary(s.get("readings", []), "reading"),
        }
        for s in subjects.values()
    ]


def known_items(
    db: Database, min_srs_stage: int = 1, item_type: str = "all", vocabulary_limit: int = 300
) -> dict[str, Any]:
    """Kanji and vocabulary at or above ``min_srs_stage``.

    0 = unlocked (lesson available), 1 = started (Apprentice I), 5 = Guru, 9 = Burned.
    Kanji come back as one compact string; vocabulary is capped by ``vocabulary_limit``
    (most solid items first) because a level-60 account has thousands.
    """
    out: dict[str, Any] = {"min_srs_stage": SRS_STAGE_NAMES.get(min_srs_stage, min_srs_stage)}
    level = db.scalar("SELECT level FROM user_profile")
    if level is not None:
        out["user_level"] = level
    if item_type in ("all", "kanji"):
        rows = db.query(
            """SELECT s.characters FROM assignments a JOIN subjects s ON s.id = a.subject_id
               WHERE a.subject_type = 'kanji' AND a.srs_stage >= ? AND a.hidden = 0
               ORDER BY s.level, s.id""",
            (min_srs_stage,),
        )
        out["kanji_count"] = len(rows)
        out["kanji"] = "".join(r[0] for r in rows)
    if item_type in ("all", "vocabulary"):
        rows = db.query(
            """SELECT s.characters, s.data FROM assignments a JOIN subjects s ON s.id = a.subject_id
               WHERE a.subject_type IN ('vocabulary', 'kana_vocabulary') AND a.srs_stage >= ? AND a.hidden = 0
               ORDER BY a.srs_stage DESC, s.level, s.id""",
            (min_srs_stage,),
        )
        out["vocabulary_count"] = len(rows)
        out["vocabulary"] = [
            f"{r['characters']} ({_primary(json.loads(r['data']).get('meanings', []), 'meaning')})"
            for r in rows[:vocabulary_limit]
        ]
        out["vocabulary_truncated"] = len(rows) > vocabulary_limit
    return out
