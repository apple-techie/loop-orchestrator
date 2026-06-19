"""DECISION CONTRACT v1 — parse + validate the brain's fenced decision block.

Pure module: no substrate, no IO. The brain's reply must contain a fenced
block with info-string ``decision`` whose body is YAML
``{version: 1, critique: <str>, actions: [...]}``. The LAST such fence wins.

Error messages from :class:`DecisionParseError` / :class:`DecisionValidationError`
are written for verbatim inclusion in a corrective re-prompt to the brain.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ClassVar

import yaml

MAX_ACTIONS = 8
MAX_TEXT_CHARS = 16384

_FENCE_RE = re.compile(r"```decision[ \t]*\n(.*?)```", re.DOTALL)
_WINDOW_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]+$")
# A model id reaches a shell when add_lane spawns a harness, so it is locked to
# a strict safe-token charset: letters, digits, and the punctuation real model
# ids use (claude-fable-5, gpt-5.5, anthropic/claude-3.5, o1-preview). The
# anchored full match excludes every shell metacharacter — whitespace, quotes,
# ; | & $ ` ( ) < > — killing model-field command injection at parse time.
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]+$")


class DecisionError(Exception):
    """Base for decision contract failures."""


class DecisionParseError(DecisionError):
    """No ``decision`` fence, or its body is not parseable YAML mapping."""


class DecisionValidationError(DecisionError):
    """The parsed decision violates contract v1 schema or lane constraints."""


def _need_str(field: str, value: object, limit: int | None = None) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DecisionValidationError(f"field '{field}' must be a non-empty string")
    if limit is not None and len(value) > limit:
        raise DecisionValidationError(
            f"field '{field}' is {len(value)} chars; the limit is {limit}"
        )


def _need_bool(field: str, value: object) -> None:
    if not isinstance(value, bool):
        raise DecisionValidationError(f"field '{field}' must be a boolean (true/false)")


def _need_int(field: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DecisionValidationError(f"field '{field}' must be an integer")


def _no_coord(field: str, value: str) -> None:
    if value == "coord":
        raise DecisionValidationError(
            f"field '{field}' targets 'coord' — actions must never target the coord lane"
        )


@dataclass(frozen=True)
class DispatchAction:
    lane: str
    payload: str
    rationale: str
    mode: str = "text"
    wait_ready: bool = False
    kind: ClassVar[str] = "dispatch"

    def __post_init__(self):
        _need_str("lane", self.lane)
        _need_str("payload", self.payload, limit=MAX_TEXT_CHARS)
        _need_str("rationale", self.rationale)
        if self.mode not in ("text", "command"):
            raise DecisionValidationError("field 'mode' must be 'text' or 'command'")
        _need_bool("wait_ready", self.wait_ready)
        _no_coord("lane", self.lane)


@dataclass(frozen=True)
class AddLaneAction:
    window: str
    brief: str
    rationale: str
    harness: str | None = None
    cmd: str | None = None
    model: str | None = None
    role: str | None = None
    auto_approve: bool = False
    kind: ClassVar[str] = "add_lane"

    def __post_init__(self):
        _need_str("window", self.window)
        _need_str("brief", self.brief, limit=MAX_TEXT_CHARS)
        _need_str("rationale", self.rationale)
        if not _WINDOW_RE.match(self.window):
            raise DecisionValidationError(
                f"field 'window' {self.window!r} must match ^[A-Za-z][A-Za-z0-9_-]+$"
            )
        if self.harness is None and self.cmd is None:
            raise DecisionValidationError("add_lane requires 'harness' or 'cmd'")
        for name in ("harness", "cmd", "model", "role"):
            value = getattr(self, name)
            if value is not None:
                _need_str(name, value)
        # harness is registry-validated downstream (unknown harnesses are
        # rejected before any process spawns); model is free-form, so it is
        # pinned here to a shell-safe charset to kill command injection at parse
        # time (the model id is interpolated into the harness command line).
        if self.model is not None and not _MODEL_RE.match(self.model):
            raise DecisionValidationError(
                f"field 'model' {self.model!r} must match ^[A-Za-z0-9._:/-]+$ "
                "(no shell metacharacters)"
            )
        _need_bool("auto_approve", self.auto_approve)
        _no_coord("window", self.window)


@dataclass(frozen=True)
class DropLaneAction:
    window: str
    rationale: str
    kind: ClassVar[str] = "drop_lane"

    def __post_init__(self):
        _need_str("window", self.window)
        _need_str("rationale", self.rationale)
        _no_coord("window", self.window)


@dataclass(frozen=True)
class SteerAction:
    lane: str
    payload: str
    rationale: str
    mode: str = "text"
    interrupt: bool = False
    wait_for_idle: bool = False
    expects_reply: bool = False
    reply_timeout_s: int = 1800
    kind: ClassVar[str] = "steer"

    def __post_init__(self):
        _need_str("lane", self.lane)
        _need_str("payload", self.payload, limit=MAX_TEXT_CHARS)
        _need_str("rationale", self.rationale)
        if self.mode not in ("text", "command"):
            raise DecisionValidationError("field 'mode' must be 'text' or 'command'")
        _need_bool("interrupt", self.interrupt)
        _need_bool("wait_for_idle", self.wait_for_idle)
        _need_bool("expects_reply", self.expects_reply)
        _need_int("reply_timeout_s", self.reply_timeout_s)
        _no_coord("lane", self.lane)


@dataclass(frozen=True)
class StopAction:
    rationale: str
    kind: ClassVar[str] = "stop"

    def __post_init__(self):
        _need_str("rationale", self.rationale)


@dataclass(frozen=True)
class EscalateAction:
    summary: str
    rationale: str
    kind: ClassVar[str] = "escalate"

    def __post_init__(self):
        _need_str("summary", self.summary)
        _need_str("rationale", self.rationale)


@dataclass(frozen=True)
class VerifyAction:
    lane: str
    rationale: str
    kind: ClassVar[str] = "verify"

    def __post_init__(self):
        _need_str("lane", self.lane)
        _need_str("rationale", self.rationale)
        if not _WINDOW_RE.match(self.lane):
            raise DecisionValidationError(
                f"field 'lane' {self.lane!r} must match ^[A-Za-z][A-Za-z0-9_-]+$"
            )
        _no_coord("lane", self.lane)


@dataclass(frozen=True)
class BuildAction:
    window: str
    brief: str
    rationale: str
    kind: ClassVar[str] = "build"

    def __post_init__(self):
        _need_str("window", self.window)
        _need_str("brief", self.brief, limit=MAX_TEXT_CHARS)
        _need_str("rationale", self.rationale)
        if not _WINDOW_RE.match(self.window):
            raise DecisionValidationError(
                f"field 'window' {self.window!r} must match ^[A-Za-z][A-Za-z0-9_-]+$"
            )
        _no_coord("window", self.window)


Action = (
    DispatchAction
    | AddLaneAction
    | DropLaneAction
    | SteerAction
    | StopAction
    | EscalateAction
    | VerifyAction
    | BuildAction
)

_ACTION_TYPES: dict[str, type] = {
    "dispatch": DispatchAction,
    "add_lane": AddLaneAction,
    "drop_lane": DropLaneAction,
    "steer": SteerAction,
    "stop": StopAction,
    "escalate": EscalateAction,
    "verify": VerifyAction,
    "build": BuildAction,
}


@dataclass(frozen=True)
class Decision:
    id: str
    critique: str
    actions: list[Action]
    raw_text: str


def parse(text: str) -> dict:
    """Extract the LAST ```decision fence and yaml.safe_load its body.

    Raises DecisionParseError when there is no fence, the YAML is invalid, or
    the body is not a mapping.
    """
    fences = _FENCE_RE.findall(text)
    if not fences:
        raise DecisionParseError(
            "no ```decision fence found in the reply — respond with exactly one "
            "fenced block whose info-string is 'decision' and whose body is YAML "
            "{version: 1, critique: ..., actions: [...]}"
        )
    body = fences[-1]
    try:
        raw = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise DecisionParseError(f"the decision fence body is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise DecisionParseError(
            "the decision fence body must be a YAML mapping with keys version, critique, actions"
        )
    return raw


