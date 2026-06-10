"""Modal dialogs: add-lane, steer, confirm (typed-name for base lanes), reason, help."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

_DIALOG_CSS = """
$dialog-bg: $surface;

.deck-dialog {
    align: center middle;
}

.deck-dialog > Vertical {
    width: 70;
    max-height: 80%;
    height: auto;
    border: round $accent;
    background: $dialog-bg;
    padding: 1 2;
}

.deck-dialog Label.title {
    text-style: bold;
    margin-bottom: 1;
}

.deck-dialog Label.error {
    color: $error;
}

.deck-dialog Input, .deck-dialog TextArea, .deck-dialog Checkbox {
    margin-bottom: 1;
}

.deck-dialog TextArea {
    height: 6;
}

.deck-dialog Horizontal.buttons {
    height: auto;
    align-horizontal: right;
}

.deck-dialog Button {
    margin-left: 2;
}
"""


class AddLaneModal(ModalScreen[dict | None]):
    """Collect add-lane fields; the app runs substrate.add_lane (+ dispatch brief)."""

    CSS = _DIALOG_CSS
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("add lane", classes="title")
            yield Input(placeholder="window (required)", id="window")
            yield Input(placeholder="harness (e.g. claude)", id="harness")
            yield Input(placeholder="model (optional)", id="model")
            yield Input(placeholder="role (optional)", id="role")
            yield Input(placeholder="brief — dispatched after the lane is ready", id="brief")
            yield Checkbox("auto-approve", id="auto_approve")
            yield Label("", classes="error", id="error")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Add lane", variant="primary", id="submit")

    def on_mount(self) -> None:
        self.add_class("deck-dialog")
        self.query_one("#window", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "submit":
            self.dismiss(None)
            return
        self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        window = self.query_one("#window", Input).value.strip()
        harness = self.query_one("#harness", Input).value.strip()
        if not window:
            self.query_one("#error", Label).update("window is required")
            return
        if not harness:
            self.query_one("#error", Label).update("harness is required")
            return
        self.dismiss(
            {
                "window": window,
                "harness": harness,
                "model": self.query_one("#model", Input).value.strip() or None,
                "role": self.query_one("#role", Input).value.strip() or None,
                "brief": self.query_one("#brief", Input).value.strip(),
                "auto_approve": self.query_one("#auto_approve", Checkbox).value,
            }
        )


class SteerModal(ModalScreen[dict | None]):
    """Steer payload for one lane -> substrate.dispatch(lane, payload, ...)."""

    CSS = _DIALOG_CSS
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, lane: str):
        super().__init__()
        self.lane = lane

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"steer lane '{self.lane}'", classes="title")
            yield TextArea(id="payload")
            yield Checkbox("interrupt (Escape first — cancels in-flight work)", id="interrupt")
            yield Checkbox("wait until lane is ready", id="wait_idle")
            yield Label("", classes="error", id="error")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Dispatch", variant="primary", id="submit")

    def on_mount(self) -> None:
        self.add_class("deck-dialog")
        self.query_one("#payload", TextArea).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "submit":
            self.dismiss(None)
            return
        payload = self.query_one("#payload", TextArea).text.strip()
        if not payload:
            self.query_one("#error", Label).update("payload is empty")
            return
        self.dismiss(
            {
                "lane": self.lane,
                "payload": payload,
                "interrupt": self.query_one("#interrupt", Checkbox).value,
                "wait_idle": self.query_one("#wait_idle", Checkbox).value,
            }
        )


class ConfirmModal(ModalScreen[bool]):
    """Generic confirm; with require_text set, confirming needs the exact text typed
    (drop-lane on a base lane). Even then the deck never passes --force — the
    substrate's refusal is surfaced verbatim."""

    CSS = _DIALOG_CSS
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, prompt: str, require_text: str | None = None):
        super().__init__()
        self.prompt = prompt
        self.require_text = require_text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt, classes="title")
            if self.require_text:
                yield Label(f"type '{self.require_text}' to confirm:")
                yield Input(id="confirm-text")
            yield Label("", classes="error", id="error")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", variant="error", id="submit")

    def on_mount(self) -> None:
        self.add_class("deck-dialog")
        if self.require_text:
            self.query_one("#confirm-text", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "submit":
            self.dismiss(False)
            return
        self._submit()

    def _submit(self) -> None:
        if self.require_text:
            typed = self.query_one("#confirm-text", Input).value.strip()
            if typed != self.require_text:
                self.query_one("#error", Label).update(
                    f"name mismatch: type '{self.require_text}' exactly"
                )
                return
        self.dismiss(True)


class ReasonModal(ModalScreen[str | None]):
    """One-line reason input (decision reject)."""

    CSS = _DIALOG_CSS
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, prompt: str):
        super().__init__()
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.prompt, classes="title")
            yield Input(placeholder="reason", id="reason")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Reject", variant="error", id="submit")

    def on_mount(self) -> None:
        self.add_class("deck-dialog")
        self.query_one("#reason", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(self.query_one("#reason", Input).value.strip())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.dismiss(self.query_one("#reason", Input).value.strip())
        else:
            self.dismiss(None)


HELP_TEXT = """\
q quit                ? help
r force refresh       tab/shift-tab focus panel
j/k or arrows cursor  enter lane detail (fleet)
n add lane            s steer selected lane
x drop selected lane  g jump to tmux window
c checkpoint (once)   p pause/resume engine
y approve decision    N reject decision
a ADR screen          e events screen
escape back/cancel
"""


class HelpModal(ModalScreen[None]):
    CSS = _DIALOG_CSS
    BINDINGS = [
        Binding("escape", "cancel", "close"),
        Binding("q", "cancel", "close", show=False),
        Binding("question_mark", "cancel", "close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("loop-deck keys", classes="title")
            yield Static(HELP_TEXT)

    def on_mount(self) -> None:
        self.add_class("deck-dialog")

    def action_cancel(self) -> None:
        self.dismiss(None)
