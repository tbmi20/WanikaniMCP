"""The MCP server: ten tools and three workflow prompts.

The only tools that write to WaniKani are ``save_mnemonic`` and ``submit_reviews``.
Both need the ``WANIKANI_MCP_ALLOW_WRITES=1`` environment variable and an explicit
``confirm=true`` after the user has seen the preview.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import WaniKaniClient, WaniKaniError
from .db import Database
from . import context as in_context
from . import items as lookups
from . import sync as syncing
from . import text as text_checks
from .items import ItemNotFound
from .leeches import LeechConfig, current_leeches
from .metrics import Recorder

WRITES_ENV_VAR = "WANIKANI_MCP_ALLOW_WRITES"
SUBMIT_INCORRECT_ENV_VAR = "WANIKANI_MCP_SUBMIT_INCORRECT"

# Keep request URLs out of the server log; it only needs warnings.
logging.getLogger("httpx").setLevel(logging.WARNING)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

INSTRUCTIONS = """\
Tools for a WaniKani learner's own data, served from a local cache.
Call `sync` first if a tool says the cache is empty, or when the user wants fresh numbers.
`find_leeches` ranks weak items; `explain_item` gives the facts for root-cause analysis
(you do the analysis). Before showing Japanese text you generated for the learner, run
`check_text` and fix anything it flags. `save_mnemonic` changes the user's WaniKani
account: always show its preview and only call it with confirm=true after the user agrees.
For reviews in context: `due_items` -> text with those words -> ask the learner ->
`grade_answer` with their exact words -> `submit_reviews` preview -> confirm=true only if
the user agrees. Never reveal an answer before the learner has replied.
"""

ItemType = Literal["all", "radical", "kanji", "vocabulary", "kana_vocabulary"]


def create_server(
    db: Database | None = None,
    client_factory: Callable[[], WaniKaniClient] = WaniKaniClient,
    config: LeechConfig | None = None,
    allow_writes: bool | None = None,
    submit_incorrect: bool | None = None,
) -> MCPServer:
    db = db or Database()
    config = config or LeechConfig.from_env()
    if allow_writes is None:
        allow_writes = os.environ.get(WRITES_ENV_VAR) == "1"
    if submit_incorrect is None:
        submit_incorrect = os.environ.get(SUBMIT_INCORRECT_ENV_VAR) == "1"
    recorder = Recorder(db)
    mcp = MCPServer("wanikani", instructions=INSTRUCTIONS)

    def require_cache() -> None:
        if not db.has_synced():
            raise ToolError("The local cache is empty. Call the `sync` tool first.")

    def last_synced() -> str | None:
        return db.scalar("SELECT MAX(finished_at) FROM sync_runs")

    # ----- tools -----------------------------------------------------------

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True,
                                          openWorldHint=True))
    async def sync() -> dict[str, Any]:
        """Pull the latest WaniKani data into the local cache and report what changed
        (new/updated records, SRS movement, leeches gained or cured). Only reads from
        WaniKani; the first run takes about a minute because of the API rate limit."""
        with recorder.track("sync") as call:
            try:
                async with client_factory() as client:
                    summary = await syncing.sync(client, db, config)
            except WaniKaniError as exc:
                raise ToolError(str(exc)) from exc
            call.items = sum(c["new"] + c["updated"] for c in summary["changes"].values())
            return summary

    @mcp.tool(annotations=READ_ONLY)
    async def find_leeches(limit: int = 10, item_type: ItemType = "all", include_burned: bool = False) -> dict[str, Any]:
        """Rank the learner's worst items ("leeches"), worst first.

        Score = incorrect / max(current_streak, 1) ** exponent, worked out separately for
        meaning and reading; the worse one counts and score >= threshold is a leech.
        `weak_side` says whether meaning or reading is the problem."""
        with recorder.track("find_leeches") as call:
            require_cache()
            kind = None if item_type == "all" else item_type
            leeches = current_leeches(db, config, kind, include_burned)
            limit = max(1, min(limit, 100))
            call.items = min(limit, len(leeches))
            return {
                "formula": f"incorrect / max(current_streak, 1) ** {config.exponent}, per meaning and reading",
                "threshold": config.threshold,
                "total_leeches": len(leeches),
                "leeches": leeches[:limit],
                "last_synced": last_synced(),
            }

    @mcp.tool(annotations=READ_ONLY)
    async def explain_item(item: str) -> dict[str, Any]:
        """Facts for root-cause analysis of one item, given its characters (静, 評論) or
        subject id: meanings, on'yomi/kun'yomi readings, component radicals, visually
        similar kanji (with the learner's own scores on them), the learner's accuracy
        and streaks, the default mnemonics and any personal notes."""
        with recorder.track("explain_item") as call:
            require_cache()
            try:
                result = lookups.explain_item(db, item, config)
            except ItemNotFound as exc:
                raise ToolError(str(exc)) from exc
            call.items = 1
            return result

    @mcp.tool(annotations=READ_ONLY)
    async def known_items(
        min_srs_stage: int = 1,
        item_type: Literal["all", "kanji", "vocabulary"] = "all",
        vocabulary_limit: int = 300,
    ) -> dict[str, Any]:
        """Kanji and vocabulary the learner has reached, for level-appropriate practice.
        min_srs_stage: 0 = unlocked, 1 = started (Apprentice), 5 = Guru, 7 = Master, 9 = Burned."""
        with recorder.track("known_items") as call:
            require_cache()
            result = lookups.known_items(db, min_srs_stage, item_type, vocabulary_limit)
            call.items = result.get("kanji_count", 0) + result.get("vocabulary_count", 0)
            return result

    @mcp.tool(annotations=READ_ONLY)
    async def check_text(text: str, min_srs_stage: int = 1) -> dict[str, Any]:
        """Flag every kanji in a Japanese text that the learner hasn't learned (below
        min_srs_stage). Use it on any text you write for the learner, and rewrite until
        `ok` is true. Kana and punctuation are always fine."""
        with recorder.track("check_text") as call:
            require_cache()
            result = text_checks.check_text(db, text, min_srs_stage)
            recorder.log_check_text(result, min_srs_stage)
            call.items = result["unknown_count"]
            return result

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True,
                                          openWorldHint=True))
    async def save_mnemonic(
        item: str,
        meaning_note: str | None = None,
        reading_note: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Save a personal meaning and/or reading note to the learner's WaniKani account
        (study materials). WRITES TO THE ACCOUNT. Without confirm=true it only returns a
        before/after preview: show that to the user and call again with confirm=true
        only after they explicitly agree. Notes left out are not changed."""
        with recorder.track("save_mnemonic") as call:
            require_cache()
            if meaning_note is None and reading_note is None:
                raise ToolError("Give a meaning_note, a reading_note, or both.")
            try:
                subject, _ = lookups.resolve(db, item)
            except ItemNotFound as exc:
                raise ToolError(str(exc)) from exc
            try:
                async with client_factory() as client:
                    current = await client.get_collection("/study_materials", {"subject_ids": [subject["id"]]})
                    existing = current[0] if current else None
                    before = {
                        "meaning_note": existing["data"].get("meaning_note") if existing else None,
                        "reading_note": existing["data"].get("reading_note") if existing else None,
                    }
                    changes = {k: v for k, v in (("meaning_note", meaning_note), ("reading_note", reading_note))
                               if v is not None}
                    after = {**before, **changes}
                    preview = {
                        "item": {"subject_id": subject["id"], "characters": subject.get("characters"),
                                 "type": subject["object"]},
                        "action": "update" if existing else "create",
                        "before": before,
                        "after": after,
                    }
                    if not allow_writes:
                        return {**preview, "status": "preview_only",
                                "message": f"Writes are disabled. Set {WRITES_ENV_VAR}=1 in the server's "
                                           "environment to allow saving notes to WaniKani."}
                    if not confirm:
                        return {**preview, "status": "needs_confirmation",
                                "message": "Nothing was saved. Show this change to the user; if they approve, "
                                           "call save_mnemonic again with the same notes and confirm=true."}
                    if before == after:
                        return {**preview, "status": "unchanged", "message": "The notes already match."}
                    if existing:
                        saved = await client.update_study_material(existing["id"], **changes)
                    else:
                        saved = await client.create_study_material(subject["id"], **changes)
            except WaniKaniError as exc:
                raise ToolError(str(exc)) from exc
            db.upsert_study_materials([saved])
            db.conn.commit()
            call.items = 1
            return {**preview, "status": "saved", "message": "Saved to WaniKani."}


    # ----- reviews in context ------------------------------------------------

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
    async def due_items(limit: int = 20) -> dict[str, Any]:
        """Items due for WaniKani review right now, fetched live. Only these can count as
        real reviews, so build stories and pick articles around them. Vocabulary comes
        first because it is easiest to use in context. Do not show the meanings or
        readings to the learner before they answer."""
        with recorder.track("due_items") as call:
            require_cache()
            try:
                async with client_factory() as client:
                    live = await client.get_collection("/assignments", {"immediately_available_for_review": True})
            except WaniKaniError as exc:
                raise ToolError(str(exc)) from exc
            db.upsert_assignments(live)
            db.conn.commit()
            order = {"vocabulary": 0, "kanji": 1, "kana_vocabulary": 2, "radical": 3}
            live.sort(key=lambda a: (order.get(a["data"]["subject_type"], 9), a["data"]["srs_stage"]))
            subjects = db.subjects_by_ids([a["data"]["subject_id"] for a in live[:limit]])
            items = []
            for a in live[:limit]:
                subject = subjects.get(a["data"]["subject_id"], {})
                items.append({
                    "subject_id": a["data"]["subject_id"],
                    "characters": subject.get("characters") or subject.get("slug"),
                    "type": a["data"]["subject_type"],
                    "meaning": next((m["meaning"] for m in subject.get("meanings", []) if m.get("primary")), None),
                    "reading": next((r["reading"] for r in subject.get("readings", []) if r.get("primary")), None),
                    "srs_stage": lookups.SRS_STAGE_NAMES.get(a["data"]["srs_stage"]),
                    "needs_reading": a["data"]["subject_type"] in in_context.READING_REQUIRED,
                })
            call.items = len(items)
            counts: dict[str, int] = {}
            for a in live:
                counts[a["data"]["subject_type"]] = counts.get(a["data"]["subject_type"], 0) + 1
            return {"total_due": len(live), "due_by_type": counts, "items": items}

    @mcp.tool(annotations=READ_ONLY)
    async def find_wanikani_words(text: str) -> dict[str, Any]:
        """List the WaniKani vocabulary (that the learner has started) appearing in a Japanese
        text such as a news article or story, due items first. Use it to pick quiz words
        and to choose articles that contain due items. Also run check_text for unknown kanji."""
        with recorder.track("find_wanikani_words") as call:
            require_cache()
            result = in_context.find_wanikani_words(db, text)
            call.items = result["vocabulary_found"]
            return result

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
    async def grade_answer(
        item: str,
        meaning_answer: str | None = None,
        reading_answer: str | None = None,
        model_judgement: bool | None = None,
    ) -> dict[str, Any]:
        """Grade the learner's answer for one item against WaniKani's accepted answers and
        record it for submit_reviews. Pass the learner's own words, unedited.
        meaning_answer: English, or Japanese explaining the meaning. reading_answer: kana.
        Kanji and vocabulary need both before they can count. If the meaning comes back
        `needs_judgement`, decide honestly whether they understood it and call again with
        model_judgement=true/false (the preview will show it was your call). Only local
        state changes; nothing is sent to WaniKani."""
        with recorder.track("grade_answer") as call:
            require_cache()
            if meaning_answer is None and reading_answer is None:
                raise ToolError("Give meaning_answer, reading_answer, or both.")
            try:
                result = in_context.grade_answer(db, recorder.session_id, item, meaning_answer, reading_answer,
                                                 model_judgement)
            except ItemNotFound as exc:
                raise ToolError(str(exc)) from exc
            call.items = 1
            return result

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False,
                                          openWorldHint=True))
    async def submit_reviews(confirm: bool = False) -> dict[str, Any]:
        """Turn this session's graded context answers into real WaniKani reviews. WRITES TO
        THE ACCOUNT AND CANNOT BE UNDONE. Without confirm=true it returns a preview: which
        items will be submitted (with expected SRS changes) and which stay practice only.
        Show it to the learner and call with confirm=true only after they agree."""
        with recorder.track("submit_reviews") as call:
            require_cache()
            subject_ids = [r[0] for r in db.query(
                "SELECT subject_id FROM context_reviews WHERE session_id = ? AND status = 'open'",
                (recorder.session_id,))]
            if not subject_ids:
                return {"status": "nothing_to_submit", "message": "No graded answers in this session yet."}
            try:
                async with client_factory() as client:
                    live_rows = await client.get_collection("/assignments", {"subject_ids": subject_ids})
                    live = {a["data"]["subject_id"]: {"id": a["id"], **a["data"]} for a in live_rows}
                    plan = in_context.plan_reviews(db, recorder.session_id, live, submit_incorrect)
                    to_send = [p for p in plan if p["action"].startswith("submit")]
                    preview = {
                        "will_submit": to_send,
                        "practice_only": [p for p in plan if p["action"] == "practice"],
                        "submit_incorrect_enabled": submit_incorrect,
                    }
                    if not allow_writes:
                        return {**preview, "status": "preview_only",
                                "message": f"Writes are disabled. Set {WRITES_ENV_VAR}=1 to submit reviews."}
                    if not confirm:
                        return {**preview, "status": "needs_confirmation",
                                "message": "Nothing was sent. Show this to the learner; if they agree, call "
                                           "submit_reviews again with confirm=true. Reviews cannot be undone."}
                    results = []
                    for entry in plan:
                        if entry["action"] == "practice":
                            in_context.mark(db, entry["context_review_id"], "practice", entry["reason"])
                            continue
                        try:
                            body = await client.create_review(entry["assignment_id"],
                                                              entry["incorrect_meaning_answers"],
                                                              entry["incorrect_reading_answers"])
                        except WaniKaniError as exc:
                            in_context.mark(db, entry["context_review_id"], "failed", str(exc))
                            results.append({"characters": entry["characters"], "status": "failed",
                                            "error": str(exc)})
                            continue
                        updated = body.get("resources_updated", {})
                        if updated.get("assignment"):
                            db.upsert_assignments([updated["assignment"]])
                        if updated.get("review_statistic"):
                            db.upsert_review_statistics([updated["review_statistic"]])
                        end = body.get("data", {}).get("ending_srs_stage")
                        in_context.mark(db, entry["context_review_id"], "submitted", entry["action"])
                        results.append({"characters": entry["characters"], "status": entry["action"],
                                        "new_srs_stage": lookups.SRS_STAGE_NAMES.get(end, end)})
                    db.conn.commit()
            except WaniKaniError as exc:
                raise ToolError(str(exc)) from exc
            call.items = sum(1 for r in results if r["status"] != "failed")
            return {"status": "done", "submitted": results,
                    "practice_only": len(preview["practice_only"])}

    # ----- prompts ---------------------------------------------------------

    @mcp.prompt(title="Drill my leeches")
    def drill_leeches(limit: int = 5, item_type: str = "all") -> str:
        """Find my worst items, work out why I keep missing them, and quiz me."""
        return f"""Help me fix my WaniKani leeches.

1. Call `find_leeches` with limit={limit} and item_type="{item_type}".
2. For each leech, call `explain_item` and diagnose the most likely root cause from the
   facts, for example: confusing it with a visually similar kanji I also miss, mixing up
   on'yomi and kun'yomi, a meaning that clashes with a near-synonym, or a weak mnemonic.
   Say which side (meaning or reading) is failing and cite the numbers.
3. Quiz me one item at a time: ask for the {"reading or meaning" if item_type != "radical" else "meaning"},
   wait for my answer, then give feedback and a contrast with the look-alike I confuse it with.
4. For items I still miss, suggest a short new mnemonic. If I like it, call `save_mnemonic`
   WITHOUT confirm, show me the preview, and only call it with confirm=true if I say yes.
Finish with a one-line summary per item."""

    @mcp.prompt(title="Write a short story at my level")
    def story_at_my_level(topic: str = "an ordinary day", length: str = "about 8 sentences",
                          min_srs_stage: int = 5) -> str:
        """Write graded reading practice that only uses kanji I have learned."""
        return f"""Write a short Japanese story for me to read.

Topic: {topic}. Length: {length}.
1. Call `known_items` with min_srs_stage={min_srs_stage} to get the kanji and vocabulary I know.
2. Call `find_leeches` with limit=5 and try to use some of those words naturally, so the
   story doubles as review.
3. Write the story using only kanji from my known list; write anything else in kana.
4. Call `check_text` on the full story with min_srs_stage={min_srs_stage}. If `ok` is false,
   rewrite the flagged words (known kanji or kana) and check again. Repeat until `ok` is true.
5. Show me the final story, then a vocabulary list of the leech words you used, then an
   English translation under a "Translation" heading so I can try reading first.
Tell me how many check_text rounds it took."""


    @mcp.prompt(title="Read and review in context")
    def read_and_review(source: str = "story", topic: str = "", max_items: int = 8) -> str:
        """Read a news article or story and answer questions on the WaniKani words in it;
        correct answers on due items can count as real WaniKani reviews."""
        if source == "news":
            get_text = f"""2. Find a recent Japanese news article, preferably NHK News Web Easy
   (https://www3.nhk.or.jp/news/easy/){f' about {topic}' if topic else ''}, with your web tools,
   or ask me to paste one. Call `find_wanikani_words` on it; if it contains few of my due
   items, try another article (at most 3). Call `check_text` and gloss any unknown kanji
   with furigana in brackets."""
        else:
            get_text = f"""2. Call `known_items` with min_srs_stage=5, then write a short story
   ({topic or "any everyday topic"}, 8-12 sentences) that uses as many of my due vocabulary
   items as fit naturally. Run `check_text` and rewrite until it passes (due items are fine)."""
        return f"""Let's do WaniKani reviews in context.

1. Call `due_items` (limit=30). Do not show me their meanings or readings.
{get_text}
3. Show me the Japanese text without a translation.
4. Quiz me on up to {max_items} WaniKani words from the text, due items first, one at a time.
   For each, quote the sentence and ask what the word means here (I may answer in English or
   Japanese) and, for kanji and vocabulary, how it is read (in kana).
5. Call `grade_answer` with my exact answers. If the meaning is `needs_judgement`, decide
   honestly whether I showed I understood it and call again with model_judgement. If the
   reading is `not_kana` or `other_reading`, ask me again. Give feedback after each item.
6. At the end, call `submit_reviews` without confirm, show me what will be submitted and
   what stays practice, and only call it with confirm=true if I say yes.
Finish with a short summary: items passed, missed, and submitted."""

    return mcp


def main() -> None:
    create_server().run()
