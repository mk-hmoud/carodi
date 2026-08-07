from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from carodi.enrichment.sponsors import SponsorIndex, SponsorRegister
from carodi.models import Kind, Opportunity, Remote, org_key
from carodi.pipeline import geo
from carodi.pipeline.dedupe import dedupe
from carodi.pipeline.rules import Rules
from carodi.sources.deadlines import Deadlines
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


PROFILE = {
    "target_countries": ["GB", "DE", "NL"],
    "remote": {"accept": True, "accept_anywhere": True, "accept_eu_wide": True},
    "hard_filters": {
        "require_sponsor_if_onsite": True,
        "sponsor_required_countries": ["GB", "NL"],
        "exclude_titles": ["senior", "staff"],
        "exclude_phrases": ["no visa sponsorship", "security clearance"],
        "max_age_days": 45,
    },
    "keywords": {
        "must_any": ["backend", "software engineer", "intern"],
        "boost": {"python": 3, "junior": 4},
        "penalize": {"10+ years": -8},
    },
    "scoring": {"threshold": 6, "target_country_bonus": 3, "sponsor_bonus": 5},
}


# -- normalization ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Monzo Bank Limited", "monzo bank"),
        ("Monzo", "monzo"),
        ("ACME Inc.", "acme"),
        ("Foo GmbH", "foo"),
    ],
)
def test_org_key_strips_legal_suffixes(raw, expected):
    assert org_key(raw) == expected


def test_fingerprint_ignores_url_so_cross_source_dupes_collapse():
    a = opp(source="greenhouse", url="https://boards.greenhouse.io/monzo/1")
    b = opp(source="hn", url="https://news.ycombinator.com/item?id=2")
    assert a.fingerprint == b.fingerprint


# -- geography -------------------------------------------------------------


@pytest.mark.parametrize(
    "location,expected",
    [
        ("London, UK", "GB"),
        ("Berlin", "DE"),
        ("Amsterdam, Netherlands", "NL"),
        ("Zurich", "CH"),
    ],
)
def test_detect_countries_from_cities(location, expected):
    assert expected in geo.detect_countries(location)


def test_uk_hint_does_not_fire_inside_other_words():
    codes = geo.detect_countries("Kyiv, Ukraine")
    assert "GB" not in codes
    assert "UA" in codes


def test_named_foreign_location_is_not_overridden_by_the_description():
    """Regression: a Beijing role was tagged US because its location was
    unrecognized and the description mentioned the company's US offices."""
    o = opp(
        location_raw="Beijing, China",
        description="Airbnb has offices in San Francisco, California and New York.",
    )
    geo.annotate(o)
    assert o.countries == ["CN"]


def test_description_fallback_still_applies_to_placeholder_locations():
    o = opp(location_raw="In-Office", description="This role is based in Austin, Texas.")
    geo.annotate(o)
    assert "US" in o.countries


@pytest.mark.parametrize("placeholder", ["", "Remote", "In-Office", "Various Locations", "HQ"])
def test_placeholder_locations_are_recognized_as_uninformative(placeholder):
    assert geo.uninformative_location(placeholder)


def test_a_real_city_is_not_uninformative():
    assert not geo.uninformative_location("Beijing, China")


def test_remote_without_country_signal_is_treated_as_open():
    o = opp(location_raw="Remote", remote=Remote.REMOTE)
    geo.annotate(o)
    assert o.enrichment["region_anywhere"] is True


def test_eu_wide_remote_is_flagged():
    o = opp(location_raw="Remote (Europe)", remote=Remote.REMOTE)
    geo.annotate(o)
    assert o.enrichment["region_eu_wide"] is True


# -- dedupe ----------------------------------------------------------------


def test_exact_duplicates_collapse_and_record_provenance():
    a = opp(source="greenhouse")
    b = opp(source="remotive")
    [kept] = dedupe([a, b])
    assert kept.enrichment["also_seen_in"] == ["remotive"]


def test_fuzzy_title_variants_within_one_org_collapse():
    a = opp(title="Backend Engineer")
    b = opp(title="Backend Engineer (Remote)")
    assert len(dedupe([a, b])) == 1


def test_different_orgs_never_collapse():
    a = opp(org="Monzo")
    b = opp(org="Starling Bank")
    assert len(dedupe([a, b])) == 2


def test_merge_prefers_the_richer_description():
    a = opp(description="short")
    b = opp(description="a considerably longer description with real detail")
    [kept] = dedupe([a, b])
    assert kept.description.startswith("a considerably longer")


# -- hard filters ----------------------------------------------------------


def test_onsite_in_target_country_without_sponsor_is_rejected():
    rules = Rules(PROFILE)
    o = opp(location_raw="London, UK")
    geo.annotate(o)
    assert "sponsor register" in rules.reject(o)


