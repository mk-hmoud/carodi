"""The canonical record every source normalizes into.

Everything downstream of a source -- dedupe, enrichment, filtering, the digest --
only ever sees an Opportunity. Sources are the only place that knows about JSON
shapes, RSS quirks or HTML.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl


class Kind(StrEnum):
    JOB = "job"
    INTERNSHIP = "internship"
    SCHOLARSHIP = "scholarship"
    FELLOWSHIP = "fellowship"


class Remote(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")

# Legal-form suffixes that make one employer look like two across sources.
# Matched *after* slug() has stripped punctuation, so 'B.V.' arrives as the two
# tokens 'b v' and needs the spaced alternative to be listed explicitly --
# nearly every Dutch entry in the IND register carries it.
_ORG_NOISE = re.compile(
    r"\b(inc|llc|ltd|limited|gmbh|ag|plc|corp|corporation|co|group|holdings?"
    r"|b\s*v|n\s*v|s\s*a|s\s*r\s*l|a\s*s|oy|ab|as|aps)\b"
)


def slug(text: str) -> str:
    """Aggressively normalize a string for comparison and hashing."""
    text = _PUNCT.sub(" ", text.casefold())
    return _WS.sub(" ", text).strip()


def org_key(org: str) -> str:
    """Normalize an organization name so the same employer collapses to one key.

    Used both for dedupe and for joining against the visa sponsor registers,
    where 'Monzo Bank Ltd' must match a posting from 'Monzo'.
    """
    return _WS.sub(" ", _ORG_NOISE.sub(" ", slug(org))).strip()


class Opportunity(BaseModel):
    """One thing you could apply to."""

    source: str
    kind: Kind = Kind.JOB

    title: str
    org: str
    url: HttpUrl

    location_raw: str = ""
    countries: list[str] = Field(default_factory=list)  # ISO-3166 alpha-2, uppercase
    remote: Remote = Remote.UNKNOWN

    description: str = ""
    posted_at: datetime | None = None
    deadline: date | None = None

    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None

    tags: list[str] = Field(default_factory=list)

    # Filled by the enrichment stage; sources must not set these.
    enrichment: dict[str, object] = Field(default_factory=dict)
    score: float = 0.0
    reasons: list[str] = Field(default_factory=list)

    @property
    def fingerprint(self) -> str:
        """Stable identity across runs *and* across sources.

        Deliberately excludes the URL: the same job on Greenhouse and on an
        aggregator has different URLs but is one opportunity to you.
        """
        basis = f"{org_key(self.org)}|{slug(self.title)}|{slug(self.location_raw)}"
        return hashlib.sha256(basis.encode()).hexdigest()[:16]

    @property
    def org_key(self) -> str:
        return org_key(self.org)

    def haystack(self) -> str:
        """Everything a keyword rule may match against, punctuation intact.

        Not slugged: keyword rules must be able to see 'c++' and 'node.js'.
        Use slug()/org_key() for identity, this for content matching.
        """
        return _WS.sub(
            " ",
            " ".join(
                [self.title, self.org, self.location_raw, self.description, *self.tags]
            ).casefold(),
        ).strip()

    def title_haystack(self) -> str:
        """Title and tags only -- for rules that must not be fooled by boilerplate.

        Job descriptions routinely name every technology the company uses, so a
        'is this even my field?' check against the full text passes almost
        everything. The title is the honest signal.
        """
        return _WS.sub(" ", " ".join([self.title, *self.tags]).casefold()).strip()