def _build_action(idx: int, raw: object) -> Action:
    if not isinstance(raw, dict):
        raise DecisionValidationError(f"action {idx}: must be a mapping, got {type(raw).__name__}")
    kind = raw.get("kind")
    cls = _ACTION_TYPES.get(kind)  # type: ignore[arg-type]
    if cls is None:
        raise DecisionValidationError(
            f"action {idx}: unknown kind {kind!r}; expected one of "
            f"{', '.join(sorted(_ACTION_TYPES))}"
        )
    field_names = {f.name for f in dataclasses.fields(cls)}
    required = {
        f.name
        for f in dataclasses.fields(cls)
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
    }
    missing = sorted(required - raw.keys())
    if missing:
        raise DecisionValidationError(
            f"action {idx} ({kind}): missing required field(s): {', '.join(missing)}"
        )
    kwargs = {k: v for k, v in raw.items() if k != "kind" and k in field_names}
    try:
        return cls(**kwargs)
    except DecisionValidationError as exc:
        raise DecisionValidationError(f"action {idx} ({kind}): {exc}") from exc


def validate(raw: dict, live_lanes: set[str], raw_text: str = "") -> Decision:
    """Validate a parsed decision mapping against contract v1 and live lanes."""
    version = raw.get("version")
    if version != 1:
        raise DecisionValidationError(
            f"unsupported decision version {version!r}; this engine speaks version 1"
        )
    critique = raw.get("critique")
    if not isinstance(critique, str) or not critique.strip():
        raise DecisionValidationError("'critique' must be a non-empty string")
    raw_actions = raw.get("actions")
    if not isinstance(raw_actions, list):
        raise DecisionValidationError("'actions' must be a list (it may be empty)")
    if len(raw_actions) > MAX_ACTIONS:
        raise DecisionValidationError(
            f"{len(raw_actions)} actions given; the limit is {MAX_ACTIONS} per decision"
        )

    actions: list[Action] = []
    for idx, raw_action in enumerate(raw_actions):
        action = _build_action(idx, raw_action)
        live = ", ".join(sorted(live_lanes)) or "(none)"
        if isinstance(action, DispatchAction | SteerAction | VerifyAction) and (
            action.lane not in live_lanes
        ):
            raise DecisionValidationError(
                f"action {idx} ({action.kind}): unknown lane {action.lane!r}; live lanes: {live}"
            )
        if isinstance(action, BuildAction) and action.window not in live_lanes:
            raise DecisionValidationError(
                f"action {idx} (build): unknown window {action.window!r}; live lanes: {live}"
            )
        if isinstance(action, DropLaneAction) and action.window not in live_lanes:
            raise DecisionValidationError(
                f"action {idx} (drop_lane): unknown window {action.window!r}; live lanes: {live}"
            )
        if isinstance(action, AddLaneAction) and action.window in live_lanes:
            raise DecisionValidationError(
                f"action {idx} (add_lane): window {action.window!r} is already a live "
                "lane — pick an unused window name"
            )
        actions.append(action)

    decision_id = datetime.now(timezone.utc).strftime("d-%Y%m%d-%H%M%S")
    return Decision(id=decision_id, critique=critique, actions=actions, raw_text=raw_text)


