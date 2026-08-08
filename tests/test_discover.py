from __future__ import annotations

import pytest

from carodi.discover import Discoverer, Hit, as_sources_yaml, candidate_tokens, name_confidence
from carodi.models import Opportunity, org_key
from carodi.store import Store


def opp(org: str, **kw) -> Opportunity:
    base = dict(
        source="test",
        title="Backend Engineer",
        org=org,
        url="https://example.com/job/1",
        location_raw="London, UK",
    )
    return Opportunity(**{**base, **kw})


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "t.db") as s:
        yield s


# -- token generation ------------------------------------------------------


@pytest.mark.parametrize(
    "key,expected",
    [
        ("monzo", ["monzo"]),
        ("monzo bank", ["monzobank", "monzo-bank"]),
        ("super payments", ["superpayments", "super-payments"]),
    ],
)
def test_candidate_tokens(key, expected):
    assert candidate_tokens(key) == expected


def test_a_short_first_word_is_not_probed_alone():
    """Calibration showed bare 'link' and 'apex' resolve to unrelated boards."""
    assert "link" not in candidate_tokens("link data management")
    assert "apex" not in candidate_tokens("apex housing")


def test_a_distinctive_first_word_is_worth_probing():
    assert "spryker" in candidate_tokens("spryker systems")


def test_empty_name_yields_nothing():
    assert candidate_tokens("") == []


# -- verification ----------------------------------------------------------


def test_exact_name_is_full_confidence():
    assert name_confidence("Monzo", "monzo") == 100


def test_legal_suffix_difference_still_verifies():
    assert name_confidence("GoCardless Limited", "gocardless") == 100


def test_prefix_match_is_trusted_when_distinctive():
    assert name_confidence("Spryker", "spryker systems") == 90


def test_generic_prefix_is_rejected():
    """Regression from calibration: 'Company' scored 100 against
    'Company 3 Studios UK Ltd' under token_set_ratio."""
    assert name_confidence("Company", "company 3 studios") == 0
    assert name_confidence("Future", "future intelligence technology") == 0


def test_unrelated_company_scores_zero():
    assert name_confidence("SentinelOne", "sentinel labs") == 0
    assert name_confidence("Apex Eye", "apex housing solutions") == 0


def test_missing_declared_name_scores_zero():
    assert name_confidence(None, "monzo") == 0


# -- harvesting ------------------------------------------------------------


def test_orgs_are_recorded_even_when_their_roles_are_filtered_out(store):
    """The seed list must include companies whose current openings all failed
    the filter -- they are still worth watching."""
    o = opp("Monzo")
    o.enrichment["sponsor_countries"] = ["GB"]
    store.record_orgs([o])
    store.commit()

    assert [s["org"] for s in store.seed_orgs()] == ["Monzo"]


def test_repeat_sightings_increment_rather_than_duplicate(store):
    for _ in range(3):
        o = opp("Monzo")
        o.enrichment["sponsor_countries"] = ["GB"]
        store.record_orgs([o])
    store.commit()

    [seed] = store.seed_orgs()
    assert seed["times_seen"] == 3


def test_seed_list_defaults_to_sponsor_verified_employers_only(store):
    sponsored = opp("Monzo")
    sponsored.enrichment["sponsor_countries"] = ["GB"]
    unsponsored = opp("Some US Startup")
    unsponsored.enrichment["sponsor_countries"] = []
    store.record_orgs([sponsored, unsponsored])
    store.commit()

    assert [s["org"] for s in store.seed_orgs()] == ["Monzo"]
    assert len(store.seed_orgs(sponsored_only=False)) == 2


# -- probing ---------------------------------------------------------------


class FakeClient:
    def __init__(self, responses: dict):
        self.responses = responses
        self.requested: list[str] = []

    def get(self, url: str):
        self.requested.append(url)
        return self.responses.get(url, FakeResponse(404))


class FakeResponse:
    def __init__(self, status: int, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_probe_records_and_caches_a_hit(store):
    disc = Discoverer(store, providers=["greenhouse"], delay=0)
    client = FakeClient(
        {"https://boards-api.greenhouse.io/v1/boards/monzo": FakeResponse(200, {"name": "Monzo"})}
    )

    found, declared, _ = disc.probe_token(client, "greenhouse", "monzo", "monzo")
    assert found and declared == "Monzo"

    # Second call must not hit the network again.
    found2, declared2, _ = disc.probe_token(client, "greenhouse", "monzo", "monzo")
    assert (found2, declared2) == (True, "Monzo")
    assert len(client.requested) == 1
    assert disc.cache_hits == 1


def test_negative_probes_are_cached_too(store):
    """Most companies have no board; re-asking nightly would be pure waste."""
    disc = Discoverer(store, providers=["greenhouse"], delay=0)
    client = FakeClient({})

    disc.probe_token(client, "greenhouse", "nope", "nope")
    disc.probe_token(client, "greenhouse", "nope", "nope")
    assert len(client.requested) == 1


def test_unknown_provider_is_rejected(store):
    with pytest.raises(ValueError, match="unknown provider"):
        Discoverer(store, providers=["greenhouse", "workday"])


# -- output ----------------------------------------------------------------


def hit(**kw) -> Hit:
    base = dict(
        org="Monzo", org_key="monzo", provider="greenhouse", token="monzo",
        declared_name="Monzo", confidence=100, job_count=12, sponsors=["GB"],
    )
    return Hit(**{**base, **kw})


def test_yaml_block_includes_verified_boards():
    out = as_sources_yaml([hit()])
    assert "- type: greenhouse" in out
    assert "- monzo" in out
    assert "[GB]" in out


def test_yaml_block_excludes_unverified_boards():
    """Ashby and Lever declare no company name, so their hits are review-only."""
    out = as_sources_yaml([hit(provider="ashby", confidence=0, declared_name=None)])
    assert "ashby" not in out
    assert "nothing met the confidence bar" in out


@pytest.mark.parametrize(
    "declared,key",
    [
        ("Company", "company 3 studios"),
        ("Future", "future intelligence technology"),
        ("Systems", "systems planning and analysis"),
        ("Solutions", "solutions and innovations"),
        ("Digital", "digital transformation partners"),
    ],
)
def test_generic_single_words_never_verify(declared, key):
    assert name_confidence(declared, key) == 0


def test_a_distinctive_multiword_prefix_still_verifies():
    # target_key is always already normalized by callers, so 'ltd' is gone.
    assert name_confidence("Super Payments Ltd", org_key("Super Payments Ltd")) == 100
    assert name_confidence("Extend", "extend robotics") == 90
