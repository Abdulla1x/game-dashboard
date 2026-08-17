"""Per-source game scanners. Each exposes `scan(cfg) -> list[dict]`."""

from . import steam, epic, xbox, riot, folders, shortcuts

# Order matters: `shortcuts` runs last and only fills gaps left by the others.
ALL = [steam, epic, xbox, riot, folders, shortcuts]
