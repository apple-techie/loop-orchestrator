---
id: T0037
title: "F10 — bin/loop-restart must not foreground-block before the PM-assert (+ kill the false-green test)"
status: done
accepted: 2026-06-17
gate: ""
commit: c277a09 (merged d300b14)
depends_on: []
scope: bin/loop-restart + tests/test_loop_restart.py ONLY; optionally src/loop_orchestrator/engine/{watch.py,cli.py} IF a detach path is the chosen fix; NO reinstall; NO git push; NO edits to symlinked substrate *.sh
loop: lo-janitor
---

# T0037 — F10: loop-restart non-blocking before PM-assert

## Objective
Hardening. **F10 (found live):** `bin/loop-restart` step 3 (~:117) runs
`loop-engine ... restart` synchronously. `cmd_restart` → `watch.restart()`
(`watch.py` ~:628-645) ends in `Watch(...).run()`, and `Watch.run()` is a blocking
`while self._running: ...` loop (~:239-268). So `loop-engine restart` NEVER returns —
it BECOMES the foreground daemon. The wrapper hangs at ~:117 and never reaches the
PM-assert (~:122-148); its `exit 0` ("daemon restarted AND adapter available") is
unreachable for a real daemon. This is the sanctioned restart path for the LIVE leo
PM loop, so the silent hang is high-value to fix — but a bad fix can leave a daemon
down, so the regression test is the autonomy gate here.

## Required behavior (the fix)
- Make the restart non-blocking before the PM-assert. Preferred (bash-side): background
  `loop-engine restart &`, then POLL `engine.pid` + heartbeat freshness (the
  `loop-engine ... status` surface at `cli.py` ~:166-195 already reports
  `alive (pid…, heartbeat Ns ago)`) until alive-and-fresh (bounded timeout), THEN run
  the PM-assert. Alternative (engine-side): add a detaching `restart --detach` that
  forks `Watch.run()` and returns — only if cleaner.
- Keep the existing env re-source + reinstall + PM-adapter assertion semantics intact;
  the wrapper must still exit non-zero if the daemon never comes up OR a PM adapter is
  unavailable.

## Context you need
Files: `bin/loop-restart` (~:115-148), `tests/test_loop_restart.py` (the false-green —
`:50` stubs `loop-engine` as `echo "$*"; exit 0`, returns instantly, so all 5 tests
pass while the real foreground-block stays invisible). `bin/loop-restart` is invoked
via repo path / make (NOT symlinked into ~/.local/bin) — so it is NOT instant-live,
but it IS in the working tree, so do this on your worktree branch.

## Deliverables
- `bin/loop-restart` polls for daemon readiness (pid + fresh heartbeat) before the
  PM-assert; never foreground-hangs.
- A blocking-stub regression test: the stub must BLOCK like the real daemon (e.g. sleep/
  loop) so the test fails on the old foreground-blocking wrapper (red) and passes once
  the wrapper backgrounds+polls (green). The current instant-exit stub is the thing
  that lied — replace/augment it.
- Before/after note in `ops-wiki/loops/lo-janitor.md`.

## Acceptance criteria
Full gate green IN YOUR WORKTREE: `make check` (bash substrate) + `make check-python`
(pytest), no regressions. Commit to YOUR worktree branch citing T0037 / F10.

## Out of scope / ESCALATE (hard gate)
- Do NOT merge to main. Do NOT `git push`. Do NOT reinstall. Do NOT edit the main
  checkout — work ONLY in your worktree.
- `bin/loop-restart` itself is the live restart tool: do NOT use it to restart any
  live loop as part of this task. When green on your branch, STOP and ESCALATE the
  branch + diff + gate to the operator; the operator merges + verifies the restart
  path against a throwaway session before it touches leo.
