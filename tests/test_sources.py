from __future__ import annotations

import pytest

from carodi.models import Kind
from carodi.sources.ats import guess_kind, guess_remote, strip_html
from carodi.sources.feeds import HnWhoIsHiring
from carodi.sources.gov import GovJson, JobTech


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Software Engineer Intern", Kind.INTERNSHIP),
        ("Internship, Backend", Kind.INTERNSHIP),
        ("Summer Interns 2027", Kind.INTERNSHIP),
        # Regression: a substring test made both of these internships.
        ("International Sales Manager", Kind.JOB),
        ("Internal Tools Engineer", Kind.JOB),
        ("Research Fellow", Kind.FELLOWSHIP),
        ("Backend Engineer", Kind.JOB),
    ],
)
def test_kind_is_guessed_on_whole_words(title, expected):
    assert guess_kind(title) is expected


@pytest.mark.parametrize(
    "title,matches",
    [
        ("Ask HN: Who is hiring? (August 2026)", True),
        ("Ask HN: Who is hiring? (December 2026)", True),
        # Regression: this meta-thread was selected over the real one, and it
        # had two comments instead of several hundred.
        ('Ask HN: Why is the "Who is hiring?" post being re-aged?', False),
        ("Ask HN: Who is hiring freelance developers?", False),
        ('Show HN: LiveComment "Who Is Hiring?" Plugin', False),
        ("Ask HN: Who wants to be hired? (August 2026)", False),
    ],
)
def test_only_the_canonical_monthly_thread_title_matches(title, matches):
    assert bool(HnWhoIsHiring.THREAD_TITLE.match(title)) is matches


def test_strip_html_collapses_markup_and_whitespace():
    assert strip_html("<p>Hello   <b>world</b></p>\n\n<br/>") == "Hello world"


@pytest.mark.parametrize(
    "location,expected",
    [
        ("Remote", "remote"),
        ("Hybrid - London", "hybrid"),
        ("London, UK", "onsite"),
        ("", "unknown"),
    ],
)
def test_remote_is_guessed_from_the_location_string(location, expected):
    assert str(guess_remote(location)) == expected


def test_strip_html_decodes_entities():
    assert strip_html("Hybrid (Belgium, 3 days&#x2F;week)") == "Hybrid (Belgium, 3 days/week)"
    assert strip_html("R&amp;D team") == "R&D team"


def test_entities_are_decoded_after_tags_so_encoded_markup_stays_inert():
    assert strip_html("&lt;script&gt;alert(1)&lt;/script&gt;") == "<script>alert(1)</script>"


class _FakeHn(HnWhoIsHiring):
    """Drives fetch() off canned Algolia payloads, so the test needs no network."""

    def __init__(self, thread_id, hits):
        super().__init__()
        self._thread_id, self._hits = thread_id, hits

    def _latest_thread_id(self):
        return self._thread_id

    def _get_json(self, url, **kwargs):
        return {"hits": self._hits}


def _comment(oid, parent, text):
    return {"objectID": str(oid), "parent_id": parent, "comment_text": text}


def test_replies_are_not_treated_as_job_postings():
    """Regression: a reply reading 'Hi, is there any possibility for remote?'
    was ingested as a posting, with its first sentence used as the company."""
    thread = 49156683
    hits = [
        _comment(1, thread, "Odoo | Software Developer | Belgium | odoo.com hiring engineers now"),
        _comment(2, 1, "Hi, I see you hire embedded folks. Any possibility for a remote internship?"),
    ]
    items = list(_FakeHn(thread, hits).fetch())
    assert len(items) == 1
    assert items[0].org == "Odoo"


def test_short_top_level_comments_are_skipped():
    thread = 1
    items = list(_FakeHn(thread, [_comment(2, thread, "we are hiring")]).fetch())
    assert items == []


# -- national employment services ------------------------------------------


class _FakeJobTech(JobTech):
    def __init__(self, hits, **kw):
        super().__init__(**kw)
        self._hits = hits

    def _get_json(self, url, **kwargs):
        return {"hits": self._hits}


def _se_hit(ad_id, headline="Backend Developer", municipality="Göteborg"):
    return {
        "id": ad_id,
        "headline": headline,
        "employer": {"name": "Volvo Cars"},
        "workplace_address": {"municipality": municipality},
        "webpage_url": f"https://arbetsformedlingen.se/platsbanken/annonser/{ad_id}",
        "description": {"text": "<p>We are hiring engineers.</p>"},
        "publication_date": "2026-08-11T16:12:20",
        "application_deadline": "2026-09-10T23:59:59",
        "occupation": {"label": "Mjukvaruutvecklare"},
    }


def test_jobtech_stamps_the_country_onto_the_location():
    """geo.py knows Stockholm but not Laholm. An unrecognised town falls back to
    scanning the description, so the source states the country it already knows."""
    [opp] = list(_FakeJobTech([_se_hit("1", municipality="Laholm")]).fetch())
    assert opp.location_raw == "Laholm, Sweden"

    from carodi.pipeline import geo

    geo.annotate(opp)
    assert opp.countries == ["SE"]


def test_jobtech_maps_the_fields_a_digest_needs():
    [opp] = list(_FakeJobTech([_se_hit("42")]).fetch())
    assert opp.org == "Volvo Cars"
    assert opp.url.path.endswith("/42")
    assert opp.description == "We are hiring engineers."
    assert opp.deadline and opp.deadline.isoformat() == "2026-09-10"
    assert "Mjukvaruutvecklare" in opp.tags


def test_jobtech_dedupes_across_queries():
    """One ad matches several queries; the run log should not double-count it."""
    src = _FakeJobTech([_se_hit("7"), _se_hit("7")], queries=["a", "b"])
    assert len(list(src.fetch())) == 1


def test_jobtech_skips_records_missing_a_title_or_url():
    bad = {"id": "9", "headline": "", "webpage_url": None}
    assert list(_FakeJobTech([bad]).fetch()) == []


def test_gov_json_stamps_a_fixed_country():
    """National services do not repeat their own country in each listing."""
    src = GovJson(name="x", url="https://e.test", country="Germany",
                  title_field="titel", org_field="firma", url_field="link",
                  location_field="ort", description_field="text")
    src._get_json = lambda *a, **k: []          # unused; fetch() uses the client
    assert src.country == "Germany"
    assert src.fields["title"] == "titel"