def test_onsite_in_target_country_with_sponsor_passes():
    rules = Rules(PROFILE)
    o = opp(location_raw="London, UK")
    geo.annotate(o)
    o.enrichment["sponsor_countries"] = ["GB"]
    assert rules.reject(o) is None


def test_onsite_outside_target_countries_is_rejected():
    rules = Rules(PROFILE)
    o = opp(location_raw="Bengaluru, India")
    geo.annotate(o)
    assert "not a target country" in rules.reject(o)


def test_us_only_remote_is_rejected():
    rules = Rules(PROFILE)
    o = opp(location_raw="Remote (US only)", remote=Remote.REMOTE)
    geo.annotate(o)
    assert "restricted" in rules.reject(o)


def test_worldwide_remote_passes():
    rules = Rules(PROFILE)
    o = opp(location_raw="Remote — Worldwide", remote=Remote.REMOTE)
    geo.annotate(o)
    assert rules.reject(o) is None


def test_excluded_title_is_rejected():
    rules = Rules(PROFILE)
    o = opp(title="Senior Backend Engineer")
    geo.annotate(o)
    assert "excluded title" in rules.reject(o)


def test_no_sponsorship_phrase_is_rejected():
    rules = Rules(PROFILE)
    o = opp(description="Please note: no visa sponsorship is offered for this role.")
    geo.annotate(o)
    assert "excluded phrase" in rules.reject(o)


def test_off_field_role_is_rejected_by_must_any():
    rules = Rules(PROFILE)
    o = opp(title="Account Executive", description="Sell things.")
    geo.annotate(o)
    assert rules.reject(o) == "no required keyword"


def test_stale_posting_is_rejected():
    rules = Rules(PROFILE)
    o = opp(posted_at=datetime.now() - timedelta(days=120), remote=Remote.REMOTE)
    geo.annotate(o)
    assert "older than" in rules.reject(o)


def test_curated_calendar_entries_bypass_keyword_and_geography_rules():
    rules = Rules(PROFILE)
    o = opp(
        kind=Kind.SCHOLARSHIP,
        title="Chevening Scholarships — closes in 30d",
        org="UK FCDO",
        location_raw="",
        deadline=date.today() + timedelta(days=30),
    )
    geo.annotate(o)
    verdict = rules.evaluate(o)
    assert verdict.passed, verdict.rejected_by


# -- scoring ---------------------------------------------------------------


def test_sponsor_and_target_country_dominate_the_score():
    rules = Rules(PROFILE)
    o = opp(location_raw="London, UK", description="Junior role, Python.")
    geo.annotate(o)
    o.enrichment["sponsor_countries"] = ["GB"]  # clears the hard gate
    o.enrichment["sponsor_relevant"] = ["GB"]  # ...and is relevant to a GB role
    verdict = rules.evaluate(o)
    assert verdict.passed
    assert verdict.score >= 12  # 5 sponsor + 3 country + 4 junior + 3 python


def test_low_scoring_match_is_held_back_by_the_threshold():
    rules = Rules(PROFILE)
    o = opp(title="Backend Engineer", location_raw="Remote — Worldwide", remote=Remote.REMOTE)
    geo.annotate(o)
    verdict = rules.evaluate(o)
    assert not verdict.passed
    assert "below threshold" in verdict.rejected_by


def test_symbol_keywords_do_not_degrade_into_single_letters():
    """Regression: 'c++' used to be punctuation-stripped to 'c', which then
    matched the bare letter c in any prose and scored every posting."""
    profile = {**PROFILE, "keywords": {**PROFILE["keywords"], "boost": {"c++": 2}}}
    rules = Rules(profile)

    innocent = opp(description="We work in a fast paced environment, c'est la vie.")
    assert rules.score(innocent)[0] == 0

    genuine = opp(description="Strong C++ experience required.")
    assert rules.score(genuine)[0] == 2


def test_dotted_keywords_survive_matching():
    profile = {**PROFILE, "keywords": {**PROFILE["keywords"], "boost": {"node.js": 3}}}
    rules = Rules(profile)
    assert rules.score(opp(description="Our stack is Node.js and Postgres."))[0] == 3


def test_keyword_does_not_match_inside_a_longer_word():
    profile = {**PROFILE, "keywords": {**PROFILE["keywords"], "boost": {"go": 5}}}
    rules = Rules(profile)
    assert rules.score(opp(description="A golang shop with good governance."))[0] == 0


