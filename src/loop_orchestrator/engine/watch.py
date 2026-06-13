"""Watch daemon: poll the substrate, append deltas, trigger engine cycles.

:func:`evaluate_triggers` is a PURE function over :class:`TriggerState`, so
trigger policy is unit-testable with injected clocks. :meth:`Watch.tick`
assembles that state each poll from the observer delta, the ask/reply scan,
and the engine flag files, then runs loop.run_once inline when triggered (and
not suppressed by pause / a pending decision / the brain budget). The daemon
never spawns processes itself — all substrate access goes through the typed
Substrate wrappers and cycles run in-process.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..locking import file_lock, read_json
from ..paths import SessionPaths
from ..substrate import Substrate, SubstrateError
from . import actions as actions_mod
from . import gate as gate_mod
from . import loop as loop_mod
from .brain import BUDGET_WINDOW_S
from .config import EngineConfig
from .events import EventLog, parse_ts, utc_now
from .observe import Observer, delta


def _module_build_mtime_ns() -> int | None:
    """mtime of the loaded gate.py — the code-of-record for the daemon. A
    reinstall touches this file; the stale-daemon guard compares the on-disk
    mtime to the one a running daemon recorded at boot."""
    source = getattr(gate_mod, "__file__", None)
    if not source:
        return None
    return _mtime_ns(Path(source))


def record_daemon_build(paths: SessionPaths, pid: int) -> None:
    """Stamp daemon-build.json with the loaded module mtime + pid + start time so
    `status` can detect a daemon running stale code after a reinstall."""
    build = {
        "pid": pid,
        "started_at": utc_now(),
        "module": getattr(gate_mod, "__file__", ""),
        "module_mtime_ns": _module_build_mtime_ns(),
    }
    paths.daemon_build_path.parent.mkdir(parents=True, exist_ok=True)
    paths.daemon_build_path.write_text(json.dumps(build) + "\n", encoding="utf-8")


def stale_daemon_warning(paths: SessionPaths) -> str | None:
    """Warning string when the running daemon's recorded module mtime is OLDER
    than the on-disk module (a reinstall landed after the daemon started);
    None when build info is absent, unreadable, or the daemon is current."""
    try:
        build = json.loads(paths.daemon_build_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(build, dict):
        return None
    recorded = build.get("module_mtime_ns")
    current = _module_build_mtime_ns()
    if not isinstance(recorded, int) or current is None or current <= recorded:
        return None
    return (
        f"daemon is running stale code (installed {_fmt_mtime(current)} > "
        f"daemon start {_fmt_mtime(recorded)}) — run: loop-engine restart"
    )


def _fmt_mtime(mtime_ns: int) -> str:
    return datetime.fromtimestamp(mtime_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# A working lane landing in one of these states means its task ended.
_CYCLE_END_STATUSES = frozenset({"idle", "errored", "awaiting-approval"})

# Newest '## [date] lint | …' log.md entry = when the last lint run closed.
_LINT_LOG_RE = re.compile(r"^## \[(\d{4})-(\d{2})-(\d{2})\] lint \|")

# A quota reset hint in the stderr excerpt, e.g. "resets 9:30pm" / "resets 9pm".
_RESET_RE = re.compile(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?", re.IGNORECASE)


def parse_reset_deadline(excerpt: str, now: float) -> float | None:
    """Epoch of the next clock time named in a 'resets <time>' hint, in the
    machine's LOCAL timezone (that is what the provider prints); None when no
    parseable hint. If the named time is already past today, it rolls to
    tomorrow (a reset is always in the future)."""
    match = _RESET_RE.search(excerpt or "")
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    base = datetime.fromtimestamp(now)
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    deadline = target.timestamp()
    if deadline <= now:
        deadline += 86400  # already past today -> the next occurrence
    return deadline


def pid_alive(pid: int) -> bool:
    """True when `pid` is a live process (signal-0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, just not ours
    except OSError:
        return False
    return True


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _peek_subject(path: Path) -> str:
    """Frontmatter `subject:` of a mailbox file. Peek ONLY — the docs lane
    owns the ack (moving to processed/); the watch never touches the file."""
    try:
        with open(path, encoding="utf-8") as fh:
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


@dataclass(frozen=True)
class TriggerState:
    """Pure inputs to evaluate_triggers, assembled once per poll by Watch.tick."""

    last_cycle_start: float | None  # epoch seconds of the last cycle-start; None = never
    checkpoint_interval_s: int
    min_cycle_interval_s: int
    mailbox_new: bool = False
    lane_transitions: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    state_file_changed: bool = False
    cycle_now: bool = False
    reply_received: bool = False
    # Interval baseline when no cycle has ever run: a fresh daemon must NOT
    # burn a brain call at boot — it waits a full checkpoint interval from
    # start (real triggers still fire immediately; debounce only applies
    # after a first cycle exists). Default 0.0 preserves fire-immediately
    # for callers that don't supply it.
    watch_start: float = 0.0


def evaluate_triggers(state: TriggerState, now: float) -> list[str]:
    """Reasons to run a cycle now; [] when nothing fired or debounce blocks."""
    reasons: list[str] = []
    last = state.last_cycle_start
    baseline = last if last is not None else state.watch_start
    if now - baseline >= state.checkpoint_interval_s:
        reasons.append("interval")
    if state.mailbox_new:
        reasons.append("mailbox-new")
    for lane, old, new in state.lane_transitions:
        if old == "working" and new in _CYCLE_END_STATUSES:
            reasons.append(f"lane-transition:{lane}:{new}")
    if state.state_file_changed:
        reasons.append("state-changed")
    if state.cycle_now:
        reasons.append("cycle-now")
    if state.reply_received:
        reasons.append("reply-received")
    if reasons and last is not None and now - last < state.min_cycle_interval_s:
        return []  # debounce: too soon since the last cycle-start
    return reasons


class Watch:
    """Singleton engine daemon for one (project_root, session)."""

    def __init__(self, project_root: str | Path, session: str, config: EngineConfig):
        self.paths = SessionPaths(Path(project_root), session)
        self.paths.ensure()
        self.session = session
        self.config = config
        self.events = EventLog(self.paths.events_path)
        self.substrate = Substrate(self.paths.project_root, session)
        self.observer = Observer(self.substrate, self.paths)
        self._stop = False
        prev = read_json(self.paths.snapshot_path)
        self._prev_snapshot: dict | None = prev if isinstance(prev, dict) else None
        self._last_cycle_start = self._last_event_epoch("cycle-start")
        self._watch_started = time.time()
        self._state_mtime = _mtime_ns(self.paths.state_file)
        self._mailbox_since_cycle = False
        self._seen_replies: set[str] = set()
        self._paused_skip_logged = False
        self._last_skip: tuple | None = None
        # Brain-suppression backoff: epoch until which the BRAIN is suppressed
        # after a failure that must NOT be retried into the wall — failure_kind
        # "quota" (usage/session limit) or "model-unavailable" (F3: requested
        # model down). observation/PM keep running. None = no active backoff.
        # Seeded from a prior brain-failed so a daemon restart inside the window
        # does not immediately burn another doomed call. (Field name kept for
        # back-compat; _backoff_reason records which cause is in force.)
        self._quota_backoff_until: float | None = None
        self._backoff_reason: str = "quota-backoff"

    # ── lifecycle ─────────────────────────────────────────────────────────

    def run(self) -> int:
        pid_file = self.paths.pid_path
        existing = _read_pid(pid_file)
        if existing is not None and pid_alive(existing):
            print(
                f"error: watch already running (pid {existing}); "
                f"stop it or remove {pid_file} if that is stale",
                file=sys.stderr,
            )
            return 1
        pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
        record_daemon_build(self.paths, os.getpid())
        old_term = signal.signal(signal.SIGTERM, self._on_signal)
        old_int = signal.signal(signal.SIGINT, self._on_signal)
        self.events.append("watch-start", pid=os.getpid(), session=self.session)
        try:
            while not self._stop:
                self.tick(time.time())
                self._sleep(self.config.poll_interval_s)
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGINT, old_int)
            self.events.append("watch-stop", pid=os.getpid())
            try:
                pid_file.unlink()
            except FileNotFoundError:
                pass
        return 0

    def _on_signal(self, signum, frame) -> None:
        self._stop = True

    def _sleep(self, seconds: float) -> None:
        """Sleep in <=1s slices so a stop signal exits promptly."""
        deadline = time.monotonic() + seconds
        while not self._stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))

    # ── one poll ──────────────────────────────────────────────────────────

    def tick(self, now: float) -> None:
        self._heartbeat()
        paused = self.paths.paused_path.exists()
        if not paused:
            self._paused_skip_logged = False
            self._maybe_lint(now)

        try:
            snap = self.observer.snapshot().to_dict()
        except SubstrateError as exc:
            self.events.append("error", kind="observe-failed", error=str(exc))
            return
        transitions: list[tuple[str, str | None, str | None]] = []
        for kind, fields in delta(self._prev_snapshot, snap):
            self.events.append(kind, **fields)
            if kind == "lane-status":
                transitions.append((fields["lane"], fields["from"], fields["to"]))
            elif kind == "mailbox-new":
                self._mailbox_since_cycle = True
        self._prev_snapshot = snap

        reply_received = self._scan_replies()
        self._expire_asks(now)

        state_mtime = _mtime_ns(self.paths.state_file)
        state_changed = state_mtime != self._state_mtime
        self._state_mtime = state_mtime

        trigger_state = TriggerState(
            last_cycle_start=self._last_cycle_start,
            checkpoint_interval_s=self.config.checkpoint_interval_s,
            min_cycle_interval_s=self.config.min_cycle_interval_s,
            mailbox_new=self._mailbox_since_cycle,
            lane_transitions=transitions,
            state_file_changed=state_changed,
            cycle_now=self.paths.cycle_now_path.exists(),
            reply_received=reply_received,
            watch_start=self._watch_started,
        )
        reasons = evaluate_triggers(trigger_state, now)
        if not reasons:
            return
        # Suppression checks run BEFORE the cycle-now flag is consumed, so a
        # cycle-now issued while paused or while a decision is pending survives
        # and fires once the engine is unblocked (it is a request, not a hint).
        if paused:
            if not self._paused_skip_logged:  # once per pause, not per poll
                self.events.append("cycle-skip", reason="paused", triggers=reasons)
                self._paused_skip_logged = True
            return
        if self.paths.pending_decision_path.exists():  # single in-flight invariant
            self._log_skip("pending-decision", reasons)
            return
        if self._quota_backoff_active(now, reasons):
            return
        if (
            self.events.count_since("brain-call", BUDGET_WINDOW_S)
            >= self.config.brain.max_calls_per_hour
        ):
            self._log_skip("budget", reasons)
            return
        self._last_skip = None
        if trigger_state.cycle_now:
            try:
                self.paths.cycle_now_path.unlink()  # consume the flag
            except FileNotFoundError:
                pass
        self._run_cycle(now, reasons)

    def _log_skip(self, reason: str, reasons: list[str]) -> None:
        """One cycle-skip event per distinct (reason, triggers) episode — the
        persisting triggers (mailbox backlog, surviving cycle-now) would
        otherwise spam an identical skip line every poll."""
        sig = (reason, tuple(sorted(reasons)))
        if sig != self._last_skip:
            self.events.append("cycle-skip", reason=reason, triggers=reasons)
            self._last_skip = sig

    def _quota_backoff_active(self, now: float, reasons: list[str]) -> bool:
        """True when a quota backoff is in force — the BRAIN is gated until the
        deadline (observation/PM already ran this tick). Clears once the
        deadline passes; emits cycle-skip reason=quota-backoff while active."""
        deadline = self._quota_backoff_until
        if deadline is None:
            return False
        if now >= deadline:
            self._quota_backoff_until = None
            self.events.append(
                f"{self._backoff_reason}-cleared", deadline=_fmt_mtime(int(deadline * 1e9))
            )
            return False
        sig = (self._backoff_reason, round(deadline))
        if sig != self._last_skip:
            self.events.append(
                "cycle-skip",
                reason=self._backoff_reason,
                triggers=reasons,
                deadline=_fmt_mtime(int(deadline * 1e9)),
            )
            self._last_skip = sig
        return True

    def _run_cycle(self, now: float, reasons: list[str]) -> None:
        self.events.append("cycle-trigger", reasons=reasons)
        self._last_cycle_start = now
        self._mailbox_since_cycle = False
        try:
            rc = loop_mod.run_once(self.paths.project_root, self.session, self.config)
        except Exception as exc:  # the daemon must survive a crashed cycle
            self.events = EventLog(self.paths.events_path)
            # A 'crash' event (component=engine, error=type+message) makes the
            # unhandled exception minable by improve._crash_clusters; the legacy
            # 'error kind=cycle-crashed' event is retained for back-compat.
            self.events.append(
                "crash",
                component="engine",
                error=f"{type(exc).__name__}: {exc}",
            )
            self.events.append("error", kind="cycle-crashed", error=str(exc))
            return
        # run_once wrote events through its own EventLog; re-derive our seq.
        self.events = EventLog(self.paths.events_path)
        self.events.append("cycle-result", rc=rc, reasons=reasons)
        self._maybe_arm_quota_backoff(now, rc)
        if self.config.metrics.log_after_cycle:
            try:
                self.substrate.metrics_log()
            except SubstrateError as exc:
                self.events.append("error", kind="metrics-failed", error=str(exc))
            else:
                self.events.append("metrics", after_cycle=True, rc=rc)

    def _maybe_arm_quota_backoff(self, now: float, rc: int) -> None:
        """When the cycle just ended on a brain failure that must NOT be retried
        into the wall, arm the brain backoff. Two cases (rc==1 is the
        brain-failed cycle outcome from loop.run_once):

        - 'quota' (usage/session limit): deadline from the stderr 'resets <time>'
          hint when present, else config.brain.quota_backoff_minutes.
        - 'model-unavailable' (F3: the requested model is down): no reset hint,
          so the config window; the set event carries the declared
          model_failover so a human/the deck can re-pin instead of waiting.
        """
        if rc != 1:
            return
        failed = self._last_brain_failed()
        if failed is None:
            return
        kind = failed.get("failure_kind")
        if kind == "quota":
            excerpt = str(failed.get("stderr_excerpt") or "")
            deadline = parse_reset_deadline(excerpt, now)
            source = "stderr-reset"
            if deadline is None:
                deadline = now + self.config.brain.quota_backoff_minutes * 60
                source = "config-default"
            self._backoff_reason = "quota-backoff"
            self._quota_backoff_until = deadline
            self._last_skip = None  # let the first backoff skip log fresh
            self.events.append(
                "quota-backoff-set",
                deadline=_fmt_mtime(int(deadline * 1e9)),
                source=source,
            )
        elif kind == "model-unavailable":
            deadline = now + self.config.brain.quota_backoff_minutes * 60
            try:
                failover = self.substrate.harness_field(self.config.brain.harness, "model_failover")
            except SubstrateError:
                failover = ""
            self._backoff_reason = "model-unavailable-backoff"
            self._quota_backoff_until = deadline
            self._last_skip = None
            self.events.append(
                "model-unavailable-backoff-set",
                deadline=_fmt_mtime(int(deadline * 1e9)),
                harness=self.config.brain.harness,
                model_failover=failover,
            )

    def _last_brain_failed(self) -> dict | None:
        for event in reversed(self.events.tail(20)):
            if event.get("event") == "brain-failed":
                return event
        return None

    # ── scheduled lint ────────────────────────────────────────────────────

    def _newest_lint_log_epoch(self) -> float | None:
        """Midnight-UTC epoch of the newest '## [date] lint |' entry; None when
        log.md is absent or carries no lint entries (entries are date-only)."""
        try:
            text = (self.paths.ops_wiki / "log.md").read_text(encoding="utf-8")
        except OSError:
            return None
        newest: float | None = None
        for line in text.splitlines():
            match = _LINT_LOG_RE.match(line)
            if not match:
                continue
            try:
                stamp = datetime(
                    int(match[1]), int(match[2]), int(match[3]), tzinfo=timezone.utc
                ).timestamp()
            except ValueError:
                continue
            if newest is None or stamp > newest:
                newest = stamp
        return newest

    def _maybe_lint(self, now: float) -> None:
        """Dispatch loop-wiki-lint when the last lint run is older than
        lint.interval_h (or absent) — at most one attempt per interval (the
        lint-dispatch event, ok or not, is the once-per-interval latch)."""
        if not self.config.lint.enabled:
            return
        interval_s = self.config.lint.interval_h * 3600
        newest = self._newest_lint_log_epoch()
        if newest is not None and now - newest < interval_s:
            return
        if self.events.count_since("lint-dispatch", interval_s):
            return
        try:
            self.substrate.wiki_lint_dispatch()
        except SubstrateError as exc:
            self.events.append("lint-dispatch", ok=False, error=str(exc))
            return
        self.events.append("lint-dispatch", ok=True, interval_h=self.config.lint.interval_h)

    # ── plumbing ──────────────────────────────────────────────────────────

    def _heartbeat(self) -> None:
        """Touch the pid file so `status` can report liveness by mtime age."""
        try:
            os.utime(self.paths.pid_path, None)
        except FileNotFoundError:
            try:
                self.paths.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
            except OSError:
                pass
        except OSError:
            pass

    def _last_event_epoch(self, kind: str) -> float | None:
        for event in reversed(self.events.tail(500)):
            if event.get("event") == kind:
                try:
                    return parse_ts(event["ts"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    return None
        return None

    # ── asks ──────────────────────────────────────────────────────────────

    def _scan_replies(self) -> bool:
        """New *-to-coord.md mailbox files whose subject matches re:<ask-id>.

        Peek-only (the docs lane owns the ack). Returns True when at least one
        outstanding ask was marked replied — trigger (f).
        """
        mailbox = self.paths.mailbox_dir
        if not mailbox.is_dir():
            return False
        outstanding = {
            ask["id"]: ask
            for ask in actions_mod.load_asks(self.paths)
            if ask.get("status") == "outstanding" and isinstance(ask.get("id"), str)
        }
        matched = False
        for path in sorted(mailbox.glob("*-to-coord.md")):
            if path.name in self._seen_replies:
                continue
            self._seen_replies.add(path.name)
            subject = _peek_subject(path)
            if not subject:
                continue
            for ask_id in list(outstanding):
                if f"re:{ask_id}" in subject:
                    self._mark_ask(ask_id, "replied")
                    self.events.append("reply-received", ask=ask_id, file=path.name)
                    outstanding.pop(ask_id)
                    matched = True
        return matched

    def _expire_asks(self, now: float) -> None:
        """Outstanding asks past reply_timeout_s -> timed-out (exactly once)."""
        for ask in actions_mod.load_asks(self.paths):
            if ask.get("status") != "outstanding":
                continue
            try:
                created = parse_ts(ask["created_at"]).timestamp()
                timeout_s = float(ask["reply_timeout_s"])
            except (KeyError, TypeError, ValueError):
                continue
            if now - created >= timeout_s:
                self._mark_ask(ask["id"], "timed-out")
                self.events.append("reply-timeout", ask=ask["id"], lane=ask.get("lane"))

    def _mark_ask(self, ask_id: str, status: str) -> None:
        stamp_field = "replied_at" if status == "replied" else "timed_out_at"
        with file_lock(self.paths.lock_path):
            asks = actions_mod.load_asks(self.paths)
            for ask in asks:
                if ask.get("id") == ask_id and ask.get("status") == "outstanding":
                    ask["status"] = status
                    ask[stamp_field] = utc_now()
            actions_mod.save_asks(self.paths, asks)


# ── restart: confirm-dead-before-start (the singleton trap) ─────────────────

_STOP_POLL_S = 0.5


def stop_daemon(paths: SessionPaths, events: EventLog, timeout_s: float) -> bool:
    """SIGTERM the daemon named in the pid file and poll until it exits, up to
    timeout_s. Returns True when no daemon is running OR it exited; False when a
    LIVE daemon refused to die within the window (caller must NOT start a second
    — that is the singleton trap that bit us twice).

    Emits watch-stop-requested / watch-stopped / watch-stop-timeout."""
    pid = _read_pid(paths.pid_path)
    if pid is None or not pid_alive(pid):
        return True  # nothing alive to stop
    events.append("watch-stop-requested", pid=pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        events.append("watch-stopped", pid=pid)
        return True  # raced us to exit
    except OSError as exc:
        events.append("watch-stop-timeout", pid=pid, error=str(exc))
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            events.append("watch-stopped", pid=pid)
            return True
        time.sleep(_STOP_POLL_S)
    if not pid_alive(pid):
        events.append("watch-stopped", pid=pid)
        return True
    events.append("watch-stop-timeout", pid=pid, timeout_s=timeout_s)
    return False


def restart(
    project_root: str | Path, session: str, config: EngineConfig, timeout_s: float = 60.0
) -> int:
    """Stop the running daemon (confirm dead), then start watch exactly as the
    watch subcommand does. If a live daemon will not exit within timeout_s, DO
    NOT start a second one — report and return 1 (single-instance guarantee)."""
    paths = SessionPaths(Path(project_root), session)
    paths.ensure()
    events = EventLog(paths.events_path)
    if not stop_daemon(paths, events, timeout_s):
        pid = _read_pid(paths.pid_path)
        print(
            f"error: daemon (pid {pid}) did not exit within {timeout_s:.0f}s; "
            "NOT starting a second instance — kill it and retry",
            file=sys.stderr,
        )
        return 1
    return Watch(project_root, session, config).run()
