"""Reviews in context: word finding, grading, the SRS rule and the submit flow."""

import pytest
from mcp import Client

from conftest import FAKE_TOKEN, FakeWaniKani
from wanikani_mcp import context
from wanikani_mcp.client import WaniKaniClient
from wanikani_mcp.db import Database
from wanikani_mcp.metrics import stats
from wanikani_mcp.server import create_server

NOW = "2026-11-12T12:00:00+00:00"


def add_verb(db: Database, subject_id=90001, chars="働く", meaning="To Work", reading="はたらく", stage=3,
             available_at="2026-11-01T00:00:00Z"):
    db.upsert_subjects([{
        "id": subject_id, "object": "vocabulary", "data_updated_at": NOW,
        "data": {"level": 10, "characters": chars, "slug": chars, "hidden_at": None,
                 "meanings": [{"meaning": meaning, "primary": True, "accepted_answer": True}],
                 "auxiliary_meanings": [], "parts_of_speech": ["godan verb", "intransitive verb"],
                 "readings": [{"reading": reading, "primary": True, "accepted_answer": True}]},
    }])
    db.upsert_assignments([{
        "id": subject_id + 1, "data_updated_at": NOW,
        "data": {"subject_id": subject_id, "subject_type": "vocabulary", "srs_stage": stage,
                 "available_at": available_at, "burned_at": None, "hidden": False},
    }])


# ----- finding words --------------------------------------------------------


def test_finds_vocabulary_longest_match_first(synced_db):
    result = context.find_wanikani_words(synced_db, "労働の評論を読んだ。労働は大変だ。", now=NOW)
    words = {w["characters"]: w for w in result["words"]}
    assert set(words) == {"労働", "評論"}
    assert words["労働"]["occurrences"] == 2
    assert words["労働"]["due_now"] is True
    assert result["due_now"] == 2


def test_matches_conjugated_verbs_by_stem(synced_db):
    add_verb(synced_db)
    for text in ("毎日働いています", "よく働きます", "働かない"):
        found = [w["characters"] for w in context.find_wanikani_words(synced_db, text, now=NOW)["words"]]
        assert "働く" in found, text
    # 労働 is matched as a whole word, not as the stem of 働く
    found = [w["characters"] for w in context.find_wanikani_words(synced_db, "労働者", now=NOW)["words"]]
    assert found == ["労働"]


def test_not_due_items_are_marked(synced_db):
    add_verb(synced_db, available_at="2026-12-01T00:00:00Z")
    words = context.find_wanikani_words(synced_db, "働いた", now=NOW)["words"]
    assert words[0]["due_now"] is False


# ----- grading ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        ("labor", "correct"),
        ("Manual labour", "correct"),     # whitelist
        ("the work", "correct"),          # articles ignored
        ("manual labr", "correct"),       # small typo
        ("hard work", "correct"),         # the learner's own synonym
        ("肉体労働のこと", "needs_judgement"),  # Japanese explanation: the model decides
        ("relaxation", "needs_judgement"),
    ],
)
def test_grade_meaning(synced_db, answer, verdict):
    subject = synced_db.subject(3270)
    assert context.grade_meaning(subject, answer, ["hard work"]) == verdict


def test_blacklisted_meaning_is_wrong():
    subject = {"meanings": [{"meaning": "Quiet", "accepted_answer": True}],
               "auxiliary_meanings": [{"meaning": "Silent", "type": "blacklist"}]}
    assert context.grade_meaning(subject, "silent", []) == "incorrect"


@pytest.mark.parametrize(
    ("subject_id", "answer", "verdict"),
    [
        (3270, "ろうどう", "correct"),
        (3270, "ロウドウ", "correct"),     # katakana accepted
        (3270, "ろうど", "incorrect"),
        (3270, "roudou", "not_kana"),
        (1052, "せい", "correct"),
        (1052, "しず", "other_reading"),  # real reading, but WaniKani asks for on'yomi here
    ],
)
def test_grade_reading(synced_db, subject_id, answer, verdict):
    assert context.grade_reading(synced_db.subject(subject_id), answer) == verdict


def test_item_needs_meaning_and_reading_and_counts_mistakes(synced_db):
    r = context.grade_answer(synced_db, "s1", "労働", meaning_answer="labor")
    assert r["item_complete"] is False and r["still_needed"] == ["reading"]
    r = context.grade_answer(synced_db, "s1", "労働", reading_answer="ろうど")
    assert r["reading"] == "incorrect"
    r = context.grade_answer(synced_db, "s1", "労働", reading_answer="ろうどう")
    assert r["item_complete"] is True
    assert r["incorrect_so_far"] == {"meaning": 0, "reading": 1}


