"""actions.execute — drop_lane handoff-breadcrumb flush (T0023).

The flush leaves an append-only `## Handoff state` breadcrumb on the lane page,
but ONLY for a verified-idle agent lane; non-agent / unknown-harness /
not-verified-idle lanes SKIP. The teardown always proceeds.
"""

from __future__ import annotations

from loop_orchestrator.engine import actions
from loop_orchestrator.engine.config import EngineConfig
from loop_orchestrator.engine.events import EventLog
from loop_orchestrator.paths import SessionPaths
from loop_orchestrator.substrate import LaneInfo, SubstrateError

_KNOWN = {"claude", "codex", "pi", "shell", "mprocs"}


class FakeSub:
    def __init__(self, infos, statuses, pane="line one\nworking on step 3\n"):
        self._infos = infos
        self._statuses = statuses
        self._pane = pane
        self.dropped: list[str] = []

    def lanes(self):
        return self._infos

    def harness_field(self, name, field):
        if name in _KNOWN:
            return ""
        raise SubstrateError([name], 1, f"unknown harness {name}")

    def lane_status(self, lane):
        return self._statuses.get(lane, "unknown")

    def capture_pane(self, lane, lines=40):
        return self._pane

    def drop_lane(self, window):
        self.dropped.append(window)


def _info(window, harness):
    return LaneInfo(
        window=window, harness=harness, model=None, role="impl", cmd=None, base=False, kind="worker"
    )


def _env(tmp_path):
    paths = SessionPaths(tmp_path, "demo")
    paths.ensure()
    return paths, EventLog(paths.events_path)


def _drop(window):
    return {"kind": "drop_lane", "window": window, "rationale": "r"}


def _events(events):
    return [e["event"] for e in events.tail(20)]


def test_flush_writes_breadcrumb_on_idle_agent(tmp_path):
    paths, events = _env(tmp_path)
    sub = FakeSub([_info("helper", "claude")], {"helper": "idle"})
    actions.execute(_drop("helper"), sub, events, EngineConfig(), paths=paths)
    page = paths.lane_page("helper").read_text(encoding="utf-8")
    assert "## Handoff state" in page
    assert "harness: claude" in page
    assert "working on step 3" in page  # the captured pane tail
    assert sub.dropped == ["helper"]  # teardown still happened
    assert "handoff-flush" in _events(events)


def test_flush_skips_non_agent_lane(tmp_path):
    paths, events = _env(tmp_path)
    sub = FakeSub([_info("ops1", "shell")], {"ops1": "idle"})
    actions.execute(_drop("ops1"), sub, events, EngineConfig(), paths=paths)
    assert not paths.lane_page("ops1").exists()
    assert sub.dropped == ["ops1"]
    evs = _events(events)
    assert "handoff-skip" in evs and "handoff-flush" not in evs


def test_flush_skips_unknown_harness(tmp_path):
    paths, events = _env(tmp_path)
    sub = FakeSub([_info("weird", "nosuch")], {"weird": "idle"})
    actions.execute(_drop("weird"), sub, events, EngineConfig(), paths=paths)
    assert not paths.lane_page("weird").exists()
    assert sub.dropped == ["weird"]
    assert "handoff-skip" in _events(events)


def test_flush_skips_when_not_verified_idle(tmp_path):
    paths, events = _env(tmp_path)
    sub = FakeSub([_info("helper", "claude")], {"helper": "working"})
    actions.execute(_drop("helper"), sub, events, EngineConfig(), paths=paths)
    assert not paths.lane_page("helper").exists()
    assert sub.dropped == ["helper"]
    evs = _events(events)
    assert "handoff-skip" in evs and "handoff-flush" not in evs


def test_handoff_breadcrumb_is_append_only(tmp_path):
    paths, events = _env(tmp_path)
    page = paths.lane_page("helper")
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("# lane: helper\n\n## Role\nworker lane\n", encoding="utf-8")
    sub = FakeSub([_info("helper", "claude")], {"helper": "idle"})
    actions.execute(_drop("helper"), sub, events, EngineConfig(), paths=paths)
    text = page.read_text(encoding="utf-8")
    assert text.startswith("# lane: helper\n\n## Role\nworker lane\n")  # original preserved
    assert "## Handoff state" in text


# ── T0026 conditional worktree provisioning on add_lane ─────────────────────