def parse_and_validate(text: str, live_lanes: set[str]) -> Decision:
    return validate(parse(text), live_lanes, raw_text=text)


# ── selftest fixtures (python -m loop_orchestrator.engine.decision selftest) ──

_GOOD_FIXTURE = """\
preamble the parser must skip

```decision
version: 1
critique: stale fence above, real one below
actions: []
```

```decision
version: 1
critique: web lane claims green but CI state is only inferred from its summary
actions:
  - kind: dispatch
    lane: web
    payload: run the full test suite and paste the summary
    rationale: falsify the inferred-green claim fastest
  - kind: steer
    lane: docs
    payload: stop compiling; ingest the two pending mailbox messages first
    interrupt: false
    rationale: mailbox backlog blocks the next checkpoint
  - kind: add_lane
    window: lint-1
    harness: claude
    brief: run ruff over src and report findings only
    rationale: lint drift is unverified
  - kind: drop_lane
    window: web
    rationale: lane finished its brief
  - kind: stop
    rationale: nothing else is actionable this cycle
  - kind: escalate
    summary: ADR 0007 needs human acceptance
    rationale: acceptance is human-only
```
"""

_BAD_FIXTURES: list[tuple[str, type[DecisionError]]] = [
    ("no fence at all", DecisionParseError),
    ("```decision\nversion: 2\ncritique: x\nactions: []\n```", DecisionValidationError),
    (
        "```decision\nversion: 1\ncritique: x\nactions:\n"
        "  - {kind: dispatch, lane: coord, payload: p, rationale: r}\n```",
        DecisionValidationError,
    ),
    (
        "```decision\nversion: 1\ncritique: x\nactions:\n"
        "  - {kind: dispatch, lane: web, payload: p}\n```",
        DecisionValidationError,
    ),
    (
        "```decision\nversion: 1\ncritique: x\nactions:\n"
        "  - {kind: add_lane, window: new-1, brief: b, rationale: r}\n```",
        DecisionValidationError,
    ),
]


def _selftest() -> int:
    live = {"web", "docs"}
    failures: list[str] = []
    try:
        decision = parse_and_validate(_GOOD_FIXTURE, live)
        if len(decision.actions) != 6 or not re.match(r"^d-\d{8}-\d{6}$", decision.id):
            failures.append("good fixture produced an unexpected Decision")
    except DecisionError as exc:
        failures.append(f"good fixture rejected: {exc}")
    for text, expected in _BAD_FIXTURES:
        try:
            parse_and_validate(text, live)
            failures.append(f"bad fixture accepted (wanted {expected.__name__}): {text[:60]!r}")
        except expected:
            pass
        except DecisionError as exc:
            failures.append(f"bad fixture raised {type(exc).__name__}, wanted {expected.__name__}")
    total = 1 + len(_BAD_FIXTURES)
    if failures:
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(f"decision selftest: {len(failures)}/{total} FAILED")
        return 1
    print(f"decision selftest: {total}/{total} ok")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "selftest":
        raise SystemExit(_selftest())
    print("usage: python -m loop_orchestrator.engine.decision selftest", file=sys.stderr)
    raise SystemExit(2)
