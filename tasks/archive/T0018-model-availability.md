---
id: T0018
title: F3: model-unavailable failure kind + per-harness model failover
status: done
accepted: 2026-06-13
depends_on: []
scope: src/loop_orchestrator/ + lib/harness-registry.sh + loop-*.sh + tests ONLY; ADDITIVE; the single-word lane-status output is FROZEN and every existing case must still pass; frozen probe/field/oneshot verbs untouched; do NOT reinstall or touch running daemons; no git push
loop: harness-governance
---

# T0018 — F3: model-unavailable failure kind + per-harness model failover

## Objective
Phase 2 (per-harness readiness/health contract — the linchpin) of multi-harness
governance. Spec: docs/plans/harness-governance.md (Phase 2) + the findings in
docs/board/harness-governance.md.
F3 fix (the live Fable-5 outage). classify_failure gains a `model-unavailable` kind: match an unavailable/'currently unavailable' notice in stderr AND stdout (the notice prints to stdout, which is why it was mislabeled 'exit'). On model-unavailable, arm a backoff or escalate (NOT silent retries-into-the-wall). Add a per-harness `model_failover` registry field (a fallback model id) so failover is a declared config, not an env hack. Document that availability is per-(harness, model).

## Context you need
Files: src/loop_orchestrator/engine/brain.py (classify_failure); lib/harness-registry.sh (model_failover field); engine/watch.py (backoff); tests/test_brain.py.
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
tests pass UNCHANGED. Commit the batch per the commit policy (cite T0018). Do NOT
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