def test_must_any_reads_the_title_not_the_boilerplate():
    """Regression: an 'Employee Lifecycle Analyst' passed because Ramp's
    description boilerplate happened to contain 'backend'."""
    rules = Rules(PROFILE)
    o = opp(
        title="Employee Lifecycle Analyst",
        description="Our backend engineers build software engineer tooling in Python.",
        remote=Remote.REMOTE,
        location_raw="Remote — Worldwide",
    )
    geo.annotate(o)
    assert rules.reject(o) == "no required keyword"


def test_must_any_scope_all_is_still_available():
    profile = {**PROFILE, "keywords": {**PROFILE["keywords"], "must_any_scope": "all"}}
    rules = Rules(profile)
    o = opp(
        title="Employee Lifecycle Analyst",
        description="Our backend engineers build tooling.",
        remote=Remote.REMOTE,
        location_raw="Remote — Worldwide",
    )
    geo.annotate(o)
    assert rules.reject(o) is None


def test_internship_bonus_is_not_double_counted_with_the_keyword_boost():
    profile = {**PROFILE, "keywords": {**PROFILE["keywords"], "boost": {"internship": 5}}}
    rules = Rules(profile)
    o = opp(title="Software Engineer Internship", kind=Kind.INTERNSHIP)
    score, reasons = rules.score(o)
    assert score == 5
    assert not any("internship role" in r for r in reasons)


# -- sponsor registers -----------------------------------------------------


@pytest.fixture
def uk_register(tmp_path: Path) -> SponsorRegister:
    csv = tmp_path / "uk.csv"
    csv.write_text(
        "Organisation Name,Town/City,Type & Rating,Route\n"
        "Monzo Bank Limited,London,Worker (A rating),Skilled Worker\n"
        "Deliveroo,London,Worker (A rating),Skilled Worker\n"
        "Wise Payments Ltd,London,Worker (A rating),Skilled Worker\n"
    )
    return SponsorRegister(country="GB", path=str(csv))


def test_register_loads_and_matches_exactly(uk_register):
    assert len(uk_register) == 3
    ok, matched, score = uk_register.lookup("Deliveroo")
    assert ok and score == 100 and matched == "Deliveroo"


def test_register_matches_across_legal_suffix_differences(uk_register):
    # 'Wise Payments Ltd' normalizes to 'wise payments'; 'Wise' is a unique prefix.
    ok, matched, score = uk_register.lookup("Wise")
    assert ok and matched == "Wise Payments Ltd" and score == 90


def test_register_rejects_unrelated_company(uk_register):
    ok, _, _ = uk_register.lookup("Some Random Startup")
    assert not ok


def test_ambiguous_prefix_is_flagged_with_low_confidence(tmp_path):
    """A bare 'Wise' cannot distinguish a payments company from a caterer."""
    csv = tmp_path / "uk.csv"
    csv.write_text(
        "Organisation Name\nWise Payments Ltd\nWise Guys Catering Limited\n"
    )
    reg = SponsorRegister(country="GB", path=str(csv))
    ok, _, score = reg.lookup("Wise")
    assert ok and score == 60


def test_subset_names_are_not_scored_as_perfect_matches(tmp_path):
    """Regression: token_set_ratio rated every subset 100, so an unrelated
    company sharing one word looked like a confirmed sponsor."""
    csv = tmp_path / "uk.csv"
    csv.write_text("Organisation Name\nAcme Global Catering Limited\n")
    reg = SponsorRegister(country="GB", path=str(csv))
    ok, _, _ = reg.lookup("Global")
    assert not ok


def test_inexact_matches_are_surfaced_for_review(uk_register):
    index = SponsorIndex([uk_register])
    o = opp(org="Wise", countries=["GB"])
    index.apply(o)
    assert o.enrichment["sponsor_uncertain"] == ["GB≈Wise Payments Ltd (90%)"]


def test_exact_matches_are_not_flagged_as_uncertain(uk_register):
    index = SponsorIndex([uk_register])
    o = opp(org="Deliveroo", countries=["GB"])
    index.apply(o)
    assert o.enrichment["sponsor_uncertain"] == []


def test_a_register_match_in_an_unrelated_country_earns_no_score(uk_register):
    """Regression: a Belgian role at 'Marple' matched 'MARPLE NEWS LTD' on the
    UK register and was scored as though that helped."""
    index = SponsorIndex([uk_register])
    o = opp(org="Deliveroo", location_raw="Antwerp, Belgium", countries=["BE"])
    index.apply(o)
    assert o.enrichment["sponsor_countries"] == ["GB"]  # the match is recorded
    assert o.enrichment["sponsor_relevant"] == []  # but earns nothing

    rules = Rules(PROFILE)
    _, reasons = rules.score(o)
    assert not any("sponsor register" in r for r in reasons)


