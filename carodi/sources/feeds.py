"""Generic feed and open-API sources.

`rss` is deliberately config-driven: most remote job boards publish a feed, so
adding one should mean editing sources.yaml, not writing Python.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from datetime import datetime
from time import mktime

import feedparser

from carodi.models import Kind, Opportunity
from carodi.sources.ats import guess_kind, guess_remote, strip_html
from carodi.sources.base import Source, register

log = logging.getLogger(__name__)


@register("rss")
class Rss(Source):
    """Any RSS/Atom job feed.

    `org_from_title` handles the common "Company: Job Title" convention used by
    WeWorkRemotely and friends, where the employer is only in the title string.
    """

    def __init__(
        self,
        name: str,
        url: str,
        org_from_title: bool = False,
        separator: str = ":",
        default_org: str = "",
        kind: str = "job",
    ):
        self.name = name
        self.url = url
        self.org_from_title = org_from_title
        self.separator = separator
        self.default_org = default_org
        self.kind = Kind(kind)

    def fetch(self) -> Iterator[Opportunity]:
        feed = feedparser.parse(self.url)
        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            org = self.default_org or self.name

            if self.org_from_title and self.separator in title:
                left, right = title.split(self.separator, 1)
                org, title = left.strip(), right.strip()

            location = entry.get("location") or ""
            struct = entry.get("published_parsed") or entry.get("updated_parsed")

            yield Opportunity(
                source=self.name,
                kind=self.kind if self.kind is not Kind.JOB else guess_kind(title),
                title=title,
                org=org,
                url=entry.link,
                location_raw=location,
                remote=guess_remote(f"{location} {title}"),
                description=strip_html(entry.get("summary", ""))[:4000],
                posted_at=datetime.fromtimestamp(mktime(struct)) if struct else None,
                tags=[t.term for t in entry.get("tags", []) if getattr(t, "term", None)],
            )


@register("json_board")
class JsonBoard(Source):
    """Open JSON job APIs (Remotive, Arbeitnow, Himalayas, RemoteOK, ...).

    They all return "a list of jobs with differently-named fields", so rather
    than one class per board this maps field names from config.
    """

    def __init__(
        self,
        name: str,
        url: str,
        items_path: str = "jobs",
        title_field: str = "title",
        org_field: str = "company_name",
        url_field: str = "url",
        location_field: str = "candidate_required_location",
        description_field: str = "description",
        tags_field: str | None = "tags",
    ):
        self.name = name
        self.url = url
        self.items_path = items_path
        self.fields = {
            "title": title_field,
            "org": org_field,
            "url": url_field,
            "location": location_field,
            "description": description_field,
        }
        self.tags_field = tags_field

    def _items(self, payload: object) -> Iterable[dict]:
        if not self.items_path:
            return payload if isinstance(payload, list) else []
        node = payload
        for part in self.items_path.split("."):
            if not isinstance(node, dict):
                return []
            node = node.get(part, [])
        return node if isinstance(node, list) else []

    def fetch(self) -> Iterator[Opportunity]:
        for item in self._items(self._get_json(self.url)):
            title = item.get(self.fields["title"])
            url = item.get(self.fields["url"])
            if not title or not url:
                continue

            location = str(item.get(self.fields["location"]) or "")
            tags = item.get(self.tags_field) if self.tags_field else None

            yield Opportunity(
                source=self.name,
                kind=guess_kind(title),
                title=title,
                org=str(item.get(self.fields["org"]) or self.name),
                url=url,
                location_raw=location,
                remote=guess_remote(location or "remote"),
                description=strip_html(str(item.get(self.fields["description"]) or ""))[:4000],
                tags=[str(t) for t in tags] if isinstance(tags, list) else [],
            )


@register("hn_whoishiring")
class HnWhoIsHiring(Source):
    """Top-level comments in the monthly Hacker News 'Who is hiring?' thread.

    Uses the public Algolia HN API. Freeform text, so the filter stage does the
    real work here -- but it surfaces roles that never reach a job board.
    """

    SEARCH = "https://hn.algolia.com/api/v1/search_by_date"

    #: The real monthly thread is always titled exactly
    #: "Ask HN: Who is hiring? (Month Year)". A substring check is not enough:
    #: it also matches meta-discussion like
    #: 'Ask HN: Why is the "Who is hiring?" post being re-aged?'.
    THREAD_TITLE = re.compile(r"^ask hn:\s*who is hiring\?\s*\(", re.IGNORECASE)

    def __init__(self, name: str = "hn", limit: int = 400, min_comments: int = 50):
        self.name = name
        self.limit = limit
        self.min_comments = min_comments

    def _latest_thread_id(self) -> int | None:
        data = self._get_json(
            self.SEARCH,
            params={"query": "Ask HN: Who is hiring?", "tags": "story", "hitsPerPage": 20},
        )
        candidates = [
            hit
            for hit in data.get("hits", [])
            if self.THREAD_TITLE.match((hit.get("title") or "").strip())
            and (hit.get("num_comments") or 0) >= self.min_comments
        ]
        if not candidates:
            log.warning("%s: no monthly 'Who is hiring?' thread found", self.name)
            return None
        # Results are newest-first; the freshest qualifying thread is this month's.
        return int(candidates[0]["objectID"])

    def fetch(self) -> Iterator[Opportunity]:
        thread_id = self._latest_thread_id()
        if thread_id is None:
            return

        data = self._get_json(
            self.SEARCH,
            params={"tags": f"comment,story_{thread_id}", "hitsPerPage": self.limit},
        )
        for hit in data.get("hits", []):
            # Only top-level comments are job ads. Replies are candidates asking
            # questions -- "Hi, is there any possibility for remote?" is not a
            # posting, but it looks like one to every filter downstream.
            if hit.get("parent_id") != thread_id:
                continue

            text = strip_html(hit.get("comment_text") or "")
            if len(text) < 60:
                continue
            # Convention: "Company | Role | Location | ..." on the first line.
            head = text.split("|")
            org = head[0].strip()[:80] if head else "unknown"
            title = head[1].strip()[:120] if len(head) > 1 else text[:120]
            yield Opportunity(
                source=self.name,
                kind=guess_kind(title),
                title=title,
                org=org,
                url=f"https://news.ycombinator.com/item?id={hit['objectID']}",
                location_raw=head[2].strip()[:120] if len(head) > 2 else "",
                remote=guess_remote(text[:400]),
                description=text[:4000],
            )
