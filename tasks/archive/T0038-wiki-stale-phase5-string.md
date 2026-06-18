---
id: T0038
title: "Warmup — fix stale 'deferred to Phase 5' working-tree string in wiki.py:152 (Phase 5 is DONE)"
status: done
accepted: 2026-06-17
gate: ""
commit: 8ca9ffc (merged d300b14)
depends_on: []
scope: src/loop_orchestrator/engine/wiki.py (one string/docstring line) + tests if one asserts it ONLY; NO reinstall; NO git push; NO .sh edits
loop: lo-janitor
---

# T0038 — Warmup: stale Phase-5 working-tree string

## Objective
Cosmetic doc-string staleness + a deliberate **runbook rehearsal**. `wiki.py:152`
still emits `"working-tree: shared project root (per-lane isolation deferred to
Phase 5)"`, but Phase 5 (worktree isolation — T0025/T0026/T0028) is DONE. This is a
near-zero-risk one-line change whose real value is exercising the full
draft → gate → escalate → human-merge runbook on something that cannot break anything.

## Required behavior (the fix)
- Update the string in `src/loop_orchestrator/engine/wiki.py` (~:152) to reflect that
  per-lane worktree isolation is implemented (e.g. describe the actual current
  behavior: shared root by default, dedicated worktree per code-writer lane when
  provisioned). Keep it accurate to how `should_provision_worktree` / the worktree
  lanes actually behave today — read the code, don't guess.

## Context you need
Files: `src/loop_orchestrator/engine/wiki.py` (~:152). If any test asserts the old
string, update it to match. This is engine Python (reinstall-buffered) — not one of
the 3 auto-appliable improve surfaces, so it still routes through the escalate-merge
gate like any code change.

## Deliverables
- The stale string is corrected to current behavior. Gate stays green.
- Before/after note in `ops-wiki/loops/lo-janitor.md`.

## Acceptance criteria
`make check-python` green in YOUR worktree, no regressions. Commit to YOUR worktree
branch citing T0038.

## Out of scope / ESCALATE (hard gate)
Do NOT merge / push / reinstall / edit the main checkout. When green, STOP and
ESCALATE the branch + diff to the operator. (Bundle this with T0036's escalation if
both land in the same window — one merge.)
