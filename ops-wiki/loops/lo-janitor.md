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

### T0038 / warmup — stale "deferred to Phase 5" handoff string (2026-06-16)
- **Before:** `engine/wiki.py:152` (`append_handoff`) stamped every drop_lane
  handoff breadcrumb with `working-tree: shared project root (per-lane isolation
  deferred to Phase 5)` — but Phase 5 (worktree isolation, T0025/T0026/T0028) is
  DONE, so the line lied about how lanes actually run.
- **After:** the line now reflects `gate.should_provision_worktree`'s real rule —
  `working-tree: shared project root while the sole serialized writer; a
  dedicated git worktree once parallelism is real (T0025/T0026/T0028)`. One-line
  string-accuracy fix, no behavior change; no test asserted the old string.
  (Deliberate runbook rehearsal: a zero-risk change exercising the full
  draft → gate → escalate → human-merge path.)

### T0039 / F15 — worktree-lane escalations route to the engine mailbox (2026-06-17)
- **Before:** the engine's lane-facing escalation/reply instructions
  (`actions._REPLY_FOOTER`, the handoff-recovery ack) named a cwd-relative
  `.loop/messages/<UTC>-…-to-coord.md`. A worktree lane resolves that against its
  OWN worktree (`.loop/` is gitignored → each worktree gets a fresh,
  engine-invisible mailbox), so the escalation never reached the engine — a blind
  escalation.
- **After:** new `actions._mailbox_message_hint(paths, who)` returns the ENGINE
  mailbox at the configured main-checkout root (`paths.mailbox_dir`, absolute),
  mirroring the substrate's `--project-root` worktree-correctness pattern. Wired
  into the reply footer and the handoff ack. `paths is None` (cli) keeps the
  relative hint, byte-identical. Guards in `tests/test_actions.py`
  (absolute-path routing + ingest-discoverability + not-worktree-local).

### T0040 / F14 — embed the task spec inline in worktree dispatches (2026-06-17)
- **Before:** a dispatch to a worktree lane said "read `tasks/Txxxx.md` for the
  full spec", but the worktree is cut from main HEAD and cannot see a spec that is
  uncommitted in main or seeded after the cut (this very task's spec was such a
  seed). The lane had to hunt the spec in the main repo — wasted, fragile cycles.
- **After:** `actions._embed_task_spec(payload, paths)` detects a `tasks/Txxxx-….md`
  reference and appends that file's content INLINE, resolved from the engine's
  main-checkout `tasks/` (`paths.tasks_dir`, which the engine CAN see — same
  main-root resolution as F15). Gated on the lane being worktree-isolated
  (`_is_worktree_lane`: a recorded `loops.<window>.branch`, T0025), wired into
  both the `add_lane` brief and the recurring `dispatch` payload. Non-worktree
  dispatches are byte-identical (regression-guarded). Bounded (≤16 KB), no-op when
  there is no reference / the file is absent / already embedded.

### T0041 / F16 — checkpoint overflow degrades, never aborts the cycle (2026-06-17)
- **Before:** `checkpoint_prompt` was the ONLY substrate call in `loop.run_once`
  not wrapped in `try/except SubstrateError`. An over-ceiling checkpoint prompt
  makes `scripts/loop-checkpoint.sh` exit 3 → `SubstrateError`, which aborted the
  WHOLE cycle (at the `_assemble_prompt` call, ~loop.py:523) BEFORE the brain ran
  — so the loop could not self-trim its own bloat, unlike observe/ingest/roster/
  lanes which already degrade (F7/F11). (Harvested from leo, 2026-06-16: 2
  over-ceiling crashes; leo self-recovered as state shrank — a degrade-consistency
  gap, not a wedged hot-loop.)
- **After:** the `_assemble_prompt` call is wrapped like its siblings: on overflow
  it emits a `checkpoint-overflow` event and falls back to a header-only prompt
  (`_degraded_checkpoint_body` — the contract header + an explicit directive to
  trim `ops-wiki/checkpoint.md` + `index.md` this cycle), so the brain STILL runs
  and the cycle resolves (`cycle-end`) instead of aborting. Surgical, `loop.py`
  only; no `watch.py` backoff (out of scope — leo self-recovered, so no hot-loop
  to guard). Regression guard in `tests/test_loop_integration.py`
  (`test_checkpoint_overflow_degrades_and_brain_still_runs`): red before (cycle
  aborts), green after (degrade + brain runs).
