"""Applicant-tracking-system job boards.

These are the highest-signal sources available: public JSON, no auth, no rate
limiting worth worrying about, and unblocked because companies *want* these
endpoints read. You maintain the company list; you get their whole careers page.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable, Iterator
from datetime import datetime

from carodi.models import Kind, Opportunity, Remote
from carodi.sources.base import MultiSource, register

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """Drop tags and decode entities.

    Entities must be decoded *after* tag removal, or an encoded '&lt;b&gt;'
    in the source would turn into a real tag that then survives.
    """
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text or ""))).strip()


def guess_remote(location: str) -> Remote:
    loc = location.casefold()
    if "hybrid" in loc:
        return Remote.HYBRID
    if "remote" in loc or "anywhere" in loc or "distributed" in loc:
        return Remote.REMOTE
    return Remote.ONSITE if loc.strip() else Remote.UNKNOWN


#: Whole-word only. A substring test classifies "International Sales Manager"
#: as an internship, and no amount of excluding "internal" fixes that.
_INTERN_RE = re.compile(r"\b(intern|interns|internship|internships)\b", re.IGNORECASE)
_FELLOW_RE = re.compile(r"\b(fellow|fellows|fellowship|fellowships)\b", re.IGNORECASE)


def guess_kind(title: str) -> Kind:
    if _INTERN_RE.search(title or ""):
        return Kind.INTERNSHIP
    if _FELLOW_RE.search(title or ""):
        return Kind.FELLOWSHIP
    return Kind.JOB


@register("greenhouse")
class Greenhouse(MultiSource):
    """boards-api.greenhouse.io -- `boards` are the tokens in job board URLs."""

    API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"

    def __init__(self, boards: list[str], name: str = "greenhouse"):
        self.name = name
        self.boards = boards

    def targets(self) -> Iterable[str]:
        return self.boards

    def fetch_one(self, board: str) -> Iterator[Opportunity]:
        data = self._get_json(self.API.format(board=board))
        for job in data.get("jobs", []):
            location = (job.get("location") or {}).get("name", "")
            posted = job.get("updated_at") or job.get("first_published")
            yield Opportunity(
                source=f"{self.name}:{board}",
                kind=guess_kind(job["title"]),
                title=job["title"],
                org=board.replace("-", " ").title(),
                url=job["absolute_url"],
                location_raw=location,
                remote=guess_remote(location),
                description=strip_html(job.get("content", ""))[:4000],
                posted_at=_parse_dt(posted),
            )


@register("lever")
class Lever(MultiSource):
    API = "https://api.lever.co/v0/postings/{board}?mode=json"

    def __init__(self, boards: list[str], name: str = "lever"):
        self.name = name
        self.boards = boards

    def targets(self) -> Iterable[str]:
        return self.boards

    def fetch_one(self, board: str) -> Iterator[Opportunity]:
        for job in self._get_json(self.API.format(board=board)):
            cats = job.get("categories") or {}
            location = cats.get("location") or ""
            workplace = (job.get("workplaceType") or "").casefold()
            remote = Remote.REMOTE if workplace == "remote" else guess_remote(location)
            ts = job.get("createdAt")
            yield Opportunity(
                source=f"{self.name}:{board}",
                kind=guess_kind(job["text"]),
                title=job["text"],
                org=board.replace("-", " ").title(),
                url=job["hostedUrl"],
                location_raw=location,
                remote=remote,
                description=strip_html(job.get("descriptionPlain") or job.get("description", ""))[
                    :4000
                ],
                posted_at=datetime.fromtimestamp(ts / 1000) if ts else None,
                tags=[v for v in (cats.get("team"), cats.get("commitment")) if v],
            )


@register("ashby")
class Ashby(MultiSource):
    API = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"

    def __init__(self, boards: list[str], name: str = "ashby"):
        self.name = name
        self.boards = boards

    def targets(self) -> Iterable[str]:
        return self.boards

    def fetch_one(self, board: str) -> Iterator[Opportunity]:
        data = self._get_json(self.API.format(board=board))
        for job in data.get("jobs", []):
            location = job.get("location") or ""
            yield Opportunity(
                source=f"{self.name}:{board}",
                kind=guess_kind(job["title"]),
                title=job["title"],
                org=board.replace("-", " ").title(),
                url=job["jobUrl"],
                location_raw=location,
                remote=Remote.REMOTE if job.get("isRemote") else guess_remote(location),
                description=strip_html(job.get("descriptionPlain", ""))[:4000],
                posted_at=_parse_dt(job.get("publishedAt")),
                tags=[t for t in (job.get("department"), job.get("employmentType")) if t],
            )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
