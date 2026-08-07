"""Collapse the same opportunity arriving from several sources.

Two passes, cheap before expensive:
  1. exact fingerprint (normalized org + title + location)
  2. fuzzy title match within the same employer, which catches
     "Senior Backend Engineer" vs "Senior Backend Engineer (Remote)"
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from rapidfuzz import fuzz

from carodi.models import Opportunity, slug


def dedupe(items: Iterable[Opportunity], title_threshold: int = 90) -> list[Opportunity]:
    by_fingerprint: dict[str, Opportunity] = {}
    for opp in items:
        existing = by_fingerprint.get(opp.fingerprint)
        if existing is None:
            by_fingerprint[opp.fingerprint] = opp
        else:
            _merge(existing, opp)

    by_org: dict[str, list[Opportunity]] = defaultdict(list)
    for opp in by_fingerprint.values():
        by_org[opp.org_key].append(opp)

    kept: list[Opportunity] = []
    for group in by_org.values():
        survivors: list[Opportunity] = []
        for opp in group:
            title = slug(opp.title)
            match = next(
                (s for s in survivors if fuzz.token_set_ratio(title, slug(s.title)) >= title_threshold),
                None,
            )
            if match is None:
                survivors.append(opp)
            else:
                _merge(match, opp)
        kept.extend(survivors)
    return kept


def _merge(keep: Opportunity, drop: Opportunity) -> None:
    """Fold a duplicate into the record being kept, preferring richer data."""
    if len(drop.description) > len(keep.description):
        keep.description = drop.description
    if keep.posted_at is None:
        keep.posted_at = drop.posted_at
    if keep.deadline is None:
        keep.deadline = drop.deadline
    if keep.salary_min is None and drop.salary_min is not None:
        keep.salary_min, keep.salary_max = drop.salary_min, drop.salary_max
        keep.salary_currency = drop.salary_currency
    if not keep.location_raw:
        keep.location_raw = drop.location_raw

    keep.tags = sorted(set(keep.tags) | set(drop.tags))

    also = keep.enrichment.setdefault("also_seen_in", [])
    if isinstance(also, list) and drop.source not in also and drop.source != keep.source:
        also.append(drop.source)
