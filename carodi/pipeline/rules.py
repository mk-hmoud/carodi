"""Deterministic eligibility filter and score.

Two distinct jobs, kept separate on purpose:

  reject()  hard eligibility. Can you legally take this? No score can rescue a
            role that requires work authorization you do not have.
  score()   soft ranking among the survivors.

Keeping them apart is what makes an LLM stage safe to add later: it should only
ever reorder what has already passed reject(), never overrule it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from carodi.models import Kind, Opportunity, Remote
from carodi.pipeline.matching import find_phrase, matches, normalize_text


@dataclass
class Verdict:
    passed: bool
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    rejected_by: str | None = None


def country_codes(values: list, field: str) -> set[str]:
    """Validate a list of ISO country codes from YAML.

    YAML 1.1 parses bare `NO` (Norway) as boolean false, and `ON`/`OFF` are
    booleans too. That produces a baffling failure three layers down, so catch
    it here and say exactly what to fix.
    """
    codes: set[str] = set()
    for value in values or []:
        if isinstance(value, bool):
            raise ValueError(
                f"{field}: got boolean {value!r} instead of a country code. "
                "YAML reads bare NO/ON/OFF as booleans — quote them, e.g. \"NO\"."
            )
        codes.add(str(value).upper())
    return codes


class Rules:
    def __init__(self, profile: dict):
        self.profile = profile
        self.targets = country_codes(profile.get("target_countries", []), "target_countries")

        remote_cfg = profile.get("remote", {})
        self.accept_remote = remote_cfg.get("accept", True)
        self.accept_anywhere = remote_cfg.get("accept_anywhere", True)
        self.accept_eu_wide = remote_cfg.get("accept_eu_wide", True)

        hard = profile.get("hard_filters", {})
        self.require_sponsor_onsite = hard.get("require_sponsor_if_onsite", True)
        self.sponsor_required_countries = country_codes(
            hard.get("sponsor_required_countries", ["GB", "NL"]),
            "hard_filters.sponsor_required_countries",
        )
        self.exclude_phrases = hard.get("exclude_phrases", [])
        self.exclude_titles = hard.get("exclude_titles", [])
        self.max_age_days = hard.get("max_age_days", 45)

        kw = profile.get("keywords", {})
        self.must_any = kw.get("must_any", [])
        # "title" checks the job title only; "all" also reads the description.
        # Titles are far more honest -- descriptions list every technology the
        # company has ever touched, so "all" lets non-engineering roles through.
        self.must_any_scope = kw.get("must_any_scope", "title")
        self.boost = {normalize_text(k): float(v) for k, v in (kw.get("boost") or {}).items()}
        self.penalize = {normalize_text(k): float(v) for k, v in (kw.get("penalize") or {}).items()}

        self.threshold = float(profile.get("scoring", {}).get("threshold", 4))
        self.country_bonus = float(profile.get("scoring", {}).get("target_country_bonus", 3))
        self.sponsor_bonus = float(profile.get("scoring", {}).get("sponsor_bonus", 5))

    # -- hard eligibility -----------------------------------------------------

    def reject(self, opp: Opportunity) -> str | None:
        """Return a rejection reason, or None if the opportunity is eligible."""
        # Curated calendar entries skip every rule below. You put them in the
        # file by hand, so keyword and geography filters can only get in the
        # way -- a scholarship's "location" is where you would study, not
        # where you must already be, and its title says "Erasmus Mundus",
        # not "backend engineer".
        if opp.kind in (Kind.SCHOLARSHIP, Kind.FELLOWSHIP):
            return None

        hay = opp.haystack()
        title = normalize_text(opp.title)

        if hit := find_phrase(title, self.exclude_titles):
            return f"excluded title ({hit})"
        if hit := find_phrase(hay, self.exclude_phrases):
            return f"excluded phrase ({hit})"

        scope = opp.title_haystack() if self.must_any_scope == "title" else hay
        if self.must_any and not find_phrase(scope, self.must_any):
            return "no required keyword"

        if self.max_age_days and opp.posted_at:
            if opp.posted_at < datetime.now() - timedelta(days=self.max_age_days):
                return f"older than {self.max_age_days}d"

        return self._reject_on_geography(opp)

    def _reject_on_geography(self, opp: Opportunity) -> str | None:
        in_target = bool(self.targets & set(opp.countries))
        anywhere = bool(opp.enrichment.get("region_anywhere"))
        eu_wide = bool(opp.enrichment.get("region_eu_wide"))

        if opp.remote is Remote.REMOTE:
            if not self.accept_remote:
                return "remote not accepted"
            if anywhere and self.accept_anywhere:
                return None
            if eu_wide and self.accept_eu_wide:
                return None
            if in_target:
                return None
            return f"remote but restricted to {', '.join(opp.countries) or 'unknown region'}"

        # Onsite or hybrid: must be somewhere you want to go...
        if not in_target:
            where = ", ".join(opp.countries) or "unknown location"
            return f"onsite in {where}, not a target country"

        # ...and, where the country publishes a register, at an employer that
        # is actually licensed to sponsor you.
        if self.require_sponsor_onsite:
            gated = self.sponsor_required_countries & set(opp.countries)
            if gated:
                sponsors = set(opp.enrichment.get("sponsor_countries") or [])
                if not (gated & sponsors):
                    return f"{'/'.join(sorted(gated))} employer not on sponsor register"
        return None

    # -- soft ranking ---------------------------------------------------------

    def score(self, opp: Opportunity) -> tuple[float, list[str]]:
        hay = opp.haystack()
        score = 0.0
        reasons: list[str] = []

        for term, weight in self.boost.items():
            if matches(hay, term):
                score += weight
                reasons.append(f"+{weight:g} {term}")

        for term, weight in self.penalize.items():
            if matches(hay, term):
                score += weight
                reasons.append(f"{weight:+g} {term}")

        # Only registers for a country this job is actually in. Being on the UK
        # register is worth nothing to a role in Toronto.
        if sponsors := (opp.enrichment.get("sponsor_relevant") or []):
            score += self.sponsor_bonus
            reasons.append(f"+{self.sponsor_bonus:g} sponsor register: {', '.join(sponsors)}")

        if hit := self.targets & set(opp.countries):
            score += self.country_bonus
            reasons.append(f"+{self.country_bonus:g} target country: {', '.join(sorted(hit))}")

        if opp.kind is Kind.INTERNSHIP and not matches(hay, "internship"):
            score += 1
            reasons.append("+1 internship role")

        if opp.deadline:
            score += 2
            reasons.append("+2 has deadline")

        return score, reasons

    def evaluate(self, opp: Opportunity) -> Verdict:
        if reason := self.reject(opp):
            return Verdict(passed=False, rejected_by=reason)
        score, reasons = self.score(opp)

        # Curated entries are exempt from the score threshold as well: you
        # already decided they matter by putting them in the calendar.
        if opp.kind in (Kind.SCHOLARSHIP, Kind.FELLOWSHIP):
            return Verdict(passed=True, score=score, reasons=reasons)

        if score < self.threshold:
            return Verdict(
                passed=False, score=score, reasons=reasons,
                rejected_by=f"score {score:g} below threshold {self.threshold:g}",
            )
        return Verdict(passed=True, score=score, reasons=reasons)
