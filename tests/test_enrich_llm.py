from __future__ import annotations

import pytest

from carodi.enrich_llm import Extraction, LlmEnricher, Sponsorship
from carodi.models import Opportunity
from carodi.pipeline import geo
from carodi.pipeline.rules import Rules
from carodi.store import Store

LONG = "We are hiring a backend engineer. " * 20


def opp(**kw) -> Opportunity:
    base = dict(
        source="test",
        title="Backend Engineer",
        org="Monzo",
        url="https://example.com/job/1",
        location_raw="London, UK",
        description=LONG,
    )
    return Opportunity(**{**base, **kw})


PROFILE = {
    "target_countries": ["GB", "DE", "NL"],
    "remote": {"accept": True},
    "hard_filters": {"require_sponsor_if_onsite": False, "exclude_titles": ["senior"]},
    "keywords": {"must_any": ["backend"]},
    "scoring": {"threshold": 6},
}


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "t.db") as s:
        yield s


class FakeEnricher(LlmEnricher):
    """Drives the pipeline without touching the network."""

    def __init__(self, store, result=None, boom=None, **kw):
        # Bypass LlmEnricher.__init__ so no client or API key is needed.
        self.client = None
        self.model = "fake-model"
        self.store = store
        self.max_per_run = kw.get("max_per_run", 40)
        self.min_interval = 0.0
        self.max_chars = 6000
        self.disable_thinking = True
        self._reset_counters()  # shared with the real enricher, so state can't drift
        self._result, self._boom = result, boom
        self.calls = 0

    def extract(self, opp):
        self.calls += 1
        if self._boom:
            raise self._boom
        return self._result


def extraction(**kw) -> Extraction:
    base = dict(
        sponsorship=Sponsorship.UNMENTIONED,
        is_entry_level=False,
        remote_restricted_to=[],
        one_line_fit="test",
    )
    return Extraction(**{**base, **kw})


# -- construction ----------------------------------------------------------


def test_missing_api_key_is_a_clear_error(store):
    with pytest.raises(ValueError, match="no API key"):
        LlmEnricher(api_key="", model="m", store=store)


# -- caching ---------------------------------------------------------------


def test_a_posting_is_read_once_ever(store):
    enricher = FakeEnricher(store, result=extraction())
    o = opp()

    enricher.enrich(o)
    enricher.enrich(o)

    assert enricher.calls == 1
    assert enricher.extracted == 1
    assert enricher.cached == 1


def test_cached_facts_survive_a_new_process(store, tmp_path):
    first = FakeEnricher(store, result=extraction(sponsorship=Sponsorship.OFFERED))
    o = opp()
    first.enrich(o)

    second = FakeEnricher(store, result=None)
    fresh = opp()
    second.enrich(fresh)

    assert second.calls == 0
    assert fresh.enrichment["llm"]["sponsorship"] == "offered"


def test_thin_descriptions_are_not_worth_reading(store):
    enricher = FakeEnricher(store, result=extraction())
    enricher.enrich(opp(description="Apply here."))
    assert enricher.calls == 0
    assert enricher.skipped == 1


def test_per_run_budget_is_respected(store):
    """Free-tier quotas are per-day; a cold cache must not burn through them."""
    enricher = FakeEnricher(store, result=extraction(), max_per_run=2)
    for i in range(5):
        enricher.enrich(opp(title=f"Backend Engineer {i}"))
    assert enricher.calls == 2
    assert enricher.skipped == 3


# -- failure handling ------------------------------------------------------


def test_an_api_failure_degrades_instead_of_crashing(store):
    enricher = FakeEnricher(store, boom=RuntimeError("quota exceeded"))
    o = opp()

    enricher.enrich(o)  # must not raise

    assert enricher.failed == 1
    assert "llm" not in o.enrichment


def test_an_unparseable_response_is_counted_not_stored(store):
    enricher = FakeEnricher(store, result=None)
    o = opp()
    enricher.enrich(o)
    assert enricher.failed == 1
    assert store.count_extractions() == 0


# -- the extract-don't-judge boundary --------------------------------------


def test_the_enricher_never_runs_on_a_rejected_role(store):
    """Hard eligibility is settled first, so a rejected posting is never paid for."""
    rules = Rules(PROFILE)
    enricher = FakeEnricher(store, result=extraction())

    verdict = rules.evaluate(opp(title="Senior Backend Engineer"), enrich=enricher.enrich)

    assert not verdict.passed
    assert enricher.calls == 0


def test_extracted_facts_cannot_rescue_a_rejected_role(store):
    rules = Rules(PROFILE)
    enricher = FakeEnricher(store, result=extraction(sponsorship=Sponsorship.OFFERED))

    # Excluded title: no extraction, however favourable, may overturn this.
    verdict = rules.evaluate(opp(title="Senior Backend Engineer"), enrich=enricher.enrich)
    assert not verdict.passed
    assert "excluded title" in verdict.rejected_by


