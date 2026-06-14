# Substrate contract — v1

The bash scripts in this repo are the substrate: the stable, self-sufficient
layer that works with nothing but bash ≥ 3.2, tmux ≥ 3.3, and python3. Any
higher layer (the Python `loop-engine` / `loop-deck`, CI, external tooling)
may depend ONLY on the surfaces enumerated here. Everything else — internal
functions, output formatting not listed below, file layouts not listed below —
is free to change without notice.

Versioning rule: **additive-only within a major version.** Every JSON payload
embeds `"contract_version": 1`. New fields may be added to JSON objects and
new flags to CLIs at any time; existing fields, flag semantics, exit codes,
and the word/exit outputs frozen below only change with a major bump and a new
section in this file.

## CLI surfaces

### loop-tmux.sh
- `up` flags incl. `--no-attach`, `--boot-check` (exit non-zero if an AI lane
  fell back to a bare shell), `--print-cmds` (dry-run, no tmux).
- `add-lane --session <s> --window <w> (--harness <h> | --cmd <c>) [--model m]
  [--repo p] [--role r] [--kind standing|worker] [--worktree] [--auto-approve]
  [--wait-ready [--ready-timeout s]]`
  — exit 0 = window created (and, with `--wait-ready`, best-effort readiness).
  `--kind` declares the lane lifecycle (stored as the `@loop_lane_kind` window
  option); default = inferred (an add-lane window is dynamic, so `worker`).
  `--worktree` (T0025) provisions a dedicated git worktree for the lane at
  `<repo>/.loop/worktrees/<session>/<window>` on branch `loop/<session>/<window>`,
  records that branch in `loops.<window>.branch` of the ledger, and sets cwd
  there (`@loop_lane_isolation=worktree`, `@loop_lane_branch=<branch>`). DEFAULT
  is `shared` (the lane inherits the repo root — byte-identical to today); a
  harness may also declare `isolation: worktree` in the registry. Worktree
  applies only to code-writer agent lanes (never cmd/shell/mprocs).
- `drop-lane --session <s> --window <w> [--force]` — refuses non-dynamic
  windows without `--force`. Automation must NEVER pass `--force`. A worktree
  lane's tree is torn down on drop and NEVER orphaned: a clean tree is removed
  (+ pruned); a tree with uncommitted work is PRESERVED (left listed, branch
  recorded) with a warning — never `git worktree remove --force` over real work.
- `list-lanes --session <s>` — human table (do not machine-parse).
- `list-lanes --session <s> --json` — `{contract_version, session,
  generated_at, lanes: [{window, harness, model, role, cmd, base, kind}]}`.
  `base: true` = not created by add-lane. `kind: standing|worker` is the declared
  lane lifecycle (`@loop_lane_kind`), else inferred (`base`→`standing`,
  dynamic→`worker`); a `standing` lane is never auto-dropped. Null fields =
  option unset.

### loop-dispatch.sh
- `[--session <s>] [--mode command|text] [--no-enter] [--verify]
  [--wait-ready [--ready-timeout s]] [--interrupt] <lane> <payload>` and the
  `--window <target>` direct form.
- Exit 0 = payload delivered to the pane (paste + Enter). Non-zero = NOT
  delivered (usage, missing session/lane, tmux failure). Dispatch is
  at-most-once; callers must not retry blindly (double-paste into a TUI
  composer corrupts the turn).
- `--interrupt` sends Escape, waits 1s, then dispatches — cancels in-flight
  generation for steer-style redirects.
- `--mode text` is bracketed-paste with a paste→Enter delay
  (`LOOP_DISPATCH_PASTE_DELAY`, default 2.0s).

### loop-lane-status.sh
- `<session> <lane>` — exactly one word on stdout:
  `working | awaiting-approval | idle | errored | unknown`. Exit 0 for any
  recognized status; non-zero only on usage/resolution failure. FROZEN.
- `--print-target <session> <lane>` — prints the resolved tmux target (a valid
  `-t` argument; shape is session:window.pane for fixed lanes, a `%pane-id`
  for dynamic ones — callers must not parse it).
- `--json <session> <lane>` / `--json --all <session>` — `{contract_version,
  session, generated_at, lanes: {<lane>: {status, target, kind}}}` with
  `kind: fixed|dynamic`. `--all` covers fixed lanes whose windows exist plus
  all add-lane windows; absent fixed lanes are skipped, not errors.
- `LOOP_LANE_IDLE_HOME_PATTERN` (ERE alternation) extends idle detection.

### loop-digest.sh
- `--once` — human ASCII frame (do not machine-parse).
- `--json` (implies `--once`) — `{contract_version, generated_at,
  state: <parsed orchestrator-state.json or null>,
  mailbox: {pending: [{file, from, to, subject, mtime}], processed_count},
  unpushed: [{repo, path, branch, upstream, count}],
  adrs: [{id, status, title, path}]}`.

### loop-adr.sh
- `new <title>`, `list`, `show <id>`, `accept <id>`, `--adr-dir`/
  `LOOP_ADR_DIR`. `accept` refuses unless frontmatter has `verify_record` AND
  (`rollback` OR `canary_record`). **ADR acceptance is human-gated: automation
  must never invoke `accept`.**

### lib/harness-registry.sh
- Sourced: `harness_known`, `harness_field <name> <field>`,
  `harness_binary_path`, `harness_resolve_launch <name> [model]`.
- CLI: `list`, `fields <name>`, `field <name> <field>`, `oneshot <name>`,
  `probe <name> [model]`.
