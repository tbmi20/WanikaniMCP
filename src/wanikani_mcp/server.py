"""The MCP server: six tools and two workflow prompts.

Every tool is read-only except ``save_mnemonic``, which needs both the
``WANIKANI_MCP_ALLOW_WRITES=1`` environment variable and an explicit
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
from . import items as lookups
from . import sync as syncing
from . import text as text_checks
from .items import ItemNotFound
from .leeches import LeechConfig, current_leeches
from .metrics import Recorder

WRITES_ENV_VAR = "WANIKANI_MCP_ALLOW_WRITES"

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
"""

ItemType = Literal["all", "radical", "kanji", "vocabulary", "kana_vocabulary"]


def create_server(
    db: Database | None = None,
    client_factory: Callable[[], WaniKaniClient] = WaniKaniClient,
    config: LeechConfig | None = None,
    allow_writes: bool | None = None,
) -> MCPServer:
    db = db or Database()
    config = config or LeechConfig.from_env()
    if allow_writes is None:
        allow_writes = os.environ.get(WRITES_ENV_VAR) == "1"
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

    return mcp


def main() -> None:
    create_server().run()
