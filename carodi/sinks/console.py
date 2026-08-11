from __future__ import annotations

from carodi.models import Opportunity
from carodi.sinks.base import (
    Sink,
    accountability_line,
    group_by_kind,
    location_line,
    source_label,
    sponsor_badge,
)


class ConsoleSink(Sink):
    """Default sink for `--dry-run`, so you can tune the profile without spamming yourself."""

    name = "console"

    def deliver(self, items: list[Opportunity], accountability: dict, errors: dict) -> None:
        if not items:
            print("no new matches")
        for kind, group in group_by_kind(items).items():
            print(f"\n=== {kind.upper()} ({len(group)}) ===")
            for opp in group:
                badge = sponsor_badge(opp)
                print(f"\n  [{opp.score:g}] {opp.title}")
                print(f"       {opp.org} · {location_line(opp)}{'  ' + badge if badge else ''}")
                print(f"       {opp.url}")
                if opp.deadline:
                    print(f"       deadline: {opp.deadline.isoformat()}")
                print(f"       {opp.fingerprint} · via {source_label(opp)} · {', '.join(opp.reasons)}")

        print(f"\n{accountability_line(accountability)}")
        for label, err in errors.items():
            print(f"  ! source {label} failed: {err}")
