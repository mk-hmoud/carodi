from __future__ import annotations

from datetime import date

import pytest

from carodi.bot import Bot, render_card, render_done
from carodi.models import Kind, Opportunity, Remote
from carodi.store import Store


def opp(**kw) -> Opportunity:
    base = dict(
        source="test",
        title="Backend Engineer",
        org="Monzo",
        url="https://example.com/job/1",
        location_raw="London, UK",
    )
    return Opportunity(**{**base, **kw})


class FakeBot(Bot):
    """Records API calls instead of making them."""

    def __init__(self, db_path):
        super().__init__(token="t", chat_id="c", db_path=str(db_path))
        self.calls: list[tuple[str, dict]] = []

    def _call(self, method: str, payload: dict | None = None,
              http_timeout: float = 30.0) -> dict:
        self.calls.append((method, payload or {}))
        return {"result": []}

    def edits(self) -> list[dict]:
        return [p for m, p in self.calls if m == "editMessageText"]


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "t.db") as s:
        yield s


def deliver(store: Store, *opps: Opportunity) -> None:
    for o in opps:
        store.upsert(o)
    store.commit()
    store.mark_notified(o.fingerprint for o in opps)


def test_constructor_refuses_without_credentials(tmp_path):
    with pytest.raises(ValueError, match="bot token"):
        Bot(token="", chat_id="c", db_path=str(tmp_path / "t.db"))


# -- card rendering --------------------------------------------------------


def test_card_shows_position_and_carries_the_fingerprint():
    o = opp()
    o.score = 17.0
    card = render_card(o, index=0, total=5)
    assert "1 of 5" in card.text

    payloads = [
        b["callback_data"]
        for row in card.markup["inline_keyboard"]
        for b in row
        if "callback_data" in b
    ]
    assert f"a|{o.fingerprint}|0" in payloads
    assert f"s|{o.fingerprint}|0" in payloads


def test_card_links_straight_to_the_posting():
    o = opp(url="https://example.com/apply/42")
    urls = [b.get("url") for row in render_card(o, 0, 1).markup["inline_keyboard"] for b in row]
    assert "https://example.com/apply/42" in urls


def test_card_shows_a_deadline_when_present():
    o = opp(kind=Kind.SCHOLARSHIP, deadline=date(2026, 11, 4))
    assert "closes 2026-11-04" in render_card(o, 0, 1).text


def test_card_escapes_html_in_titles():
    o = opp(title="C++ & <script>alert(1)</script>")
    text = render_card(o, 0, 1).text
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_done_card_offers_resume_while_work_remains(store):
    deliver(store, opp())
    assert "Resume" in str(render_done(store).markup)


def test_done_card_is_terminal_at_inbox_zero(store):
    card = render_done(store)
    assert "Nothing left" in card.text
    assert card.markup["inline_keyboard"] == []


# -- callback handling -----------------------------------------------------


def test_applied_records_the_decision(store, tmp_path):
    o = opp()
    deliver(store, o)
    bot = FakeBot(tmp_path / "t.db")

    bot.handle_callback(
        {"id": "1", "data": f"a|{o.fingerprint}|0", "message": {"message_id": 9}}, store
    )

    row = store.conn.execute(
        "SELECT status FROM opportunities WHERE fingerprint = ?", (o.fingerprint,)
    ).fetchone()
    assert row["status"] == "applied"


def test_skipped_records_the_decision(store, tmp_path):
    o = opp()
    deliver(store, o)
    bot = FakeBot(tmp_path / "t.db")

    bot.handle_callback(
        {"id": "1", "data": f"s|{o.fingerprint}|0", "message": {"message_id": 9}}, store
    )

    row = store.conn.execute(
        "SELECT status FROM opportunities WHERE fingerprint = ?", (o.fingerprint,)
    ).fetchone()
    assert row["status"] == "skipped"


