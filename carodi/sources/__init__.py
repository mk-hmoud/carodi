"""Importing this package registers every built-in source type.

New source module? Import it here or it will never appear in sources.yaml.
"""

from carodi.sources import ats, deadlines, feeds  # noqa: F401
from carodi.sources.base import Source, build, known_types, register

__all__ = ["Source", "build", "known_types", "register"]
