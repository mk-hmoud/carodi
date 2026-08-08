"""The funnel.

    sources -> dedupe -> geo -> sponsors -> rules -> store

Every source is isolated: one board returning HTML instead of JSON logs a
warning and the run continues. A funnel that dies because one of forty sources
changed its schema is a funnel you stop trusting.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from carodi.config import Config
from carodi.enrichment import SponsorIndex
from carodi.models import Opportunity
from carodi.pipeline import geo
from carodi.pipeline.dedupe import dedupe
from carodi.pipeline.rules import Rules
from carodi.sources import build
from carodi.store import Store

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    fetched: int = 0
    after_dedupe: int = 0
    passed: int = 0
    orgs_seen: int = 0
    new: list[Opportunity] = field(default_factory=list)
    rejections: Counter = field(default_factory=Counter)
    source_counts: Counter = field(default_factory=Counter)
    source_errors: dict[str, str] = field(default_factory=dict)

    def as_stats(self) -> dict:
        return {
            "fetched": self.fetched,
            "after_dedupe": self.after_dedupe,
            "passed": self.passed,
            "orgs_seen": self.orgs_seen,
            "new": len(self.new),
            "top_rejections": dict(self.rejections.most_common(8)),
            "sources": dict(self.source_counts),
            "errors": self.source_errors,
        }


class Funnel:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self.rules = Rules(config.profile)
        self.sponsors = SponsorIndex.from_config(config.registers)

    def collect(self, result: RunResult) -> list[Opportunity]:
        items: list[Opportunity] = []
        for entry in self.config.sources:
            type_name = entry["type"]
            params = entry.get("params", {})
            label = params.get("name", type_name)
            try:
                source = build(type_name, params)
                produced = list(source.fetch())
            except Exception as exc:  # noqa: BLE001 - per-source isolation is the point
                log.warning("source %s failed: %s", label, exc)
                result.source_errors[label] = f"{type(exc).__name__}: {exc}"
                continue
            result.source_counts[label] = len(produced)
            items.extend(produced)
            log.info("source %s produced %d items", label, len(produced))
        return items

    def run(self, dry_run: bool = False) -> RunResult:
        result = RunResult()

        raw = self.collect(result)
        result.fetched = len(raw)

        merged = dedupe(raw)
        result.after_dedupe = len(merged)

        for opp in merged:
            geo.annotate(opp)
            self.sponsors.apply(opp)

            verdict = self.rules.evaluate(opp)
            # Recorded before the filter decides, on purpose: a company whose
            # current openings are all senior is still a lead worth watching,
            # and is exactly what `carodi discover` seeds from.
            if not verdict.passed:
                result.rejections[verdict.rejected_by or "unknown"] += 1
                continue

            opp.score = verdict.score
            opp.reasons = verdict.reasons
            result.passed += 1

            if dry_run:
                result.new.append(opp)
            elif self.store.upsert(opp):
                result.new.append(opp)

        if not dry_run:
            result.orgs_seen = self.store.record_orgs(merged)
            self.store.commit()

        result.new.sort(key=lambda o: o.score, reverse=True)
        return result