class AddLaneSub:
    def __init__(self, branch_heads: list[str | None] | None = None, dirty: bool = False):
        self.calls: list[dict] = []
        self.dispatched: list[tuple] = []
        self.verifies: list[dict] = []
        self.builds: list[dict] = []
        self.branch_heads = list(branch_heads or [])
        self.dirty = dirty

    def worktree_dirty(self, worktree):
        return self.dirty

    def add_lane(self, window, **kwargs):
        self.calls.append({"window": window, **kwargs})

    def dispatch(self, lane, payload, **k):
        self.dispatched.append((lane, payload))

    def spawn_verify(self, worktree, base, tip, out_path):
        self.verifies.append({"worktree": worktree, "base": base, "tip": tip, "out_path": out_path})
        return 4242

    def spawn_build(self, worktree, brief):
        self.builds.append({"worktree": worktree, "brief": brief})
        return 4343

    def branch_head(self, worktree, branch):
        if self.branch_heads:
            return self.branch_heads.pop(0)
        return None


class FailingVerifySub(AddLaneSub):
    def spawn_verify(self, worktree, base, tip, out_path):
        raise SubstrateError(["missing-loop-verify"], 127, "exec failed")


class FailingBuildSub(AddLaneSub):
    def spawn_build(self, worktree, brief):
        raise SubstrateError(["codex"], 127, "exec failed")


def _add(harness="claude", cmd=None, window="w", brief="b"):
    a = {"kind": "add_lane", "window": window, "harness": harness, "brief": brief, "rationale": "r"}
    if cmd:
        a["cmd"] = cmd
    return a


def test_add_lane_shared_when_sole_code_writer(tmp_path):
    paths, events = _env(tmp_path)
    sub = AddLaneSub()
    actions.execute(_add(), sub, events, EngineConfig(), paths=paths, code_writers=0)
    assert sub.calls[0]["worktree"] is False  # DORMANT at concurrency=1


def test_add_lane_worktree_when_a_peer_is_concurrent(tmp_path):
    paths, events = _env(tmp_path)
    sub = AddLaneSub()
    actions.execute(_add(), sub, events, EngineConfig(), paths=paths, code_writers=1)
    assert sub.calls[0]["worktree"] is True  # second concurrent code-writer


def test_add_lane_shell_lane_never_worktree(tmp_path):
    paths, events = _env(tmp_path)
    sub = AddLaneSub()
    actions.execute(_add(harness="shell"), sub, events, EngineConfig(), paths=paths, code_writers=5)
    assert sub.calls[0]["worktree"] is False  # not a code-writer lane


def test_add_lane_dormant_without_concurrency_signal(tmp_path):
    # cli path / no policy: code_writers=None => shared, byte-identical to today.
    paths, events = _env(tmp_path)
    sub = AddLaneSub()
    actions.execute(_add(), sub, events, EngineConfig(), paths=paths, code_writers=None)
    assert sub.calls[0]["worktree"] is False


def test_add_lane_explicit_worktree_field_still_honored(tmp_path):
    paths, events = _env(tmp_path)
    sub = AddLaneSub()
    action = {**_add(), "worktree": True}
    actions.execute(action, sub, events, EngineConfig(), paths=paths, code_writers=0)
    assert sub.calls[0]["worktree"] is True  # explicit T0025 opt-in preserved


# ── T0028 full lane-handoff (recovery brief + ack + worktree carry) ──────────

import json  # noqa: E402

from loop_orchestrator.engine import wiki  # noqa: E402


def _seed_handoff(paths, window="w"):
    page = paths.lane_page(window)
    page.parent.mkdir(parents=True, exist_ok=True)
    wiki.append_handoff(page, window, "claude", "last step: wiring the gate\n", "2026-06-14T00:00Z")


def test_handoff_ack_subject_format():
    assert actions.handoff_ack_subject("worker-3") == "re:handoff:worker-3"


def test_cold_add_brief_unchanged(tmp_path):
    # No predecessor handoff state => the dispatched brief is the original, verbatim.
    paths, events = _env(tmp_path)
    sub = AddLaneSub()
    actions.execute(_add(brief="do the thing"), sub, events, EngineConfig(), paths=paths)
    assert sub.dispatched == [("w", "do the thing")]


def test_successor_gets_recovery_brief(tmp_path):
    # A window with a `## Handoff state` page => the successor's brief is augmented.
    paths, events = _env(tmp_path)
    _seed_handoff(paths, "w")
    sub = AddLaneSub()
    actions.execute(_add(brief="do the thing"), sub, events, EngineConfig(), paths=paths)
    _, payload = sub.dispatched[0]
    assert "Handoff recovery" in payload
    assert "ops-wiki/lanes/w.md" in payload
    assert "subject: re:handoff:w" in payload  # ack instruction
    assert payload.endswith("do the thing")  # original brief preserved
    assert any(e["event"] == "handoff-recovery" for e in events.tail(10))


