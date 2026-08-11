from __future__ import annotations

from datetime import date

from carodi.models import Kind, Opportunity
from carodi.sinks.base import sponsor_badge
from carodi.sinks.telegram import LIMIT, TelegramSink


def opp(**kw) -> Opportunity:
    base = dict(
        source="test",
        title="Backend Engineer",
        org="Monzo",
        url="https://example.com/job/1",
        location_raw="London, UK",
    )
    return Opportunity(**{**base, **kw})


def sink() -> TelegramSink:
    return TelegramSink(token="t", chat_id="c")


STATS = {"days": 30, "delivered": 62, "applied": 3, "undecided": 47}


def test_sink_refuses_to_construct_without_credentials():
    import pytest

    with pytest.raises(ValueError, match="bot token"):
        TelegramSink(token="", chat_id="c")


def test_digest_groups_deadlines_above_jobs():
    o_job = opp()
    o_sch = opp(kind=Kind.SCHOLARSHIP, title="Chevening", deadline=date(2026, 11, 4))
    text = sink().render([o_job, o_sch], STATS, {})
    assert text.index("Scholarships") < text.index("Jobs")


def test_digest_reports_the_accountability_numbers():
    text = sink().render([opp()], STATS, {})
    assert "62 delivered · 3 applied · 47 still undecided" in text


def test_digest_names_failed_sources():
    text = sink().render([opp()], STATS, {"lever": "HTTPError: 404"})
    assert "sources failed: lever" in text


def test_digest_survives_an_empty_run():
    text = sink().render([], STATS, {})
    assert "no new matches" in text


def test_titles_with_html_are_escaped():
    text = sink().render([opp(title="C++ & <script>alert(1)</script> Engineer")], STATS, {})
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "C++ &amp;" in text


def test_fingerprint_is_included_so_you_can_mark_it():
    o = opp()
    assert o.fingerprint in sink().render([o], STATS, {})


def test_uncertain_sponsor_match_is_shown_rather_than_a_clean_tick():
    o = opp()
    o.enrichment["sponsor_relevant"] = ["GB"]
    o.enrichment["sponsor_uncertain"] = ["GB≈Wise Payments Ltd (90%)"]
    badge = sponsor_badge(o)
    assert badge.startswith("⚠️ sponsor?")
    assert "Wise Payments Ltd" in badge


def test_confirmed_sponsor_gets_a_clean_tick():
    o = opp()
    o.enrichment["sponsor_relevant"] = ["GB"]
    o.enrichment["sponsor_uncertain"] = []
    assert sponsor_badge(o) == "✅ sponsor: GB"


def test_long_digests_are_chunked_under_the_telegram_limit():
    items = [opp(title=f"Backend Engineer {i}", url=f"https://example.com/{i}") for i in range(200)]
    text = sink().render(items, STATS, {})
    chunks = sink()._chunks(text)
    assert len(chunks) > 1
    assert all(len(c) <= LIMIT for c in chunks)


def test_chunking_never_drops_content():
    items = [opp(title=f"Engineer {i}", url=f"https://example.com/{i}") for i in range(120)]
    text = sink().render(items, STATS, {})
    assert "\n".join(sink()._chunks(text)) == text


def test_digest_names_the_source_of_each_listing():
    """A dozen sources of very different quality: knowing which one found a
    listing is the fastest way to judge it, and to spot a source gone bad."""
    text = sink().render([opp(source="jobtech-se")], STATS, {})
    assert "via jobtech-se" in text


def test_ats_sources_show_the_provider_not_the_board():
    from carodi.sinks.base import source_label

    assert source_label(opp(source="greenhouse:monzo")) == "greenhouse"
    assert source_label(opp(source="eures")) == "eures"
