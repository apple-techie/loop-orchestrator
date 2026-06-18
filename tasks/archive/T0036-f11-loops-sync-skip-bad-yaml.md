---
id: T0036
title: "F11 — loops-sync must skip a malformed task YAML instead of crashing the whole engine cycle"
status: done
accepted: 2026-06-17
gate: ""
commit: af2b121 (merged d300b14)
depends_on: []
scope: src/loop_orchestrator/engine/observe.py + src/loop_orchestrator/engine/taskfiles.py + tests ONLY; ADDITIVE defensive catch; NO reinstall; NO git push; NO edits to any *.sh substrate
loop: lo-janitor
---

# T0036 — F11: loops-sync skips malformed task YAML, never crashes the cycle

## Objective
Hardening. **F11 (found live 2026-06-16 — it crashed lo-janitor's OWN first cycle):**
`derive_loops_from_tasks` (`observe.py` ~:208-231) parses each task's frontmatter
inside `try/except ValueError` (~:217-219). But `parse_frontmatter` → `split_task`
(`taskfiles.py` ~:61-73) calls `yaml.safe_load(...)`, which on malformed YAML raises
`yaml.YAMLError` — and `issubclass(yaml.YAMLError, ValueError)` is **False**. So a
single bad task file is NOT caught: the error propagates out of
`derive_loops_from_tasks` → `sync_loops_registry` → the UNGUARDED call at
`loop.py:418`, aborting the entire engine cycle. Proven live: a colon-bearing title
threw `ScannerError` and was band-aided by commit `6836ee1` (quoting titles) — the
engine is still one malformed file away from a dead cycle.

## Required behavior (the fix)
- Widen the guard in `derive_loops_from_tasks` so a malformed task YAML is SKIPPED
  (logged/ignored, that one file dropped from the derivation) rather than raised —
  catch `(ValueError, yaml.YAMLError)` (import yaml, or catch both).
- ROBUST option (preferred, higher ROI): harden `taskfiles.split_task` to re-raise
  YAML parse failures as `ValueError` (e.g. `raise ValueError(...) from err`). This
  closes the SAME latent bug in the other callers that catch only `ValueError`
  (`jira.py` ~:475/509/564, `deck/model.py` ~:185, `taskfiles.find_by_jira` ~:133) —
  fix once, cover all six sites. If you take this option, keep the observe.py guard
  too (defense in depth).

## Context you need
Files: `src/loop_orchestrator/engine/observe.py` (the except at ~:217-219 +
`sync_loops_registry`), `src/loop_orchestrator/engine/taskfiles.py` (`split_task`
`yaml.safe_load` at ~:70). The change is purely defensive/additive — derivation must
degrade by skipping the offending file, never abort the cycle.

## Deliverables
- A malformed task YAML is skipped, the cycle proceeds; valid tasks still derive.
- Test in `tests/test_loops_sync.py`: write a deliberately malformed task file →
  assert the sync SKIPS it and returns the valid loops (does NOT raise). Make the
  test FIRST reproduce the crash (red) before the fix (green) — a real regression
  guard, not a tautology.
- Before/after note in `ops-wiki/loops/lo-janitor.md`.

## Acceptance criteria
Full gate green IN YOUR WORKTREE: `make check-python`
(`uv run --no-sync --group dev ruff check src tests` + `ruff format --check src tests`
+ `uv run --no-sync --group dev pytest -q`), no regressions off the 528/0 baseline.
Commit to YOUR worktree branch `loop/lo-janitor/<window>` citing T0036 / F11.

## Out of scope / ESCALATE (hard gate)
- Do NOT merge to main. Do NOT `git push`. Do NOT reinstall (`make install-python`).
  Do NOT edit anything in the main checkout — work ONLY in your worktree.
- When the gate is green on your branch, STOP and ESCALATE: report the branch name +
  the diff + the green gate to the operator via a `*-to-coord.md` mailbox message.
  The operator merges to main + reinstalls + restarts the live loops. That promotion
  is human-only (loop-orchestrator is self-modifying — highest blast radius).
