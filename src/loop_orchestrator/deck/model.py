"""Pure data assembly for the deck — file reads + substrate read methods only.

No textual imports here: everything is plain dataclasses so it can be unit
tested without a terminal. The deck is a NON-WRITER; this module never writes
a file.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..engine.events import EventLog
from ..locking import read_json
from ..paths import SessionPaths
from ..pm import taskfiles

UNRESOLVED = ("pending", "needs-human")

_TASK_ID_RE = re.compile(r"^(T\d{4})")


@dataclass
class LaneRow:
    window: str
    harness: str
    model: str
    role: str
    status: str
    kind: str
    base: bool
    restarts: int


@dataclass
class LoopRow:
    id: str
    status: str
    branch: str
    name: str


@dataclass
class ReviewRow:
    id: str
    title: str
    jira: str


@dataclass
class DeckState:
    session: str
    engine: str  # running | paused | off
    lanes: list[LaneRow] = field(default_factory=list)
    loops: list[LoopRow] = field(default_factory=list)
    pending: dict | None = None
    mailbox_pending: list[dict] = field(default_factory=list)
    processed_count: int = 0
    adrs: list[dict] = field(default_factory=list)
    events_tail: list[dict] = field(default_factory=list)
    review_items: list[ReviewRow] = field(default_factory=list)

    @property
    def pending_unresolved(self) -> dict | None:
        if isinstance(self.pending, dict) and self.pending.get("status") in UNRESOLVED:
            return self.pending
        return None


def engine_state(paths: SessionPaths) -> str:
    """paused file beats everything; then a live pid; else off."""
    if paths.paused_path.exists():
        return "paused"
    try:
        pid = int(paths.pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return "off"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "off"
    except PermissionError:
        return "running"  # exists, owned by someone else
    return "running"


def restart_counts(paths: SessionPaths) -> dict[str, int]:
    """Per-lane restart counts from lane-restarts.jsonl (append-only, bash-written).

    restart_pane writes lines without an `event` field; lifecycle lines
    (`giving-up`, …) carry one — so a missing event means a restart.
    """
    try:
        lines = paths.lane_restarts.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    counts: dict[str, int] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("event", "restart") != "restart":
            continue
        lane = obj.get("lane")
        if isinstance(lane, str) and lane:
            counts[lane] = counts.get(lane, 0) + 1
    return counts


def _snapshot_statuses(paths: SessionPaths, max_age_s: float) -> dict[str, dict] | None:
    """Lane statuses from snapshot.json when it is fresh enough, else None."""
    try:
        age = time.time() - paths.snapshot_path.stat().st_mtime
    except OSError:
        return None
    if age > max_age_s:
        return None
    doc = read_json(paths.snapshot_path)
    if not isinstance(doc, dict):
        return None
    lanes = doc.get("lanes")
    return lanes if isinstance(lanes, dict) else None


def lane_statuses(substrate, paths: SessionPaths, prefer_snapshot_age_s: float) -> dict[str, dict]:
    statuses = _snapshot_statuses(paths, prefer_snapshot_age_s)
    if statuses is not None:
        return statuses
    return {
        name: {"status": st.status, "target": st.target, "kind": st.kind}
        for name, st in substrate.lane_status_all().items()
    }


def snapshot_age(paths: SessionPaths) -> float | None:
    try:
        return time.time() - paths.snapshot_path.stat().st_mtime
    except OSError:
        return None


def _loops(digest: dict) -> list[LoopRow]:
    state = digest.get("state")
    loops = state.get("loops") if isinstance(state, dict) else None
    rows: list[LoopRow] = []
    for loop_id, info in sorted((loops or {}).items()):
        if not isinstance(info, dict):
            info = {}
        rows.append(
            LoopRow(
                id=loop_id,
                status=str(info.get("status") or "-"),
                branch=str(info.get("branch") or "-"),
                name=str(info.get("name") or info.get("title") or ""),
            )
        )
    return rows


def review_items(paths: SessionPaths) -> list[ReviewRow]:
    """Tasks in <project_root>/tasks/ whose frontmatter status is 'review' —
    tech-QA-complete work awaiting the PO's validation.

    Review items stay in tasks/ (never tasks/archive/, which holds done/dropped),
    so this scans the top-level tasks dir only. id comes from the filename's
    T<NNNN> prefix; title and jira come from the frontmatter. Sorted by id.
    Read-only — the deck non-writer invariant holds.
    """
    tasks_dir = paths.tasks_dir
    if not tasks_dir.is_dir():
        return []
    rows: list[ReviewRow] = []
    for path in sorted(tasks_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        try:
            frontmatter = taskfiles.parse_frontmatter(path)
        except (ValueError, OSError):
            continue
        if frontmatter.get("status") != "review":
            continue
        match = _TASK_ID_RE.match(path.name)
        task_id = match.group(1) if match else path.stem
        rows.append(
            ReviewRow(
                id=task_id,
                title=str(frontmatter.get("title") or ""),
                jira=str(frontmatter.get("jira") or ""),
            )
        )
    rows.sort(key=lambda row: row.id)
    return rows


def load_state(substrate, paths: SessionPaths, prefer_snapshot_age_s: float = 10) -> DeckState:
    """Join lanes + statuses + digest + engine files into one DeckState."""
    statuses = lane_statuses(substrate, paths, prefer_snapshot_age_s)
    restarts = restart_counts(paths)
    lanes: list[LaneRow] = []
    for info in substrate.lanes():
        st = statuses.get(info.window) or {}
        lanes.append(
            LaneRow(
                window=info.window,
                harness=info.harness or "-",
                model=info.model or "-",
                role=info.role or "-",
                status=str(st.get("status") or "unknown"),
                kind=str(st.get("kind") or ("fixed" if info.base else "dynamic")),
                base=info.base,
                restarts=restarts.get(info.window, 0),
            )
        )
    digest = substrate.digest()
    mailbox = digest.get("mailbox") or {}
    pending_doc = read_json(paths.pending_decision_path)
    adrs = digest.get("adrs")
    return DeckState(
        session=paths.session,
        engine=engine_state(paths),
        lanes=lanes,
        loops=_loops(digest),
        pending=pending_doc if isinstance(pending_doc, dict) else None,
        mailbox_pending=[m for m in mailbox.get("pending") or [] if isinstance(m, dict)],
        processed_count=int(mailbox.get("processed_count") or 0),
        adrs=[a for a in adrs or [] if isinstance(a, dict)],
        events_tail=EventLog(paths.events_path).tail(20),
        review_items=review_items(paths),
    )


def event_line(event: dict) -> str:
    """One-line rendering of an events.jsonl record for tickers/screens."""
    extras = {k: v for k, v in event.items() if k not in ("ts", "seq", "event")}
    suffix = f" {json.dumps(extras, sort_keys=True)}" if extras else ""
    return f"{event.get('ts', '?')} #{event.get('seq', '?')} {event.get('event', '?')}{suffix}"


def processed_names(paths: SessionPaths, limit: int = 20) -> list[str]:
    """Newest processed mailbox filenames (cheap dir listing for the 'm' toggle)."""
    try:
        names = [p.name for p in paths.processed_dir.iterdir() if p.is_file()]
    except OSError:
        return []
    return sorted(names, reverse=True)[:limit]


def adr_text(project_root: Path, adr: dict) -> str:
    path = Path(adr.get("path") or "")
    if not path.is_absolute():
        path = project_root / path
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"(could not read {path}: {exc})"
