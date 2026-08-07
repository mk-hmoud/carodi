from __future__ import annotations

import pytest

from carodi.models import Kind
from carodi.sources.ats import guess_kind, guess_remote, strip_html
from carodi.sources.feeds import HnWhoIsHiring


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
