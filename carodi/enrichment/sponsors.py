"""Visa sponsor registers -- the part that makes this worth building.

Several countries publish, as open data, the list of employers legally permitted
to sponsor a foreign worker. Joining scraped jobs against those lists is what
turns "10,000 postings" into "the ones that could actually hire you".

    UK  Register of Licensed Sponsors: Workers
        https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers
    NL  IND Public Register of Recognised Sponsors
        https://ind.nl/en/public-register-recognised-sponsors

Both publish dated files whose URLs change on every update, so carodi does not
hardcode a download link. Download the current CSV by hand into data/registers/
(or point `path` at a URL in sources.yaml) and verify with `carodi registers`.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from rapidfuzz import fuzz, process

from carodi.models import Opportunity, org_key

log = logging.getLogger(__name__)

#: Column headings that have held the employer name across register revisions.
_NAME_COLUMNS = (
    "organisation name",
    "organization name",
    "name",
    "company",
    "employer",
    "bedrijfsnaam",
    "naam",
)


#: UK routes under which a company can hire a foreign software engineer off the
#: street. Everything else in the register -- Charity Worker, Creative Worker,
#: Ministers of Religion, Seasonal Worker -- is a licence to sponsor someone for
#: a different purpose entirely, and the Global Business Mobility routes require
#: you to already work for the group overseas. Counting those as "can sponsor
#: you" is a false positive on the one filter that has to be right.
DEFAULT_UK_ROUTES = ("skilled worker", "scale-up")


@dataclass
class SponsorRegister:
    """One country's register, indexed for fast exact-then-approximate lookup.

    The UK register is ~143k rows, so every lookup strategy here is O(1) or
    bounded by a first-token bucket. An earlier version scanned all keys per
    lookup and fuzzy-matched globally on a miss; at ~1000 employers per run
    that was hundreds of millions of comparisons.
    """

    country: str  # ISO-3166 alpha-2
    path: str
    name_column: str | None = None
    route_column: str | None = None
    routes: tuple[str, ...] | None = None
    threshold: int = 92

    _index: dict[str, str] = field(default_factory=dict, repr=False)
    _compact: dict[str, str] = field(default_factory=dict, repr=False)
    _by_first: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _skipped_routes: int = 0

    def __post_init__(self) -> None:
        self.country = self.country.upper()
        if self.routes is not None:
            self.routes = tuple(r.strip().casefold() for r in self.routes)
        self._load()

    def _read(self) -> str:
        if self.path.startswith(("http://", "https://")):
            r = httpx.get(self.path, timeout=60.0, follow_redirects=True)
            r.raise_for_status()
            return r.text
        p = Path(self.path)
        if not p.exists():
            raise FileNotFoundError(
                f"sponsor register for {self.country} not found at {p}. "
                "Download the current CSV from the government page and place it there."
            )
        return p.read_text(encoding="utf-8-sig", errors="replace")

    def _pick_column(self, headers: list[str]) -> str:
        if self.name_column:
            return self.name_column
        lowered = {h.strip().casefold(): h for h in headers}
        for candidate in _NAME_COLUMNS:
            for key, original in lowered.items():
                if candidate in key:
                    return original
        # Registers occasionally ship with the name in the first column, unlabelled.
        return headers[0]

    def _pick_route_column(self, headers: list[str]) -> str | None:
        if self.route_column:
            return self.route_column
        for header in headers:
            if header.strip().casefold() in ("route", "type & rating"):
                return header
        return None

    def _load(self) -> None:
        reader = csv.DictReader(io.StringIO(self._read()))
        if not reader.fieldnames:
            raise ValueError(f"{self.country} register at {self.path} has no header row")

        headers = list(reader.fieldnames)
        column = self._pick_column(headers)
        route_col = self._pick_route_column(headers) if self.routes else None

        for row in reader:
            if route_col is not None:
                route = (row.get(route_col) or "").strip().casefold()
                if route not in self.routes:
                    self._skipped_routes += 1
                    continue

            raw = (row.get(column) or "").strip()
            if not raw:
                continue
            key = org_key(raw)
            if not key:
                continue

            if key not in self._index:
                self._index[key] = raw
                self._compact.setdefault(key.replace(" ", ""), raw)
                self._by_first.setdefault(key.split(" ", 1)[0], []).append(key)

        if not self._index:
            raise ValueError(
                f"{self.country} register at {self.path} produced no entries"
                + (f" ({self._skipped_routes} rows filtered out by route)" if route_col else "")
            )
        log.info(
            "loaded %d sponsors for %s from %s%s",
            len(self._index),
            self.country,
            self.path,
            f" ({self._skipped_routes} rows excluded by route)" if self._skipped_routes else "",
        )

    def __len__(self) -> int:
        return len(self._index)

    def lookup(self, org: str) -> tuple[bool, str | None, int]:
        """Return (is_sponsor, matched_name, confidence 0-100).

        Confidence is graded rather than boolean because company-name matching
        is genuinely ambiguous and pretending otherwise is how you end up
        applying to a company that cannot sponsor you:

          100  exact match after normalizing legal suffixes
                 'GoCardless Ltd' -> 'GoCardless'
           95  identical once spacing is ignored
                 'Starlingbank' -> 'Starling Bank Limited'
           90  unique word-boundary prefix
                 'Monzo' -> 'Monzo Bank Limited'
           60  ambiguous prefix -- several registered employers share it
                 'Wise' -> 'Wise Guys Catering' *and* 'Wise Payments'
          >=threshold  typo-tolerant match within the same first token

        Anything below 100 travels with the matched name into the digest, so a
        wrong match is visible to you rather than silently trusted.

        Deliberately *not* token_set_ratio: it scores every subset at 100, which
        would rate 'Wise' -> 'Wise Guys Catering Ltd' a perfect match.
        """
        key = org_key(org)
        if not key:
            return False, None, 0
        if key in self._index:
            return True, self._index[key], 100

        # Board tokens arrive de-spaced ('starlingbank' from a careers URL),
        # which no prefix or edit-distance rule reliably recovers.
        compact = key.replace(" ", "")
        if match := self._compact.get(compact):
            return True, match, 95

        # Bounded by the first-token bucket rather than the whole register.
        candidates = self._by_first.get(key.split(" ", 1)[0], ())
        prefix = f"{key} "
        hits = [k for k in candidates if k.startswith(prefix)]
        if len(hits) == 1:
            return True, self._index[hits[0]], 90
        if len(hits) > 1:
            return True, self._index[min(hits, key=len)], 60

        if not candidates:
            return False, None, 0

        # Spelling differences only, and only against names already sharing a
        # first token -- a global fuzzy sweep over 143k rows per lookup is not
        # affordable and produces worse matches anyway.
        hit = process.extractOne(key, candidates, scorer=fuzz.ratio, score_cutoff=self.threshold)
        if hit is None:
            return False, None, 0
        matched_key, score, _ = hit
        return True, self._index[matched_key], int(score)


#: A match at or above this confidence is trustworthy enough to let a role
#: through the hard sponsorship gate. Prefix matches (90) are included: with
#: false negatives you silently never see a job you could have taken, which is
#: worse than an occasional wasted evening.
GATE_CONFIDENCE = 90

#: A match must be at least this confident to *earn score*. Ambiguous prefix
#: hits (60) are excluded -- across 121k registered companies, a single common
#: word like 'Marple' or 'Ramp' collides with something almost every time.
SCORE_CONFIDENCE = 90


class SponsorIndex:
    """All configured registers, applied as one enrichment pass."""

    def __init__(self, registers: list[SponsorRegister]):
        self.registers = registers

    @classmethod
    def from_config(cls, entries: list[dict]) -> "SponsorIndex":
        registers = []
        for entry in entries:
            if not entry.get("enabled", True):
                continue
            try:
                routes = entry.get("routes")
                registers.append(
                    SponsorRegister(
                        country=entry["country"],
                        path=entry["path"],
                        name_column=entry.get("name_column"),
                        route_column=entry.get("route_column"),
                        routes=tuple(routes) if routes else None,
                        threshold=entry.get("threshold", 92),
                    )
                )
            except (FileNotFoundError, ValueError) as exc:
                # A missing register must degrade to "unknown", never crash the run.
                log.warning("skipping sponsor register %s: %s", entry.get("country"), exc)
        return cls(registers)

    def apply(self, opp: Opportunity) -> None:
        """Annotate in place with sponsor flags.

        Must run *after* geo.annotate, because a sponsor licence only means
        anything in the country the job is actually in. A UK licence does not
        help you take a role in Antwerp, and 'Stripe Toronto is on the UK
        register' is not a fact worth scoring.
        """
        confirmed: list[str] = []  # match good enough to clear the hard gate
        scoring: list[str] = []  # ...and relevant to this job's country
        uncertain: list[str] = []
        countries = set(opp.countries)

        for reg in self.registers:
            ok, matched, score = reg.lookup(opp.org)
            cc = reg.country.casefold()
            opp.enrichment[f"sponsor_{cc}"] = ok
            if not ok:
                continue

            opp.enrichment[f"sponsor_{cc}_match"] = matched
            opp.enrichment[f"sponsor_{cc}_score"] = score

            if score >= GATE_CONFIDENCE:
                confirmed.append(reg.country)
            if score >= SCORE_CONFIDENCE and reg.country in countries:
                scoring.append(reg.country)
                if score < 100:
                    # Only surfaced when it is relevant to this job -- flagging
                    # a UK near-match on a Belgian role is pure noise.
                    uncertain.append(f"{reg.country}≈{matched} ({score}%)")

        opp.enrichment["sponsor_countries"] = confirmed
        opp.enrichment["sponsor_relevant"] = scoring
        opp.enrichment["sponsor_uncertain"] = uncertain
