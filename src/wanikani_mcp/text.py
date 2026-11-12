"""check_text: the guardrail that keeps generated Japanese within the user's kanji."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from .db import Database
from .leeches import SRS_STAGE_NAMES

# CJK Unified Ideographs, Extension A and Compatibility Ideographs. Kana, punctuation
# and the repetition mark 々 are not matched, so they never count as unknown.
KANJI = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def extract_kanji(text: str) -> list[str]:
    return KANJI.findall(text)


def check_text(db: Database, text: str, min_srs_stage: int = 1) -> dict[str, Any]:
    found = extract_kanji(text)
    counts = Counter(found)
    known_rows = db.query(
        """SELECT s.characters FROM assignments a JOIN subjects s ON s.id = a.subject_id
           WHERE a.subject_type = 'kanji' AND a.srs_stage >= ?""",
        (min_srs_stage,),
    )
    known = {r[0] for r in known_rows}

    unknown: list[dict[str, Any]] = []
    for kanji in counts:  # Counter keeps first-seen order
        if kanji in known:
            continue
        entry: dict[str, Any] = {"kanji": kanji, "occurrences": counts[kanji]}
        row = db.one(
            """SELECT s.id, s.level, s.data, a.srs_stage FROM subjects s
               LEFT JOIN assignments a ON a.subject_id = s.id
               WHERE s.object = 'kanji' AND s.characters = ?""",
            (kanji,),
        )
        if row:
            data = json.loads(row["data"])
            entry["in_wanikani"] = True
            entry["wanikani_level"] = row["level"]
            entry["meaning"] = next((m["meaning"] for m in data.get("meanings", []) if m.get("primary")), None)
            entry["my_srs_stage"] = SRS_STAGE_NAMES.get(row["srs_stage"], "Locked") if row["srs_stage"] is not None else "Locked"
        else:
            entry["in_wanikani"] = False
        unknown.append(entry)

    return {
        "ok": not unknown,
        "min_srs_stage": SRS_STAGE_NAMES.get(min_srs_stage, min_srs_stage),
        "kanji_total": len(found),
        "unique_kanji": len(counts),
        "unknown_count": len(unknown),
        "unknown": unknown,
        "advice": None if not unknown else (
            "Rewrite the flagged words using known kanji, or write them in kana, then run check_text again."
        ),
    }
