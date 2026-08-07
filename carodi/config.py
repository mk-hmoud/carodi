from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    """Expand ${VAR} and ${VAR:-default} so secrets stay out of the config files."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_yaml(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    return _expand(yaml.safe_load(path.read_text()) or {})


@dataclass
class Config:
    root: Path
    profile: dict
    sources: list[dict]
    registers: list[dict]
    settings: dict

    @classmethod
    def load(cls, root: str | Path = "config") -> "Config":
        root = Path(root)
        profile = load_yaml(root / "profile.yaml")
        sources_doc = load_yaml(root / "sources.yaml")
        return cls(
            root=root,
            profile=profile,
            sources=[s for s in sources_doc.get("sources", []) if s.get("enabled", True)],
            registers=sources_doc.get("sponsor_registers", []),
            settings=sources_doc.get("settings", {}),
        )

    @property
    def db_path(self) -> str:
        return self.settings.get("db_path", "data/carodi.db")

    @property
    def digest_limit(self) -> int:
        return int(self.settings.get("digest_limit", 20))
