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
from . import loop as loop_mod
from .brain import BUDGET_WINDOW_S
from .config import EngineConfig
from .events import EventLog, parse_ts, utc_now
from .observe import Observer, delta

# A working lane landing in one of these states means its task ended.
_CYCLE_END_STATUSES = frozenset({"idle", "errored", "awaiting-approval"})

# Newest '## [date] lint | …' log.md entry = when the last lint run closed.
_LINT_LOG_RE = re.compile(r"^## \[(\d{4})-(\d{2})-(\d{2})\] lint \|")


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

    def _run_cycle(self, now: float, reasons: list[str]) -> None:
        self.events.append("cycle-trigger", reasons=reasons)
        self._last_cycle_start = now
        self._mailbox_since_cycle = False
        try:
            rc = loop_mod.run_once(self.paths.project_root, self.session, self.config)
        except Exception as exc:  # the daemon must survive a crashed cycle
            self.events = EventLog(self.paths.events_path)
            self.events.append("error", kind="cycle-crashed", error=str(exc))
            return
        # run_once wrote events through its own EventLog; re-derive our seq.
        self.events = EventLog(self.paths.events_path)
        self.events.append("cycle-result", rc=rc, reasons=reasons)
        if self.config.metrics.log_after_cycle:
            try:
                self.substrate.metrics_log()
            except SubstrateError as exc:
                self.events.append("error", kind="metrics-failed", error=str(exc))
            else:
                self.events.append("metrics", after_cycle=True, rc=rc)

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
