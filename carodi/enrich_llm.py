"""Read job descriptions with an LLM and extract structured facts.

This is the one part of carodi that is not deterministic, so its role is
deliberately narrow: it **extracts facts, it does not make decisions**. It runs
after `Rules.reject()` has already settled hard eligibility and before
`Rules.score()` ranks the survivors. The rules stay in charge of what the facts
mean, which keeps eligibility auditable and testable.

Two consequences of that split are load-bearing:

* It cannot rescue a rejected role, so a hallucination can never smuggle an
  ineligible job into the digest.
* It cannot reject one either. Even `sponsorship: denied` only applies a score
  penalty. A false positive there would silently delete a real opportunity you
  would never learn existed -- and a lost job is a far worse error than a wasted
  evening. Demote, never drop.

Results are cached by fingerprint forever: a posting is read once, ever.

Provider is Google Gemini (`google-genai`) rather than the Anthropic SDK used
nowhere else in this project -- chosen for its free tier, which comfortably
covers this volume. See config/sources.yaml.
"""

from __future__ import annotations

import logging
import time
from enum import Enum

from pydantic import BaseModel, Field

from carodi.models import Opportunity
from carodi.store import Store

log = logging.getLogger(__name__)


class Sponsorship(str, Enum):
    """Modelled as three states, not a bool.

    "The posting never mentions sponsorship" is different from "the posting
    says no", and collapsing them loses the distinction that matters most.
    """

    OFFERED = "offered"
    DENIED = "denied"
    UNMENTIONED = "unmentioned"


class Extraction(BaseModel):
    """What the model reads out of a posting.

    Every field exists because a regex gets it wrong today.
    """

    sponsorship: Sponsorship = Field(
        description=(
            "offered: the posting explicitly says visa sponsorship or relocation "
            "support is available. denied: it explicitly says sponsorship is not "
            "available, or that applicants must already hold work authorization. "
            "unmentioned: the posting does not address it. Do not infer from the "
            "mere presence of the word 'visa'."
        )
    )
    min_years_experience: int | None = Field(
        default=None,
        description="Minimum years of professional experience required. Null if unstated.",
    )
    is_entry_level: bool = Field(
        description=(
            "True if a candidate finishing a master's with internship-level "
            "experience could plausibly be hired. Judge the requirements, not the "
            "job title."
        )
    )
    remote_restricted_to: list[str] = Field(
        default_factory=list,
        description=(
            "If the role is remote but restricted to particular countries, their "
            "ISO 3166-1 alpha-2 codes. Empty if not remote, or if remote with no "
            "stated geographic restriction."
        ),
    )
    salary_min: int | None = Field(default=None, description="Annual minimum, if stated.")
    salary_currency: str | None = Field(default=None, description="ISO 4217, e.g. EUR.")
    one_line_fit: str = Field(
        description=(
            "One sentence, max 20 words, on why this role does or does not suit a "
            "graduating computer engineering master's student who needs visa "
            "sponsorship. Be blunt about mismatches."
        )
    )


INSTRUCTIONS = """You extract structured facts from job postings.

Report only what the posting states. Do not infer, do not guess, and do not be
generous: when a posting is silent on something, say so rather than assuming the
favourable reading.

The reader is a computer engineering master's student graduating mid-2027, a
Jordanian national who requires visa sponsorship for any role outside their
home country. Sponsorship and seniority are therefore the fields that matter
most -- get those right before anything else.
"""


class LlmEnricher:
    """Extracts facts from postings, caching by fingerprint."""

    def __init__(
        self,
        api_key: str,
        model: str,
        store: Store,
        max_per_run: int = 40,
        min_interval: float = 6.5,
        max_chars: int = 6000,
        disable_thinking: bool = True,
    ):
        if not api_key:
            raise ValueError(
                "LLM enrichment is enabled but no API key is set "
                "(set CARODI_GEMINI_API_KEY, or disable llm in sources.yaml)"
            )
        from google import genai  # imported lazily so the dep stays optional

        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.store = store
        # Free-tier quotas are per-minute and per-day. min_interval keeps us
        # under the RPM ceiling; max_per_run bounds a cold cache, where a first
        # run would otherwise try to read every posting at once.
        self.max_per_run = max_per_run
        self.min_interval = min_interval
        self.max_chars = max_chars
        self.disable_thinking = disable_thinking

        self.extracted = 0
        self.cached = 0
        self.skipped = 0
        self.failed = 0
        self._last_call = 0.0

    # -- request ---------------------------------------------------------------

    def _config(self):
        from google.genai import types

        kwargs = {
            "system_instruction": INSTRUCTIONS,
            "response_mime_type": "application/json",
            "response_schema": Extraction,
            "temperature": 0,  # extraction should be reproducible
        }
        if self.disable_thinking:
            # Reading a posting is not a reasoning task; thinking tokens here are
            # spend against a free-tier quota for no gain.
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        return types.GenerateContentConfig(**kwargs)

    def _prompt(self, opp: Opportunity) -> str:
        return (
            f"Title: {opp.title}\n"
            f"Company: {opp.org}\n"
            f"Location: {opp.location_raw or 'unstated'}\n\n"
            f"{opp.description[: self.max_chars]}"
        )

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def extract(self, opp: Opportunity) -> Extraction | None:
        """Extract from one posting, or return None if it could not be read."""
        self._throttle()
        prompt = self._prompt(opp)
        try:
            response = self.client.models.generate_content(
                model=self.model, contents=prompt, config=self._config()
            )
        except Exception as exc:  # noqa: BLE001 - inspected and re-raised below
            # Not every model accepts a zero thinking budget: the `*-latest`
            # aliases reject it with a bare 400 INVALID_ARGUMENT. Rather than
            # let a model name and a boolean in config combine into something
            # unusable, drop the option once and carry on.
            if not (self.disable_thinking and "INVALID_ARGUMENT" in str(exc)):
                raise
            log.info("%s rejects thinking_budget=0; retrying with thinking on", self.model)
            self.disable_thinking = False
            response = self.client.models.generate_content(
                model=self.model, contents=prompt, config=self._config()
            )

        parsed = response.parsed
        if isinstance(parsed, Extraction):
            return parsed
        log.warning("model returned no parseable extraction for %s", opp.fingerprint)
        return None

    # -- pipeline hook ---------------------------------------------------------

    def enrich(self, opp: Opportunity) -> None:
        """Annotate an opportunity in place. Never raises.

        A failure here must degrade to "unenriched" rather than taking down the
        run -- the deterministic signals still work without it, exactly as a
        missing sponsor register degrades to "unknown".
        """
        if cached := self.store.cached_extraction(opp.fingerprint):
            opp.enrichment["llm"] = cached
            self.cached += 1
            return

        if not opp.description or len(opp.description) < 200:
            self.skipped += 1
            return

        if self.extracted >= self.max_per_run:
            self.skipped += 1
            return

        try:
            extraction = self.extract(opp)
        except Exception as exc:  # noqa: BLE001 - degrade, never crash the run
            log.warning("extraction failed for %s: %s", opp.fingerprint, exc)
            self.failed += 1
            return

        if extraction is None:
            self.failed += 1
            return

        payload = extraction.model_dump(mode="json")
        opp.enrichment["llm"] = payload
        self.store.record_extraction(opp.fingerprint, self.model, payload)
        self.extracted += 1

    def stats(self) -> dict:
        return {
            "extracted": self.extracted,
            "cached": self.cached,
            "skipped": self.skipped,
            "failed": self.failed,
        }
