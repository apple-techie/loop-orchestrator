"""Pure data assembly for the deck — file reads + substrate read methods only.

No textual imports here: everything is plain dataclasses so it can be unit
tested without a terminal. The deck is a NON-WRITER; this module never writes
a file.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..engine.actions import load_build_markers, load_verify_markers
from ..engine.events import EventLog
from ..locking import read_json
from ..paths import SessionPaths
from ..pm import taskfiles

UNRESOLVED = ("pending", "needs-human")

_TASK_ID_RE = re.compile(r"^(T\d{4})")

# Mirror loop._BUILD_TIMEOUT_S WITHOUT importing the in-flight engine/loop.py
# (T0055): a build/verify whose runner is gone but whose marker outlives this
# many seconds reads as "stale" rather than "done".
_BUILD_TIMEOUT_S = 1500
_VERIFY_TIMEOUT_S = 1800
_HEADLESS_EVENTS_TAIL = 40

_BUILD_OUTCOME_GATE = {
    "build-done": "done",
    "build-failed": "fail",
    "build-timeout": "timeout",
}
_VERIFY_OUTCOME_GATE = {
    "verify-passed": "pass",
    "verify-failed": "fail",
    "verify-timeout": "timeout",
    "verify-stale": "stale",
    "verify-skip": "skip",
}
_DECISION_EVENTS = ("decision-approved", "decision-pending", "decision-rejected", "action")


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
class HeadlessRow:
    """A headless worktree lane (no tmux pane): detached build/verify activity."""

    window: str
    activity: str  # build | verify | idle
    task_id: str
    branch: str
    liveness: str  # live | done | stale | unknown | -
    age_s: int | None
    pid: int | None
    gate: str  # pass | fail | timeout | stale | done | skip | -
    log_tail: str


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
    headless: list[HeadlessRow] = field(default_factory=list)
    last_decision: str = ""

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


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _age_seconds(started_at, now: float) -> int | None:
    """Seconds since an ISO `%Y-%m-%dT%H:%M:%SZ` (UTC) timestamp; None if unparseable."""
    if not isinstance(started_at, str):
        return None
    try:
        epoch = calendar.timegm(time.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return None
    return max(0, int(now - epoch))


def _worktree_dir(paths: SessionPaths, window: str) -> Path:
    """The lane's git worktree (matches engine actions._lane_worktree + lane-worktree.sh)."""
    return paths.project_root / ".loop" / "worktrees" / paths.session / window


def _open_tasks_by_loop(paths: SessionPaths) -> dict[str, list[str]]:
    """loop-id -> sorted open/in-progress/review task ids (frontmatter `loop:`), so
    the headless panel can name what each lane is working on. Read-only."""
    tasks_dir = paths.tasks_dir
    out: dict[str, list[str]] = {}
    if not tasks_dir.is_dir():
        return out
    for path in sorted(tasks_dir.glob("*.md")):
        if path.name == "README.md":
            continue
        try:
            frontmatter = taskfiles.parse_frontmatter(path)
        except (ValueError, OSError):
            continue
        if frontmatter.get("status") not in ("open", "in-progress", "review"):
            continue
        loop = frontmatter.get("loop")
        if not (isinstance(loop, str) and loop):
            continue
        match = _TASK_ID_RE.match(path.name)
        out.setdefault(loop, []).append(match.group(1) if match else path.stem)
    for ids in out.values():
        ids.sort()
    return out


def _latest_outcomes(events: list[dict]) -> dict[str, str]:
    """window -> latest build/verify gate label from an events list (oldest first)."""
    out: dict[str, str] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        label = _BUILD_OUTCOME_GATE.get(event.get("event")) or _VERIFY_OUTCOME_GATE.get(
            event.get("event")
        )
        if label is None:
            continue
        window = event.get("window") or event.get("lane")
        if isinstance(window, str) and window:
            out[window] = label
    return out


def _runner_liveness(substrate, pid_value, anchor, age_s: int | None, timeout_s: int) -> str:
    """live | done | stale | unknown for a detached runner, mirroring
    loop._build_runner_alive's stance: a ps FAILURE is 'unknown' (never falsely
    'done'); a runner whose ps command no longer matches the per-lane anchor is
    gone -> 'stale' past the timeout, else 'done'."""
    pid = _as_int(pid_value)
    if pid is None:
        return "unknown"
    if pid <= 1:
        return "done"
    try:
        command = substrate.process_command(pid, timeout=2)
    except Exception:
        return "unknown"
    if command is not None and anchor(command):
        return "live"
    if age_s is not None and age_s > timeout_s:
        return "stale"
    return "done"


