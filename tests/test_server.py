"""End-to-end through the MCP protocol, using the SDK's in-process Client."""

from mcp import Client

from conftest import FAKE_TOKEN, FakeWaniKani
from wanikani_mcp.client import WaniKaniClient
from wanikani_mcp.db import Database
from wanikani_mcp.server import create_server


def build(fake: FakeWaniKani, db: Database, allow_writes: bool = True):
    async def no_sleep(_):
        return None

    return create_server(
        db, lambda: WaniKaniClient(FAKE_TOKEN, transport=fake.transport(), sleep=no_sleep), allow_writes=allow_writes
    )


def connect(fake: FakeWaniKani, db: Database, allow_writes: bool = True) -> Client:
    # Opened inside each test: the client's task group must start and stop in the same task.
    return Client(build(fake, db, allow_writes))


async def call(client, tool, **args):
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def test_lists_tools_and_prompts(fake_api, db):
    async with connect(fake_api, db) as mcp_client:
        tools = {t.name: t for t in (await mcp_client.list_tools()).tools}
        assert set(tools) == {"sync", "find_leeches", "explain_item", "known_items", "check_text", "save_mnemonic",
                             "due_items", "find_wanikani_words", "grade_answer", "submit_reviews"}
        read_only = {name for name, t in tools.items() if t.annotations and t.annotations.read_only_hint}
        assert read_only == {"find_leeches", "explain_item", "known_items", "check_text", "due_items",
                             "find_wanikani_words"}
        prompts = {p.name for p in (await mcp_client.list_prompts()).prompts}
        assert prompts == {"drill_leeches", "story_at_my_level", "read_and_review"}


async def test_tools_ask_for_sync_when_cache_is_empty(fake_api, db):
    async with connect(fake_api, db) as mcp_client:
        result = await mcp_client.call_tool("find_leeches", {})
        assert result.is_error
        assert "sync" in result.content[0].text


async def test_sync_then_find_and_explain(fake_api, db):
    async with connect(fake_api, db) as mcp_client:
        summary = await call(mcp_client, "sync")
        assert summary["leeches"]["count"] == 7
        leeches = await call(mcp_client, "find_leeches", limit=3, item_type="kanji")
        assert [item["characters"] for item in leeches["leeches"]] == ["静", "処", "靖"]
        assert leeches["total_leeches"] == 4
        info = await call(mcp_client, "explain_item", item="静")
        assert info["subject_id"] == 1052


async def test_check_text_through_protocol(fake_api, db):
    async with connect(fake_api, db) as mcp_client:
        await call(mcp_client, "sync")
        result = await call(mcp_client, "check_text", text="静かな部屋")
        assert result["ok"] is False
        assert [u["kanji"] for u in result["unknown"]] == ["部", "屋"]


async def test_tool_calls_are_logged_without_arguments(db, fake_api):
    async with connect(fake_api, db) as mcp_client:
        await call(mcp_client, "sync")
        await call(mcp_client, "find_leeches", limit=5)
        await call(mcp_client, "check_text", text="秘密の文章")
        await mcp_client.call_tool("explain_item", {"item": "猫"})  # not in cache -> error
        rows = [dict(r) for r in db.query("SELECT tool, item_count, ok, error_type FROM tool_calls ORDER BY id")]
        assert [(r["tool"], r["ok"]) for r in rows] == [
            ("sync", 1), ("find_leeches", 1), ("check_text", 1), ("explain_item", 0),
        ]
        assert rows[1]["item_count"] == 5
        assert rows[3]["error_type"] == "ToolError"
        dump = "\n".join(db.conn.iterdump())
        assert "秘密" not in dump  # check_text never stores the text
        assert FAKE_TOKEN not in dump


async def test_save_mnemonic_previews_and_needs_confirm(fake_api, db):
    async with connect(fake_api, db) as mcp_client:
        await call(mcp_client, "sync")
        preview = await call(mcp_client, "save_mnemonic", item="静", meaning_note="Blue + conflict = quiet")
        assert preview["status"] == "needs_confirmation"
        assert preview["action"] == "create"
        assert preview["before"] == {"meaning_note": None, "reading_note": None}
        assert preview["after"]["meaning_note"] == "Blue + conflict = quiet"
        assert fake_api.writes == []  # nothing written without confirm

        saved = await call(mcp_client, "save_mnemonic", item="静", meaning_note="Blue + conflict = quiet", confirm=True)
        assert saved["status"] == "saved"
        assert fake_api.writes == [
            ("POST", "study_materials", {"study_material": {"subject_id": 1052, "meaning_note": "Blue + conflict = quiet"}})
        ]


async def test_save_mnemonic_updates_existing_material_with_put(fake_api, db):
    async with connect(fake_api, db) as mcp_client:
        await call(mcp_client, "sync")
        saved = await call(mcp_client, "save_mnemonic", item="労", reading_note="ろう like 'row'", confirm=True)
        assert saved["action"] == "update"
        method, path, body = fake_api.writes[0]
        assert (method, path) == ("PUT", "study_materials/29549542")
        assert body == {"study_material": {"reading_note": "ろう like 'row'"}}  # meaning note untouched


async def test_save_mnemonic_refuses_when_writes_disabled(fake_api, db):
    async with connect(fake_api, db, allow_writes=False) as client:
        await call(client, "sync")
        result = await call(client, "save_mnemonic", item="静", meaning_note="x", confirm=True)
    assert result["status"] == "preview_only"
    assert "WANIKANI_MCP_ALLOW_WRITES" in result["message"]
    assert fake_api.writes == []


async def test_prompts_render(fake_api, db):
    async with connect(fake_api, db) as mcp_client:
        drill = await mcp_client.get_prompt("drill_leeches", {"limit": "3"})
        assert "find_leeches" in drill.messages[0].content.text and "confirm=true" in drill.messages[0].content.text
        story = await mcp_client.get_prompt("story_at_my_level", {"topic": "cats"})
        text = story.messages[0].content.text
        assert "cats" in text and "check_text" in text and "known_items" in text