def test_successor_carries_predecessor_worktree(tmp_path):
    # Predecessor was worktree-isolated (branch in the ledger) => carry it.
    paths, events = _env(tmp_path)
    _seed_handoff(paths, "w")
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text(
        json.dumps({"loops": {"w": {"branch": "loop/demo/w"}}}), encoding="utf-8"
    )
    sub = AddLaneSub()
    actions.execute(_add(brief="resume"), sub, events, EngineConfig(), paths=paths, code_writers=0)
    assert sub.calls[0]["worktree"] is True  # carried despite code_writers=0
    _, payload = sub.dispatched[0]
    assert "loop/demo/w" in payload  # the carried branch is named in the brief


def test_successor_without_predecessor_branch_no_carry(tmp_path):
    # Handoff state but no recorded branch (shared predecessor) => no worktree carry.
    paths, events = _env(tmp_path)
    _seed_handoff(paths, "w")
    sub = AddLaneSub()
    actions.execute(_add(brief="resume"), sub, events, EngineConfig(), paths=paths, code_writers=0)
    assert sub.calls[0]["worktree"] is False


# ── T0032 (B2) defensive stop: re-probe before honoring a `stop` ─────────────


class _ReprobeSub:
    """Records lane_status re-probes; a 'RAISE' status throws SubstrateError."""

    def __init__(self, statuses: dict[str, str]):
        self._statuses = statuses
        self.probed: list[str] = []

    def lane_status(self, lane: str) -> str:
        self.probed.append(lane)
        status = self._statuses.get(lane, "idle")
        if status == "RAISE":
            raise SubstrateError(["loop-lane-status", lane], 1, "boom")
        return status


class _MailboxSub:
    def __init__(self, pending: list[str]):
        self.pending = pending
        self.digest_calls = 0

    def digest(self) -> dict:
        self.digest_calls += 1
        return {
            "mailbox": {
                "pending": [{"file": name} for name in self.pending],
                "processed_count": 0,
            },
        }


def test_stop_no_working_lane_executes_without_reprobe(tmp_path):
    # Genuinely-idle fleet => no re-probe, stop is honored (return False).
    paths, events = _env(tmp_path)
    sub = _ReprobeSub({})
    assert actions.stop_suspected_idle_stall(sub, set(), events) is False
    assert sub.probed == []
    assert "stop-suspected-idle-stall" not in _events(events)


def test_stop_new_mailbox_message_suppresses(tmp_path):
    paths, events = _env(tmp_path)
    sub = _MailboxSub(["20260610-010000-web-to-coord.md"])
    assert actions.stop_suspected_mailbox_race(sub, [], events) is True
    assert sub.digest_calls == 1
    race = [e for e in events.tail(10) if e["event"] == "stop-suspected-mailbox-race"]
    assert race and race[-1]["files"] == ["20260610-010000-web-to-coord.md"]


def test_stop_unchanged_empty_mailbox_executes(tmp_path):
    paths, events = _env(tmp_path)
    sub = _MailboxSub([])
    assert actions.stop_suspected_mailbox_race(sub, [], events) is False
    assert sub.digest_calls == 1
    assert "stop-suspected-mailbox-race" not in _events(events)


def test_stop_reprobe_still_working_suppresses(tmp_path):
    # Observed working + re-probe STILL working => suspected idle-stall, suppress.
    paths, events = _env(tmp_path)
    sub = _ReprobeSub({"web": "working"})
    assert actions.stop_suspected_idle_stall(sub, {"web"}, events) is True
    assert sub.probed == ["web"]  # re-probe invoked
    stall = [e for e in events.tail(10) if e["event"] == "stop-suspected-idle-stall"]
    assert stall and stall[-1]["lanes"] == ["web"]


def test_stop_reprobe_now_idle_executes(tmp_path):
    # Observed working but the lane finished by the re-probe => honor the stop.
    paths, events = _env(tmp_path)
    sub = _ReprobeSub({"web": "idle"})
    assert actions.stop_suspected_idle_stall(sub, {"web"}, events) is False
    assert sub.probed == ["web"]
    assert "stop-suspected-idle-stall" not in _events(events)


def test_stop_reprobe_error_is_not_counted_as_working(tmp_path):
    # A lane we can't probe is not evidence of work => does not suppress.
    paths, events = _env(tmp_path)
    sub = _ReprobeSub({"web": "RAISE"})
    assert actions.stop_suspected_idle_stall(sub, {"web"}, events) is False
    assert "stop-suspected-idle-stall" not in _events(events)


