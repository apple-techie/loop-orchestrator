"""DECISION CONTRACT v1 — parser + validator tests (pure, no substrate)."""

from __future__ import annotations

import re

import pytest

from loop_orchestrator.engine.decision import (
    AddLaneAction,
    Decision,
    DecisionParseError,
    DecisionValidationError,
    DispatchAction,
    DropLaneAction,
    EscalateAction,
    SteerAction,
    StopAction,
    parse,
    parse_and_validate,
    validate,
)

LIVE = {"web", "docs", "infra"}


def fence(body: str) -> str:
    return f"```decision\n{body}\n```"


GOOD = fence(
    """\
version: 1
critique: web's green claim is inferred from its own summary, not from CI output
actions:
  - kind: dispatch
    lane: web
    payload: run the full test suite and paste the summary verbatim
    rationale: falsify the inferred-green claim
  - kind: steer
    lane: docs
    payload: ingest the pending mailbox messages before compiling
    rationale: backlog blocks the next checkpoint
  - kind: add_lane
    window: lint-1
    harness: claude
    brief: run ruff over src and report findings only
    rationale: lint state is unverified
  - kind: drop_lane
    window: infra
    rationale: lane finished its brief
  - kind: stop
    rationale: nothing else actionable
  - kind: escalate
    summary: ADR 0007 needs acceptance
    rationale: acceptance is human-only
"""
)


def test_good_fixture_parses_with_defaults_and_id_shape():
    decision = parse_and_validate(GOOD, LIVE)
    assert isinstance(decision, Decision)
    assert re.fullmatch(r"d-\d{8}-\d{6}", decision.id)
    assert decision.raw_text == GOOD
    assert decision.critique.startswith("web's green claim")
    assert [type(a) for a in decision.actions] == [
        DispatchAction,
        SteerAction,
        AddLaneAction,
        DropLaneAction,
        StopAction,
        EscalateAction,
    ]
    dispatch = decision.actions[0]
    assert (dispatch.mode, dispatch.wait_ready) == ("text", False)
    steer = decision.actions[1]
    assert (steer.interrupt, steer.wait_for_idle, steer.expects_reply) == (False, False, False)
    assert steer.reply_timeout_s == 1800
    add = decision.actions[2]
    assert (add.cmd, add.model, add.role, add.auto_approve) == (None, None, None, False)


def test_last_fence_wins():
    stale = fence("version: 1\ncritique: stale\nactions: []")
    text = f"{stale}\nnarration between fences\n{GOOD}"
    decision = parse_and_validate(text, LIVE)
    assert len(decision.actions) == 6
    assert parse(text)["critique"] != "stale"


def test_missing_fence_raises_parse_error():
    with pytest.raises(DecisionParseError):
        parse("no fence here, just prose")
    with pytest.raises(DecisionParseError):
        parse("```yaml\nversion: 1\n```")


def test_unparseable_yaml_raises_parse_error():
    with pytest.raises(DecisionParseError):
        parse(fence("critique: [unclosed"))


def test_non_mapping_body_raises_parse_error():
    with pytest.raises(DecisionParseError):
        parse(fence("just a string"))


def test_version_2_rejected():
    raw = parse(fence("version: 2\ncritique: x\nactions: []"))
    with pytest.raises(DecisionValidationError, match="version"):
        validate(raw, LIVE)


def test_more_than_8_actions_rejected():
    actions = "\n".join(f"  - {{kind: stop, rationale: r{i}}}" for i in range(9))
    raw = parse(fence(f"version: 1\ncritique: x\nactions:\n{actions}"))
    with pytest.raises(DecisionValidationError, match="limit is 8"):
        validate(raw, LIVE)


def test_oversized_payload_rejected():
    big = "x" * 16385
    raw = parse(
        fence(
            "version: 1\ncritique: x\nactions:\n"
            f"  - {{kind: dispatch, lane: web, payload: {big}, rationale: r}}"
        )
    )
    with pytest.raises(DecisionValidationError, match="16384"):
        validate(raw, LIVE)


