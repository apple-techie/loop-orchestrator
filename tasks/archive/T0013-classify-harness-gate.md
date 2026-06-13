---
id: T0013
title: Add classify_harness to the gate
status: done
depends_on: [T0010, T0012]
scope: src/loop_orchestrator/ + lib/harness-registry.sh + tests ONLY; ADDITIVE per CONTRACT.md; never break the FROZEN single-word status contract or the frozen probe/field/oneshot verbs; do NOT reinstall the tool or touch the running daemons; no git push
loop: harness-governance
---

# T0013 — Add classify_harness to the gate

## Objective
Phase 1 of the multi-harness agent governance build (DOGFOOD: loop-orchestrator
building its own governance). Full spec: docs/plans/harness-governance.md.
Add a PURE classify_harness(action, config, roster=None) pass ABOVE the existing SAFE/DESTRUCTIVE/BLOCKED logic for AddLaneAction, per the override-semantics table in plan A.2: allowlist->safe; denied/unknown-to-roster->BLOCKED; not-allowed-but-role-default->rewrite action.harness + emit a governance event; over cost-ceiling / autonomy-cap / auto_approve-over-cap / roster missing|unauthenticated->DESTRUCTIVE; high-drift + unattended + high-risk->DESTRUCTIVE. roster=None default = NO behavior change so existing tests pass. Read action.harness off the action (it exists), keep the gate pure (no IO).

## Context you need
Files: src/loop_orchestrator/engine/gate.py; src/loop_orchestrator/engine/loop.py (resolve a per-cycle roster snapshot and thread it in); tests/test_gate.py.
The plan (docs/plans/harness-governance.md) is the authoritative spec — read the
cited sections. This is the loop-orchestrator repo building its own feature, on
the feature/harness-governance branch in an isolated worktree, so edits here do
NOT affect the running daemons (which resolve scripts from the main checkout).

## Deliverables
- The change above, ADDITIVE and backward-compatible (empty/None defaults =
  today's behavior). Tests for the new surface.
- Before/after note appended to ops-wiki/loops/harness-governance.md.

## Acceptance criteria
The full gate is green: `make check` AND, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` AND
`uv run --no-sync --group dev ruff format --check src tests` AND
`uv run --no-sync --group dev pytest -q` (all pass, no regressions).
Existing tests pass UNCHANGED (additive). Commit the batch per the commit policy
(conventional message citing T0013). Do NOT reinstall; do NOT push.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev ruff check src tests && uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
Phases 2-5 of the plan (readiness/health contract, deck, worktree isolation,
handoff). If a change would touch a FROZEN surface (status word, probe/field/
oneshot verbs, CONTRACT.md non-additively) or require a reinstall — STOP and
escalate.

## Notes — coord ruling (batch-2 judgment call 1, empty-policy semantics)
RULING: `HarnessPolicy()` defaults are PASS-THROUGH by design — all add_lane
harness choices classify SAFE (including the unknown-to-roster / typo case).
Typo-blocking is OPT-IN: it requires an explicit non-default policy field
(e.g. a written `deny`/`allow`/`role_defaults`). The behavior built in 99c6596
is correct as-is; the proposed one-line change to lift the unknown-to-roster
block out of the empty-policy guard was NOT applied. This preserves the
plan A.2 row 1 invariant ("or no policy → SAFE, unchanged") and keeps the
empty policy byte-identical to today's behavior. Accepted by coord; T0013 done.