# ── loop improvement #36: per-kind auto-/clear opt-out contract ─────────────
# The auto-/clear lives in loop-dispatch.sh and is gated there on harness=claude
# + fresh (non-interrupt) + idle. The engine's only responsibility is to opt OUT
# (no_clear=True) on the dispatch kinds that must preserve context, and to leave
# it ON (default) for the fresh self-contained-task dispatch. These pin that.


class _DispatchKwargsSub:
    """Records every dispatch call's full kwargs so we can assert no_clear."""

    def __init__(self):
        self.dispatched: list[tuple[str, dict]] = []
        self.added: list[str] = []

    def add_lane(self, window, **kwargs):
        self.added.append(window)

    def dispatch(self, lane, payload, **kwargs):
        self.dispatched.append((lane, kwargs))

    def lanes(self):
        return []


def test_fresh_dispatch_kind_does_not_opt_out_of_clear(tmp_path):
    # The fresh self-contained-task dispatch (kind="dispatch") is THE path the
    # auto-/clear targets — it must NOT set no_clear, so loop-dispatch can reset
    # a claude lane that accumulated context across the session.
    paths, events = _env(tmp_path)
    sub = _DispatchKwargsSub()
    action = {"kind": "dispatch", "lane": "web", "payload": "do task T0001", "rationale": "r"}
    actions.execute(action, sub, events, EngineConfig(), paths=paths)
    assert sub.dispatched == [("web", {"mode": "text", "wait_ready": False})]
    assert sub.dispatched[0][1].get("no_clear", False) is False


def test_add_lane_brief_opts_out_of_clear(tmp_path):
    # A freshly-provisioned lane has no accumulated context — opt out so the
    # clear can't race the booting welcome screen.
    paths, events = _env(tmp_path)
    sub = _DispatchKwargsSub()
    actions.execute(_add(brief="explore"), sub, events, EngineConfig(), paths=paths)
    assert sub.dispatched[0][0] == "w"
    assert sub.dispatched[0][1].get("no_clear") is True


def test_steer_opts_out_of_clear(tmp_path):
    # A steer is mid-conversation guidance — it MUST preserve the lane's context,
    # so it always opts out, even without --interrupt.
    paths, events = _env(tmp_path)
    sub = _DispatchKwargsSub()
    action = {"kind": "steer", "lane": "coord", "payload": "focus on the API", "rationale": "r"}
    actions.execute(action, sub, events, EngineConfig(), paths=paths)
    assert sub.dispatched[0][1].get("no_clear") is True


# ── F15 (T0039): worktree-lane escalations route to the ENGINE mailbox ───────
# A lane running in a git worktree has its OWN cwd-isolated .loop/messages
# (.loop/ is gitignored, so each worktree gets a fresh one) that the engine
# never ingests. The escalation/reply instruction the engine hands a lane must
# therefore name the ENGINE's mailbox at the MAIN-checkout root by ABSOLUTE
# path — never a cwd-relative one a worktree lane would resolve inside its own
# tree (the blind-escalation gap surfaced by batch 1).

import re as _re  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from loop_orchestrator.engine import improve  # noqa: E402

_COORD_MSG_RE = _re.compile(r"(\S+)/<UTC ts YYYYMMDD-HHMMSS>-(\S+?)-to-coord\.md")


def _coord_dir_from_instruction(payload: str) -> Path:
    """The directory the engine told the lane to write its *-to-coord.md into."""
    match = _COORD_MSG_RE.search(payload)
    assert match, f"no coord-message instruction in payload: {payload!r}"
    return Path(match.group(1))


def _steer_reply(lane="web"):
    return {
        "kind": "steer",
        "lane": lane,
        "payload": "status?",
        "expects_reply": True,
        "rationale": "r",
    }


def test_steer_reply_routes_to_engine_mailbox_by_absolute_path(tmp_path):
    # The engine runs from the MAIN checkout; a worktree lane's cwd differs.
    paths = SessionPaths(tmp_path / "main", "demo")
    paths.ensure()
    events = EventLog(paths.events_path)
    sub = AddLaneSub()
    actions.execute(_steer_reply(), sub, events, EngineConfig(), ask_id="D1-0", paths=paths)
    _, payload = sub.dispatched[0]
    coord_dir = _coord_dir_from_instruction(payload)
    # routes to the ENGINE mailbox at the main root, by ABSOLUTE path so a
    # worktree-chdir'd lane cannot misresolve it against its own cwd ...
    assert coord_dir.is_absolute()
    assert coord_dir == paths.mailbox_dir
    # ... GUARD: NOT a worktree-local mailbox (what the old relative hint hit).
    worktree_local = tmp_path / "worktrees" / "web" / ".loop" / "messages"
    assert coord_dir != worktree_local


