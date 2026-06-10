"""Coordinator-owned writes to ops-wiki/checkpoint.md.

This is the ONLY engine module allowed to touch ops-wiki, and only at/below
the '<!-- coord-decisions -->' marker line — everything above it is
docs-compiled and must be preserved byte-for-byte (appending keeps the
original content as an exact prefix). Concurrent docs recompiles are handled
optimistically: re-stat before write, retry on mtime change (max 3).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

MARKER = "<!-- coord-decisions -->"
_MAX_ATTEMPTS = 3


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _compose(content: str, entry: str) -> str:
    if entry and not entry.endswith("\n"):
        entry += "\n"
    if content and not content.endswith("\n"):
        content += "\n"
    if MARKER not in content:
        content += MARKER + "\n"
    return content + entry


def _write_replace(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def file_decision(checkpoint_page: Path, entry_markdown: str) -> None:
    """Append entry_markdown below the coord-decisions marker.

    Missing file or missing marker: the marker line is created at EOF first.
    Raises RuntimeError if the page keeps changing under us for 3 attempts.
    """
    for _attempt in range(_MAX_ATTEMPTS):
        before = _mtime_ns(checkpoint_page)
        if before is None:
            content = ""
        else:
            try:
                content = checkpoint_page.read_text(encoding="utf-8")
            except OSError:
                continue  # deleted between stat and read — retry
        new_content = _compose(content, entry_markdown)
        if _mtime_ns(checkpoint_page) != before:
            continue  # concurrent writer (docs recompile) — re-read and retry
        _write_replace(checkpoint_page, new_content)
        return
    raise RuntimeError(
        f"{checkpoint_page}: page changed during {_MAX_ATTEMPTS} write attempts; giving up"
    )


def render_decision_entry(doc: dict) -> str:
    """Markdown entry for a pending-decision document (see decisions.py)."""
    ts = doc.get("decided_at") or doc.get("created_at") or ""
    lines = [f"### [{ts}] decision {doc.get('id')} ({doc.get('status')})", ""]
    critique = (doc.get("critique") or "").strip()
    if critique:
        lines += [critique, ""]
    for action in doc.get("actions") or []:
        target = action.get("lane") or action.get("window") or "-"
        lines.append(
            f"{action.get('idx')}. {action.get('kind')} {target} "
            f"[{action.get('classification')}/{action.get('status')}]: "
            f"{action.get('rationale')}"
        )
    return "\n".join(lines) + "\n"