def _headless_row(substrate, paths, window, build, verify, branch_fallback, gate, tasks, now):
    """Build one HeadlessRow from this window's build/verify markers + ledger branch."""
    task_id = tasks[0] if tasks else "-"
    if build is not None:
        worktree = _worktree_dir(paths, window)
        age_s = _age_seconds(build.get("started_at"), now)
        liveness = _runner_liveness(
            substrate,
            build.get("pid"),
            lambda cmd: "codex exec" in cmd and f"--cd {worktree} " in cmd,
            age_s,
            _BUILD_TIMEOUT_S,
        )
        return HeadlessRow(
            window=window,
            activity="build",
            task_id=task_id,
            branch=str(build.get("branch") or branch_fallback or "-"),
            liveness=liveness,
            age_s=age_s,
            pid=_as_int(build.get("pid")),
            gate=gate,
            log_tail=substrate.build_log_tail(worktree),
        )
    if verify is not None:
        age_s = _age_seconds(verify.get("started_at"), now)
        out_path = verify.get("out_path")
        liveness = _runner_liveness(
            substrate,
            verify.get("pid"),
            lambda cmd: (
                "loop-verify" in cmd and isinstance(out_path, str) and f"--out {out_path}" in cmd
            ),
            age_s,
            _VERIFY_TIMEOUT_S,
        )
        return HeadlessRow(
            window=window,
            activity="verify",
            task_id=task_id,
            branch=str(verify.get("branch") or branch_fallback or "-"),
            liveness=liveness,
            age_s=age_s,
            pid=_as_int(verify.get("pid")),
            gate=gate,
            log_tail="",
        )
    # No in-flight marker: an idle headless worktree lane (shows its last outcome).
    return HeadlessRow(
        window=window,
        activity="idle",
        task_id=task_id,
        branch=str(branch_fallback or "-"),
        liveness="-",
        age_s=None,
        pid=None,
        gate=gate,
        log_tail="",
    )


def headless_rows(substrate, paths: SessionPaths, digest: dict, events: list[dict], now=None):
    """One row per headless worktree lane (build/verify markers UNION ledger
    worktree lanes): current activity, runner liveness, last gate, build-log tail.
    Pure reads — markers + events + ledger + a read-only ps check; never writes."""
    now = time.time() if now is None else now
    builds = {
        marker["window"]: marker
        for marker in load_build_markers(paths)
        if isinstance(marker.get("window"), str) and marker.get("window")
    }
    verifies = {
        marker["window"]: marker
        for marker in load_verify_markers(paths)
        if isinstance(marker.get("window"), str) and marker.get("window")
    }
    state = digest.get("state") if isinstance(digest, dict) else None
    loops = state.get("loops") if isinstance(state, dict) else None
    worktree_branch: dict[str, str] = {}
    if isinstance(loops, dict):
        for window, info in loops.items():
            branch = info.get("branch") if isinstance(info, dict) else None
            if isinstance(branch, str) and branch:
                worktree_branch[window] = branch
    outcomes = _latest_outcomes(events)
    open_tasks = _open_tasks_by_loop(paths)
    rows: list[HeadlessRow] = []
    for window in sorted(set(builds) | set(verifies) | set(worktree_branch)):
        try:
            rows.append(
                _headless_row(
                    substrate,
                    paths,
                    window,
                    builds.get(window),
                    verifies.get(window),
                    worktree_branch.get(window),
                    outcomes.get(window, "-"),
                    open_tasks.get(window) or [],
                    now,
                )
            )
        except Exception:
            continue  # one bad marker must never blank the whole panel
    return rows


def last_engine_decision(events: list[dict]) -> str:
    """One-line summary of the most recent decision-bearing event (footer); '' if none."""
    for event in reversed(events):
        if isinstance(event, dict) and event.get("event") in _DECISION_EVENTS:
            return event_line(event)
    return ""


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
    events = EventLog(paths.events_path).tail(_HEADLESS_EVENTS_TAIL)
    return DeckState(
        session=paths.session,
        engine=engine_state(paths),
        lanes=lanes,
        loops=_loops(digest),
        pending=pending_doc if isinstance(pending_doc, dict) else None,
        mailbox_pending=[m for m in mailbox.get("pending") or [] if isinstance(m, dict)],
        processed_count=int(mailbox.get("processed_count") or 0),
        adrs=[a for a in adrs or [] if isinstance(a, dict)],
        events_tail=events[-20:],  # ticker shows the recent slice; headless mines the wider window
        review_items=review_items(paths),
        headless=headless_rows(substrate, paths, digest, events),
        last_decision=last_engine_decision(events),
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
