---
id: T0040
title: "F14 — dispatch to a worktree lane must embed the task spec (the worktree can't see uncommitted seeds)"
status: done
accepted: 2026-06-17
gate: ""
commit: 7183437 (merged 2e71161)
depends_on: []
scope: src/loop_orchestrator/engine/contracts/checkpoint-header.md (dispatch guidance) AND/OR the dispatch-composition path in src/loop_orchestrator/engine/{actions.py,loop.py} + tests ONLY; ADDITIVE; NO reinstall; NO git push; NO *.sh edits
loop: lo-janitor
---

# T0040 — F14: embed the task spec in a worktree dispatch

## Objective
Fix the friction found live on the F11 dispatch (2026-06-16). A worktree lane is cut
from main HEAD, so operator-seeded `tasks/Txxxx.md` (and `ops-wiki/loops/<loop>.md`)
that are uncommitted in main — or added after the worktree was cut — are NOT present
in the worktree. The dispatch said "read `tasks/T0036.md` for the full spec", which
fails in the worktree; the agent recovered by hunting the spec in the main repo, but
that is wasted cycles and fragile.

## Required behavior (the fix)
When dispatching a task to a lane (especially a worktree lane), the payload must be
SELF-CONTAINED: embed the task file's body inline rather than referencing a path the
lane may not have. Prefer the deterministic engine-side approach if clean:
- The engine already reads the task backlog during observe — when it composes/guides
  a task dispatch, include the task's full body in the payload (or in the brain's
  context with an explicit "embed the spec you were given; do NOT tell the lane to
  read a tasks/ path" instruction in the checkpoint-header).
- Keep it bounded (don't bloat the payload with huge specs — a task spec is small).

## Context you need
Dispatch payloads are composed by the brain (guided by
`engine/contracts/checkpoint-header.md`); the task files live in `tasks/` read via
`pm/taskfiles.py`. Relates to T0039/F15 (same dispatch-composition area) — you MAY
do both on the same branch in one batch if it's cleaner.

## Deliverables
- A task dispatch carries the spec inline; a worktree lane needs no access to
  `tasks/` to execute. Test: assert a composed task dispatch contains the spec body
  (not just a `tasks/...md` path reference). Before/after note in
  `ops-wiki/loops/lo-janitor.md`.

## Acceptance criteria
`make check-python` green in YOUR worktree, no regressions. Commit to YOUR worktree
branch citing T0040/F14.

## Out of scope / ESCALATE (hard gate)
Do NOT merge / push / reinstall / edit the main checkout. When green, STOP and
ESCALATE to the operator (mailbox path per T0039/F15). The operator merges the batch.
