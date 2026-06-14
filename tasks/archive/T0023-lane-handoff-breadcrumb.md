---
id: T0023
title: "Phase-5 lane-handoff breadcrumb — Handoff state + idle-gated drop_lane flush"
status: done
accepted: 2026-06-14
gate: "445/0"
commit: cea8288
depends_on: [T0019]
scope: src/loop_orchestrator/engine/{actions.py,wiki.py} + ops-wiki lane-page schema + tests ONLY; ADDITIVE; flush gated on verified-idle and skips otherwise; no reinstall; no git push
loop: harness-governance
---

# T0023 — lane-handoff breadcrumb (cheap half of Phase 5)

## Objective
Phase 3 continuity (pulls forward the cheap half of the designed Phase-5
lane-handoff contract). Today `drop_lane` is a bare teardown with no flush, so a
mid-task harness swap silently discards in-flight lane work with no observable
signal. Add an append-only `## Handoff state` section to the lane page (step /
touched / working-tree / blocked-on / assumptions / as-of) and make `drop_lane`'s
flush a DEFAULT, gated on a verified-idle reading (reuses T0015's idle markers),
so a swap always leaves a breadcrumb even at concurrency = 1. Defer the expensive
worktree / concurrency machinery.

## Context you need
Files: `engine/actions.py` (the `drop_lane` executor — add a pre-teardown flush
prompt to the lane, gated on a trustworthy idle read), `engine/wiki.py` and the
`ops-wiki/lanes/<lane>.md` schema (the append-only `## Handoff state` section).
Idle gating depends on T0015's readiness markers (done) being trustworthy.
No-op by default: a lane with no agent / already idle / unknown harness skips.

## Deliverables
- `## Handoff state` lane-page section (append-only) + `drop_lane` idle-gated
  flush.
- Tests (flush writes the breadcrumb on a verified-idle agent lane; SKIPS on
  non-agent / unknown / not-verified-idle lanes). Before/after note in
  `ops-wiki/loops/harness-governance.md`.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (no regressions). Existing tests pass
UNCHANGED. Commit per the commit policy (cite T0023). Do NOT reinstall; do NOT push.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev ruff check src tests && uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
Worktree isolation + the full Phase-5 concurrency machinery + same-harness native
resume. If the flush would paste into a lane that is not verified-idle → it MUST
SKIP (never risk a mid-generation paste); escalate if idle cannot be trusted.
