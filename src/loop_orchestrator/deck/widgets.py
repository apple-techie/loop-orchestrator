"""Deck panels: fleet/loops tables, decision queue, mailbox, ticker, status bar."""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable, Static

from .model import DeckState, LaneRow, LoopRow, event_line

STATUS_STYLES = {
    "working": "yellow",
    "awaiting-approval": "red",
    "idle": "green",
    "errored": "bold red",
    "unknown": "dim",
}

ACTION_STATUS_STYLES = {
    "awaiting-approval": "yellow",
    "approved": "green",
    "executed": "green",
    "rejected": "red",
    "failed": "bold red",
}


def status_text(status: str) -> Text:
    return Text(status, style=STATUS_STYLES.get(status, ""))


class DeckTable(DataTable):
    """DataTable with vim row-cursor keys; rows rebuilt in place on refresh."""

    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(cursor_type="row", **kwargs)

    def rebuild(self, rows: list[tuple]) -> None:
        cursor = self.cursor_row
        self.clear()
        for key, cells in rows:
            self.add_row(*cells, key=key)
        if self.row_count:
            self.move_cursor(row=min(max(cursor, 0), self.row_count - 1))


class FleetTable(DeckTable):
    def on_mount(self) -> None:
        self.border_title = "fleet"
        self.add_columns("WINDOW", "HARNESS", "MODEL", "ROLE", "STATUS", "KIND", "RST")
        self._rows: list[LaneRow] = []

    def update_lanes(self, lanes: list[LaneRow]) -> None:
        self._rows = lanes
        self.rebuild(
            [
                (
                    lane.window,
                    (
                        lane.window,
                        lane.harness,
                        lane.model,
                        lane.role,
                        status_text(lane.status),
                        lane.kind + (" (base)" if lane.base else ""),
                        str(lane.restarts) if lane.restarts else "-",
                    ),
                )
                for lane in lanes
            ]
        )

    def selected_lane(self) -> LaneRow | None:
        if 0 <= self.cursor_row < len(self._rows):
            return self._rows[self.cursor_row]
        return None


class LoopsTable(DeckTable):
    def on_mount(self) -> None:
        self.border_title = "loops"
        self.add_columns("ID", "STATUS", "BRANCH", "NAME")

    def update_loops(self, loops: list[LoopRow]) -> None:
        self.rebuild([(loop.id, (loop.id, loop.status, loop.branch, loop.name)) for loop in loops])


class DecisionQueue(Static):
    """The single pending decision: id, critique, one line per action."""

    def on_mount(self) -> None:
        self.border_title = "decision queue"
        self.update_decision(None)

    def update_decision(self, doc: dict | None) -> None:
        self._doc = doc
        if not doc:
            self.update(Text("(no pending decision)", style="dim"))
            return
        text = Text()
        text.append(f"{doc.get('id', '?')} ", style="bold")
        text.append(f"[{doc.get('status', '?')}] ", style="yellow")
        text.append(f"mode={doc.get('approval_mode', '?')}\n", style="dim")
        critique = (doc.get("critique") or "").strip()
        if critique:
            text.append(critique + "\n", style="italic")
        for action in doc.get("actions") or []:
            target = action.get("lane") or action.get("window") or "-"
            status = str(action.get("status") or "?")
            text.append(f"\n{action.get('idx')} ", style="bold")
            text.append(f"{action.get('kind')} {target} ")
            text.append(f"{action.get('classification')} ", style="dim")
            text.append(status, style=ACTION_STATUS_STYLES.get(status, ""))
            rationale = (action.get("rationale") or "").strip()
            if rationale:
                text.append(f"\n  {rationale}", style="dim")
        text.append("\n\ny approve · N reject", style="dim")
        self.update(text)


class MailboxPanel(Static):
    """Pending mailbox messages; 'm' flips to a processed-files view."""

    def on_mount(self) -> None:
        self.border_title = "mailbox"
        self._pending: list[dict] = []
        self._processed_count = 0
        self._processed_view: list[str] | None = None
        self._refresh_view()

    def update_mailbox(self, pending: list[dict], processed_count: int) -> None:
        self._pending = pending
        self._processed_count = processed_count
        self._refresh_view()

    def show_processed(self, names: list[str] | None) -> None:
        self._processed_view = names
        self._refresh_view()

    def _refresh_view(self) -> None:
        text = Text()
        if self._processed_view is not None:
            text.append("processed (m: back)\n", style="bold")
            if not self._processed_view:
                text.append("(none)", style="dim")
            for name in self._processed_view:
                text.append(f"{name}\n")
        else:
            if not self._pending:
                text.append("(no pending messages)", style="dim")
            for msg in self._pending:
                text.append(f"{msg.get('from', '?')} -> {msg.get('to', '?')}", style="bold")
                text.append(f"  {msg.get('subject', '')}\n")
        text.append(f"\nprocessed: {self._processed_count}", style="dim")
        self.update(text)


class EventTicker(Static):
    def on_mount(self) -> None:
        self.border_title = "events"
        self.update(Text("(no events)", style="dim"))

    def update_events(self, events: list[dict], last: int = 8) -> None:
        if not events:
            self.update(Text("(no events)", style="dim"))
            return
        self.update(Text("\n".join(event_line(e) for e in events[-last:])))


class StatusBar(Static):
    ENGINE_STYLES = {"running": "bold green", "paused": "bold yellow", "off": "bold red"}

    status_line = ""  # plain-text mirror of the rendered bar

    def update_status(self, state: DeckState, refreshed_at: str) -> None:
        text = Text()
        text.append(f" session {state.session} ", style="bold")
        text.append("· engine ")
        text.append(state.engine.upper(), style=self.ENGINE_STYLES.get(state.engine, ""))
        if state.engine == "off":
            text.append("  OBSERVE MODE", style="bold red reverse")
        text.append(f" · refreshed {refreshed_at}", style="dim")
        self.status_line = text.plain
        self.update(text)