def test_later_leaves_the_item_undecided_and_advances(store, tmp_path):
    a = opp(title="Backend Engineer", org="Monzo")
    b = opp(title="Platform Engineer", org="Wise")
    a.score, b.score = 10.0, 5.0
    deliver(store, a, b)

    bot = FakeBot(tmp_path / "t.db")
    bot.handle_callback(
        {"id": "1", "data": f"l|{a.fingerprint}|0", "message": {"message_id": 9}}, store
    )

    assert store.count_undecided() == 2, "Later must not resolve anything"
    assert "Platform Engineer" in bot.edits()[-1]["text"]


def test_a_stale_button_cannot_resolve_the_wrong_job(store, tmp_path):
    """Actions carry the fingerprint, not the position. A button from an old
    card must never mark whatever now happens to sit at that index."""
    keep = opp(title="Backend Engineer", org="Monzo")
    gone = opp(title="Data Engineer", org="Lendable")
    deliver(store, keep, gone)

    bot = FakeBot(tmp_path / "t.db")
    # Tap a button minted when `gone` was at index 0, after things moved.
    bot.handle_callback(
        {"id": "1", "data": f"a|{gone.fingerprint}|0", "message": {"message_id": 9}}, store
    )

    statuses = dict(
        store.conn.execute("SELECT fingerprint, status FROM opportunities").fetchall()
    )
    assert statuses[gone.fingerprint] == "applied"
    assert statuses[keep.fingerprint] == "notified", "the wrong job was resolved"


def test_resolving_the_last_item_shows_the_done_card(store, tmp_path):
    o = opp()
    deliver(store, o)
    bot = FakeBot(tmp_path / "t.db")

    bot.handle_callback(
        {"id": "1", "data": f"a|{o.fingerprint}|0", "message": {"message_id": 9}}, store
    )
    assert "Nothing left" in bot.edits()[-1]["text"]


def test_every_tap_answers_the_callback_query(store, tmp_path):
    """Without answerCallbackQuery the button spins forever in the client."""
    o = opp()
    deliver(store, o)
    bot = FakeBot(tmp_path / "t.db")

    bot.handle_callback(
        {"id": "abc", "data": f"a|{o.fingerprint}|0", "message": {"message_id": 9}}, store
    )
    answers = [p for m, p in bot.calls if m == "answerCallbackQuery"]
    assert answers and answers[0]["callback_query_id"] == "abc"


def test_triage_opens_the_highest_scoring_item(store, tmp_path):
    low = opp(title="Backend Engineer", org="Monzo")
    high = opp(title="Founding Engineer", org="Pango")
    low.score, high.score = 3.0, 19.0
    deliver(store, low, high)

    bot = FakeBot(tmp_path / "t.db")
    bot.handle_callback({"id": "1", "data": "tri", "message": {"message_id": 9}}, store)
    assert "Founding Engineer" in bot.edits()[-1]["text"]


def test_unknown_payload_is_ignored_but_still_answered(store, tmp_path):
    bot = FakeBot(tmp_path / "t.db")
    bot.handle_callback({"id": "1", "data": "garbage", "message": {"message_id": 9}}, store)
    assert [m for m, _ in bot.calls] == ["answerCallbackQuery"]


def test_bad_callback_does_not_kill_the_poll_loop(store, tmp_path, monkeypatch):
    bot = FakeBot(tmp_path / "t.db")
    def fake(method, payload=None, http_timeout=30.0):
        if method != "getUpdates":
            return {}
        return {"result": [{
            "update_id": 1,
            "callback_query": {"id": "1", "data": "a|nope|notanint",
                               "message": {"message_id": 9}},
        }]}

    monkeypatch.setattr(bot, "_call", fake)
    assert bot.poll_once(store) == 1  # survived
    assert bot._offset == 2


def test_long_poll_payload_does_not_collide_with_the_transport_timeout(store, tmp_path):
    """Regression: getUpdates takes a field named `timeout`, and _call used to
    accept **kwargs alongside its own `timeout` parameter -- so the very first
    poll raised TypeError: got multiple values for keyword argument 'timeout'."""
    bot = FakeBot(tmp_path / "t.db")
    bot.poll_once(store)

    method, payload = bot.calls[0]
    assert method == "getUpdates"
    assert payload["timeout"] == bot.poll_timeout
