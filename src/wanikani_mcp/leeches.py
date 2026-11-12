"""Leech scoring.

Community formula (from the WaniKani leech-training scripts):

    score = incorrect / max(current_streak, 1) ** EXPONENT

worked out separately for meaning and reading. An item's score is the worse of the
two, and an item with score >= THRESHOLD counts as a leech. Radicals and kana-only
vocabulary have no reading question, so only their meaning score counts.

To try a different formula, change ``leech_score`` or set the environment variables
``WANIKANI_LEECH_EXPONENT`` / ``WANIKANI_LEECH_THRESHOLD``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

HAS_READING = {"kanji", "vocabulary"}


@dataclass(frozen=True)
class LeechConfig:
    exponent: float = 1.5
    threshold: float = 1.0

    @classmethod
    def from_env(cls) -> LeechConfig:
        return cls(
            exponent=float(os.environ.get("WANIKANI_LEECH_EXPONENT", cls.exponent)),
            threshold=float(os.environ.get("WANIKANI_LEECH_THRESHOLD", cls.threshold)),
        )


def leech_score(incorrect: int, current_streak: int, config: LeechConfig = LeechConfig()) -> float:
    """Higher means worse. A streak of 0 (just missed) is treated as 1."""
    return incorrect / max(current_streak, 1) ** config.exponent


@dataclass(frozen=True)
class ItemScore:
    meaning: float
    reading: float | None

    @property
    def worst(self) -> float:
        return max(self.meaning, self.reading or 0.0)

    @property
    def weak_side(self) -> str:
        if self.reading is not None and self.reading > self.meaning:
            return "reading"
        return "meaning"


def score_item(stats: dict[str, Any], config: LeechConfig = LeechConfig()) -> ItemScore:
    """Score one review_statistics row (a dict with the API's field names)."""
    meaning = leech_score(stats["meaning_incorrect"], stats["meaning_current_streak"], config)
    reading = None
    if stats["subject_type"] in HAS_READING:
        reading = leech_score(stats["reading_incorrect"], stats["reading_current_streak"], config)
    return ItemScore(meaning=meaning, reading=reading)


def is_leech(score: ItemScore, config: LeechConfig = LeechConfig()) -> bool:
    return score.worst >= config.threshold


SRS_STAGE_NAMES = {
    0: "Lesson", 1: "Apprentice I", 2: "Apprentice II", 3: "Apprentice III", 4: "Apprentice IV",
    5: "Guru I", 6: "Guru II", 7: "Master", 8: "Enlightened", 9: "Burned",
}

ITEM_TYPES = ("radical", "kanji", "vocabulary", "kana_vocabulary")


def current_leeches(
    db: Any,
    config: LeechConfig = LeechConfig(),
    item_type: str | None = None,
    include_burned: bool = False,
) -> list[dict[str, Any]]:
    """All items at or above the leech threshold, worst first.

    Hidden subjects are skipped. Burned items are skipped by default because they no
    longer come up in reviews, so drilling them has no payoff.
    """
    sql = """
        SELECT rs.*, s.characters, s.slug, s.level, a.srs_stage, a.burned_at
        FROM review_statistics rs
        JOIN subjects s ON s.id = rs.subject_id
        LEFT JOIN assignments a ON a.subject_id = rs.subject_id
        WHERE rs.hidden = 0 AND s.hidden = 0
    """
    params: list[Any] = []
    if item_type:
        sql += " AND rs.subject_type = ?"
        params.append(item_type)
    if not include_burned:
        sql += " AND (a.burned_at IS NULL)"
    leeches = []
    for row in db.query(sql, tuple(params)):
        stats = dict(row)
        score = score_item(stats, config)
        if not is_leech(score, config):
            continue
        has_reading = score.reading is not None
        leeches.append({
            "subject_id": stats["subject_id"],
            "characters": stats["characters"] or stats["slug"],
            "type": stats["subject_type"],
            "level": stats["level"],
            "srs_stage": SRS_STAGE_NAMES.get(stats["srs_stage"], stats["srs_stage"]),
            "score": round(score.worst, 2),
            "weak_side": score.weak_side,
            "meaning": {
                "score": round(score.meaning, 2),
                "incorrect": stats["meaning_incorrect"],
                "current_streak": stats["meaning_current_streak"],
            },
            "reading": {
                "score": round(score.reading, 2),
                "incorrect": stats["reading_incorrect"],
                "current_streak": stats["reading_current_streak"],
            } if has_reading else None,
            "percentage_correct": stats["percentage_correct"],
        })
    leeches.sort(key=lambda item: (-item["score"], item["subject_id"]))
    return leeches