def test_a_register_match_in_the_jobs_own_country_does_earn_score(uk_register):
    index = SponsorIndex([uk_register])
    o = opp(org="Deliveroo", location_raw="London, UK", countries=["GB"])
    index.apply(o)
    assert o.enrichment["sponsor_relevant"] == ["GB"]

    rules = Rules(PROFILE)
    _, reasons = rules.score(o)
    assert any("sponsor register: GB" in r for r in reasons)


def test_ambiguous_matches_are_recorded_but_never_scored(tmp_path):
    """A 60%-confidence hit clears nothing: across 121k registered companies a
    single common word collides with something almost every time."""
    csv = tmp_path / "uk.csv"
    csv.write_text("Organisation Name\nRamp Swaps Limited\nRamp Networks Ltd\n")
    index = SponsorIndex([SponsorRegister(country="GB", path=str(csv))])
    o = opp(org="Ramp", location_raw="London, UK", countries=["GB"])
    index.apply(o)
    assert o.enrichment["sponsor_gb"] is True
    assert o.enrichment["sponsor_relevant"] == []
    assert o.enrichment["sponsor_countries"] == []


def test_index_annotates_sponsor_countries(uk_register):
    index = SponsorIndex([uk_register])
    o = opp(org="Monzo")
    index.apply(o)
    assert o.enrichment["sponsor_countries"] == ["GB"]
    assert o.enrichment["sponsor_gb"] is True


def test_missing_register_degrades_instead_of_crashing(tmp_path):
    index = SponsorIndex.from_config([{"country": "GB", "path": str(tmp_path / "nope.csv")}])
    assert index.registers == []
    o = opp()
    index.apply(o)
    assert o.enrichment["sponsor_countries"] == []


# -- deadline calendar -----------------------------------------------------


@pytest.fixture
def calendar_file(tmp_path: Path) -> Path:
    path = tmp_path / "deadlines.yaml"
    path.write_text(
        "- name: Chevening\n"
        "  url: https://example.com/chevening\n"
        "  deadline: 2026-11-04\n"
        "  kind: scholarship\n"
        "  countries: [GB]\n"
    )
    return path


def test_calendar_stays_quiet_between_alert_thresholds(calendar_file):
    cal = Deadlines(file=str(calendar_file), alerts=[30])
    assert list(cal.fetch(today=date(2026, 10, 20))) == []  # 15 days out
    assert list(cal.fetch(today=date(2026, 9, 1))) == []  # 64 days out


def test_calendar_fires_on_an_alert_threshold(calendar_file):
    cal = Deadlines(file=str(calendar_file), alerts=[30])
    [hit] = list(cal.fetch(today=date(2026, 11, 4) - timedelta(days=30)))
    assert hit.kind is Kind.SCHOLARSHIP
    assert "30d" in hit.title


def test_calendar_skips_deadlines_that_have_passed(calendar_file):
    cal = Deadlines(file=str(calendar_file), alerts=[30])
    # Same day as the deadline is 0 days out, which is not an alert threshold.
    assert list(cal.fetch(today=date(2026, 11, 4))) == []


def test_calendar_rolls_past_annual_deadlines_forward(calendar_file):
    cal = Deadlines(file=str(calendar_file), alerts=[30])
    # A year after the seeded date, the same entry should target 2027.
    [hit] = list(cal.fetch(today=date(2027, 10, 5)))
    assert hit.deadline == date(2027, 11, 4)


# -- store -----------------------------------------------------------------


def test_store_reports_new_only_once(tmp_path):
    with Store(tmp_path / "t.db") as store:
        o = opp()
        assert store.upsert(o) is True
        assert store.upsert(o) is False


def test_decision_survives_the_job_being_rescraped(tmp_path):
    with Store(tmp_path / "t.db") as store:
        o = opp()
        store.upsert(o)
        store.commit()
        store.mark_notified([o.fingerprint])
        store.set_status(o.fingerprint, "applied")

        store.upsert(o)  # next run re-scrapes the same posting
        store.commit()

        row = store.conn.execute(
            "SELECT status FROM opportunities WHERE fingerprint = ?", (o.fingerprint,)
        ).fetchone()
        assert row["status"] == "applied"


def test_accountability_counts_applications(tmp_path):
    with Store(tmp_path / "t.db") as store:
        a, b = opp(title="Backend Engineer"), opp(title="Platform Engineer")
        store.upsert(a)
        store.upsert(b)
        store.commit()
        store.mark_notified([a.fingerprint, b.fingerprint])
        store.set_status(a.fingerprint, "applied")

        stats = store.accountability()
        assert stats["delivered"] == 2
        assert stats["applied"] == 1
        assert stats["undecided"] == 1
