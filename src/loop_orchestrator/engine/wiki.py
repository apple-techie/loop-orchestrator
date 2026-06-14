"""Coordinator-owned writes to ops-wiki/checkpoint.md.

This is the ONLY engine module allowed to touch ops-wiki, and only at/below
the '<!-- coord-decisions -->' marker line — everything above it is
docs-compiled and must be preserved byte-for-byte (appending keeps the
original content as an exact prefix). Concurrent docs recompiles are handled
optimistically: re-stat before write, retry on mtime change (max 3).
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

MARKER = "<!-- coord-decisions -->"
_MAX_ATTEMPTS = 3
# Default decision entries kept below the marker before rotation (T0022); the
# overflow goes to the decisions archive. Configurable via EngineConfig.
DEFAULT_KEEP_DECISIONS = 10
_ENTRY_RE = re.compile(r"(?m)^### ")
# Lane-page section a drop_lane flush appends (T0023) and a successor lane reads
# to recover (T0028). Its presence on a lane page = a predecessor was handed off.
HANDOFF_MARKER = "## Handoff state"


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


def _rotate(content: str, keep: int) -> tuple[str, str]:
    """Keep only the last `keep` decision entries below the marker; return
    (kept_content, overflow_markdown). A decision entry is a block starting with
    a '### ' line. Content above the marker, the marker line, and any preamble
    before the first entry are preserved byte-for-byte, and entries are split on
    their '### ' boundary so a partial entry is never created. No overflow and
    byte-identical content when keep < 1, the marker is absent, or there are
    <= keep entries — so rotation is inert until the log exceeds N."""
    if keep < 1 or MARKER not in content:
        return content, ""
    marker_nl = content.find("\n", content.find(MARKER))
    if marker_nl == -1:
        return content, ""
    head, tail = content[: marker_nl + 1], content[marker_nl + 1 :]
    starts = [m.start() for m in _ENTRY_RE.finditer(tail)]
    if len(starts) <= keep:
        return content, ""
    preamble = tail[: starts[0]]
    bounds = starts + [len(tail)]
    entries = [tail[bounds[i] : bounds[i + 1]] for i in range(len(starts))]
    cut = len(entries) - keep
    return head + preamble + "".join(entries[cut:]), "".join(entries[:cut])


def _append_archive(archive_page: Path, overflow_markdown: str) -> None:
    """Atomically append rotated-out entries to the decisions archive (full
    read+rewrite via os.replace, so a crash never leaves a partial entry)."""
    if not overflow_markdown:
        return
    try:
        existing = archive_page.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    if not overflow_markdown.endswith("\n"):
        overflow_markdown += "\n"
    _write_replace(archive_page, existing + overflow_markdown)


def file_decision(
    checkpoint_page: Path,
    entry_markdown: str,
    keep: int = DEFAULT_KEEP_DECISIONS,
    archive_page: Path | None = None,
) -> None:
    """Append entry_markdown below the coord-decisions marker, retaining only the
    last `keep` decision entries and rotating the overflow into the decisions
    archive (default: <checkpoint dir>/decisions-archive.md) — all inside the
    existing mtime-guarded atomic write, so the boot checkpoint stays bounded.

    Missing file or missing marker: the marker line is created at EOF first.
    Raises RuntimeError if the page keeps changing under us for 3 attempts.
    """
    if archive_page is None:
        archive_page = checkpoint_page.parent / "decisions-archive.md"
    for _attempt in range(_MAX_ATTEMPTS):
        before = _mtime_ns(checkpoint_page)
        if before is None:
            content = ""
        else:
            try:
                content = checkpoint_page.read_text(encoding="utf-8")
            except OSError:
                continue  # deleted between stat and read — retry
        kept_content, overflow = _rotate(_compose(content, entry_markdown), keep)
        if _mtime_ns(checkpoint_page) != before:
            continue  # concurrent writer (docs recompile) — re-read and retry
        # Archive BEFORE replacing the checkpoint: if the second write fails the
        # overflow survives in the archive AND the un-rotated checkpoint
        # (duplicated on the next rotation, never lost).
        _append_archive(archive_page, overflow)
        _write_replace(checkpoint_page, kept_content)
        return
    raise RuntimeError(
        f"{checkpoint_page}: page changed during {_MAX_ATTEMPTS} write attempts; giving up"
    )


def append_handoff(lane_page: Path, window: str, harness: str, pane_tail: str, now: str) -> None:
    """Append-only `## Handoff state` breadcrumb to a lane page (T0023), creating
    the page (and lanes/ dir) if absent. The engine stamps what it can — as-of,
    harness, the shared working tree — plus the idle agent's captured pane tail,
    which holds the in-flight step/touched/blocked-on/assumptions as the agent
    left them. The pane is indented (not fenced) so a ``` in the capture can't
    break the block. A drop_lane swap thus always leaves an observable signal."""
    indented = "\n".join("    " + line for line in pane_tail.rstrip("\n").splitlines()) or "    "
    block = (
        f"\n{HANDOFF_MARKER}\n"
        f"### [{now}] {window} handoff — {harness} (drop_lane flush)\n"
        f"- as-of: {now}\n"
        f"- harness: {harness}\n"
        "- working-tree: shared project root (per-lane isolation deferred to Phase 5)\n"
        "- step / touched / blocked-on / assumptions — as the agent left the lane pane:\n\n"
        f"{indented}\n"
    )
    try:
        existing = lane_page.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    _write_replace(lane_page, existing + block)


def has_handoff_state(lane_page: Path) -> bool:
    """T0028: True when a lane page carries a `## Handoff state` section — i.e. a
    predecessor was flushed out of this window (T0023), so a newly (re)provisioned
    lane of the same window is a SUCCESSOR that should recover, not cold-start.
    Missing page / no section = a normal cold add (today's behavior)."""
    try:
        return HANDOFF_MARKER in lane_page.read_text(encoding="utf-8")
    except OSError:
        return False


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
