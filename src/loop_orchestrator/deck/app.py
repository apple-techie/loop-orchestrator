"""LoopDeckApp — the Textual flight deck.

The deck is a NON-WRITER: reads are files + substrate read methods, mutations
are substrate methods only, and every substrate call (all blocking) runs in a
thread worker, never on the event loop. The Substrate instance is injected so
pilot tests can pass a stub.
"""

from __future__ import annotations

import os
import time
import traceback as traceback_mod
from datetime import datetime, timezone
from pathlib import Path

from textual import work
from textual.app import App
from textual.binding import Binding

from ..paths import SessionPaths
from ..substrate import Substrate, SubstrateError
from . import model
from .modals import AddLaneModal, ConfirmModal, HelpModal, ReasonModal, SteerModal
from .model import DeckState, LaneRow
from .screens import LaneDetailScreen, MainScreen
from .widgets import MailboxPanel

SNAPSHOT_FRESH_S = 10


class LoopDeckApp(App):
    TITLE = "loop-deck"

    CSS = """
    #status-bar {
        dock: top;
        height: 1;
        background: $panel;
    }
    #main-cols { height: 1fr; }
    #left-col { width: 3fr; }
    #right-col { width: 2fr; }
    FleetTable { height: 2fr; }
    LoopsTable { height: 1fr; }
    DecisionQueue { height: 2fr; }
    ReviewQueue { height: 1fr; }
    MailboxPanel { height: 1fr; }
    EventTicker { height: 1fr; }
    FleetTable, LoopsTable, DecisionQueue, ReviewQueue, MailboxPanel, EventTicker,
    #lane-pane-scroll, #events-scroll, #adr-table, #adr-content-scroll,
    #brain-pane-scroll {
        border: round $primary;
        border-title-color: $text;
    }
    DecisionQueue, ReviewQueue, MailboxPanel, EventTicker { padding: 0 1; }
    FleetTable:focus, LoopsTable:focus, #adr-table:focus,
    #lane-pane-scroll:focus, #events-scroll:focus, #adr-content-scroll:focus,
    #brain-pane-scroll:focus {
        border: round $accent;
    }
    #lane-meta, #brain-meta { height: 1; background: $panel; }
    #lane-pane-scroll, #events-scroll, #brain-pane-scroll { height: 1fr; }
    #adr-table { height: 1fr; }
    #adr-content-scroll { height: 2fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("question_mark", "help", "help"),
        Binding("r", "force_refresh", "refresh"),
        Binding("n", "add_lane", "add lane"),
        Binding("s", "steer", "steer"),
        Binding("x", "drop_lane", "drop"),
        Binding("g", "jump", "jump", show=False),
        Binding("c", "checkpoint", "checkpoint"),
        Binding("y", "approve", "approve"),
        Binding("N", "reject", "reject"),
        Binding("p", "pause_toggle", "pause/resume"),
        Binding("a", "adrs", "adrs"),
        Binding("e", "events", "events"),
        Binding("b", "brain", "brain"),
        Binding("m", "toggle_processed", "mailbox view", show=False),
    ]

    def __init__(self, project_root: str | Path, session: str, substrate: Substrate | None = None):
        super().__init__()
        self.paths = SessionPaths(Path(project_root), session)
        self.session_name = session
        self.substrate = substrate if substrate is not None else Substrate(project_root, session)
        self.state: DeckState | None = None
        self.toasts: list[str] = []
        self.refreshed_at = ""
        self._watch_sig: tuple | None = None
        self._main = MainScreen()

    # ── crash diagnostics ─────────────────────────────────────────────────

    def _handle_exception(self, error: Exception) -> None:
        """Append a one-line crash record to the deck-OWNED deck-crash.log, then
        fall through to Textual's default (which exits the app).

        NON-WRITER NOTE: the deck is a strict non-writer of engine STATE — the
        invariant that protects decisions / snapshot / wiki (the things the
        engine reads back as inputs). deck-crash.log is a PLAIN DIAGNOSTIC LOG,
        not engine state JSON: writing it is the *point* (the DuplicateKey deck
        crash this session left zero trace), and it never feeds a decision. So
        this does not break the non-writer contract. improve._crash_clusters
        mines these lines as 'crash:deck' (surface 'none', report-only)."""
        try:
            self._append_crash_log(error)
        except Exception:  # diagnostics must never mask the real crash
            pass
        super()._handle_exception(error)

    def _append_crash_log(self, error: Exception) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tb = "".join(traceback_mod.format_exception_only(type(error), error)).strip()
        # Single-lined so each crash is exactly one minable record.
        record = f"{ts} component=deck error={' '.join(tb.split())}"
        log = self.paths.deck_crash_log
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(record + "\n")

    # ── lifecycle ─────────────────────────────────────────────────────────

    def get_default_screen(self) -> MainScreen:
        return self._main

    def on_mount(self) -> None:
        self._reload_worker()
        self.set_interval(2.0, self._stat_tick)
        self.set_interval(5.0, self._lane_tick)

    def toast(self, message: str, severity: str = "information") -> None:
        self.toasts.append(message)
        self.notify(message, severity=severity, timeout=6)

    # ── refresh machinery ─────────────────────────────────────────────────

    def _watch_list(self) -> tuple[Path, ...]:
        p = self.paths
        return (
            p.snapshot_path,
            p.pending_decision_path,
            p.events_path,
            p.state_file,
            p.mailbox_dir,
            p.checkpoint_page,
            p.paused_path,
            p.pid_path,
        )

    @staticmethod
    def _mtime_ns(path: Path) -> int | None:
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    def _stat_tick(self) -> None:
        sig = tuple(self._mtime_ns(path) for path in self._watch_list())
        if sig != self._watch_sig:
            self._watch_sig = sig
            self._reload_worker()

    def _lane_tick(self) -> None:
        age = model.snapshot_age(self.paths)
        if age is not None and age < SNAPSHOT_FRESH_S:
            return  # engine is observing; snapshot.json is authoritative
        self._lane_status_worker()

    @work(thread=True, exclusive=True, group="reload")
    def _reload_worker(self) -> None:
        try:
            state = model.load_state(self.substrate, self.paths, SNAPSHOT_FRESH_S)
        except Exception as exc:
            self.call_from_thread(self.toast, f"refresh failed: {exc}", "error")
            return
        self.call_from_thread(self._apply_state, state)

    @work(thread=True, exclusive=True, group="lane-status")
    def _lane_status_worker(self) -> None:
        try:
            statuses = self.substrate.lane_status_all()
        except Exception as exc:
            self.call_from_thread(self.toast, f"lane status failed: {exc}", "error")
            return
        self.call_from_thread(self._merge_statuses, statuses)

    def _apply_state(self, state: DeckState) -> None:
        self.state = state
        self.refreshed_at = time.strftime("%H:%M:%S")
        if self._main.is_mounted:
            self._main.apply_state(state, self.refreshed_at)

    def _merge_statuses(self, statuses: dict) -> None:
        if self.state is None:
            return
        for lane in self.state.lanes:
            st = statuses.get(lane.window)
            if st is not None:
                lane.status = st.status
                lane.kind = st.kind
        self._apply_state(self.state)

    def action_force_refresh(self) -> None:
        self._reload_worker()

    # ── selection helpers ─────────────────────────────────────────────────

    def _selected_lane(self) -> LaneRow | None:
        if not self._main.is_mounted:
            return None
        from .widgets import FleetTable

        return self._main.query_one(FleetTable).selected_lane()

    def open_lane_detail(self, lane: LaneRow) -> None:
        self.push_screen(LaneDetailScreen(lane))

    # ── decisions: y approve / N reject ──────────────────────────────────

    def action_approve(self) -> None:
        doc = self.state.pending_unresolved if self.state else None
        if doc is None:
            self.toast("no pending decision", "warning")
            return
        self._engine_cmd_worker("approve", str(doc["id"]))

    def action_reject(self) -> None:
        doc = self.state.pending_unresolved if self.state else None
        if doc is None:
            self.toast("no pending decision", "warning")
            return
        decision_id = str(doc["id"])

        def on_reason(reason: str | None) -> None:
            if reason is None:
                return
            self._engine_cmd_worker("reject", decision_id, "--reason", reason)

        self.push_screen(ReasonModal(f"reject decision {decision_id}"), on_reason)

    # ── engine: p pause/resume, c checkpoint ─────────────────────────────

    def action_pause_toggle(self) -> None:
        if self.state is not None and self.state.engine == "paused":
            self._engine_cmd_worker("resume")
        else:
            self._engine_cmd_worker("pause")

    def action_checkpoint(self) -> None:
        self.toast("running engine cycle (approval=manual)…")
        self._engine_cmd_worker("once", "--approval", "manual")

    @work(thread=True, group="mutate")
    def _engine_cmd_worker(self, *args: str) -> None:
        try:
            proc = self.substrate.engine_cmd(*args)
        except Exception as exc:
            self.call_from_thread(self.toast, f"{args[0]} failed: {exc}", "error")
            return
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        tail = "\n".join(out.splitlines()[-4:])
        if proc.returncode == 0:
            self.call_from_thread(self.toast, tail or f"{args[0]}: ok")
        else:
            message = err or tail or f"exit {proc.returncode}"
            self.call_from_thread(self.toast, f"{args[0]} failed: {message}", "error")
        self.call_from_thread(self._reload_worker)

    # ── lanes: n add, s steer, x drop, g jump ────────────────────────────

    def action_add_lane(self) -> None:
        def on_result(spec: dict | None) -> None:
            if spec is not None:
                self._add_lane_worker(spec)

        self.push_screen(AddLaneModal(), on_result)

    @work(thread=True, group="mutate")
    def _add_lane_worker(self, spec: dict) -> None:
        try:
            self.substrate.add_lane(
                window=spec["window"],
                harness=spec["harness"],
                model=spec["model"],
                role=spec["role"],
                auto_approve=spec["auto_approve"],
            )
        except Exception as exc:
            self.call_from_thread(self.toast, f"add-lane failed: {exc}", "error")
            return
        if spec["brief"]:
            try:
                # Fresh lane, fresh composer — no context to clear (#36).
                self.substrate.dispatch(
                    spec["window"], spec["brief"], wait_ready=True, no_clear=True
                )
            except Exception as exc:
                self.call_from_thread(
                    self.toast, f"lane '{spec['window']}' added; brief failed: {exc}", "error"
                )
                self.call_from_thread(self._reload_worker)
                return
        self.call_from_thread(self.toast, f"lane '{spec['window']}' added")
        self.call_from_thread(self._reload_worker)

    def action_steer(self) -> None:
        lane = self._selected_lane()
        if lane is None:
            self.toast("no lane selected", "warning")
            return

        def on_result(spec: dict | None) -> None:
            if spec is not None:
                self._steer_worker(spec)

        self.push_screen(SteerModal(lane.window), on_result)

    @work(thread=True, group="mutate")
    def _steer_worker(self, spec: dict) -> None:
        try:
            # A steer preserves the lane's existing context by definition —
            # never auto-/clear it (#36).
            self.substrate.dispatch(
                spec["lane"],
                spec["payload"],
                interrupt=spec["interrupt"],
                wait_ready=spec["wait_idle"],
                no_clear=True,
            )
        except Exception as exc:
            self.call_from_thread(self.toast, f"dispatch failed: {exc}", "error")
            return
        self.call_from_thread(self.toast, f"dispatched to '{spec['lane']}'")

    def action_drop_lane(self) -> None:
        lane = self._selected_lane()
        if lane is None:
            self.toast("no lane selected", "warning")
            return
        if lane.base:
            prompt = f"Drop BASE lane '{lane.window}'? The substrate guard may refuse."
            require = lane.window
        else:
            prompt = f"Drop lane '{lane.window}'?"
            require = None

        def on_result(confirmed: bool) -> None:
            if confirmed:
                self._drop_lane_worker(lane.window)

        self.push_screen(ConfirmModal(prompt, require_text=require), on_result)

    @work(thread=True, group="mutate")
    def _drop_lane_worker(self, window: str) -> None:
        # No force, ever: a base-lane drop surfaces the bash refusal verbatim.
        try:
            self.substrate.drop_lane(window)
        except SubstrateError as exc:
            self.call_from_thread(self.toast, str(exc), "error")
            return
        except Exception as exc:
            self.call_from_thread(self.toast, f"drop-lane failed: {exc}", "error")
            return
        self.call_from_thread(self.toast, f"lane '{window}' dropped")
        self.call_from_thread(self._reload_worker)

    def action_jump(self) -> None:
        lane = self._selected_lane()
        if lane is None:
            self.toast("no lane selected", "warning")
            return
        self._jump_worker(lane.window)

    @work(thread=True, group="mutate")
    def _jump_worker(self, window: str) -> None:
        try:
            self.substrate.jump_to_window(window)
        except SubstrateError as exc:
            self.call_from_thread(self.toast, str(exc), "warning")
        except Exception as exc:
            self.call_from_thread(self.toast, f"jump failed: {exc}", "error")

    # ── adr accept (human-gated; called from AdrScreen) ──────────────────

    def confirm_adr_accept(self, adr_id: str) -> None:
        def on_result(confirmed: bool) -> None:
            if confirmed:
                self._adr_accept_worker(adr_id)

        self.push_screen(ConfirmModal(f"Accept ADR {adr_id}? This is the human gate."), on_result)

    @work(thread=True, group="mutate")
    def _adr_accept_worker(self, adr_id: str) -> None:
        try:
            out = self.substrate.adr_accept(adr_id)
        except SubstrateError as exc:
            self.call_from_thread(self.toast, str(exc), "error")  # verbatim, incl. refusals
            return
        except Exception as exc:
            self.call_from_thread(self.toast, f"adr accept failed: {exc}", "error")
            return
        self.call_from_thread(self.toast, out.strip() or f"ADR {adr_id} accepted")
        self.call_from_thread(self._reload_worker)

    # ── navigation ────────────────────────────────────────────────────────

    def action_help(self) -> None:
        self.push_screen(HelpModal())

    def action_events(self) -> None:
        from .screens import EventsScreen

        self.push_screen(EventsScreen())

    def action_adrs(self) -> None:
        from .screens import AdrScreen

        self.push_screen(AdrScreen())

    def action_brain(self) -> None:
        from .screens import BrainScreen

        self.push_screen(BrainScreen())

    def action_toggle_processed(self) -> None:
        if not self._main.is_mounted:
            return
        panel = self._main.query_one(MailboxPanel)
        if panel._processed_view is None:
            panel.show_processed(model.processed_names(self.paths))
        else:
            panel.show_processed(None)
