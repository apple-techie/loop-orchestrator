---
id: T0028
title: "Phase 5 — full lane-handoff contract (recovery brief + handoff ack + worktree continuity)"
status: open
depends_on: [T0023, T0025]
scope: src/loop_orchestrator/engine/{actions.py,wiki.py} + ops-wiki lane-page schema + tests ONLY; ADDITIVE; flush stays gated on verified-idle; no reinstall; no git push
loop: harness-governance
---

# T0028 — Phase 5: full lane-handoff contract

## Objective
Phase 5. T0023 shipped the cheap half (the append-only `## Handoff state`
lane-page section + an idle-gated `drop_lane` flush breadcrumb). Build the
expensive half so a harness swap loses zero in-flight context:
- **Recovery brief**: when a lane is (re)provisioned to succeed a dropped one,
  the dispatch brief points the successor at the predecessor's `## Handoff state`
  + the active task file, so it resumes rather than cold-starts.
- **Handoff ack**: the successor writes a `handoff-in` mailbox message (subject
  `re:handoff:<window>`) so the handoff is OBSERVABLE — confirmed landed, not
  silently dropped.
- **Worktree continuity**: a swap of a `worktree`-isolated lane (T0025) carries
  the predecessor's worktree/branch to the successor (don't strand in-flight
  commits in an orphaned tree).
NOTE: built ahead of the concurrency>1 trigger per operator decision — keep it
ADDITIVE + safe at concurrency=1 (the flush already SKIPS unless verified-idle).

## Context you need
Files: `engine/actions.py` (the T0023 drop_lane flush; add_lane recovery brief),
`engine/wiki.py` (the `## Handoff state` section from T0023), the lane-page
schema, the mailbox ack convention (T0002 move-on-ingest; `subject: re:` match).
Worktree carry builds on T0025's `loops.<id>.branch` record. The flush stays
gated on T0015's verified-idle marker — never flush a non-idle lane.

## Deliverables
- Recovery brief on a succeeding-lane provision; `handoff-in` ack mailbox;
  worktree carry on swap of an isolated lane. ADDITIVE; concurrency=1 unaffected.
- Tests: recovery brief references the predecessor handoff state; ack round-trip;
  worktree carry preserves the branch; a non-idle lane is NOT flushed. Before/after
  note in `ops-wiki/loops/harness-governance.md`.

## Acceptance criteria
Full gate green: `make check` and, after
`chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null`,
`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q` (no regressions). Commit per the commit
policy (cite T0028). Do NOT reinstall; do NOT push; do NOT touch running daemons.

## Verification
```
make check
chflags nohidden .venv/lib/python*/site-packages/*.pth 2>/dev/null
uv run --no-sync --group dev pytest -q
git diff --stat
```

## Out of scope / escalate
Actually running 2+ concurrent code-writers (operator decision). If a flush would
target a not-verified-idle lane, it MUST skip + escalate — never risk a
mid-generation paste or an orphaned worktree.
