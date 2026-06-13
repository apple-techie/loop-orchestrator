---
id: T0017
title: F1: validate dispatch/steer targets are agent lanes
status: done
accepted: 2026-06-13
depends_on: []
scope: src/loop_orchestrator/ + lib/harness-registry.sh + loop-*.sh + tests ONLY; ADDITIVE; the single-word lane-status output is FROZEN and every existing case must still pass; frozen probe/field/oneshot verbs untouched; do NOT reinstall or touch running daemons; no git push
loop: harness-governance
---

# T0017 — F1: validate dispatch/steer targets are agent lanes

## Objective
Phase 2 (per-harness readiness/health contract — the linchpin) of multi-harness
governance. Spec: docs/plans/harness-governance.md (Phase 2) + the findings in
docs/board/harness-governance.md.
F1 fix (surfaced + recurred live in the dogfood). classify must reject agent work sent to a non-agent lane: a dispatch/steer with mode=text (an agent brief) whose TARGET lane runs a non-agent harness (shell/mprocs, i.e. empty oneshot_template OR harness=shell) is DESTRUCTIVE (gates) — and BLOCKED if the lane is unknown to the roster. mode=command to a shell lane stays allowed (that's how you run shell commands). Read the target lane's harness from the per-cycle roster/lane snapshot the loop already resolves. Pure gate, additive, roster=None = no change.

## Context you need
Files: src/loop_orchestrator/engine/gate.py; engine/loop.py (resolve target lane harness into the roster snapshot); tests/test_gate.py.
This is the loop-orchestrator repo building its own Phase 2, on
feature/harness-governance in an isolated worktree — edits here do NOT reach
the running daemons (they resolve scripts from the main checkout) until a human
reinstalls. THE single-word status contract is FROZEN: working | awaiting-
approval | idle | errored | unknown. Every existing lane-status special case
must keep classifying identically — this is the hardest constraint.

## Deliverables
- The change above, ADDITIVE (empty/None defaults = today). Tests for the new
  surface AND a regression check that existing behavior is unchanged.
- Before/after note appended to ops-wiki/loops/harness-governance.md.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (393+ pass, no regressions). Existing
tests pass UNCHANGED. Commit the batch per the commit policy (cite T0017). Do NOT
reinstall; do NOT push.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev ruff check src tests && uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
Phases 3-5 (deck, worktree isolation, lane handoff) and F2/F4. If a change would
alter the FROZEN status output or a frozen verb non-additively, or require a
reinstall — STOP and escalate.