def test_radicals_need_only_meaning(synced_db):
    r = context.grade_answer(synced_db, "s1", "重", meaning_answer="heavy")
    assert r["item_complete"] is True


def test_needs_judgement_is_not_recorded_until_model_decides(synced_db):
    r = context.grade_answer(synced_db, "s1", "労働", meaning_answer="体を使う仕事")
    assert r["meaning"] == "needs_judgement" and "accepted_answers" not in r
    row = synced_db.one("SELECT meaning_passed, incorrect_meaning FROM context_reviews")
    assert tuple(row) == (0, 0)
    r = context.grade_answer(synced_db, "s1", "労働", meaning_answer="体を使う仕事", model_judgement=True)
    assert r["meaning"] == "correct" and r["meaning_judged_by_model"] is True


def test_no_answer_text_is_stored(synced_db):
    context.grade_answer(synced_db, "s1", "労働", meaning_answer="secret-answer-text", model_judgement=False)
    assert "secret-answer-text" not in "\n".join(synced_db.conn.iterdump())


# ----- SRS rule and planning ------------------------------------------------------


@pytest.mark.parametrize(
    ("stage", "incorrect", "expected"),
    [(2, 0, 3), (8, 0, 9), (2, 1, 1), (4, 2, 3), (5, 1, 3), (6, 3, 2), (1, 4, 1)],
)
def test_expected_stage(stage, incorrect, expected):
    assert context.expected_stage(stage, incorrect) == expected


def _live(db, *subject_ids):
    rows = db.query(f"SELECT * FROM assignments WHERE subject_id IN ({','.join('?' * len(subject_ids))})",
                    subject_ids)
    return {r["subject_id"]: dict(r) for r in rows}


def test_plan_sorts_items_into_submit_and_practice(synced_db):
    g = context.grade_answer
    g(synced_db, "s1", "労働", meaning_answer="labor", reading_answer="ろうどう")      # due, clean
    g(synced_db, "s1", "評論", meaning_answer="critique", reading_answer="ひょうろ")    # due, a mistake...
    g(synced_db, "s1", "評論", reading_answer="ひょうろん")                            # ...then right
    g(synced_db, "s1", "静", meaning_answer="quiet")                                # reading missing
    g(synced_db, "s1", "働", meaning_answer="work", reading_answer="どう")            # burned, not due
    live = _live(synced_db, 3270, 4446, 1052, 809)

    plan = {p["characters"]: p for p in context.plan_reviews(synced_db, "s1", live, False, now=NOW)}
    assert plan["労働"]["action"] == "submit_correct"
    assert plan["労働"]["srs_change"] == "Apprentice III -> Apprentice IV (expected)"
    assert plan["評論"]["action"] == "practice" and "incorrect" in plan["評論"]["reason"]
    assert plan["静"]["action"] == "practice" and "reading" in plan["静"]["reason"]
    assert plan["働"]["action"] == "practice" and "not due" in plan["働"]["reason"]

    plan = {p["characters"]: p for p in context.plan_reviews(synced_db, "s1", live, True, now=NOW)}
    assert plan["評論"]["action"] == "submit_incorrect"
    assert plan["評論"]["incorrect_reading_answers"] == 1
    assert plan["評論"]["srs_change"] == "Apprentice III -> Apprentice II (expected)"


# ----- end to end through MCP ---------------------------------------------------


def connect(fake: FakeWaniKani, db: Database, allow_writes=True, submit_incorrect=False) -> Client:
    async def no_sleep(_):
        return None

    return Client(create_server(
        db, lambda: WaniKaniClient(FAKE_TOKEN, transport=fake.transport(), sleep=no_sleep),
        allow_writes=allow_writes, submit_incorrect=submit_incorrect,
    ))


async def call(client, tool, **args):
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content[0].text
    return result.structured_content


