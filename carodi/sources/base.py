"""Source plugin contract and registry.

A source does exactly one thing: produce Opportunity records. It does not
filter, score, dedupe or know that Telegram exists. That keeps "add a new
source" to a single file plus a config entry.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from typing import Any, ClassVar

import httpx

from carodi.models import Opportunity

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type["Source"]] = {}

USER_AGENT = "carodi/0.1 (personal job funnel)"


def register(type_name: str):
    """Class decorator that makes a source addressable from sources.yaml."""

    def wrap(cls: type[Source]) -> type[Source]:
        if type_name in _REGISTRY:
            raise ValueError(f"duplicate source type: {type_name}")
        cls.type_name = type_name
        _REGISTRY[type_name] = cls
        return cls

    return wrap


def build(type_name: str, params: dict[str, Any]) -> "Source":
    try:
        cls = _REGISTRY[type_name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise KeyError(f"unknown source type {type_name!r}; known types: {known}") from None
    return cls(**params)


def known_types() -> list[str]:
    return sorted(_REGISTRY)


class Source(ABC):
    """Base class for every source."""

    type_name: ClassVar[str] = "unset"

    #: Human-readable instance name, e.g. "greenhouse:monzo". Appears in the digest.
    name: str

    @abstractmethod
    def fetch(self) -> Iterable[Opportunity]:
        """Yield opportunities. May raise; the runner isolates failures per source."""

    # -- helpers available to all sources -------------------------------------

    def _client(self, timeout: float = 20.0) -> httpx.Client:
        return httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )

    def _get_json(self, url: str, **kwargs: Any) -> Any:
        with self._client() as c:
            r = c.get(url, **kwargs)
            r.raise_for_status()
            return r.json()


class MultiSource(Source):
    """A source that fans out over a list of targets (e.g. many ATS boards).

    One failing board must not take out the other forty, so failures are logged
    and skipped rather than raised.
    """

    def fetch(self) -> Iterator[Opportunity]:
        for target in self.targets():
            try:
                yield from self.fetch_one(target)
            except Exception as exc:  # noqa: BLE001 - deliberate per-target isolation
                log.warning("%s: target %r failed: %s", self.name, target, exc)

    @abstractmethod
    def targets(self) -> Iterable[str]: ...

    @abstractmethod
    def fetch_one(self, target: str) -> Iterable[Opportunity]: ...
