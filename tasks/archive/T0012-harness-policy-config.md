---
id: T0012
title: Add HarnessPolicy to the engine config
status: done
depends_on: []
scope: src/loop_orchestrator/ + lib/harness-registry.sh + tests ONLY; ADDITIVE per CONTRACT.md; never break the FROZEN single-word status contract or the frozen probe/field/oneshot verbs; do NOT reinstall the tool or touch the running daemons; no git push
loop: harness-governance
---

# T0012 — Add HarnessPolicy to the engine config

## Objective
Phase 1 of the multi-harness agent governance build (DOGFOOD: loop-orchestrator
building its own governance). Full spec: docs/plans/harness-governance.md.
Add a HarnessPolicy dataclass on EngineConfig (allow/deny lists, cost ceiling, autonomy cap, role_tag_map). Defaults reproduce TODAY's behavior exactly (empty policy = pass-through). _merge already recurses into nested dataclasses and ignores unknown keys — verify and rely on it. See plan A.1.

## Context you need
Files: src/loop_orchestrator/engine/config.py; tests/test_config*.py.
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
(conventional message citing T0012). Do NOT reinstall; do NOT push.

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
