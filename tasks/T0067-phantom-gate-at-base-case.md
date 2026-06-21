---
id: T0067
title: "Extend the phantom-escalation gate to the AT-BASE (0-ahead) case"
status: review
loop: code
depends_on: []
scope: src/loop_orchestrator/engine/loop.py verify-drive merge eligibility, plus tests/test_loop_integration.py.
jira:
---

# T0067 - phantom gate: cover the at-base lane

## Objective
T0059 suppressed phantom merge escalations for ahead-but-stale branches, but the
at-base case still needed a hard gate: after a merge and reset, a lane can sit at
main with a cached `verify-passed` outcome and no commits left to merge. That
cached pass must not surface as mergeable.

## Context you need
The engine's verify-drive prompt is assembled in
`src/loop_orchestrator/engine/loop.py`. Cached verify results come from the
event log, and `verify-passed` is the only verify outcome that can drive a brain
`escalate` asking an operator to merge a branch. A branch that is already at
main, or whose verified tip is already contained by main, has no merge work left.

## Deliverables
- Suppress merge escalation eligibility when a lane branch is 0-ahead of main.
- Emit a `drive-already-landed` event when a cached `verify-passed` is discarded
  because the lane is already landed.
- Ignore cached `verify-passed` outcomes whose recorded tip is an ancestor of
  main.
- Preserve the genuinely ahead, current, verified branch path so it still
  surfaces exactly one merge-ready verify outcome.

## Acceptance criteria
- An at-base lane with a cached `verify-passed` produces no merge escalation and
  emits `drive-already-landed`.
- A cached `verify-passed` whose recorded tip is already contained by main is not
  surfaced to the brain.
- A current branch that is ahead of main and verified still appears once in
  recent verify outcomes.

## Verification
- Run the focused integration tests for verify-drive already-landed behavior.
- Run `make check-all`.

## Out of scope
- Do not change merge execution, approval policy, deployment behavior, Jira sync,
  or lane scheduling beyond verify-drive merge eligibility.
