"""National employment-service job feeds.

Government portals are the best-behaved sources available: they are the
authoritative record for their country, they are free, they are not defending
themselves against scrapers, and their listings are legally required to be real
vacancies rather than pipeline-building fiction.

Coverage is uneven. Sweden and Germany are open with no key at all -- though
Germany's is easy to write off, because the documented header returns 403 on
/pc/v4/ and only works on /pc/v6/. Norway and France are real APIs behind a
registration step. Most others publish nothing, or feed EURES without exposing
an endpoint of their own.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from datetime import datetime

from carodi.models import Kind, Opportunity, Remote
from carodi.sources.ats import guess_kind, guess_remote, strip_html
from carodi.sources.base import Source, register

log = logging.getLogger(__name__)

#: The German agency names countries in German. geo.py speaks English, so the
#: common cross-border origins are translated rather than passed through.
_GERMAN_COUNTRY_NAMES = {
    "DEUTSCHLAND": "Germany",
    "OESTERREICH": "Austria",
    "ÖSTERREICH": "Austria",
    "SCHWEIZ": "Switzerland",
    "NIEDERLANDE": "Netherlands",
    "FRANKREICH": "France",
    "BELGIEN": "Belgium",
    "LUXEMBURG": "Luxembourg",
    "ITALIEN": "Italy",
    "SPANIEN": "Spain",
    "PORTUGAL": "Portugal",
    "POLEN": "Poland",
    "TSCHECHIEN": "Czechia",
    "DAENEMARK": "Denmark",
    "DÄNEMARK": "Denmark",
    "SCHWEDEN": "Sweden",
    "NORWEGEN": "Norway",
    "FINNLAND": "Finland",
    "IRLAND": "Ireland",
    "UNGARN": "Hungary",
    "GRIECHENLAND": "Greece",
}


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

    Norway's NAV feed 401s without a consumer token, and France Travail's OAuth
    endpoint answers `invalid_client` -- both are real APIs behind a
    registration step rather than a scraping problem.

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


__all__ = ["Arbeitsagentur", "Eures", "GovJson", "Himalayas", "JobTech", "Kind"]


@register("arbeitsagentur")
class Arbeitsagentur(Source):
    """Germany -- Bundesagentur für Arbeit, the federal employment agency.

    The largest job database in Germany, and open with no registration: the
    documented `X-API-Key: jobboerse-jobsuche` works on `/pc/v6/jobs`. Note the
    version -- v4 returns 403 with the same header, which is easy to mistake for
    the whole API being gated.
    """

    API = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
    KEY = "jobboerse-jobsuche"

    def __init__(
        self,
        name: str = "arbeitsagentur-de",
        queries: list[str] | None = None,
        size: int = 50,
        published_since: int = 7,
    ):
        self.name = name
        self.queries = queries or ["Softwareentwickler", "Software Engineer"]
        self.size = size
        # `veroeffentlichtseit` is in days; a daily digest has no use for a
        # three-month-old vacancy that the age filter would drop anyway.
        self.published_since = published_since

    def fetch(self) -> Iterator[Opportunity]:
        seen: set[str] = set()
        for query in self.queries:
            try:
                data = self._get_json(
                    self.API,
                    params={
                        "was": query,
                        "size": self.size,
                        "veroeffentlichtseit": self.published_since,
                    },
                    headers={"X-API-Key": self.KEY},
                )
            except Exception as exc:  # noqa: BLE001 - one query must not sink the rest
                log.warning("%s: query %r failed: %s", self.name, query, exc)
                continue

            for job in data.get("ergebnisliste") or []:
                ref = str(job.get("referenznummer") or "")
                if not ref or ref in seen:
                    continue
                seen.add(ref)
                if opp := self._to_opportunity(job, ref):
                    yield opp

    def _to_opportunity(self, job: dict, ref: str) -> Opportunity | None:
        title = (job.get("stellenangebotsTitel") or job.get("hauptberuf") or "").strip()
        if not title:
            return None

        address = ((job.get("stellenlokationen") or [{}])[0]).get("adresse") or {}
        town = (address.get("ort") or "").split(",")[0].strip()
        # Do not assume Germany. The agency is EURES-connected and returns
        # cross-border vacancies -- a "Softwareentwickler" search comes back
        # full of Austrian listings, which are welcome but must not be
        # mislabelled. Read the country the record states.
        country = _GERMAN_COUNTRY_NAMES.get(
            (address.get("land") or "").strip().upper(), "Germany"
        )
        location = f"{town}, {country}" if town else country

        return Opportunity(
            source=self.name,
            kind=guess_kind(title),
            title=title,
            org=(job.get("firma") or "unknown").strip(),
            # externeURL is the employer's own posting where present; otherwise
            # the agency's public detail page. The API's detail endpoint would
            # add one request per posting for a description we mostly do not
            # get to use.
            url=job.get("externeURL")
            or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}",
            location_raw=location,
            # The one structured remote signal the list response carries.
            remote=Remote.HYBRID if job.get("homeofficemoeglich") else guess_remote(location),
            posted_at=_parse_dt(job.get("datumErsteVeroeffentlichung")),
            tags=[b for b in (job.get("alleBerufe") or []) if isinstance(b, str)],
        )


@register("himalayas")
class Himalayas(Source):
    """Remote-only roles, with the restriction data most boards omit.

    Every listing is verified remote, and each carries `locationRestrictions`
    (country names) and `timezoneRestrictions` (UTC offsets). The offsets are
    the useful part: a role open only to [-10..-5] is US-hours work, which no
    amount of "remote" in the title tells you.
    """

    API = "https://himalayas.app/jobs/api"

    def __init__(self, name: str = "himalayas", pages: int = 5, per_page: int = 20):
        self.name = name
        # The API caps a page at 20 following its own performance work.
        self.per_page = min(per_page, 20)
        self.pages = pages

    def fetch(self) -> Iterator[Opportunity]:
        for page in range(self.pages):
            try:
                data = self._get_json(
                    self.API, params={"limit": self.per_page, "offset": page * self.per_page}
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: page %d failed: %s", self.name, page, exc)
                return
            jobs = data.get("jobs") or []
            if not jobs:
                return
            for job in jobs:
                if opp := self._to_opportunity(job):
                    yield opp

    def _to_opportunity(self, job: dict) -> Opportunity | None:
        title = (job.get("title") or "").strip()
        url = job.get("applicationLink")
        if not title or not url:
            return None

        restrictions = [str(r) for r in (job.get("locationRestrictions") or [])]
        offsets = [int(t) for t in (job.get("timezoneRestrictions") or []) if isinstance(t, int)]

        opp = Opportunity(
            source=self.name,
            kind=guess_kind(title),
            title=title,
            org=(job.get("companyName") or "unknown").strip(),
            url=url,
            location_raw=", ".join(restrictions) or "Remote",
            remote=Remote.REMOTE,
            description=strip_html(job.get("description") or job.get("excerpt") or "")[:4000],
            posted_at=_from_epoch(job.get("pubDate")),
            tags=[s for s in (job.get("seniority") or []) if isinstance(s, str)],
        )
        # Handed to the rules rather than acted on here: a source reports, it
        # does not decide. See Rules._score_timezone.
        if offsets:
            opp.enrichment["timezone_offsets"] = sorted(set(offsets))
        return opp


def _from_epoch(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value))
    except (TypeError, ValueError, OSError):
        return None


@register("eures")
class Eures(Source):
    """The European Commission's job mobility portal -- 250k+ vacancies, 31 countries.

    Undocumented but public: a POST search endpoint the portal's own front end
    uses. No key, no registration. `locationMap` gives clean ISO country codes,
    and records carry full description text, which is what the extraction stage
    needs.

    One honest caveat: EURES exists to support free movement of EU/EEA
    *citizens*. A listing appearing here is not evidence the employer will
    sponsor a non-EU national -- these are simply real vacancies with unusually
    good metadata. `euresFlag` marks employers interested in cross-border
    recruitment within the EEA, which is a different thing again.

    Volume is the real constraint: an unfiltered search matches a quarter of a
    million rows, so queries and countries are mandatory and paging is capped.
    """

    API = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
    DETAIL = "https://europa.eu/eures/portal/jv-se/jv-details/{id}?lang=en"
    #: The endpoint 400s above 50.
    MAX_PER_PAGE = 50

    def __init__(
        self,
        name: str = "eures",
        queries: list[str] | None = None,
        countries: list[str] | None = None,
        pages: int = 2,
        per_page: int = 50,
    ):
        self.name = name
        self.queries = queries or ["software engineer"]
        self.countries = [c.upper() for c in (countries or [])]
        self.pages = pages
        self.per_page = min(per_page, self.MAX_PER_PAGE)

    def _search(self, query: str, page: int) -> dict:
        with self._client(timeout=40.0) as client:
            response = client.post(
                self.API,
                json={
                    "keywords": [{"keyword": query, "specificSearchCode": "EVERYWHERE"}],
                    "locationCodes": self.countries,
                    "resultsPerPage": self.per_page,
                    "page": page,
                    "sortSearch": "BEST_MATCH",
                    "sessionId": "",
                },
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()

    def fetch(self) -> Iterator[Opportunity]:
        seen: set[str] = set()
        for query in self.queries:
            for page in range(1, self.pages + 1):
                try:
                    data = self._search(query, page)
                except Exception as exc:  # noqa: BLE001 - one page must not sink the rest
                    log.warning("%s: %r page %d failed: %s", self.name, query, page, exc)
                    break

                rows = data.get("jvs") or []
                if not rows:
                    break
                for row in rows:
                    jv_id = str(row.get("id") or "")
                    if not jv_id or jv_id in seen:
                        continue
                    seen.add(jv_id)
                    if opp := self._to_opportunity(row, jv_id):
                        yield opp

    def _to_opportunity(self, row: dict, jv_id: str) -> Opportunity | None:
        title = (row.get("title") or "").strip()
        if not title:
            return None

        # locationMap is {"DE": ["DE929"]} -- ISO country code to NUTS regions.
        # The country codes are exactly what geo.py wants, so hand them over
        # directly rather than round-tripping through a place name.
        location_map = row.get("locationMap") or {}
        countries = sorted(str(c).upper() for c in location_map if c)

        employer = (row.get("employer") or {}).get("name") or ""
        # EURES anonymises many employers; "not specified" in five languages is
        # noise in a digest, so normalise it.
        if employer.strip().lower() in _UNNAMED_EMPLOYERS or not employer.strip():
            employer = "undisclosed employer"

        opp = Opportunity(
            source=self.name,
            kind=guess_kind(title),
            title=title,
            org=employer.strip(),
            url=self.DETAIL.format(id=jv_id),
            location_raw=", ".join(countries) or "Europe",
            countries=countries,
            description=strip_html(row.get("description") or "")[:4000],
            posted_at=_from_epoch_ms(row.get("creationDate")),
        )
        if row.get("euresFlag"):
            opp.enrichment["eures_cross_border"] = True
        return opp


#: EURES lets employers stay anonymous; the placeholder varies by language.
_UNNAMED_EMPLOYERS = {
    "non renseigné", "not specified", "nicht angegeben", "no especificado",
    "non specificato", "niet gespecificeerd", "confidential", "n/a",
}


def _from_epoch_ms(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000)
    except (TypeError, ValueError, OSError):
        return None
