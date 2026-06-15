---
id: T0035
title: "B5 — every active loop appears in the ledger loops registry (deck shows it); auto-register + backfill"
status: open
depends_on: []
jira:
scope: src/loop_orchestrator/engine/ (observe/checkpoint loop-registry sync + the checkpoint-header guidance that tells the coordinator to register a loop) + tests ONLY; ADDITIVE; no reinstall by the task; no git push
loop: operating-model
---

# T0035 — B5: ledger loops-registry sync (the deck shows every active loop)

## Objective
Observability / continuity. The loop-deck "loops" table reads ONLY the ledger's
`state["loops"]` registry (deck/model.py `_loops`; the engine only READS it —
observe.py/actions.py — the coordinator WRITES it). Found live: a new loop gets
tagged on its tasks (`loop:` field) AND gets an ops-wiki/loops/<id>.md doc, but
NOTHING registers it in the ledger registry — so the deck UNDER-REPORTS active
loops. Concretely: leo's `mvp-first-time-flow` loop (all Wave-1 tasks carry it,
has an ops-wiki doc) is absent from leo's ledger `loops`, and govern has NO
`.loop/orchestrator-state.json` at all so its loops panel is empty despite every
task carrying `loop: operating-model`. (Sibling concern to B3 — both are "the
deck must reflect reality.")

## Required behavior (the fix)
- The set of loops the deck shows must stay in sync with the loops that actually
  have tasks. The task `loop:` field across tasks/ (active + archive) is the
  source of truth for which loops exist and their status (open/in-progress vs
  done); the ledger `loops` registry is a projection of it.
- Choose the cleanest mechanism (recommend: the engine DERIVES/refreshes the
  loops registry from the task loop: fields during observe/checkpoint — read-only
  over tasks/, then a coordinator-owned write of the ledger, preserving any
  hand-authored loop metadata like branch/name; do NOT clobber existing fields —
  honor the F5/T0024 non-destructive-projection rule). Each loop row needs:
  id, status (derive: in-progress if any task open/in-progress, else shipped/done),
  branch, name (from the ops-wiki/loops doc title or the loop id if absent).
- BACKFILL the existing drift as part of the change/verification: leo's ledger
  gains `mvp-first-time-flow` (+ shakedown, verification-sweep); govern gets a
  ledger with `harness-governance` (done) + `operating-model` (in-progress). After
  the fix, `loop-deck`/`loop-digest --json` lists every loop that has tasks.

## Context you need
Files: src/loop_orchestrator/engine/observe.py (reads state.loops), actions.py
(reads ledger.loops), the checkpoint projection (T0021 ledger-canonical, T0024
non-destructive per-field), deck/model.py `_loops` (the consumer), the task
loop-lint / tasks-as-files convention (the loop: field). Honor "state JSON written
only by agents" — if the engine derives the registry, the write must go through
the coordinator-owned ledger write path, non-destructively.

## Deliverables
- A read-only derivation of the loops registry from the task `loop:` fields across
  tasks/ (active + archive) during observe/checkpoint, written back through the
  coordinator-owned, non-destructive (F5/T0024) ledger write path — preserving
  hand-authored loop metadata (branch/name). Each row carries: id, status
  (in-progress if any task is open/in-progress, else done/shipped), branch, and
  name (the ops-wiki/loops doc title, else the loop id).
- Backfill of the existing drift: leo's ledger gains `mvp-first-time-flow`
  (+ shakedown, verification-sweep); govern gets a ledger listing
  `harness-governance` (done) + `operating-model` (in-progress) — so
  `loop-deck`/`loop-digest --json` lists every loop that has tasks.
- Tests covering the derivation and the non-destructive merge.

## Acceptance criteria
- A loop with tasks but no ledger entry appears in `loop-digest --json` loops
  (and the deck) after a cycle with a correct status; an all-tasks-done loop shows
  done/shipped; hand-authored branch/name are preserved (non-destructive per
  F5/T0024).
- Backfill confirmed: leo ledger lists `mvp-first-time-flow`; govern ledger exists
  and lists `operating-model`.
- Full governance gate green; the 487/0 baseline must not regress; the ledger
  write stays coordinator-owned and non-destructive. Commit cites T0035/B5; no
  reinstall; no `git push`.

## Verification (done-when)
- A loop that has tasks but no ledger entry → appears in `loop-digest --json`
  loops (and the deck) after a cycle, with a correct status; a loop with all tasks
  done shows shipped/done; hand-authored branch/name are preserved (non-destructive).
- Backfill confirmed: leo ledger lists mvp-first-time-flow; govern ledger exists
  and lists operating-model.
- Full governance gate green; the 487/0 baseline must not regress.
- ADR/verify_record: test output + rollback note (revert the projection change).

## Out of scope
- The cross-session fleet view (that's B3/T0033); this is per-loop registry sync.
- Changing the tasks-as-files loop: convention or the FROZEN lint contract.