def test_worktree_lane_escalation_is_ingest_discoverable(tmp_path):
    # Simulate the lane FOLLOWING the instruction: write its escalation into the
    # exact dir the engine named, then assert the engine's mailbox scan finds it.
    paths = SessionPaths(tmp_path / "main", "demo")
    paths.ensure()
    events = EventLog(paths.events_path)
    sub = AddLaneSub()
    actions.execute(_steer_reply(), sub, events, EngineConfig(), ask_id="D1-0", paths=paths)
    coord_dir = _coord_dir_from_instruction(sub.dispatched[0][1])
    coord_dir.mkdir(parents=True, exist_ok=True)
    msg = coord_dir / "20260617-101500-web-to-coord.md"
    msg.write_text("---\nsubject: blocked: need a decision\n---\n\nbody\n", encoding="utf-8")
    # it physically landed under the MAIN engine mailbox (not a worktree) ...
    assert msg.parent == paths.mailbox_dir
    # ... and the engine's mailbox-ingest scan discovers it.
    clusters = improve._human_intervention_clusters(
        paths, datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert any("web-to-coord.md" in s for c in clusters for s in c["samples"])


def test_handoff_ack_routes_to_engine_mailbox_by_absolute_path(tmp_path):
    # A successor recovering a handoff (often in a CARRIED worktree) must ack to
    # the engine mailbox too — same absolute-path guarantee.
    paths = SessionPaths(tmp_path / "main", "demo")
    paths.ensure()
    events = EventLog(paths.events_path)
    _seed_handoff(paths, "w")
    sub = AddLaneSub()
    actions.execute(_add(brief="resume"), sub, events, EngineConfig(), paths=paths)
    _, payload = sub.dispatched[0]
    coord_dir = _coord_dir_from_instruction(payload)
    assert coord_dir.is_absolute()
    assert coord_dir == paths.mailbox_dir
    assert "subject: re:handoff:w" in payload  # ack semantics preserved


def test_mailbox_hint_falls_back_to_relative_only_without_engine_root():
    # paths=None is the legacy cli path (no engine root threaded): byte-identical
    # to pre-F15. With a real engine root, the hint is the absolute mailbox path.
    assert actions._mailbox_message_hint(None, "web").startswith(".loop/messages/")
    paths = SessionPaths(Path("/srv/leo-main"), "demo")
    hint = actions._mailbox_message_hint(paths, "web")
    assert hint.startswith("/srv/leo-main/.loop/messages/")
    assert Path(hint).is_absolute()


# ── F14 (T0040): worktree dispatches embed the task spec inline ──────────────
# A worktree lane is cut from main HEAD, so a tasks/Txxxx-….md spec that is
# uncommitted in main or seeded after the cut is ABSENT from its working tree.
# When the engine dispatches to a worktree lane, the spec body must ride inline
# in the payload; a shared (non-worktree) lane's dispatch stays byte-identical.

_SPEC_SENTINEL = "embedded-spec-sentinel-line"


def _seed_task(paths, task_id="T0040", slug="f14-demo"):
    paths.tasks_dir.mkdir(parents=True, exist_ok=True)
    f = paths.tasks_dir / f"{task_id}-{slug}.md"
    f.write_text(
        f"---\nid: {task_id}\ntitle: t\nstatus: open\n---\n\n## Objective\n{_SPEC_SENTINEL}\n",
        encoding="utf-8",
    )
    return f


def _seed_branch(paths, window, branch="loop/demo/web"):
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    paths.state_file.write_text(
        json.dumps({"loops": {window: {"branch": branch}}}), encoding="utf-8"
    )


_BRIEF_WITH_REF = "Do T0040. Read tasks/T0040-f14-demo.md for the full spec."


def test_worktree_add_lane_embeds_task_spec_inline(tmp_path):
    paths, events = _env(tmp_path)
    _seed_task(paths)
    sub = AddLaneSub()
    action = {**_add(brief=_BRIEF_WITH_REF), "worktree": True}  # explicit T0025 opt-in
    actions.execute(action, sub, events, EngineConfig(), paths=paths)
    assert sub.calls[0]["worktree"] is True
    _, payload = sub.dispatched[0]
    assert _SPEC_SENTINEL in payload  # (a) spec body rides inline
    assert "embedded inline" in payload  # the self-contained marker
    assert payload.startswith(_BRIEF_WITH_REF)  # original brief preserved, spec appended


def test_nonworktree_add_lane_is_byte_identical(tmp_path):
    # (b)+(c): a shared lane add is byte-for-byte the original brief — no embed.
    paths, events = _env(tmp_path)
    _seed_task(paths)
    sub = AddLaneSub()
    actions.execute(
        _add(brief=_BRIEF_WITH_REF), sub, events, EngineConfig(), paths=paths, code_writers=0
    )
    assert sub.calls[0]["worktree"] is False
    assert sub.dispatched == [("w", _BRIEF_WITH_REF)]  # byte-identical, spec NOT embedded


def test_worktree_dispatch_kind_embeds_task_spec_inline(tmp_path):
    # The recurring task dispatch to an EXISTING worktree lane (ledger branch).
    paths, events = _env(tmp_path)
    _seed_task(paths)
    _seed_branch(paths, "web")
    sub = AddLaneSub()
    action = {"kind": "dispatch", "lane": "web", "payload": _BRIEF_WITH_REF, "rationale": "r"}
    actions.execute(action, sub, events, EngineConfig(), paths=paths)
    _, payload = sub.dispatched[0]
    assert _SPEC_SENTINEL in payload
    assert payload.startswith(_BRIEF_WITH_REF)


def test_nonworktree_dispatch_kind_is_byte_identical(tmp_path):
    # No ledger branch => shared lane => byte-identical payload (regression guard).
    paths, events = _env(tmp_path)
    _seed_task(paths)
    sub = AddLaneSub()
    action = {"kind": "dispatch", "lane": "web", "payload": _BRIEF_WITH_REF, "rationale": "r"}
    actions.execute(action, sub, events, EngineConfig(), paths=paths)
    assert sub.dispatched == [("web", _BRIEF_WITH_REF)]  # unchanged


def test_embed_is_noop_when_spec_file_absent(tmp_path):
    # Worktree lane but the referenced spec isn't in the engine's tasks/ => the
    # payload is left intact (graceful, never raises).
    paths, events = _env(tmp_path)
    _seed_branch(paths, "web")  # worktree, but no _seed_task
    sub = AddLaneSub()
    action = {"kind": "dispatch", "lane": "web", "payload": _BRIEF_WITH_REF, "rationale": "r"}
    actions.execute(action, sub, events, EngineConfig(), paths=paths)
    assert sub.dispatched == [("web", _BRIEF_WITH_REF)]  # no file => unchanged


def test_embed_noop_when_payload_has_no_task_reference(tmp_path):
    # Worktree lane, spec present, but the payload names no tasks/ path => no embed.
    paths, events = _env(tmp_path)
    _seed_task(paths)
    _seed_branch(paths, "web")
    sub = AddLaneSub()
    action = {"kind": "dispatch", "lane": "web", "payload": "just do the thing", "rationale": "r"}
    actions.execute(action, sub, events, EngineConfig(), paths=paths)
    assert sub.dispatched == [("web", "just do the thing")]


# ── T0048: async verify action ──────────────────────────────────────────────


def test_verify_action_spawns_detached_runner_and_records_marker(tmp_path):
    paths, events = _env(tmp_path)
    _seed_branch(paths, "web", branch="loop/demo/web")
    sub = AddLaneSub(branch_heads=["sha-a"])
    action = {"kind": "verify", "lane": "web", "rationale": "ready for review"}

    actions.execute(action, sub, events, EngineConfig(), paths=paths)

    assert len(sub.verifies) == 1
    out_path = sub.verifies[0]["out_path"]
    assert out_path.parent == paths.verify_dir
    assert out_path.name.startswith("web-")
    assert out_path.name.endswith(".json")
    assert out_path.name != "web.json"
    assert sub.verifies == [
        {
            "worktree": paths.project_root / ".loop" / "worktrees" / "demo" / "web",
            "base": "main",
            "tip": "sha-a",
            "out_path": out_path,
        }
    ]
    started = [e for e in events.tail(10) if e["event"] == "verify-started"]
    assert started
    assert started[-1]["window"] == "web"
    assert started[-1]["branch"] == "loop/demo/web"
    assert started[-1]["out_path"] == str(out_path)
    markers = actions.load_verify_markers(paths)
    assert len(markers) == 1
    assert markers[0]["window"] == "web"
    assert markers[0]["branch"] == "loop/demo/web"
    assert markers[0]["tip"] == "sha-a"
    assert markers[0]["tip_sha"] == "sha-a"
    assert markers[0]["out_path"] == str(out_path)
    assert markers[0]["pid"] == 4242


def test_verify_action_omits_tip_sha_when_branch_head_is_unresolved(tmp_path):
    paths, events = _env(tmp_path)
    _seed_branch(paths, "web", branch="loop/demo/web")
    sub = AddLaneSub(branch_heads=[None])

    actions.execute(
        {"kind": "verify", "lane": "web", "rationale": "ready"},
        sub,
        events,
        EngineConfig(),
        paths=paths,
    )

    assert sub.verifies[0]["tip"] == "loop/demo/web"
    marker = actions.load_verify_markers(paths)[0]
    assert marker["tip"] == "loop/demo/web"
    assert "tip_sha" not in marker


def test_verify_action_skips_when_lane_is_at_base(tmp_path):
    # Over-eager-verify guard: a lane sitting exactly at base (tip == main) has an
    # empty diff and nothing to merge, so verify must skip (never spawn) — else the
    # brain's spurious verify escalates an empty merge.
    paths, events = _env(tmp_path)
    _seed_branch(paths, "web", branch="loop/demo/web")
    sub = AddLaneSub(branch_heads=["same-sha", "same-sha"])  # tip == base

    actions.execute(
        {"kind": "verify", "lane": "web", "rationale": "ready"},
        sub,
        events,
        EngineConfig(),
        paths=paths,
    )

    assert sub.verifies == []
    assert actions.load_verify_markers(paths) == []
    skipped = [e for e in events.tail(10) if e["event"] == "verify-skip"]
    assert skipped and skipped[-1]["reason"] == "at-base"


def test_verify_action_spawns_when_branch_is_ahead_of_base(tmp_path):
    # The complement: a branch ahead of base (tip != main) has real work -> spawn.
    paths, events = _env(tmp_path)
    _seed_branch(paths, "web", branch="loop/demo/web")
    sub = AddLaneSub(branch_heads=["tip-sha", "base-sha"])  # tip != base

    actions.execute(
        {"kind": "verify", "lane": "web", "rationale": "ready"},
        sub,
        events,
        EngineConfig(),
        paths=paths,
    )

    assert len(sub.verifies) == 1
    assert sub.verifies[0]["tip"] == "tip-sha"
    assert actions.load_verify_markers(paths)[0]["tip_sha"] == "tip-sha"


def test_verify_action_skips_when_marker_exists(tmp_path):
    paths, events = _env(tmp_path)
    _seed_branch(paths, "web", branch="loop/demo/web")
    existing = {
        "window": "web",
        "branch": "loop/demo/web",
        "base": "main",
        "tip": "loop/demo/web",
        "out_path": str(paths.verify_dir / "web-old.json"),
        "pid": 123,
        "started_at": "2026-06-18T00:00:00Z",
    }
    actions.record_verify_marker(paths, existing)
    sub = AddLaneSub()

    actions.execute(
        {"kind": "verify", "lane": "web", "rationale": "again"},
        sub,
        events,
        EngineConfig(),
        paths=paths,
    )

    assert sub.verifies == []
    assert actions.load_verify_markers(paths) == [existing]
    skipped = [e for e in events.tail(10) if e["event"] == "verify-skip"]
    assert skipped
    assert skipped[-1]["window"] == "web"
    assert skipped[-1]["reason"] == "in-progress"


def test_verify_spawn_failure_marks_action_failed_without_marker(tmp_path):
    paths, events = _env(tmp_path)
    _seed_branch(paths, "web", branch="loop/demo/web")
    doc = {
        "id": "d-verify",
        "actions": [
            {
                "idx": 0,
                "kind": "verify",
                "lane": "web",
                "status": "approved",
                "rationale": "ready",
            }
        ],
    }

    updated = actions.execute_batch(doc, FailingVerifySub(), events, EngineConfig(), paths=paths)

    assert updated["actions"][0]["status"] == "failed"
    assert actions.load_verify_markers(paths) == []
    failed = [e for e in events.tail(10) if e["event"] == "action-failed"]
    assert failed
    assert failed[-1]["kind"] == "verify"
    assert failed[-1]["lane"] == "web"


# ── T0050: async build action ───────────────────────────────────────────────


def test_build_action_spawns_detached_runner_and_records_marker(tmp_path):
    paths, events = _env(tmp_path)
    _seed_branch(paths, "web", branch="loop/demo/web")
    sub = AddLaneSub(branch_heads=["sha-a"])
    action = {"kind": "build", "window": "web", "brief": "implement T0050", "rationale": "ready"}

    actions.execute(action, sub, events, EngineConfig(), paths=paths)

    assert sub.builds == [
        {
            "worktree": paths.project_root / ".loop" / "worktrees" / "demo" / "web",
            "brief": sub.builds[0]["brief"],
        }
    ]
    brief = sub.builds[0]["brief"]
    assert "implement T0050" in brief
    assert "CURRENT BRANCH in this worktree" in brief
    assert "DO NOT merge, push, reinstall" in brief
    started = [e for e in events.tail(10) if e["event"] == "build-started"]
    assert started
    assert started[-1]["window"] == "web"
    assert started[-1]["branch"] == "loop/demo/web"
    assert started[-1]["pre_build_sha"] == "sha-a"
    markers = actions.load_build_markers(paths)
    assert len(markers) == 1
    assert markers[0]["window"] == "web"
    assert markers[0]["branch"] == "loop/demo/web"
    assert markers[0]["pre_build_sha"] == "sha-a"
    assert markers[0]["pid"] == 4343


def test_build_action_skips_when_marker_exists(tmp_path):
    paths, events = _env(tmp_path)
    _seed_branch(paths, "web", branch="loop/demo/web")
    existing = {
        "window": "web",
        "branch": "loop/demo/web",
        "pre_build_sha": "sha-a",
        "pid": 123,
        "started_at": "2026-06-18T00:00:00Z",
    }
    actions.record_build_marker(paths, existing)
    sub = AddLaneSub()

    actions.execute(
        {"kind": "build", "window": "web", "brief": "again", "rationale": "again"},
        sub,
        events,
        EngineConfig(),
        paths=paths,
    )

    assert sub.builds == []
    assert actions.load_build_markers(paths) == [existing]
    skipped = [e for e in events.tail(10) if e["event"] == "build-skip"]
    assert skipped
    assert skipped[-1]["window"] == "web"
    assert skipped[-1]["reason"] == "in-progress"


def test_build_action_skips_when_worktree_dirty(tmp_path):
    # Decoupled eligibility can hint a build for a lane an interactive agent is
    # mid-task in; the spawn-boundary clean-tree guard must refuse it (no spawn,
    # no marker) so uncommitted work is never built over.
    paths, events = _env(tmp_path)
    _seed_branch(paths, "web", branch="loop/demo/web")
    sub = AddLaneSub(branch_heads=["sha-a"], dirty=True)

    actions.execute(
        {"kind": "build", "window": "web", "brief": "implement", "rationale": "ready"},
        sub,
        events,
        EngineConfig(),
        paths=paths,
    )

    assert sub.builds == []
    assert actions.load_build_markers(paths) == []
    skipped = [e for e in events.tail(10) if e["event"] == "build-skip"]
    assert skipped and skipped[-1]["reason"] == "worktree-dirty"


def test_build_action_omits_pre_build_sha_when_branch_head_is_unresolved(tmp_path):
    # HIGH-2: an unresolved baseline at spawn must NOT be stored as None — else
    # surface_build_results reads a later non-None branch_head as an advance and
    # emits a false build-done with zero commits. Mirror the verify tip_sha guard.
    paths, events = _env(tmp_path)
    _seed_branch(paths, "web", branch="loop/demo/web")
    sub = AddLaneSub(branch_heads=[None])
    action = {"kind": "build", "window": "web", "brief": "implement", "rationale": "ready"}

    actions.execute(action, sub, events, EngineConfig(), paths=paths)

    assert len(sub.builds) == 1
    marker = actions.load_build_markers(paths)[0]
    assert "pre_build_sha" not in marker
    started = [e for e in events.tail(10) if e["event"] == "build-started"]
    assert started and started[-1]["pre_build_sha"] is None


def test_build_spawn_failure_marks_action_failed_without_marker(tmp_path):
    paths, events = _env(tmp_path)
    _seed_branch(paths, "web", branch="loop/demo/web")
    doc = {
        "id": "d-build",
        "actions": [
            {
                "idx": 0,
                "kind": "build",
                "window": "web",
                "brief": "implement",
                "status": "approved",
                "rationale": "ready",
            }
        ],
    }

    updated = actions.execute_batch(doc, FailingBuildSub(), events, EngineConfig(), paths=paths)

    assert updated["actions"][0]["status"] == "failed"
    assert actions.load_build_markers(paths) == []
    failed = [e for e in events.tail(10) if e["event"] == "action-failed"]
    assert failed
    assert failed[-1]["kind"] == "build"
    assert failed[-1]["lane"] == "web"
