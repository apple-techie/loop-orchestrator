"""LoopDeckApp pilot tests against a stub substrate (no subprocess, no tmux).

run_test() is async; the suite has no async plugin, so each test drives its
own asyncio.run().
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from loop_orchestrator.deck.app import LoopDeckApp
from loop_orchestrator.deck.modals import AddLaneModal, ConfirmModal, ReasonModal, SteerModal
from loop_orchestrator.deck.widgets import DecisionQueue, FleetTable, StatusBar
from loop_orchestrator.paths import SessionPaths
from loop_orchestrator.substrate import LaneInfo, LaneStatus, SubstrateError

PENDING = {
    "contract_version": 1,
    "id": "d-20260610-120000",
    "created_at": "2026-06-10T12:00:00Z",
    "approval_mode": "manual",
    "status": "pending",
    "critique": "web is idle; ship the fix",
    "actions": [
        {
            "idx": 0,
            "kind": "dispatch",
            "lane": "web",
            "payload": "run tests",
            "rationale": "unblock review",
            "classification": "safe",
            "status": "awaiting-approval",
        }
    ],
    "decided_by": None,
    "decided_at": None,
    "reason": "",
}


class StubSubstrate:
    """Canned reads + a recorded-calls list for every mutation."""

    def __init__(self):
        self.calls: list[tuple] = []

    # reads
    def lanes(self):
        return [
            LaneInfo("coord", "claude", None, "coordinator", None, True),
            LaneInfo("web", "claude", None, "impl", None, True),
            LaneInfo("helper", "claude", None, "impl", "claude", False),
        ]

    def lane_status_all(self):
        return {
            "coord": LaneStatus("coord", "idle", "demo:coord.1", "fixed"),
            "web": LaneStatus("web", "working", "demo:web.1", "fixed"),
            "helper": LaneStatus("helper", "idle", "%7", "dynamic"),
        }

    def digest(self):
        return {
            "contract_version": 1,
            "state": {"loops": {"loop-a": {"status": "implement", "branch": "loop/a"}}},
            "mailbox": {"pending": [], "processed_count": 0},
            "unpushed": [],
            "adrs": [],
        }

    def capture_pane(self, lane, lines=40):
        return f"pane tail for {lane}"

    # mutations (recorded)
    def engine_cmd(self, *args, **kwargs):
        self.calls.append(("engine_cmd", args))
        return SimpleNamespace(returncode=0, stdout=f"{args[0]}: ok\n", stderr="")

    def add_lane(self, **kwargs):
        self.calls.append(("add_lane", kwargs))

    def dispatch(self, lane, payload, **kwargs):
        self.calls.append(("dispatch", lane, payload, kwargs))

    def drop_lane(self, window):
        self.calls.append(("drop_lane", window))

    def jump_to_window(self, window):
        self.calls.append(("jump_to_window", window))

    def adr_accept(self, adr_id, adr_dir=None):
        self.calls.append(("adr_accept", adr_id))
        return "accepted\n"


@pytest.fixture
def paths(tmp_path):
    p = SessionPaths(tmp_path, "demo")
    p.ensure()
    return p


def make_app(paths, stub):
    return LoopDeckApp(paths.project_root, "demo", substrate=stub)


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_boot_renders_fleet_decision_and_observe_banner(paths):
    paths.pending_decision_path.write_text(json.dumps(PENDING), encoding="utf-8")
    stub = StubSubstrate()
    app = make_app(paths, stub)

    async def main():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            fleet = app.query_one(FleetTable)
            assert fleet.row_count == 3
            assert [lane.window for lane in fleet._rows] == ["coord", "web", "helper"]
            queue = app.query_one(DecisionQueue)
            assert queue._doc is not None and queue._doc["id"] == PENDING["id"]
            bar = app.query_one(StatusBar).status_line
            assert "OBSERVE MODE" in bar  # no pid, no paused file -> engine off
            assert "demo" in bar

    asyncio.run(main())


def test_engine_running_hides_observe_banner(paths):
    paths.pid_path.write_text(str(os.getpid()), encoding="utf-8")
    app = make_app(paths, StubSubstrate())

    async def main():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            assert app.state is not None and app.state.engine == "running"
            assert "OBSERVE MODE" not in app.query_one(StatusBar).status_line

    asyncio.run(main())


def test_approve_records_engine_cmd_and_toasts(paths):
    paths.pending_decision_path.write_text(json.dumps(PENDING), encoding="utf-8")
    stub = StubSubstrate()
    app = make_app(paths, stub)

    async def main():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("y")
            await settle(app, pilot)
            assert ("engine_cmd", ("approve", PENDING["id"])) in stub.calls
            assert any("approve" in toast for toast in app.toasts)

    asyncio.run(main())


def test_approve_without_pending_decision_is_a_noop_toast(paths):
    stub = StubSubstrate()
    app = make_app(paths, stub)

    async def main():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("y")
            await settle(app, pilot)
            assert not [c for c in stub.calls if c[0] == "engine_cmd"]
            assert "no pending decision" in app.toasts

    asyncio.run(main())


def test_reject_prompts_for_reason(paths):
    paths.pending_decision_path.write_text(json.dumps(PENDING), encoding="utf-8")
    stub = StubSubstrate()
    app = make_app(paths, stub)

    async def main():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("N")
            await pilot.pause()
            assert isinstance(app.screen, ReasonModal)
            from textual.widgets import Input

            app.screen.query_one("#reason", Input).value = "wrong lane"
            await pilot.press("enter")
            await settle(app, pilot)
            assert (
                "engine_cmd",
                ("reject", PENDING["id"], "--reason", "wrong lane"),
            ) in stub.calls

    asyncio.run(main())


def test_add_lane_modal_submits_add_lane_then_brief_dispatch(paths):
    stub = StubSubstrate()
    app = make_app(paths, stub)

    async def main():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, AddLaneModal)
            from textual.widgets import Button, Checkbox, Input

            app.screen.query_one("#window", Input).value = "scout"
            app.screen.query_one("#harness", Input).value = "claude"
            app.screen.query_one("#brief", Input).value = "explore the repo"
            app.screen.query_one("#auto_approve", Checkbox).value = True
            app.screen.query_one("#submit", Button).press()
            await settle(app, pilot)
            assert (
                "add_lane",
                {
                    "window": "scout",
                    "harness": "claude",
                    "model": None,
                    "role": None,
                    "auto_approve": True,
                },
            ) in stub.calls
            assert ("dispatch", "scout", "explore the repo", {"wait_ready": True}) in stub.calls
            # add_lane strictly before the brief dispatch
            kinds = [c[0] for c in stub.calls if c[0] in ("add_lane", "dispatch")]
            assert kinds == ["add_lane", "dispatch"]

    asyncio.run(main())


def test_drop_base_lane_requires_typed_window_name(paths):
    stub = StubSubstrate()
    app = make_app(paths, stub)

    async def main():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            # cursor starts on row 0 = coord (base lane)
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            from textual.widgets import Button, Input

            app.screen.query_one("#submit", Button).press()
            await settle(app, pilot)
            assert isinstance(app.screen, ConfirmModal)  # still open: nothing typed
            assert not [c for c in stub.calls if c[0] == "drop_lane"]
            app.screen.query_one("#confirm-text", Input).value = "wrong-name"
            app.screen.query_one("#submit", Button).press()
            await settle(app, pilot)
            assert not [c for c in stub.calls if c[0] == "drop_lane"]
            app.screen.query_one("#confirm-text", Input).value = "coord"
            app.screen.query_one("#submit", Button).press()
            await settle(app, pilot)
            assert ("drop_lane", "coord") in stub.calls  # no force, ever

    asyncio.run(main())


def test_drop_dynamic_lane_plain_confirm(paths):
    stub = StubSubstrate()
    app = make_app(paths, stub)

    async def main():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("j", "j")  # cursor to helper (dynamic)
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            from textual.widgets import Button

            app.screen.query_one("#submit", Button).press()
            await settle(app, pilot)
            assert ("drop_lane", "helper") in stub.calls

    asyncio.run(main())


def test_steer_modal_dispatch_with_interrupt(paths):
    stub = StubSubstrate()
    app = make_app(paths, stub)

    async def main():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("s")  # selected lane = coord (row 0)
            await pilot.pause()
            assert isinstance(app.screen, SteerModal)
            from textual.widgets import Button, Checkbox, TextArea

            app.screen.query_one("#payload", TextArea).load_text("focus on the API tests")
            app.screen.query_one("#interrupt", Checkbox).value = True
            app.screen.query_one("#submit", Button).press()
            await settle(app, pilot)
            assert (
                "dispatch",
                "coord",
                "focus on the API tests",
                {"interrupt": True, "wait_ready": False},
            ) in stub.calls

    asyncio.run(main())


def test_checkpoint_and_pause_toggle(paths):
    stub = StubSubstrate()
    app = make_app(paths, stub)

    async def main():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("c")
            await settle(app, pilot)
            assert ("engine_cmd", ("once", "--approval", "manual")) in stub.calls
            await pilot.press("p")  # engine off -> pause is still meaningful
            await settle(app, pilot)
            assert ("engine_cmd", ("pause",)) in stub.calls
            # now simulate the paused marker the engine CLI would have written
            paths.paused_path.touch()
            app.action_force_refresh()
            await settle(app, pilot)
            assert app.state is not None and app.state.engine == "paused"
            await pilot.press("p")
            await settle(app, pilot)
            assert ("engine_cmd", ("resume",)) in stub.calls

    asyncio.run(main())


def test_jump_outside_tmux_surfaces_substrate_error(paths):
    class JumpFail(StubSubstrate):
        def jump_to_window(self, window):
            raise SubstrateError(["tmux"], None, "not inside tmux (use attach instead)")

    app = make_app(paths, JumpFail())

    async def main():
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("g")
            await settle(app, pilot)
            assert any("not inside tmux" in toast for toast in app.toasts)

    asyncio.run(main())
