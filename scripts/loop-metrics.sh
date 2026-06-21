#!/usr/bin/env bash
# loop-metrics.sh — coordinator-efficiency metrics for the keep/discard gate.
#
# Single-session mode prints the legacy summary block. `--all` prints one row per
# .loop/sessions/<session>/ entry plus a fleet aggregate. Metrics that identify a
# session's attention/autonomy cost come from that session's engine surfaces, not
# shared repo-level ops-wiki history.

set -euo pipefail

PROJECT_ROOTS=()
SESSION_NAME=""
DO_LOG=0
ALL=0

_metrics_script_dir() {
  local src="${BASH_SOURCE[0]}"
  while [[ -L "$src" ]]; do
    local dir; dir="$(cd -P "$(dirname "$src")" && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  cd -P "$(dirname "$src")" && pwd
}
SCRIPT_DIR="$(_metrics_script_dir)"

usage() {
  cat <<'EOF'
Usage:
  loop-metrics.sh [options]

Prints the coordinator-efficiency summary block: checkpoint_tokens,
pending_messages, restarts_24h, giveups_24h, autonomy_ratio,
interventions_per_shipped_unit, event-derived 7d attention/autonomy counts,
brain_tokens_7d, cost_usd_7d, cost_per_shipped_unit, cost_per_decision,
dispatches_per_lane_7d, distinct_lanes_used_7d, ingests_7d, lints_7d,
checkpoints_7d, experiments (legacy dispatch counts are n/a).
Missing inputs degrade to 0/n-a with a note instead of failing.

Options:
  --all                 Print one row per .loop/sessions/<session> plus a
                        fleet aggregate across all selected project roots
  --log                 Also append `## [YYYY-MM-DD] metrics | <summary>`
                        (single-session mode only; rejected with --all)
  --session <name>      Session for .loop/sessions/<name>/ engine metrics;
                        default: the sole directory under .loop/sessions/,
                        else the current $TMUX session if it has one there
  --project-root <path> Repo root containing ops-wiki/ and .loop/. May be
                        repeated with --all.
                        default: this script's parent-of-parent directory
  -h, --help            Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --all)          ALL=1; shift ;;
    --log)          DO_LOG=1; shift ;;
    --session)      SESSION_NAME="$2"; shift 2 ;;
    --project-root) PROJECT_ROOTS+=("$2"); shift 2 ;;
    -h|--help)      usage; exit 0 ;;
    --*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      echo "Unexpected argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ "${#PROJECT_ROOTS[@]}" -eq 0 ]]; then
  PROJECT_ROOTS+=("$(cd "$SCRIPT_DIR/.." && pwd)")
fi

if [[ "$ALL" -eq 1 && "$DO_LOG" -eq 1 ]]; then
  echo "error: --all and --log cannot be combined; run --log per session" >&2
  exit 2
fi

if [[ "$ALL" -eq 1 && -n "$SESSION_NAME" ]]; then
  echo "error: --all and --session cannot be combined; --all scans every session" >&2
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required (substrate dependency, used for date math)" >&2
  exit 1
fi

python3 - "$SCRIPT_DIR" "$DO_LOG" "$ALL" "$SESSION_NAME" "${PROJECT_ROOTS[@]}" <<'PYEOF'
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

EVENT_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MAILBOX_TS_FORMAT = "%Y%m%d-%H%M%S"
mailbox_name_re = re.compile(r"^(\d{8}-\d{6})-([^-]+(?:-[^-]+)*?)-to-([^-]+)\.md$")
reply_subject_re = re.compile(r"re\s*:", re.IGNORECASE)
task_id_re = re.compile(r"^(T\d+)-")
log_pat = re.compile(r"^## \[(\d{4})-(\d{2})-(\d{2})\] ([a-z-]+) \| ?(.*)$")
PENDING_TIMEOUT_S = 15
TMUX_TIMEOUT_S = 5


@dataclass
class Metrics:
    root: Path
    session: str
    checkpoint_tokens: int = 0
    checkpoint_detail: str = ""
    pending_messages: str = "0"
    restarts_24h: int = 0
    giveups_24h: int = 0
    lane_restarts_7d: int = 0
    autonomy_ratio: str = "n/a"
    autonomy_engine: int = 0
    autonomy_total: int = 0
    interventions_per_shipped_unit: str = "n/a"
    interventions_total: int = 0
    tasks_done_7d: int = 0
    escalations_7d: int = 0
    rejects_7d: int = 0
    stops_7d: int = 0
    ingest_timeouts_7d: int = 0
    unsolicited_steers_7d: int = 0
    brain_calls_7d: int = 0
    brain_tokens_7d: str = "0"
    cost_usd_7d: str = "0.000000"
    cost_per_shipped_unit: str = "n/a"
    cost_per_decision: str = "n/a"
    dispatches_per_lane_7d: str = "{}"
    distinct_lanes_used_7d: int = 0
    ingests_7d: int = 0
    lints_7d: int = 0
    checkpoints_7d: int = 0
    experiments: int = 0
    lanes_idle_with_backlog: int = 0
    notes: list[str] = field(default_factory=list)


script_dir = Path(sys.argv[1])
do_log = sys.argv[2] == "1"
all_mode = sys.argv[3] == "1"
requested_session = sys.argv[4]
project_roots = [Path(p).resolve() for p in sys.argv[5:]]
pending_script = script_dir / "loop-wiki-pending.sh"
today = dt.date.today().isoformat()
now = dt.datetime.now(dt.timezone.utc)
cutoff_24h = now - dt.timedelta(hours=24)
cutoff_7d = now - dt.timedelta(days=7)
cutoff_day = dt.date.today() - dt.timedelta(days=7)


def parse_event_ts(value):
    return dt.datetime.strptime(value, EVENT_TS_FORMAT).replace(tzinfo=dt.timezone.utc)


def session_names(root: Path) -> list[str]:
    sessions_dir = root / ".loop" / "sessions"
    if not sessions_dir.is_dir():
        return []
    return sorted(path.name for path in sessions_dir.iterdir() if path.is_dir())


def default_session(root: Path, notes: list[str]) -> str:
    sessions = session_names(root)
    if len(sessions) == 1:
        notes.append(f"session defaulted to sole .loop/sessions/ entry '{sessions[0]}'")
        return sessions[0]
    if sessions and os.environ.get("TMUX"):
        try:
            proc = subprocess.run(
                ["tmux", "display-message", "-p", "#S"],
                check=False,
                capture_output=True,
                text=True,
                timeout=TMUX_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            proc = None
            notes.append("tmux session lookup timed out; pass --session for metrics")
        except OSError:
            proc = None
        tmux_session = proc.stdout.strip() if proc and proc.returncode == 0 else ""
        if tmux_session and tmux_session in sessions:
            notes.append(f"session defaulted to current tmux session '{tmux_session}'")
            return tmux_session
    if sessions:
        notes.append(f"{len(sessions)} sessions under .loop/sessions/ — pass --session; restart counts read 0")
    return ""


def json_file(path: Path, notes: list[str] | None = None, label: str | None = None):
    display = label or str(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        if notes is not None:
            notes.append(f"{display} unreadable: {exc}; metrics using fallback/0")
        return None
    except json.JSONDecodeError as exc:
        if notes is not None:
            notes.append(f"{display} unparseable: {exc}; metrics using fallback/0")
        return None
    if not isinstance(data, dict):
        if notes is not None:
            notes.append(f"{display} is not a JSON object; metrics using fallback/0")
        return None
    return data


def pending_count(root: Path, notes: list[str]) -> str:
    if pending_script.is_file() and os.access(pending_script, os.X_OK):
        try:
            proc = subprocess.run(
                [str(pending_script), "--quiet", "--project-root", str(root)],
                check=False,
                capture_output=True,
                text=True,
                timeout=PENDING_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            notes.append("pending_messages n/a: loop-wiki-pending.sh timed out")
            return "n/a"
        if proc.returncode == 0:
            return proc.stdout.strip() or "0"
        notes.append("pending_messages n/a: loop-wiki-pending.sh failed")
        return "n/a"
    notes.append("pending_messages n/a: sibling loop-wiki-pending.sh not found/executable")
    return "n/a"


def session_snapshot(root: Path, session: str, notes: list[str]) -> dict | None:
    if not session:
        return None
    path = root / ".loop" / "sessions" / session / "engine" / "snapshot.json"
    return json_file(path, notes, f"snapshot.json for session '{session}'")


def checkpoint_tokens(
    root: Path, session: str, snapshot: dict | None, notes: list[str]
) -> tuple[int, str]:
    if not session:
        notes.append("no session selected — checkpoint_tokens read 0")
        return 0, ""
    engine_dir = root / ".loop" / "sessions" / session / "engine"
    if snapshot is not None:
        value = snapshot.get("checkpoint_tokens")
        if type(value) is int and value >= 0:
            return value, ""
        if value is not None:
            notes.append(
                f"snapshot.json for session '{session}' has invalid checkpoint_tokens; using fallback/0"
            )
    direct = engine_dir / "checkpoint.md"
    if direct.is_file():
        try:
            chars = len(direct.read_text(encoding="utf-8"))
        except OSError as exc:
            notes.append(f"checkpoint.md for session '{session}' unreadable: {exc}; trying brain prompts")
        else:
            return chars // 4, f" ({chars} chars / 4)"
    try:
        prompts = sorted((engine_dir / "brain").glob("*.prompt.md"))
    except OSError as exc:
        notes.append(f"brain prompts for session '{session}' unreadable: {exc}; checkpoint_tokens read 0")
        prompts = []
    candidates: list[tuple[float, Path]] = []
    for path in prompts:
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError as exc:
            notes.append(f"brain prompt {path.name} for session '{session}' unreadable: {exc}; skipped")
    if candidates:
        latest = max(candidates, key=lambda item: item[0])[1]
        try:
            chars = len(latest.read_text(encoding="utf-8"))
        except OSError as exc:
            notes.append(f"brain prompt {latest.name} for session '{session}' unreadable: {exc}; checkpoint_tokens read 0")
        else:
            return chars // 4, f" ({chars} chars / 4)"
    notes.append(f"no session checkpoint surface for session '{session}' — checkpoint_tokens read 0")
    return 0, ""


def repo_log_counts(root: Path, notes: list[str]) -> tuple[int, int, int]:
    experiments = 0
    tasks_done = 0
    checkpoints = 0
    log_file = root / "ops-wiki" / "log.md"
    try:
        lines = log_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        notes.append("no ops-wiki/log.md — checkpoints_7d/experiments/tasks_done_7d read 0")
        return 0, 0, 0
    except OSError as exc:
        notes.append(f"ops-wiki/log.md unreadable: {exc}; checkpoints_7d/experiments/tasks_done_7d read 0")
        return 0, 0, 0
    skipped = 0
    for line in lines:
        match = log_pat.match(line)
        if not match:
            continue
        try:
            day = dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            skipped += 1
            continue
        kind = match.group(4)
        detail = match.group(5)
        if kind == "experiment":
            experiments += 1
        elif kind == "checkpoint" and day >= cutoff_day:
            checkpoints += 1
        elif kind == "task" and day >= cutoff_day and re.match(r"T\d+\s+done\b", detail):
            tasks_done += 1
    if skipped:
        notes.append(f"ops-wiki/log.md skipped {skipped} malformed dated line(s)")
    return experiments, tasks_done, checkpoints


def restart_counts(root: Path, session: str, notes: list[str]) -> tuple[int, int, int]:
    if not session:
        return 0, 0, 0
    path = root / ".loop" / "sessions" / session / "lane-restarts.jsonl"
    if not path.is_file():
        notes.append(
            f"no lane-restarts.jsonl for session '{session}' — restarts_24h/giveups_24h/lane_restarts_7d read 0"
        )
        return 0, 0, 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        notes.append(
            f"lane-restarts.jsonl for session '{session}' unreadable; restarts_24h/giveups_24h/lane_restarts_7d read 0"
        )
        return 0, 0, 0
    restarts_24h = giveups_24h = lane_restarts_7d = 0
    skipped = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            when = parse_event_ts(rec.get("timestamp", ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            skipped += 1
            continue
        if when >= cutoff_7d and "event" not in rec:
            lane_restarts_7d += 1
        if when < cutoff_24h:
            continue
        if "event" not in rec:
            restarts_24h += 1
        elif rec.get("event") == "giving-up":
            giveups_24h += 1
    if skipped:
        notes.append(f"lane-restarts.jsonl for session '{session}' skipped {skipped} corrupt line(s)")
    return restarts_24h, giveups_24h, lane_restarts_7d


def parse_mailbox_subject(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as fh:
            head = [fh.readline() for _ in range(40)]
    except OSError:
        return ""
    if not head or head[0].strip() != "---":
        return ""
    for line in head[1:]:
        if line.strip() == "---":
            break
        if line.startswith("subject:"):
            return line[len("subject:") :].strip()
    return ""


def subject_for_mail(root: Path, name: str) -> str | None:
    mailbox = root / ".loop" / "messages"
    for directory in (mailbox, mailbox / "processed", mailbox / "failed"):
        path = directory / name
        if path.is_file():
            return parse_mailbox_subject(path)
    return None


def mailbox_file_is_unsolicited_steer(root: Path, name: str, notes: list[str]) -> bool:
    match = mailbox_name_re.match(name)
    if not match:
        return False
    stamp_raw, sender, recipient = match.group(1), match.group(2), match.group(3)
    try:
        stamp = dt.datetime.strptime(stamp_raw, MAILBOX_TS_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return False
    if stamp < cutoff_7d or recipient != "coord" or sender == "coord":
        return False
    subject = subject_for_mail(root, name)
    if subject is None:
        notes.append(f"mailbox-new file '{name}' not found — skipped unsolicited steer count")
        return False
    return not reply_subject_re.match(subject.strip())


def nonnegative_int(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def nonnegative_float(value) -> float | None:
    if type(value) in {int, float} and not isinstance(value, bool) and value >= 0:
        return float(value)
    return None


def usage_total(rec: dict) -> int | None:
    total = nonnegative_int(rec.get("total_tokens"))
    if total is not None:
        return total
    parts = [
        nonnegative_int(rec.get("input_tokens")),
        nonnegative_int(rec.get("output_tokens")),
        nonnegative_int(rec.get("cache_creation_input_tokens")),
        nonnegative_int(rec.get("cache_read_input_tokens")),
    ]
    values = [value for value in parts if value is not None]
    return sum(values) if values else None


def fmt_usd(value: float) -> str:
    return f"{value:.6f}"


def event_metrics(root: Path, session: str, shipped: int, notes: list[str]):
    if not session:
        notes.append("no session selected — events.jsonl metrics read 0/n/a")
        return {
            "autonomy_ratio": "n/a",
            "autonomy_engine": 0,
            "autonomy_total": 0,
            "interventions_per_shipped_unit": "n/a",
            "interventions_total": 0,
            "escalations_7d": 0,
            "rejects_7d": 0,
            "stops_7d": 0,
            "ingest_timeouts_7d": 0,
            "unsolicited_steers_7d": 0,
            "brain_calls_7d": 0,
            "brain_tokens_7d": "0",
            "cost_usd_7d": "0.000000",
            "cost_per_shipped_unit": "n/a",
            "cost_per_decision": "n/a",
            "dispatches_per_lane_7d": "{}",
            "distinct_lanes_used_7d": 0,
            "ingests_7d": 0,
            "lints_7d": 0,
        }
    path = root / ".loop" / "sessions" / session / "engine" / "events.jsonl"
    if not path.is_file():
        notes.append(f"no events.jsonl for session '{session}' — event counts/autonomy read 0/n/a")
        lines: list[str] = []
    else:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            notes.append(f"events.jsonl for session '{session}' unreadable: {exc}; event counts/autonomy read 0/n/a")
            lines = []

    escalations = rejects = stops = ingest_timeouts = brain_calls = 0
    brain_usage_events = 0
    brain_token_total = 0
    brain_tokens_incomplete = False
    brain_cost_total = 0.0
    brain_cost_incomplete = False
    unpriced_usage_events = 0
    ingests = lints = 0
    engine_approvals = total_decisions = 0
    dispatches_by_lane: dict[str, int] = {}
    unsolicited_files: set[str] = set()
    seen_mailbox_files: set[str] = set()
    skipped = 0

    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            when = parse_event_ts(rec.get("ts", ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            skipped += 1
            continue
        if when < cutoff_7d:
            continue
        kind = rec.get("event")
        if kind == "decision-approved":
            total_decisions += 1
            if rec.get("decided_by") == "engine":
                engine_approvals += 1
        elif kind == "decision-rejected":
            rejects += 1
            total_decisions += 1
        elif kind == "escalate":
            escalations += 1
        elif kind == "action":
            lane = rec.get("lane")
            if isinstance(lane, str):
                lane = "_".join(lane.split())
                if lane:
                    dispatches_by_lane[lane] = dispatches_by_lane.get(lane, 0) + 1
            if rec.get("kind") == "stop":
                stops += 1
        elif kind == "ingest-timeout":
            ingest_timeouts += 1
        elif kind == "brain-call":
            brain_calls += 1
        elif kind == "brain-usage":
            brain_usage_events += 1
            tokens = usage_total(rec)
            if tokens is None:
                brain_tokens_incomplete = True
            else:
                brain_token_total += tokens
            cost = nonnegative_float(rec.get("cost_usd"))
            cost_source = rec.get("cost_source")
            if cost is not None:
                brain_cost_total += cost
            else:
                brain_cost_incomplete = True
                if cost_source == "unpriced" or (
                    tokens is not None and cost_source != "unavailable"
                ):
                    unpriced_usage_events += 1
        elif kind == "ingest-done":
            ingests += 1
        elif kind == "lint-dispatch":
            ok = rec.get("ok", True)
            if ok is True:
                lints += 1
            elif ok is not False:
                notes.append("lint-dispatch with non-boolean ok skipped")
        elif kind == "mailbox-new":
            name = rec.get("file")
            if not isinstance(name, str) or name in seen_mailbox_files:
                continue
            seen_mailbox_files.add(name)
            if mailbox_file_is_unsolicited_steer(root, name, notes):
                unsolicited_files.add(name)
    if skipped:
        notes.append(f"events.jsonl for session '{session}' skipped {skipped} corrupt line(s)")
    if brain_usage_events < brain_calls:
        missing = brain_calls - brain_usage_events
        brain_tokens_incomplete = True
        brain_cost_incomplete = True
        notes.append(f"{missing} brain-call event(s) lack brain-usage; cost metrics n/a")
    if unpriced_usage_events:
        notes.append(f"{unpriced_usage_events} brain-usage event(s) unpriced; cost metrics n/a")

    unsolicited_steers = len(unsolicited_files)
    interventions = escalations + unsolicited_steers + rejects
    autonomy = f"{engine_approvals / total_decisions:.2f}" if total_decisions else "n/a"
    per_shipped = f"{interventions / shipped:.2f}" if shipped else "n/a"
    brain_tokens = "n/a" if brain_tokens_incomplete else str(brain_token_total)
    brain_cost = "n/a" if brain_cost_incomplete else fmt_usd(brain_cost_total)
    brain_cost_per_shipped = (
        fmt_usd(brain_cost_total / shipped)
        if shipped and not brain_cost_incomplete
        else "n/a"
    )
    brain_cost_per_decision = (
        fmt_usd(brain_cost_total / brain_calls)
        if brain_calls and not brain_cost_incomplete
        else "n/a"
    )
    dispatches_json = json.dumps(dispatches_by_lane, sort_keys=True, separators=(",", ":"))
    return {
        "autonomy_ratio": autonomy,
        "autonomy_engine": engine_approvals,
        "autonomy_total": total_decisions,
        "interventions_per_shipped_unit": per_shipped,
        "interventions_total": interventions,
        "escalations_7d": escalations,
        "rejects_7d": rejects,
        "stops_7d": stops,
        "ingest_timeouts_7d": ingest_timeouts,
        "unsolicited_steers_7d": unsolicited_steers,
        "brain_calls_7d": brain_calls,
        "brain_tokens_7d": brain_tokens,
        "cost_usd_7d": brain_cost,
        "cost_per_shipped_unit": brain_cost_per_shipped,
        "cost_per_decision": brain_cost_per_decision,
        "dispatches_per_lane_7d": dispatches_json,
        "distinct_lanes_used_7d": len(dispatches_by_lane),
        "ingests_7d": ingests,
        "lints_7d": lints,
    }


def parse_task_frontmatter(path: Path, notes: list[str]) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        notes.append(f"task file {path.name} unreadable: {exc}; skipped backlog signal")
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def open_tasks_by_loop(root: Path, notes: list[str]) -> dict[str, list[str]]:
    tasks_dir = root / "tasks"
    out: dict[str, list[str]] = {}
    try:
        if not tasks_dir.exists():
            return out
        if not tasks_dir.is_dir():
            notes.append("tasks/ is not a directory — idle_lanes_with_backlog read 0")
            return out
        task_files = sorted(tasks_dir.glob("*.md"))
    except OSError as exc:
        notes.append(f"tasks/ unreadable: {exc}; idle_lanes_with_backlog read 0")
        return out
    for path in task_files:
        if path.name == "README.md":
            continue
        fm = parse_task_frontmatter(path, notes)
        if fm.get("status") not in {"open", "in-progress", "review"}:
            continue
        loop = fm.get("loop")
        if not loop:
            continue
        match = task_id_re.match(path.name)
        out.setdefault(loop, []).append(match.group(1) if match else path.stem)
    return out


def idle_lanes_with_backlog(root: Path, session: str, snapshot: dict | None, notes: list[str]) -> int:
    backlog = open_tasks_by_loop(root, notes)
    if not session:
        return 0
    snapshot_data = snapshot or {}
    lanes = snapshot_data.get("lanes") if isinstance(snapshot_data.get("lanes"), dict) else {}
    session_backlog = bool(backlog.get(session))
    count = 0
    for lane, info in lanes.items():
        if lane == "coord" or not isinstance(info, dict):
            continue
        lane_backlog = bool(backlog.get(lane))
        if info.get("status") == "idle" and (session_backlog or lane_backlog):
            count += 1
    return count


def compute(root: Path, session: str) -> Metrics:
    notes: list[str] = []
    metrics = Metrics(root=root, session=session)
    snapshot = session_snapshot(root, session, notes)
    metrics.pending_messages = pending_count(root, notes)
    metrics.checkpoint_tokens, metrics.checkpoint_detail = checkpoint_tokens(
        root, session, snapshot, notes
    )
    metrics.experiments, metrics.tasks_done_7d, metrics.checkpoints_7d = repo_log_counts(root, notes)
    metrics.restarts_24h, metrics.giveups_24h, metrics.lane_restarts_7d = restart_counts(
        root, session, notes
    )
    for key, value in event_metrics(root, session, metrics.tasks_done_7d, notes).items():
        setattr(metrics, key, value)
    metrics.lanes_idle_with_backlog = idle_lanes_with_backlog(root, session, snapshot, notes)
    metrics.notes = notes
    return metrics


def print_single(metrics: Metrics) -> None:
    session_label = metrics.session or "none"
    print(f"loop-metrics — {today} (session: {session_label})")
    print(f"  checkpoint_tokens: {metrics.checkpoint_tokens}{metrics.checkpoint_detail}")
    print(f"  pending_messages:  {metrics.pending_messages}")
    print(f"  restarts_24h:      {metrics.restarts_24h}")
    print(f"  giveups_24h:       {metrics.giveups_24h}")
    print(
        f"  autonomy_ratio:                  {metrics.autonomy_ratio} "
        f"({metrics.autonomy_engine}/{metrics.autonomy_total})"
    )
    print(
        "  interventions_per_shipped_unit: "
        f"{metrics.interventions_per_shipped_unit} "
        f"({metrics.interventions_total} interventions / {metrics.tasks_done_7d} shipped)"
    )
    print(f"  escalations_7d:                  {metrics.escalations_7d}")
    print(f"  rejects_7d:                      {metrics.rejects_7d}")
    print(f"  stops_7d:                        {metrics.stops_7d}")
    print(f"  ingest_timeouts_7d:              {metrics.ingest_timeouts_7d}")
    print(f"  lane_restarts_7d:                {metrics.lane_restarts_7d}")
    print(f"  unsolicited_steers_7d:           {metrics.unsolicited_steers_7d}")
    print(f"  brain_calls_7d:                  {metrics.brain_calls_7d}")
    print(f"  brain_tokens_7d:                 {metrics.brain_tokens_7d}")
    print(f"  cost_usd_7d:                     {metrics.cost_usd_7d}")
    print(f"  cost_per_shipped_unit:           {metrics.cost_per_shipped_unit}")
    print(f"  cost_per_decision:               {metrics.cost_per_decision}")
    print(f"  dispatches_per_lane_7d:          {metrics.dispatches_per_lane_7d}")
    print(f"  distinct_lanes_used_7d:          {metrics.distinct_lanes_used_7d}")
    print(f"  ingests_7d:        {metrics.ingests_7d}")
    print(f"  lints_7d:          {metrics.lints_7d}")
    print(f"  checkpoints_7d:    {metrics.checkpoints_7d}")
    print(f"  experiments:       {metrics.experiments}")
    print("  dispatches:        n/a (not derivable from substrate surfaces; out of scope)")
    if metrics.notes:
        print("notes:")
        for note in metrics.notes:
            print(f"  - {note}")


def summary(metrics: Metrics) -> str:
    return (
        f"tokens={metrics.checkpoint_tokens} pending={metrics.pending_messages} "
        f"restarts24h={metrics.restarts_24h} giveups24h={metrics.giveups_24h} "
        f"autonomy={metrics.autonomy_ratio}({metrics.autonomy_engine}/{metrics.autonomy_total}) "
        "interventions_per_shipped="
        f"{metrics.interventions_per_shipped_unit}({metrics.interventions_total}/{metrics.tasks_done_7d}) "
        f"escalations7d={metrics.escalations_7d} rejects7d={metrics.rejects_7d} "
        f"stops7d={metrics.stops_7d} ingest_timeouts7d={metrics.ingest_timeouts_7d} "
        f"lane_restarts7d={metrics.lane_restarts_7d} "
        f"unsolicited_steers7d={metrics.unsolicited_steers_7d} "
        f"brain_calls7d={metrics.brain_calls_7d} "
        f"brain_tokens7d={metrics.brain_tokens_7d} "
        f"cost_usd7d={metrics.cost_usd_7d} "
        f"brain_cost_per_shipped={metrics.cost_per_shipped_unit} "
        f"cost_per_decision={metrics.cost_per_decision} "
        f"dispatches_per_lane7d={metrics.dispatches_per_lane_7d} "
        f"distinct_lanes_used7d={metrics.distinct_lanes_used_7d} "
        f"ingests7d={metrics.ingests_7d} lints7d={metrics.lints_7d} "
        f"checkpoints7d={metrics.checkpoints_7d} experiments={metrics.experiments} "
        "source=session-events-v2"
    )


def log_single(metrics: Metrics) -> int:
    log_file = metrics.root / "ops-wiki" / "log.md"
    if not log_file.is_file():
        print(f"error: --log needs {log_file}", file=sys.stderr)
        return 1
    text = f"\n## [{today}] metrics | {summary(metrics)}\n"
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(text)
    print(f"logged: ## [{today}] metrics | {summary(metrics)}", file=sys.stderr)
    return 0


def format_ratio(engine: int, total: int) -> str:
    return f"{engine / total:.2f}" if total else "n/a"


def sum_int_metric(rows: list[Metrics], attr: str) -> str:
    total = 0
    for row in rows:
        value = getattr(row, attr)
        if not isinstance(value, str) or not value.isdigit():
            return "n/a"
        total += int(value)
    return str(total)


def sum_usd_metric(rows: list[Metrics], attr: str) -> str:
    total = 0.0
    for row in rows:
        value = getattr(row, attr)
        if not isinstance(value, str) or value == "n/a":
            return "n/a"
        try:
            total += float(value)
        except ValueError:
            return "n/a"
    return fmt_usd(total)


def print_all(rows: list[Metrics]) -> None:
    print(f"loop-metrics --all — {today}")
    if not rows:
        print("no sessions found under any <root>/.loop/sessions/")
    else:
        table = [
            [
                "SESSION",
                "ROOT",
                "TOKENS",
                "AUTONOMY",
                "INT",
                "ESC",
                "BRAIN",
                "INGEST",
                "LINT",
                "STEER",
                "BRAIN_TOKENS",
                "COST_USD",
                "IDLE_BACKLOG",
            ]
        ]
        for m in rows:
            table.append(
                [
                    m.session,
                    str(m.root),
                    str(m.checkpoint_tokens),
                    f"{m.autonomy_ratio}({m.autonomy_engine}/{m.autonomy_total})",
                    str(m.interventions_total),
                    str(m.escalations_7d),
                    str(m.brain_calls_7d),
                    str(m.ingests_7d),
                    str(m.lints_7d),
                    str(m.unsolicited_steers_7d),
                    m.brain_tokens_7d,
                    m.cost_usd_7d,
                    str(m.lanes_idle_with_backlog),
                ]
            )
        widths = [max(len(row[idx]) for row in table) for idx in range(len(table[0]))]
        for idx, row in enumerate(table):
            print("  ".join(cell.ljust(widths[pos]) for pos, cell in enumerate(row)))
            if idx == 0:
                print("  ".join("-" * width for width in widths))

    engine = sum(m.autonomy_engine for m in rows)
    total = sum(m.autonomy_total for m in rows)
    interventions = sum(m.interventions_total for m in rows)
    root_shipped: dict[Path, int] = {}
    root_experiments: dict[Path, int] = {}
    for m in rows:
        root_shipped.setdefault(m.root, m.tasks_done_7d)
        root_experiments.setdefault(m.root, m.experiments)
    shipped = sum(root_shipped.values())
    per_shipped = f"{interventions / shipped:.2f}" if shipped else "n/a"
    brain_cost = sum_usd_metric(rows, "cost_usd_7d")
    brain_calls = sum(m.brain_calls_7d for m in rows)
    brain_cost_per_shipped = (
        fmt_usd(float(brain_cost) / shipped) if shipped and brain_cost != "n/a" else "n/a"
    )
    brain_cost_per_decision = (
        fmt_usd(float(brain_cost) / brain_calls)
        if brain_calls and brain_cost != "n/a"
        else "n/a"
    )
    active_sessions = {f"{m.root}:{m.session}" for m in rows}
    print("fleet aggregate:")
    print(f"  sessions:                    {len(rows)}")
    print(f"  distinct_active_loops:       {len(active_sessions)}")
    print(f"  checkpoint_tokens:           {sum(m.checkpoint_tokens for m in rows)}")
    print(f"  autonomy_ratio:              {format_ratio(engine, total)} ({engine}/{total})")
    print(
        f"  interventions_per_shipped_unit: {per_shipped} "
        f"({interventions} interventions / {shipped} shipped)"
    )
    print(f"  interventions:               {interventions}")
    print(f"  escalations_7d:              {sum(m.escalations_7d for m in rows)}")
    print(f"  rejects_7d:                  {sum(m.rejects_7d for m in rows)}")
    print(f"  stops_7d:                    {sum(m.stops_7d for m in rows)}")
    print(f"  ingest_timeouts_7d:          {sum(m.ingest_timeouts_7d for m in rows)}")
    print(f"  lane_restarts_7d:            {sum(m.lane_restarts_7d for m in rows)}")
    print(f"  unsolicited_steers_7d:       {sum(m.unsolicited_steers_7d for m in rows)}")
    print(f"  brain_calls_7d:              {sum(m.brain_calls_7d for m in rows)}")
    print(f"  brain_tokens_7d:             {sum_int_metric(rows, 'brain_tokens_7d')}")
    print(f"  cost_usd_7d:                 {brain_cost}")
    print(f"  cost_per_shipped_unit:       {brain_cost_per_shipped}")
    print(f"  cost_per_decision:           {brain_cost_per_decision}")
    print(f"  ingests_7d:                  {sum(m.ingests_7d for m in rows)}")
    print(f"  lints_7d:                    {sum(m.lints_7d for m in rows)}")
    print(f"  checkpoints_7d:              {sum(m.checkpoints_7d for m in rows)}")
    print(f"  experiments:                 {sum(root_experiments.values())}")
    print(f"  lanes_idle_with_backlog:     {sum(m.lanes_idle_with_backlog for m in rows)}")
    noted = [(m, note) for m in rows for note in m.notes]
    if noted:
        print("notes:")
        for m, note in noted:
            print(f"  - {m.root}:{m.session}: {note}")


if all_mode:
    rows: list[Metrics] = []
    seen: set[tuple[str, str]] = set()
    for root in project_roots:
        for session in session_names(root):
            key = (str(root), session)
            if key in seen:
                continue
            seen.add(key)
            rows.append(compute(root, session))
    print_all(rows)
    sys.exit(0)

root = project_roots[0]
notes_for_default: list[str] = []
session = requested_session or default_session(root, notes_for_default)
single = compute(root, session)
single.notes = notes_for_default + single.notes
print_single(single)
if do_log:
    sys.exit(log_single(single))
PYEOF
