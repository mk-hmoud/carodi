"""Find public ATS boards belonging to employers who can sponsor you.

The obvious design -- walk the 121,140-company sponsor register and guess board
tokens -- was calibrated and abandoned. Filtering the register by tech-sounding
names caught 0 of 14 obvious UK tech employers (Monzo, Wise, Revolut, Ocado,
Darktrace...), because real tech companies have short brandable names while
"solutions", "systems" and "technologies" mark IT consultancies and staffing
firms. 500 probes yielded ~6 true boards, none of them somewhere you would work.

So the seed is inverted. Every employer a job board has already listed is a real
tech employer by construction, and the funnel sees a few hundred per run for
free. Cross-reference those against the sponsor register, probe only the
survivors, and every lead is both a genuine employer and licensed to hire you.

Only Greenhouse declares its own company name back, so only Greenhouse hits can
be verified automatically. Lever and Ashby expose nothing but the slug, so their
hits are reported for review rather than trusted.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from carodi.models import org_key
from carodi.store import Store

log = logging.getLogger(__name__)

USER_AGENT = "carodi/0.1 (personal job funnel; board discovery)"

#: `verifiable` means the endpoint returns the company's own name, which is the
#: only thing that distinguishes a real match from a token collision.
PROVIDERS: dict[str, dict] = {
    "greenhouse": {
        "url": "https://boards-api.greenhouse.io/v1/boards/{token}",
        "verifiable": True,
    },
    "lever": {
        "url": "https://api.lever.co/v0/postings/{token}?mode=json",
        "verifiable": False,
    },
    "ashby": {
        "url": "https://api.ashbyhq.com/posting-api/job-board/{token}",
        "verifiable": False,
    },
}

#: Confidence at or above this is safe to add to sources.yaml unreviewed.
TRUSTED = 90

#: Words that identify nobody on their own. A board declaring itself "Company"
#: or "Future" is not evidence that it belongs to "Company 3 Studios UK Ltd" or
#: "Future Intelligence Technology Solutions" -- both of which the calibration
#: run accepted before this list existed. Length is no substitute: "company" is
#: seven characters and means nothing.
GENERIC_NAMES = frozenset(
    {
        "company", "future", "global", "group", "holdings", "international",
        "systems", "solutions", "services", "technology", "technologies",
        "digital", "data", "media", "labs", "studio", "studios", "consulting",
        "partners", "ventures", "capital", "enterprise", "enterprises", "works",
        "industries", "network", "networks", "software", "computing", "online",
        "united", "national", "general", "premier", "prime", "core", "next",
        "smart", "modern", "advanced", "integrated", "automated", "innovations",
    }
)


@dataclass
class Hit:
    org: str
    org_key: str
    provider: str
    token: str
    declared_name: str | None
    confidence: int
    job_count: int | None
    sponsors: list[str]

    @property
    def trusted(self) -> bool:
        return self.confidence >= TRUSTED


def candidate_tokens(key: str) -> list[str]:
    """Plausible board slugs for a normalized company name, best guess first."""
    parts = [p for p in key.split() if p]
    if not parts:
        return []
    tokens = ["".join(parts), "-".join(parts)]
    # A bare first word only when it is distinctive enough to not collide with
    # every other company sharing it -- 'link' and 'apex' produced exactly that
    # failure during calibration.
    if len(parts) > 1 and len(parts[0]) >= 6:
        tokens.append(parts[0])
    elif len(parts) == 1:
        tokens = [parts[0]]
    return list(dict.fromkeys(tokens))[:3]


def name_confidence(declared: str | None, target_key: str) -> int:
    """How sure are we that this board belongs to the company we meant?

    Deliberately strict, and deliberately not token_set_ratio -- which scores
    every subset at 100 and reported 'Company' as a perfect match for
    'Company 3 Studios UK Ltd' during calibration.
    """
    if not declared:
        return 0
    d = org_key(declared)
    if not d:
        return 0
    if d == target_key:
        return 100
    if d.replace(" ", "") == target_key.replace(" ", ""):
        return 95
    # One name being a word-boundary prefix of the other, provided the shorter
    # actually identifies somebody on its own.
    short, long = sorted((d, target_key), key=len)
    if len(short) >= 6 and long.startswith(short + " ") and not _is_generic(short):
        return 90
    return 0


def _is_generic(name: str) -> bool:
    """True when a name is too common to identify a specific company."""
    words = name.split()
    return len(words) == 1 and words[0] in GENERIC_NAMES


def _parse(provider: str, response: httpx.Response) -> tuple[str | None, int | None]:
    """Extract (declared company name, job count) from a 200 response."""
    try:
        data = response.json()
    except ValueError:
        return None, None

    if provider == "greenhouse":
        return (data.get("name") if isinstance(data, dict) else None), None
    if provider == "lever":
        return None, len(data) if isinstance(data, list) else None
    if provider == "ashby":
        jobs = data.get("jobs") if isinstance(data, dict) else None
        return None, len(jobs) if isinstance(jobs, list) else None
    return None, None


class Discoverer:
    def __init__(
        self,
        store: Store,
        providers: list[str] | None = None,
        delay: float = 0.15,
        timeout: float = 12.0,
    ):
        unknown = set(providers or []) - set(PROVIDERS)
        if unknown:
            raise ValueError(f"unknown provider(s): {', '.join(sorted(unknown))}")
        self.store = store
        self.providers = providers or list(PROVIDERS)
        self.delay = delay
        self.timeout = timeout
        self.requests_made = 0
        self.cache_hits = 0

    def probe_token(
        self, client: httpx.Client, provider: str, token: str, org_key_: str
    ) -> tuple[bool, str | None, int | None]:
        """Probe one token, using the cache so nothing is ever asked twice."""
        if cached := self.store.cached_probe(token, provider):
            self.cache_hits += 1
            return bool(cached["found"]), cached["declared_name"], cached["job_count"]

        spec = PROVIDERS[provider]
        declared, count, found = None, None, False
        try:
            r = client.get(spec["url"].format(token=token))
            self.requests_made += 1
            if r.status_code == 200:
                declared, count = _parse(provider, r)
                # An empty board is not evidence of the wrong company, but it is
                # not worth watching either.
                found = declared is not None or bool(count)
        except httpx.HTTPError as exc:
            log.debug("probe %s/%s failed: %s", provider, token, exc)

        confidence = name_confidence(declared, org_key_) if spec["verifiable"] else 0
        self.store.record_probe(
            token, provider, org_key_, found, declared, confidence, count
        )
        time.sleep(self.delay)
        return found, declared, count

    def run(self, sponsored_only: bool = True, limit: int | None = None) -> list[Hit]:
        seeds = self.store.seed_orgs(sponsored_only=sponsored_only, limit=limit)
        log.info("probing %d employer(s) across %s", len(seeds), ", ".join(self.providers))

        hits: list[Hit] = []
        with httpx.Client(
            timeout=self.timeout, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            for seed in seeds:
                key = seed["org_key"]
                for provider in self.providers:
                    for token in candidate_tokens(key):
                        found, declared, count = self.probe_token(client, provider, token, key)
                        if not found:
                            continue
                        confidence = (
                            name_confidence(declared, key)
                            if PROVIDERS[provider]["verifiable"]
                            else 0
                        )
                        hits.append(
                            Hit(
                                org=seed["org"], org_key=key, provider=provider, token=token,
                                declared_name=declared, confidence=confidence,
                                job_count=count, sponsors=seed["sponsors"],
                            )
                        )
                        break  # one board per provider per company is enough
        self.store.commit()
        return hits


def as_sources_yaml(hits: list[Hit]) -> str:
    """Emit a sources.yaml block for the boards safe to add unreviewed."""
    by_provider: dict[str, list[Hit]] = {}
    for hit in hits:
        if hit.trusted:
            by_provider.setdefault(hit.provider, []).append(hit)

    if not by_provider:
        return "# nothing met the confidence bar for automatic inclusion"

    lines = ["  # discovered by `carodi discover` — all sponsor-register verified"]
    for provider, group in sorted(by_provider.items()):
        lines.append(f"  - type: {provider}")
        lines.append("    enabled: true")
        lines.append("    params:")
        lines.append(f"      name: {provider}-discovered")
        lines.append("      boards:")
        for hit in sorted(group, key=lambda h: h.token):
            sponsors = "/".join(hit.sponsors) or "?"
            lines.append(f"        - {hit.token}    # {hit.declared_name or hit.org} [{sponsors}]")
    return "\n".join(lines)
