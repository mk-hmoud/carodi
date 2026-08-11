from __future__ import annotations

from abc import ABC, abstractmethod

from carodi.models import Opportunity
from carodi.store import Store


class Sink(ABC):
    name: str

    @abstractmethod
    def deliver(self, items: list[Opportunity], accountability: dict, errors: dict) -> None: ...


def group_by_kind(items: list[Opportunity]) -> dict[str, list[Opportunity]]:
    groups: dict[str, list[Opportunity]] = {}
    for opp in items:
        groups.setdefault(str(opp.kind), []).append(opp)
    # Deadlines first: they are the only items that expire.
    order = ["scholarship", "fellowship", "internship", "job"]
    return {k: groups[k] for k in order if k in groups}


def sponsor_badge(opp: Opportunity) -> str:
    """Badge for registers relevant to where this job actually is.

    Country-relevant only: a UK licence says nothing useful about a role in
    Antwerp, so showing it would just train you to ignore the badge.
    """
    countries = opp.enrichment.get("sponsor_relevant") or []
    if not countries:
        return ""
    # An inexact match names what it matched, so you can spot a wrong one
    # before spending an evening on the application.
    if uncertain := opp.enrichment.get("sponsor_uncertain"):
        return f"⚠️ sponsor? {'; '.join(uncertain)}"
    return f"✅ sponsor: {', '.join(countries)}"


def source_label(opp: Opportunity) -> str:
    """Which feed found this.

    With a dozen sources of very different quality, "where did this come from"
    is the fastest way to judge a listing at a glance -- and the fastest way to
    notice a source that has started producing junk.
    """
    # ATS sources namespace themselves as "greenhouse:monzo"; the board name is
    # already visible as the employer, so the provider alone is the useful part.
    return opp.source.split(":", 1)[0]


def location_line(opp: Opportunity) -> str:
    bits = [opp.location_raw or ", ".join(opp.countries) or "location unknown"]
    if str(opp.remote) != "onsite":
        bits.append(str(opp.remote))
    return " · ".join(b for b in bits if b)


def accountability_line(stats: Store | dict) -> str:
    s = stats if isinstance(stats, dict) else {}
    delivered = s.get("delivered", 0)
    applied = s.get("applied", 0)
    undecided = s.get("undecided", 0)
    days = s.get("days", 30)
    return (
        f"Last {days}d: {delivered} delivered · {applied} applied · {undecided} still undecided"
    )