def test_oversized_brief_rejected():
    big = "x" * 16385
    raw = parse(
        fence(
            "version: 1\ncritique: x\nactions:\n"
            f"  - {{kind: add_lane, window: new-1, harness: claude, brief: {big}, rationale: r}}"
        )
    )
    with pytest.raises(DecisionValidationError, match="16384"):
        validate(raw, LIVE)


@pytest.mark.parametrize(
    "action_yaml",
    [
        "{kind: dispatch, lane: ghost, payload: p, rationale: r}",
        "{kind: steer, lane: ghost, payload: p, rationale: r}",
        "{kind: drop_lane, window: ghost, rationale: r}",
    ],
)
def test_unknown_lane_rejected(action_yaml):
    raw = parse(fence(f"version: 1\ncritique: x\nactions:\n  - {action_yaml}"))
    with pytest.raises(DecisionValidationError, match="ghost"):
        validate(raw, LIVE)


def test_add_lane_to_existing_window_rejected():
    raw = parse(
        fence(
            "version: 1\ncritique: x\nactions:\n"
            "  - {kind: add_lane, window: web, harness: claude, brief: b, rationale: r}"
        )
    )
    with pytest.raises(DecisionValidationError, match="already a live"):
        validate(raw, LIVE)


def test_add_lane_bad_window_name_rejected():
    raw = parse(
        fence(
            "version: 1\ncritique: x\nactions:\n"
            "  - {kind: add_lane, window: 1bad, harness: claude, brief: b, rationale: r}"
        )
    )
    with pytest.raises(DecisionValidationError, match="window"):
        validate(raw, LIVE)


@pytest.mark.parametrize(
    "action_yaml",
    [
        "{kind: dispatch, lane: coord, payload: p, rationale: r}",
        "{kind: steer, lane: coord, payload: p, rationale: r}",
        "{kind: drop_lane, window: coord, rationale: r}",
        "{kind: add_lane, window: coord, harness: claude, brief: b, rationale: r}",
    ],
)
def test_coord_targeting_rejected_for_every_targeting_kind(action_yaml):
    raw = parse(fence(f"version: 1\ncritique: x\nactions:\n  - {action_yaml}"))
    with pytest.raises(DecisionValidationError, match="coord"):
        validate(raw, LIVE | {"coord"})


@pytest.mark.parametrize(
    "action_yaml",
    [
        "{kind: dispatch, lane: web, payload: p}",
        "{kind: add_lane, window: new-1, harness: claude, brief: b}",
        "{kind: drop_lane, window: web}",
        "{kind: steer, lane: web, payload: p}",
        "{kind: stop}",
        "{kind: escalate, summary: s}",
    ],
)
def test_missing_rationale_rejected_on_every_kind(action_yaml):
    raw = parse(fence(f"version: 1\ncritique: x\nactions:\n  - {action_yaml}"))
    with pytest.raises(DecisionValidationError, match="rationale"):
        validate(raw, LIVE)


def test_add_lane_without_harness_and_cmd_rejected():
    raw = parse(
        fence(
            "version: 1\ncritique: x\nactions:\n"
            "  - {kind: add_lane, window: new-1, brief: b, rationale: r}"
        )
    )
    with pytest.raises(DecisionValidationError, match="harness.*cmd|'harness' or 'cmd'"):
        validate(raw, LIVE)


def test_empty_critique_rejected():
    raw = parse(fence("version: 1\ncritique: ''\nactions: []"))
    with pytest.raises(DecisionValidationError, match="critique"):
        validate(raw, LIVE)


def test_unknown_action_kind_rejected():
    raw = parse(fence("version: 1\ncritique: x\nactions:\n  - {kind: launch, rationale: r}"))
    with pytest.raises(DecisionValidationError, match="launch"):
        validate(raw, LIVE)
