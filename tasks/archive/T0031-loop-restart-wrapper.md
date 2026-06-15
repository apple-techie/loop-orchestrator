---
id: T0031
title: "B1 — loop-restart wrapper: re-source env + reinstall + assert PM adapter, the only sanctioned restart path"
status: done
accepted: 2026-06-15
gate: "492/0"
commit: f4b1e05
depends_on: []
jira:
scope: NEW bin/loop-restart (+ Makefile/CONTRACT/README doc) + tests ONLY; ADDITIVE; does NOT change engine code; the task itself does NOT restart any live daemon or git push
loop: operating-model
---

# T0031 — B1: loop-restart wrapper (env + reinstall + PM assert)

## Objective
Hardening. The HIGHEST-cost recurring continuity gotcha: `loop-engine restart`
run from a fresh shell inherits THAT shell's env, so `JIRA_*` / secrets vanish →
the PM adapter silently reports unavailable → pull/push become no-ops → loop state
goes stale invisibly. This broke the leo↔Gerardo Jira channel live 2026-06-14.
Memory alone keeps letting it recur — it must become a wrapper.

## Required behavior (the fix)
- New executable `bin/loop-restart <session> [--project-root R]`:
  1. Source the per-loop env file if present: `set -a; . ~/.loop-secrets/<session>.env; set +a`
     (path overridable via `$LOOP_SECRETS_DIR`). Absent file is allowed only when
     the loop declares no PM adapter.
  2. Reinstall the code (`make install-python` / `uv tool install … --reinstall`)
     so a running daemon picks up code changes (the "is it actually live" trap).
  3. `loop-engine --session <session> restart` (the existing stop→confirm-dead→start).
  4. ASSERT readiness: if the loop's `engine.pm.adapters` is non-empty, run
     `loop-pm --project-root R list-adapters` (or `sync --dry-run`) and require the
     configured adapter to report **available**; exit non-zero with a clear message
     if not. Exit 0 only when the daemon is alive AND PM (if configured) is live.
- Document it in CONTRACT.md + README as the ONLY sanctioned restart path for a
  PM-syncing daemon; deprecate bare `loop-engine restart` for those loops.

## Context you need
Files: NEW `bin/loop-restart`; `loop-pm` CLI (`list-adapters`, adapter availability);
`src/loop_orchestrator/engine/config.py` (read `engine.pm.adapters` to decide if the
assert applies); CONTRACT.md / README. See the memory gotcha
loop-daemon-restart-drops-env. govern itself has NO PM adapter, so the assert is a
no-op here — verify the no-PM path AND simulate a PM loop.

## Deliverables
- NEW `bin/loop-restart <session> [--project-root R]`: sources the per-loop env
  (`$LOOP_SECRETS_DIR/<session>.env`), reinstalls (`$LOOP_RESTART_INSTALL_CMD`,
  default `make install-python`), runs `loop-engine restart`, then asserts each
  configured `engine.pm.adapters` is `available` via `loop-pm list-adapters`.
  Exit 0 only when the daemon is up AND PM (if configured) is live; a missing env
  file with a PM adapter configured fails before restart.
- Makefile (`bin/` wired into check/install/uninstall), CONTRACT.md + README
  documenting it as the only sanctioned restart path for a PM-syncing daemon;
  bare `loop-engine restart` deprecated for those loops.
- `tests/test_loop_restart.py` covering env present/absent, adapter
  available/unavailable, no-PM, and usage-error paths.

## Acceptance criteria
- bash -n clean; the wrapper is idempotent and exits non-zero on a missing env
  file when a PM adapter is configured.
- Tests prove: PM loop + env present → exit 0 + adapter available; PM loop + env
  absent → non-zero before restart; no-PM loop (govern) → exit 0.
- Full governance gate green (492/0 at acceptance; 487/0 baseline not regressed);
  ADDITIVE; no reinstall by the task; no `git push`.

## Verification (done-when)
- Shellcheck/bash -n clean; the wrapper is idempotent and exits non-zero on a
  missing env file when a PM adapter is configured.
- A test (or scripted check) proves: PM-configured loop with env present → exit 0 +
  adapter available; same loop with env absent → non-zero + adapter unavailable
  message; no-PM loop (govern) → exit 0.
- ADR/verify_record: the bash check transcript + a rollback note (delete bin/loop-restart).

## Out of scope
- Re-keying or repairing the underlying PM credentials/secrets (operator concern).
- Restarting or reinstalling any live daemon as part of the task itself.
- An env-var fallback for the epic key (flagged as a possible future enhancement).
