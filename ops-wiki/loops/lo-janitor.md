# loop: lo-janitor

## Status
in-progress (per `.loop/orchestrator-state.json`)

## Scope (current)
**Worktree-isolated, code-DRAFTING janitor for loop-orchestrator.** Source:
`.loop/messages/processed/20260616-233017-andrew-to-coord.md` (2026-06-16).
SUPERSEDES the prior NON-CODE self-maintenance scope
(`20260616-001727-andrew-to-coord.md`, formerly recorded in
[lanes/coord.md](../lanes/coord.md)).

> CONFLICT: prior lo-janitor scope "LOW-RISK, NON-CODE work only … NEVER edit
> code" (20260616-001727) vs current scope "you may draft and verify real tool
> fixes — but only on an isolated branch" (20260616-233017). Current message
> explicitly supersedes; code-drafting is now permitted IN THE WORKTREE ONLY.

The janitor may draft and verify real tool fixes, but only on an isolated
branch, and NEVER promotes them itself.

### Isolation (already provisioned)
- The `web` lane runs in a dedicated git worktree at
  `.loop/worktrees/lo-janitor/web` on branch `loop/lo-janitor/web`. Its edits
  NEVER touch the main checkout — this is what makes code-drafting safe (the
  substrate `*.sh` CLIs are symlinked LIVE from `~/.local/bin` into main).

### Per-task dispatch contract (to the `web` lane)
- Work ONLY in the worktree (cwd). Author the fix + a real regression test
  (red before green). Run the gate IN THE WORKTREE (`make check` /
  `make check-python`). Commit to branch `loop/lo-janitor/web` citing the
  task id.
- Then STOP and ESCALATE: send a `web-to-coord.md` message with the branch
  name, `git diff --stat`, and the green gate result. Coord relays to Andrew.
- A green, escalated branch = task done from the janitor side; move to the
  next backlog item. Do not re-dispatch a task already in `review`.

### Backlog (drain IN ORDER, one task per dispatch)
1. **T0036** — F11: loops-sync must skip a malformed task YAML instead of
   crashing the cycle (pure Python; `observe.py` + `taskfiles.py` + tests).
   DO THIS FIRST.
2. **T0037** — F10: `bin/loop-restart` must not foreground-block before the
   PM-assert (+ kill the false-green test).
3. **T0038** — warmup: fix the stale "deferred to Phase 5" string in
   `wiki.py:152`.

### HARD limits
loop-orchestrator is the self-modifying substrate — highest blast radius.
- NEVER merge to main, NEVER git push, NEVER reinstall
  (`make install-python`), NEVER delete a branch, NEVER edit the main
  checkout, NEVER touch the govern worktree or its feature branch.
- The merge + reinstall + restart is a HUMAN gate — Andrew does it after
  reviewing the escalated branch.
- Keep doing the safe maintenance too (lints, metrics, ops-wiki recompile)
  when there is no code backlog left.

## Open items
- Drain backlog T0036 → T0037 → T0038, one dispatch at a time.

## Before/after notes

### T0036 / F11 — loops-sync skips malformed task YAML (2026-06-16)
- **Before:** `derive_loops_from_tasks` (`engine/observe.py`) guarded
  frontmatter parsing with `except ValueError` only. `pm/taskfiles.split_task`
  calls `yaml.safe_load`, which raises `yaml.YAMLError` on malformed YAML — and
  `yaml.YAMLError` is **not** a `ValueError` subclass. So one bad task file
  (e.g. a colon-bearing unquoted title → `ScannerError`) propagated out through
  `sync_loops_registry` and aborted the entire engine cycle. It crashed
  lo-janitor's own first cycle; commit `6836ee1` band-aided it by quoting two
  titles, leaving the engine one malformed file from a dead cycle.
- **After (robust fix):** `split_task` now wraps `yaml.safe_load` and re-raises
  any `yaml.YAMLError` as `ValueError(... malformed frontmatter YAML ...)`. This
  closes the same latent bug at all six callers that catch only `ValueError`
  (`observe.derive_loops_from_tasks`, `taskfiles.find_by_jira`, the `jira.py`
  sites, `deck/model.py`). The `observe.py` guard was also widened to
  `(ValueError, yaml.YAMLError)` as belt-and-suspenders. A malformed task file
  is now skipped (dropped from derivation); valid loops still derive and the
  cycle proceeds.
- **Regression guards:**
  `tests/test_pm_taskfiles.py::test_split_task_reraises_malformed_yaml_as_value_error`
  and
  `tests/test_loops_sync.py::test_derive_skips_malformed_yaml_instead_of_crashing`
  (both red before the fix, green after).

### T0037 / F10 — loop-restart non-blocking before the PM-assert (2026-06-16)
- **Before:** `bin/loop-restart` step 3 ran `loop-engine … restart`
  synchronously. `cmd_restart` → `watch.restart()` ends in `Watch.run()`'s
  blocking `while self._running` loop — the restart NEVER returns, it BECOMES
  the foreground daemon. So the wrapper hung at step 3 and never reached the
  PM-adapter assert; its `exit 0` was unreachable for a real daemon. The test
  stubbed `loop-engine` as an instant `exit 0`, so all 5 tests passed while the
  live foreground-block stayed invisible (a false green).
- **After:** the wrapper backgrounds `loop-engine restart` (stdio detached to a
  temp log) and POLLS `loop-engine status` until it reports
  `watch: alive (pid …, heartbeat Ns ago)` with a fresh heartbeat
  (`age <= LOOP_RESTART_MAX_HEARTBEAT`, default 15s) within a bounded timeout
  (`LOOP_RESTART_TIMEOUT`, default 30s), THEN runs the PM-assert. A non-zero
  early exit of the backgrounded restart, or a never-ready daemon, fails the
  wrapper non-zero and dumps the restart log. Env re-source + reinstall +
  PM-adapter assertion semantics are unchanged.
- **Regression guard:**
  `tests/test_loop_restart.py::test_restart_is_nonblocking_reaches_pm_assert` —
  the `loop-engine` stub now BLOCKS forever like the real daemon and `status`
  reports alive once up; the wrapper is run in its own process group with a
  timeout, so the old foreground-blocking wrapper trips the timeout (red,
  verified) and only the background+poll wrapper exits 0 (green). The four
  pre-existing functional tests run against the same blocking stub.
