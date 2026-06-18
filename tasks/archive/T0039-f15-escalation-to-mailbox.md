---
id: T0039
title: "F15 — worktree-lane escalations must reach the engine mailbox, not a worktree-local file"
status: done
accepted: 2026-06-17
gate: ""
commit: adbcf45 (merged 2e71161)
depends_on: []
scope: src/loop_orchestrator/engine/contracts/checkpoint-header.md (dispatch/escalation guidance) AND/OR src/loop_orchestrator/engine/{actions.py,observe.py,loop.py} or substrate.py (if an engine-side bridge is the chosen fix) + tests ONLY; ADDITIVE; NO reinstall; NO git push; NO *.sh edits unless strictly required (then worktree-only)
loop: lo-janitor
---

# T0039 — F15: route worktree-lane escalations to the mailbox

## Objective
Fix the blind-escalation gap found live on the F11/F10 batch (2026-06-16). A
worktree code lane (cwd = `.loop/worktrees/<session>/<window>`) was told by its
dispatch to write its done-signal to `.loop/worktrees/lo-janitor/web/web-to-coord.md`
— a file INSIDE the worktree. But the engine ingests escalations only from the MAIN
checkout's mailbox `.loop/messages/`. Result: the file landed where the engine could
not see it (`mailbox_pending=0`), so the brain read "no escalation arrived" and held
at the merge gate, blind. The operator had to track completion via git commits.

## Required behavior (the fix)
A worktree lane's escalation must land in the engine mailbox so the next cycle sees
it. Investigate and choose the most ROBUST approach (prefer deterministic engine-side
over brain-prose if feasible):
- **Guidance fix (minimum):** the dispatch payload must instruct the lane to write
  its escalation to the MAIN-checkout mailbox at an absolute path
  `<main>/.loop/messages/<UTC ts>-<window>-to-coord.md` (with `subject:` frontmatter),
  NOT a worktree-local file. This guidance lives in the checkpoint-header contract
  (how the brain composes a dispatch).
- **Deterministic fix (preferred if clean):** the engine, when a worktree lane goes
  idle, also scans that lane's worktree root for a `*-to-coord.md` escalation and
  bridges/moves it into `.loop/messages/` (so correctness does not depend on the
  brain remembering the path). Pick whichever is cleaner + testable; do both if cheap.

## Context you need
The dispatch payload is composed by the brain (guided by
`engine/contracts/checkpoint-header.md`); the engine delivers it and later observes
the mailbox in `observe.py` / `loop.py`. The worktree path is
`lib/lane-worktree.sh::lane_worktree_dir` = `.loop/worktrees/<session>/<window>`.
The mailbox is `paths.mailbox_dir` (`.loop/messages/`). The branch is recorded in
`.loop/orchestrator-state.json` under `loops.<window>.branch`.

## Deliverables
- A worktree lane's escalation reliably reaches `.loop/messages/` and triggers the
  next cycle (the brain sees it; no more blind hold).
- Test: simulate a worktree-lane escalation written to the wrong (worktree) path →
  assert the engine surfaces it in the mailbox (bridge) OR assert the dispatch
  guidance emits the mailbox path. Red-before-green.
- Before/after note in `ops-wiki/loops/lo-janitor.md`.

## Acceptance criteria
`make check-python` green in YOUR worktree (+ `make check` if you touch any `.sh`),
no regressions off the 544 baseline. Commit to YOUR worktree branch citing T0039/F15.

## Out of scope / ESCALATE (hard gate)
Do NOT merge / push / reinstall / edit the main checkout. When green, STOP and
ESCALATE to the operator. NOTE the bootstrap: until THIS fix is merged + live, your
own escalation for it will still land in the worktree — so for this task, ALSO write
your escalation to the absolute mailbox path
`/Users/andrewpeltekci/code/loop-orchestrator/.loop/messages/<UTC>-web-to-coord.md`
by hand, proving the target behavior.