def test_denied_sponsorship_demotes_but_never_drops(store):
    """A wrong 'denied' must cost a digest slot, not the opportunity itself."""
    rules = Rules(PROFILE)
    o = opp()
    geo.annotate(o)  # so the geography gate passes and we isolate the LLM fact
    o.enrichment["llm"] = extraction(sponsorship=Sponsorship.DENIED).model_dump(mode="json")

    assert rules.reject(o) is None, "an extracted fact must never cause a rejection"
    score, reasons = rules.score(o)
    assert score < 0
    assert any("denies sponsorship" in r for r in reasons)


# -- scoring ---------------------------------------------------------------


def test_offered_sponsorship_is_a_large_boost():
    rules = Rules(PROFILE)
    o = opp()
    o.enrichment["llm"] = extraction(sponsorship=Sponsorship.OFFERED).model_dump(mode="json")
    score, reasons = rules.score(o)
    assert score >= 8
    assert any("offers sponsorship" in r for r in reasons)


def test_excess_experience_scales_the_penalty():
    rules = Rules(PROFILE)
    mild = opp()
    mild.enrichment["llm"] = extraction(min_years_experience=4).model_dump(mode="json")
    steep = opp()
    steep.enrichment["llm"] = extraction(min_years_experience=10).model_dump(mode="json")

    assert rules.score(steep)[0] < rules.score(mild)[0] < 0


def test_experience_within_reach_is_not_penalized():
    rules = Rules(PROFILE)
    o = opp()
    o.enrichment["llm"] = extraction(min_years_experience=2).model_dump(mode="json")
    assert rules.score(o)[0] == 0


def test_geo_restricted_remote_is_penalized():
    """The most common way a promising listing turns out to be useless."""
    rules = Rules(PROFILE)
    o = opp()
    o.enrichment["llm"] = extraction(remote_restricted_to=["US", "CA"]).model_dump(mode="json")
    score, reasons = rules.score(o)
    assert score < 0
    assert any("remote limited to CA, US" in r for r in reasons)


def test_remote_restricted_to_a_target_country_is_fine():
    rules = Rules(PROFILE)
    o = opp()
    o.enrichment["llm"] = extraction(remote_restricted_to=["GB"]).model_dump(mode="json")
    assert rules.score(o)[0] == 0


def test_scoring_is_unchanged_when_the_stage_never_ran():
    """Every deterministic signal must work with the LLM disabled."""
    rules = Rules(PROFILE)
    with_llm, without = opp(), opp()
    with_llm.enrichment["llm"] = extraction().model_dump(mode="json")
    assert rules.score(with_llm)[0] == rules.score(without)[0]


def test_malformed_cached_payload_is_ignored():
    rules = Rules(PROFILE)
    o = opp()
    o.enrichment["llm"] = "not a dict"
    assert rules.score(o)[0] == 0


# -- quota handling --------------------------------------------------------


def test_the_budget_counts_attempts_not_successes(store):
    """Regression: the first live run made 65 pointless calls against an
    already-exhausted quota, because only successes counted toward the cap."""
    enricher = FakeEnricher(store, boom=RuntimeError("boom"), max_per_run=3)
    for i in range(10):
        enricher.enrich(opp(title=f"Backend Engineer {i}"))
    assert enricher.calls == 3


def test_quota_exhaustion_halts_the_stage_for_the_rest_of_the_run(store):
    enricher = FakeEnricher(
        store, boom=RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded"), max_per_run=40
    )
    for i in range(10):
        enricher.enrich(opp(title=f"Backend Engineer {i}"))

    assert enricher.calls == 1, "kept calling after the provider said stop"
    assert enricher.halted == "quota exhausted"
    assert enricher.stats()["halted"] == "quota exhausted"


def test_an_ordinary_failure_does_not_halt_the_stage(store):
    enricher = FakeEnricher(store, boom=RuntimeError("connection reset"), max_per_run=40)
    for i in range(3):
        enricher.enrich(opp(title=f"Backend Engineer {i}"))
    assert enricher.calls == 3
    assert enricher.halted is None


def test_cached_items_still_resolve_after_a_halt(store):
    """A halt must not blind the run to work already paid for."""
    warm = FakeEnricher(store, result=extraction(sponsorship=Sponsorship.OFFERED))
    known = opp(title="Backend Engineer known")
    warm.enrich(known)

    halted = FakeEnricher(store, boom=RuntimeError("429 RESOURCE_EXHAUSTED"))
    halted.enrich(opp(title="Backend Engineer new"))  # trips the halt
    fresh = opp(title="Backend Engineer known")
    halted.enrich(fresh)

    assert fresh.enrichment["llm"]["sponsorship"] == "offered"
