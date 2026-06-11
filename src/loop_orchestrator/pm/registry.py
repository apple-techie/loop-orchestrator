"""PM adapter discovery via the `loop_orchestrator.pm_adapters` entry-point
group, with an override seam so tests inject adapters without installing
distribution metadata."""

from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import entry_points

from .base import PMAdapter


def discover(
    override: Iterable[tuple[str, type[PMAdapter]]] | None = None,
) -> dict[str, type[PMAdapter]]:
    """name -> adapter class. A non-None `override` replaces the metadata scan
    entirely (deterministic tests); a broken third-party entry point is
    skipped rather than killing the CLI."""
    if override is not None:
        return dict(override)
    found: dict[str, type[PMAdapter]] = {}
    for entry_point in entry_points(group="loop_orchestrator.pm_adapters"):
        try:
            found[entry_point.name] = entry_point.load()
        except Exception:
            continue
    return found
