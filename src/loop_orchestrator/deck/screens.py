"""Deck screens: main grid, lane detail, full event tail, ADR browser."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from ..engine.events import EventLog
from . import model
from .model import DeckState, LaneRow
from .widgets import (
    DecisionQueue,
    DeckTable,
    EventTicker,
    FleetTable,
    LoopsTable,
    MailboxPanel,
    StatusBar,
)

if TYPE_CHECKING:
    from .app import LoopDeckApp


class DeckScreen(Screen):
    """Common: typed access to the deck app."""

    @property
    def deck(self) -> LoopDeckApp:
        return cast("LoopDeckApp", self.app)


class MainScreen(DeckScreen):
    def on_mount(self) -> None:
        # Cover the race where the first reload finished before this mount.
        deck = self.deck
        if deck.state is not None:
            self.apply_state(deck.state, deck.refreshed_at)

    def compose(self) -> ComposeResult:
        yield StatusBar(id="status-bar")
        with Horizontal(id="main-cols"):
            with Vertical(id="left-col"):
                yield FleetTable(id="fleet")
                yield LoopsTable(id="loops")
            with Vertical(id="right-col"):
                yield DecisionQueue(id="decisions")
                yield MailboxPanel(id="mailbox")
                yield EventTicker(id="ticker")
        yield Footer()

    def apply_state(self, state: DeckState, refreshed_at: str) -> None:
        self.query_one(FleetTable).update_lanes(state.lanes)
        self.query_one(LoopsTable).update_loops(state.loops)
        self.query_one(DecisionQueue).update_decision(state.pending)
        self.query_one(MailboxPanel).update_mailbox(state.mailbox_pending, state.processed_count)
        self.query_one(EventTicker).update_events(state.events_tail)
        self.query_one(StatusBar).update_status(state, refreshed_at)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if isinstance(event.data_table, FleetTable):
            lane = event.data_table.selected_lane()
            if lane is not None:
                self.deck.open_lane_detail(lane)


class LaneDetailScreen(DeckScreen):
    """Lane metadata + live pane tail (substrate.capture_pane every 2s)."""

    BINDINGS = [Binding("escape", "app.pop_screen", "back")]

    def __init__(self, lane: LaneRow):
        super().__init__()
        self.lane = lane

    def compose(self) -> ComposeResult:
        meta = Text()
        meta.append(f" lane {self.lane.window} ", style="bold")
        meta.append(
            f"harness={self.lane.harness} model={self.lane.model} role={self.lane.role} "
            f"kind={self.lane.kind}{' (base)' if self.lane.base else ''} "
            f"restarts={self.lane.restarts}",
            style="dim",
        )
        yield Static(meta, id="lane-meta")
        with VerticalScroll(id="lane-pane-scroll"):
            yield Static("(capturing pane…)", id="lane-pane")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#lane-pane-scroll").border_title = f"pane tail: {self.lane.window}"
        self._refresh_pane()
        self.set_interval(2.0, self._refresh_pane)

    @work(thread=True, exclusive=True, group="pane-tail")
    def _refresh_pane(self) -> None:
        try:
            tail = self.deck.substrate.capture_pane(self.lane.window)
        except Exception as exc:
            tail = f"(capture failed: {exc})"
        self.app.call_from_thread(self._show_tail, tail)

    def _show_tail(self, tail: str) -> None:
        if self.is_mounted:
            self.query_one("#lane-pane", Static).update(tail or "(empty pane)")


class EventsScreen(DeckScreen):
    """Scrollable tail of the last 100 engine events."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("r", "reload", "refresh"),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="events-scroll"):
            yield Static("(no events)", id="events-full")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#events-scroll").border_title = "events (last 100)"
        self.action_reload()

    def action_reload(self) -> None:
        events = EventLog(self.deck.paths.events_path).tail(100)
        body = "\n".join(model.event_line(e) for e in events) or "(no events)"
        self.query_one("#events-full", Static).update(body)


class AdrScreen(DeckScreen):
    """ADR list from the digest; enter shows the record, A runs the human-gated accept."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("A", "accept", "accept (human-gated)"),
    ]

    def compose(self) -> ComposeResult:
        yield DeckTable(id="adr-table")
        with VerticalScroll(id="adr-content-scroll"):
            yield Static("(select an ADR and press enter)", id="adr-content")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#adr-table", DeckTable)
        table.border_title = "adrs"
        table.add_columns("ID", "STATUS", "TITLE")
        self.query_one("#adr-content-scroll").border_title = "record"
        self._adrs: list[dict] = list(self.deck.state.adrs) if self.deck.state else []
        table.rebuild(
            [
                (
                    str(adr.get("id")),
                    (str(adr.get("id")), str(adr.get("status")), str(adr.get("title"))),
                )
                for adr in self._adrs
            ]
        )

    def _selected_adr(self) -> dict | None:
        table = self.query_one("#adr-table", DeckTable)
        if 0 <= table.cursor_row < len(self._adrs):
            return self._adrs[table.cursor_row]
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        adr = self._selected_adr()
        if adr is not None:
            text = model.adr_text(self.deck.paths.project_root, adr)
            self.query_one("#adr-content", Static).update(text)

    def action_accept(self) -> None:
        adr = self._selected_adr()
        if adr is None:
            self.deck.toast("no ADR selected", "warning")
            return
        self.deck.confirm_adr_accept(str(adr.get("id")))
