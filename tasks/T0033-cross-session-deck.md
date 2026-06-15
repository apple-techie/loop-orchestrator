---
id: T0033
title: "B3 — cross-session deck: one read-only view of every running loop (engine, queue depth, lane health)"
status: open
depends_on: []
jira:
scope: src/loop_orchestrator/deck/ + a multi-session read helper in substrate.py + tests ONLY; ADDITIVE; deck stays NON-WRITER; no reinstall by the task; no git push
loop: operating-model
---

# T0033 — B3: cross-session deck (single pane of glass)

## Objective
Continuity / operating model. The deck is the operator's "I can step away" signal,
but today it is bound to a single (project_root, session) — to watch N loops you
run N decks. The multi-loop happy place needs ONE view across all running loops;
without it the operator fragments.

## Required behavior (the feature)
- A cross-session view — `loop-deck --all` (or a `loop-deck fleet` mode / a top
  "fleet" screen) — that enumerates EVERY running loop and shows per loop:
  session name, engine daemon state (running/paused/stopped + pid), pending-decision
  queue depth (and whether one is awaiting approval), and a lane-health summary
  (counts of idle/working/needs-approval/errored).
- Discovery: find running loops by scanning known session state dirs
  (`.loop/sessions/*/engine/engine.pid` across configured project roots) — define
  how roots are supplied (e.g. a `--roots` list or a small `~/.loop/registry`).
- MUST preserve the deck NON-WRITER invariant — read-only aggregation only
  (reuse `loop-lane-status --json --all` / `loop-engine status` / snapshot.json);
  drilling into one loop hands off to the existing per-session deck.

## Context you need
Files: `src/loop_orchestrator/deck/*` (Textual screens/tables; the per-session deck
is bound to one `self.session` in substrate.py — the fleet view must NOT assume a
single session), `substrate.py` (add a read-only multi-session enumerator),
`paths.py` (SessionPaths — how engine dirs are located). Read-only surfaces:
`loop-lane-status --json --all <session>`, `loop-engine --session S status`,
per-session snapshot.json. The per-session deck and its non-writer rule are the
pattern to extend.

## Verification (done-when)
- Two loops running on distinct sessions → the fleet view lists BOTH with accurate
  engine state, queue depth, and lane-health counts; a paused/stopped loop shows
  correctly; no session → empty fleet, no crash.
- Textual pilot/smoke test for the fleet screen; assert it issues only read-only
  CLIs (non-writer invariant intact).
- ADR/verify_record: test output + a manual two-loop screenshot/transcript +
  rollback note (remove the fleet screen/flag).