- `oneshot <name>` prints the one-shot command template with a literal
  `{prompt}` placeholder (e.g. `claude -p {prompt}`); exit 1 + empty when the
  harness has no one-shot mode. Callers shlex-split the template and
  substitute `{prompt}` as ONE argv element — never via shell interpolation.

### lib/lane-config-resolver.sh
- CLI: `print-resolved`, `lane-launch <lane> [mode]`, `lane-field <lane>
  <field>`, `validate`, `lanes` against a lane-config YAML.
- The YAML's top-level `lanes:` key is the only key bash reads; other
  top-level keys (e.g. `engine:`) are reserved for higher layers.

### scripts/ (compiled-coordinator helpers)
- `loop-wiki-pending.sh [--project-root p] [--quiet]` — `--quiet` prints a
  bare integer pending-message count.
- `loop-checkpoint.sh (--print | --dispatch [lane]) [--project-root p]
  [--header-file path] [--token-ceiling n]` — `--print` emits the assembled
  coordinator prompt on stdout; size report goes to stderr (`prompt size: <N>
  bytes (~<M> tokens, bytes/4)`, warning above 24000 tokens, HARD-refused
  (exit 3) above the ceiling — `--token-ceiling` / `LOOP_CHECKPOINT_TOKEN_CEILING`,
  default 48000). `--header-file` substitutes the coordinator header.
  T0021: the checkpoint COMPILED region (above the `<!-- coord-decisions -->`
  marker) is projected from `.loop/orchestrator-state.json` (the canonical loop
  ledger) at assembly time when the ledger is present; the marker line and the
  coord-owned region below it are preserved byte-for-byte from checkpoint.md. An
  absent / empty / unparseable ledger falls back to checkpoint.md's hand-authored
  region (byte-identical default).
- `loop-task-lint.sh [--tasks-dir d]` — exit 0 = all task files pass; exit 1
  with per-file findings otherwise.
- `loop-jira-sync.sh (pull|push|both) [--dry-run] [--tasks-dir d]` — exit 64 =
  not implemented / implementation unavailable (stable even after the real
  implementation lands, for the no-Python case).
- `loop-metrics.sh [--session s] [--project-root p] [--log]` — coordinator
  metrics summary block on stdout; missing inputs degrade to 0/n-a with notes,
  exit 0. `--log` appends exactly one `## [YYYY-MM-DD] metrics | <summary>`
  entry to ops-wiki/log.md. The Python engine invokes this via its substrate
  wrapper (`loop-metrics` repo-relative entry).
- `loop-wiki-lint.sh (--print | --dispatch [--lane <name>]) [--session s]
  [--project-root p]` — assembles the wiki-lint prompt; `--print` emits it on
  stdout (size report on stderr), `--dispatch` creates/reuses a lint lane and
  pastes it (exit 0 = delivered; the lint window is never auto-dropped). The
  Python engine invokes this via its substrate wrapper (`loop-wiki-lint`
  repo-relative entry).

## File conventions (read-only to higher layers unless stated)

- `.loop/orchestrator-state.json` — schema v2 (`schema_version`, `updated_at`,
  `loops.<id>.{status, branch, blast_radius, artifacts, commits, …}`, and the
  optional top-level `objective` (string) + `open_conflicts` (list) projected
  into the checkpoint). T0021: this ledger is the CANONICAL work-state surface;
  `checkpoint.md`'s compiled region (above the coord-decisions marker) is its
  projection (see `loop-checkpoint.sh --print`), so the brain boots from a view
  provably equal to the ledger. Written by agents; additionally, `loop-tmux
  add-lane --worktree` records the provisioned `loops.<window>.branch` (T0025)
  so the digest and a future integration lane find each worktree branch.
- `.loop/messages/` — mailbox, `YYYYMMDD-HHMMSS-<from>-to-<to>.md` with
  `subject:` frontmatter. New messages may be ADDED by any layer following the
  naming convention; never modify or delete existing ones.
  `.loop/messages/processed/` — ack dir; only the docs lane moves files here.
- `.loop/sessions/<session>/lane-restarts.jsonl` — watchdog events, one JSON
  per line. Restart lines carry NO `event` field (lane-health's restart_pane
  writes {timestamp, session, lane, target, cmd}); lifecycle lines carry
  `event` (e.g. `giving-up`). Treat a missing `event` as a restart.
  Append-only, written by lane-health.
- `.loop/sessions/<session>/engine/` — RESERVED for the Python engine layer
  (its own files; bash never reads or writes here).
- `ops-wiki/` — write partitions per AGENTS.md. `ops-wiki/checkpoint.md`
  contains one `<!-- coord-decisions -->` marker line; everything at/below it
  is coordinator-owned, everything above is docs-compiled.
- `docs/adr/NNNN-<slug>.md` — MADR records with frontmatter
  `status/verify_record/canary_record/rollback`.
- `tasks/T<NNNN>-<slug>.md` + `tasks/archive/` — task files per AGENTS.md
  "Task files" (frontmatter `id, title, status, depends_on, scope` +
  optional `loop, jira`).

## Environment

- bash ≥ 3.2 (macOS system bash), tmux ≥ 3.3, python3 (required: digest and
  all `--json` outputs), PyYAML (only for `--lane-config`).
- The pure-bash substrate MUST keep working with no Python packages installed
  beyond the standard library; CI enforces this by running `make check` with
  no extras.
- Locking: bash takes no locks (append-only + unique filenames). Higher
  layers needing read-modify-write coordination use their own lock files
  under `.loop/sessions/<session>/engine/` — never `flock(1)` (absent on
  macOS).
