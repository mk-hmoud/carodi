"""National employment-service job feeds.

Government portals are the best-behaved sources available: they are the
authoritative record for their country, they are free, they are not defending
themselves against scrapers, and their listings are legally required to be real
vacancies rather than pipeline-building fiction.

Coverage is uneven, though. Sweden publishes an open API with no key at all.
Germany and Norway have public APIs behind a registration step. Most others
have nothing, or feed EURES without exposing an endpoint of their own.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from datetime import datetime

from carodi.models import Kind, Opportunity
from carodi.sources.ats import guess_kind, guess_remote, strip_html
from carodi.sources.base import Source, register

log = logging.getLogger(__name__)


@register("jobtech")
class JobTech(Source):
    """Sweden -- Arbetsförmedlingen's Platsbanken, via the JobTech search API.

    Every job advertised through the Swedish Public Employment Service, which
    by law is most of the formal market: ~45k live ads. No API key, no
    registration, no rate limit worth pacing around.

    Free-text queries rather than the occupation taxonomy: taxonomy concept IDs
    are stable but opaque, and the filter downstream already decides what counts
    as your field. The queries here only need to be wide enough not to miss it.
    """

    API = "https://jobsearch.api.jobtechdev.se/search"

    def __init__(
        self,
        name: str = "jobtech-se",
        queries: list[str] | None = None,
        limit: int = 100,
    ):
        self.name = name
        self.queries = queries or ["software engineer", "utvecklare"]
        # The API caps `limit` at 100 per request.
        self.limit = min(limit, 100)

    def fetch(self) -> Iterator[Opportunity]:
        seen: set[str] = set()
        for query in self.queries:
            try:
                data = self._get_json(self.API, params={"q": query, "limit": self.limit})
            except Exception as exc:  # noqa: BLE001 - one query must not sink the rest
                log.warning("%s: query %r failed: %s", self.name, query, exc)
                continue

            for hit in data.get("hits", []):
                # The same ad matches several queries; dedupe here rather than
                # relying on the pipeline, so the counts in the run log are honest.
                ad_id = str(hit.get("id") or "")
                if not ad_id or ad_id in seen:
                    continue
                seen.add(ad_id)
                if opp := self._to_opportunity(hit):
                    yield opp

    def _to_opportunity(self, hit: dict) -> Opportunity | None:
        title = (hit.get("headline") or "").strip()
        url = hit.get("webpage_url")
        if not title or not url:
            return None

        employer = (hit.get("employer") or {}).get("name") or "unknown"
        address = hit.get("workplace_address") or {}
        # Municipality alone is not enough: geo.py knows Stockholm and Göteborg
        # but not Laholm or Skellefteå, and an unrecognised location falls back
        # to scanning the description. The source knows the country, so it says so.
        municipality = address.get("municipality") or address.get("region") or ""
        location = f"{municipality}, Sweden" if municipality else "Sweden"

        description = (hit.get("description") or {}).get("text") or ""
        occupation = (hit.get("occupation") or {}).get("label")

        return Opportunity(
            source=self.name,
            kind=guess_kind(title),
            title=title,
            org=employer,
            url=url,
            location_raw=location,
            remote=guess_remote(f"{location} {title}"),
            description=strip_html(description)[:4000],
            posted_at=_parse_dt(hit.get("publication_date")),
            deadline=_parse_date(hit.get("application_deadline")),
            tags=[t for t in (occupation,) if t],
        )


@register("gov_json")
class GovJson(Source):
    """Config-driven adapter for national feeds that need a key.

    Germany's Bundesagentur für Arbeit and Norway's NAV both publish real APIs,
    but both refuse anonymous calls -- Germany 403s the documented
    `X-API-KEY: jobboerse-jobsuche` header and its public OAuth client no longer
    issues tokens; NAV's feed 401s without a consumer token. Neither is a
    scraping problem, just a registration step.

    Rather than hardcode either, this maps an arbitrary JSON feed the same way
    `json_board` does, plus a header for the credential and a fixed country to
    stamp on every posting -- a national service does not repeat its own country
    in each listing, and geo.py cannot infer it from a town name it has never
    heard of.
    """

    def __init__(
        self,
        name: str,
        url: str,
        country: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        items_path: str = "",
        title_field: str = "title",
        org_field: str = "employer",
        url_field: str = "url",
        location_field: str = "location",
        description_field: str = "description",
    ):
        self.name = name
        self.url = url
        self.country = country
        self.headers = headers or {}
        self.params = params or {}
        self.items_path = items_path
        self.fields = {
            "title": title_field,
            "org": org_field,
            "url": url_field,
            "location": location_field,
            "description": description_field,
        }

    def _items(self, payload: object) -> Iterable[dict]:
        node = payload
        for part in filter(None, self.items_path.split(".")):
            if not isinstance(node, dict):
                return []
            node = node.get(part, [])
        return node if isinstance(node, list) else []

    def fetch(self) -> Iterator[Opportunity]:
        with self._client() as client:
            response = client.get(self.url, params=self.params, headers=self.headers)
            response.raise_for_status()
            payload = response.json()

        for item in self._items(payload):
            title = item.get(self.fields["title"])
            url = item.get(self.fields["url"])
            if not title or not url:
                continue
            town = str(item.get(self.fields["location"]) or "")
            location = f"{town}, {self.country}" if town else self.country
            yield Opportunity(
                source=self.name,
                kind=guess_kind(title),
                title=title,
                org=str(item.get(self.fields["org"]) or self.name),
                url=url,
                location_raw=location,
                remote=guess_remote(location),
                description=strip_html(str(item.get(self.fields["description"]) or ""))[:4000],
            )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _parse_date(value: str | None):
    dt = _parse_dt(value)
    return dt.date() if dt else None


__all__ = ["GovJson", "JobTech", "Kind"]
