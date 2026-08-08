"""Hand-maintained deadline calendar.

Scholarships and fellowships are not a feed. There are a few dozen of them, they
open and close on fixed annual dates, and scraping them daily gets you 364 days
of "nothing new" and one day where you are already too late.

So they live in a YAML file you curate once, and this source re-emits them as
the deadline approaches. Downstream, they are ordinary Opportunity records --
same dedupe, same filter, same Telegram digest as a scraped job.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import yaml

from carodi.models import Kind, Opportunity
from carodi.sources.base import Source, register

#: Days before a deadline at which an entry is (re-)announced.
DEFAULT_ALERTS = [90, 60, 30, 14, 7, 3, 1]


@register("deadlines")
class Deadlines(Source):
    def __init__(
        self,
        file: str,
        name: str = "calendar",
        alerts: list[int] | None = None,
        window: int = 1,
    ):
        self.name = name
        self.file = Path(file)
        self.alerts = sorted(alerts or DEFAULT_ALERTS, reverse=True)
        # Days of catch-up *after* a threshold passes. window=1 fires only on
        # the exact day, so a single missed run loses that alert forever;
        # window=3 still fires up to two days late.
        self.window = window

    def _next_occurrence(self, deadline: date, recurring: bool, today: date) -> date:
        """Roll an annual deadline forward to its next occurrence."""
        if not recurring or deadline >= today:
            return deadline
        try:
            return deadline.replace(year=today.year if deadline.replace(year=today.year) >= today
                                    else today.year + 1)
        except ValueError:  # Feb 29 on a non-leap year
            return deadline.replace(year=today.year + 1, day=28)

    def fetch(self, today: date | None = None) -> Iterator[Opportunity]:
        today = today or date.today()
        if not self.file.exists():
            raise FileNotFoundError(f"deadline calendar not found: {self.file}")

        entries = yaml.safe_load(self.file.read_text()) or []
        for entry in entries:
            raw_deadline = entry["deadline"]
            deadline = (
                raw_deadline if isinstance(raw_deadline, date) else date.fromisoformat(raw_deadline)
            )
            deadline = self._next_occurrence(deadline, entry.get("recurring", True), today)

            days_left = (deadline - today).days
            if days_left < 0:
                continue

            # Fire on a threshold, or within `window` days after it passed --
            # the tolerance must be on the far side of the threshold, or a
            # missed run is exactly the case it fails to cover.
            threshold = next(
                (a for a in self.alerts if 0 <= a - days_left < self.window), None
            )
            if threshold is None:
                continue

            yield Opportunity(
                source=self.name,
                kind=Kind(entry.get("kind", "scholarship")),
                # The threshold, not days_left: the fingerprint is derived from
                # the title, so a changing number would make each day of the
                # catch-up window look like a brand new alert and notify twice.
                # The exact date still reaches you via the deadline field.
                title=f"{entry['name']} — {threshold}d warning",
                org=entry.get("org", entry["name"]),
                url=entry["url"],
                location_raw=", ".join(entry.get("countries", [])),
                countries=[c.upper() for c in entry.get("countries", [])],
                description=entry.get("notes", ""),
                deadline=deadline,
                tags=["deadline", *entry.get("tags", [])],
            )

    def upcoming(self, within_days: int = 365, today: date | None = None) -> list[tuple[date, str]]:
        """All calendar entries regardless of alert thresholds, for `carodi calendar`."""
        today = today or date.today()
        out: list[tuple[date, str]] = []
        for entry in yaml.safe_load(self.file.read_text()) or []:
            raw = entry["deadline"]
            d = raw if isinstance(raw, date) else date.fromisoformat(raw)
            d = self._next_occurrence(d, entry.get("recurring", True), today)
            if today <= d <= today + timedelta(days=within_days):
                out.append((d, entry["name"]))
        return sorted(out)