async def test_full_read_and_review_flow(fake_api: FakeWaniKani, db: Database):
    async with connect(fake_api, db) as c:
        await call(c, "sync")
        due = await call(c, "due_items")
        assert due["total_due"] == 7
        assert due["items"][0]["type"] == "vocabulary"   # vocabulary first: easiest in context

        words = await call(c, "find_wanikani_words", text="労働の評論")
        assert words["due_now"] == 2

        await call(c, "grade_answer", item="労働", meaning_answer="labour", reading_answer="ろうどう")
        await call(c, "grade_answer", item="評論", meaning_answer="review", reading_answer="ひょうろ")
        await call(c, "grade_answer", item="評論", reading_answer="ひょうろん")

        preview = await call(c, "submit_reviews")
        assert preview["status"] == "needs_confirmation"
        assert [p["characters"] for p in preview["will_submit"]] == ["労働"]
        assert [p["characters"] for p in preview["practice_only"]] == ["評論"]
        assert not [w for w in fake_api.writes if w[1] == "reviews"]   # nothing sent yet

        done = await call(c, "submit_reviews", confirm=True)
        assert done["submitted"] == [{"characters": "労働", "status": "submit_correct", "new_srs_stage": "Apprentice IV"}]
        assert fake_api.writes[-1] == ("POST", "reviews", {"review": {
            "assignment_id": 770920299, "incorrect_meaning_answers": 0, "incorrect_reading_answers": 0}})
        assert db.scalar("SELECT srs_stage FROM assignments WHERE subject_id = 3270") == 4

        again = await call(c, "submit_reviews")
        assert again["status"] == "nothing_to_submit"   # rows closed; no double submission

    r = stats(db)["context_reviews"]
    assert r == {**r, "items_quizzed": 2, "submitted_correct": 1, "submitted_incorrect": 0, "kept_as_practice": 1}


async def test_submit_incorrect_when_enabled(fake_api: FakeWaniKani, db: Database):
    async with connect(fake_api, db, submit_incorrect=True) as c:
        await call(c, "sync")
        await call(c, "grade_answer", item="静", meaning_answer="loud", model_judgement=False)
        await call(c, "grade_answer", item="静", meaning_answer="quiet", reading_answer="せい")
        done = await call(c, "submit_reviews", confirm=True)
    assert done["submitted"][0]["status"] == "submit_incorrect"
    assert fake_api.writes[-1][2]["review"]["incorrect_meaning_answers"] == 1


async def test_item_reviewed_elsewhere_meanwhile_becomes_practice(fake_api: FakeWaniKani, db: Database):
    async with connect(fake_api, db) as c:
        await call(c, "sync")
        await call(c, "grade_answer", item="労働", meaning_answer="labor", reading_answer="ろうどう")
        await call(c, "submit_reviews")
        # The learner did this review on the website in the meantime.
        for a in fake_api.data["assignments"]["data"]:
            if a["data"]["subject_id"] == 3270:
                a["data"]["available_at"] = "2026-12-01T00:00:00.000000Z"
        done = await call(c, "submit_reviews", confirm=True)
    # The fresh assignment says it is no longer due, so it is kept as practice instead.
    assert done["submitted"] == [] and done["practice_only"] == 1


async def test_reviews_not_sent_when_writes_disabled(fake_api: FakeWaniKani, db: Database):
    async with connect(fake_api, db, allow_writes=False) as c:
        await call(c, "sync")
        await call(c, "grade_answer", item="労働", meaning_answer="labor", reading_answer="ろうどう")
        result = await call(c, "submit_reviews", confirm=True)
    assert result["status"] == "preview_only"
    assert not [w for w in fake_api.writes if w[1] == "reviews"]


async def test_read_and_review_prompt(fake_api: FakeWaniKani, db: Database):
    async with connect(fake_api, db) as c:
        news = (await c.get_prompt("read_and_review", {"source": "news", "topic": "weather"})).messages[0].content.text
        story = (await c.get_prompt("read_and_review", {})).messages[0].content.text
    assert "NHK News Web Easy" in news and "weather" in news
    assert "known_items" in story and "check_text" in story
    for text in (news, story):
        assert "due_items" in text and "grade_answer" in text and "confirm=true" in text


async def test_rejected_review_is_reported_and_others_still_sent(fake_api: FakeWaniKani, db: Database):
    import httpx

    original = fake_api._review

    def reject_labor(body):
        if body["review"]["assignment_id"] == 770920299:
            return httpx.Response(422, json={"error": "Assignment is not available for review", "code": 422})
        return original(body)

    fake_api._review = reject_labor
    async with connect(fake_api, db) as c:
        await call(c, "sync")
        await call(c, "grade_answer", item="労働", meaning_answer="labor", reading_answer="ろうどう")
        await call(c, "grade_answer", item="評論", meaning_answer="critique", reading_answer="ひょうろん")
        done = await call(c, "submit_reviews", confirm=True)
    by_item = {r["characters"]: r for r in done["submitted"]}
    assert by_item["労働"]["status"] == "failed" and "422" in by_item["労働"]["error"]
    assert by_item["評論"]["status"] == "submit_correct"
    assert db.scalar("SELECT status FROM context_reviews WHERE subject_id = 3270") == "failed"
