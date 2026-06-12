# loop-orchestrator

[![CI](https://github.com/apple-techie/loop-orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/apple-techie/loop-orchestrator/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Project-agnostic tmux orchestration for agentic coding loops. The base is a
set of shell scripts that spin up a loop-aware tmux session — a coordinator
pane, AI implementation lanes, a validation lane, an ops/health lane, and a
docs lane — wired together with a small mailbox + state-file convention so
several coding agents can work the same objective in parallel. On top of
that substrate (and strictly optional): a deterministic orchestration engine
whose LLM "brain" is a swappable one-shot CLI call, and a TUI flight deck
for steering the fleet.

The scripts stay substrate-level: they bootstrap the tmux layout, dispatch
prompts into lanes, classify lane readiness, and render an ASCII digest of the
orchestrator state. They make no assumptions about your stack — any project
whose orchestrator state file matches the schema below (v2) can use them.

On top of the substrate sit two optional layers, each usable without the one
above it:

```
┌─ loop-deck (Textual TUI) ──── interactive flight deck: fleet, loops,
│       mailbox, decision queue, ADR ledger. Strict non-writer.
├─ loop-engine (Python) ─────── deterministic orchestration loop with a
│       swappable headless LLM "brain", human-gated decisions, a watch
│       daemon, and a self-improvement loop. Owns all side effects.
└─ bash substrate ───────────── everything below; works forever with just
        bash ≥ 3.2 + tmux ≥ 3.3 + python3 (CI enforces this).
```

The exact bash surfaces the upper layers may depend on are pinned in
[CONTRACT.md](CONTRACT.md) (versioned, additive-only). Orchestration
conventions — write partitions, ingest protocol, coordinator contract,
task files, lint and experiment protocols — live in [AGENTS.md](AGENTS.md).

## Layout

```
loop-orchestrator/
├── loop-tmux.sh           # 6-window tmux bootstrap + dynamic add/drop-lane
├── loop-dispatch.sh       # paste prompts into a lane (--interrupt to steer)
├── loop-digest.sh         # ASCII state + mailbox digest (--json for machines)
├── loop-lane-status.sh    # readiness classifier (--json --all for the fleet)
├── loop-adr.sh            # MADR decision-record helper (new/list/accept)
├── lib/
│   ├── harness-registry.sh    # per-harness contract incl. oneshot templates
│   ├── lane-config-resolver.sh # YAML → resolved launch commands
│   └── lane-health.sh         # auto-restart watchdog
├── scripts/               # compiled-coordinator helpers (see below)
│   ├── loop-wiki-pending.sh   #   mailbox pending count / summary
│   ├── loop-checkpoint.sh     #   assemble the stateless coordinator prompt
│   ├── loop-task-lint.sh      #   validate tasks/ files
│   ├── loop-jira-sync.sh      #   PM sync shim (delegates to loop-pm)
│   ├── loop-metrics.sh        #   coordinator-efficiency metrics
│   └── loop-wiki-lint.sh      #   nightly ops-wiki lint prompt
├── src/loop_orchestrator/ # the Python layer (engine / deck / pm)
├── tests/                 # 268 tests; fakes-on-PATH harness, no tmux needed
├── ops-wiki/              # compiled coordinator memory (see AGENTS.md)
├── tasks/                 # tasks-as-files (+ archive/)
├── examples/
│   ├── lane-config.example.yaml   # incl. the engine: config section
│   └── madr-decision.lane-config.yaml
├── skills/loop-orchestrator/  # operator skill for agent harnesses (SKILL.md)
├── AGENTS.md              # agent operating rules + protocols
├── CONTRACT.md            # substrate contract v1 (frozen surfaces)
├── pyproject.toml         # loop-engine / loop-deck / loop-pm entry points
├── Makefile
└── README.md
```

## Install

```bash
# Symlinks the scripts into ~/.local/bin (already on PATH for most user
# setups, no sudo required). Override with BIN=... for /usr/local/bin etc.
make install                         # → ~/.local/bin
make install BIN=/usr/local/bin      # → /usr/local/bin (likely needs sudo)
make uninstall                       # remove the symlinks
make print-paths                     # dry-run: show what install would do
make check                           # bash -n syntax-check all scripts + libs
```

After install, call the scripts bare: `loop-tmux`, `loop-dispatch`,
`loop-digest`, `loop-lane-status`, `loop-adr`. Or skip the install and invoke them by
absolute path — they are self-contained.

All scripts are self-contained Bash (bash >= 3.2), tested on macOS (tmux
>= 3.3, zsh default shell) and on Linux with tmux installed.

### Requirements

- `tmux` (>= 3.3) — required by every script.
- `python3` — required by `loop-digest.sh` (and the default `coord` pane) to
  render the orchestrator state JSON.
- **PyYAML** — required only for `--lane-config` (`lib/lane-config-resolver.sh`
  parses the YAML via `python3 -c 'import yaml'`). macOS's system `python3`
  ships without it; install with `python3 -m pip install pyyaml`. Without
  PyYAML, `--lane-config` now fails fast with an actionable error instead of
  silently launching an empty session.

## Scripts

### `loop-tmux.sh` — bootstrap a six-window tmux session

Windows: `coord`, `web`, `infra`, `validate`, `ops`, `docs` — mapped to the
generic lane roles described below.

- `coord` — live loop-digest (state + mailbox + unpushed commits)
- `web` — primary-repo AI implementation lane (default preset: `pi`)
- `infra` — secondary-repo AI implementation lane (default preset: `claude`)
- `validate` — two panes: left for watcher/test runner, right for adhoc
- `ops` — two panes: top for health probe, bottom for log tail
- `docs` — synthesis + memory lane

Defaults are opinionated: the `coord` pane auto-runs `loop-digest.sh`
pointed at the project's state file.

For non-interactive callers, pass `--no-attach` so the session is created and
commands are seeded without requiring a terminal attach.

#### Worktree overrides

When you want the implementation lanes to run inside a git worktree (for a
parallel branch, a canary, or an isolated experiment) instead of the canonical
clone, pass:

- `--worktree-web <path>` — opens the `web` + `validate` panes in `<path>`
  rather than `--project-root`.
- `--worktree-infra <path>` — opens the `infra` pane in `<path>` rather than
  `--infra-root`.

`coord`, `ops`, and `docs` deliberately stay on the canonical roots — the
orchestrator state and prod-facing scripts should not shift with a branch
experiment. Both paths are validated up-front; a non-existent worktree fails
fast instead of silently `cd $HOME`-ing.

Equivalent env vars: `LOOP_WORKTREE_WEB`, `LOOP_WORKTREE_INFRA`.

#### Dry-run inspection

`--print-cmds` resolves preset + per-lane overrides + worktree paths and
prints the final assignment without touching tmux. Useful for diffing
`--preset pi-claude` against a hand-rolled `--web-cmd` / `--infra-cmd`
composition before launching.

#### Multi-harness lane composition (`--lane-config`)

Instead of (or in addition to) `--preset` and per-lane `--*-cmd` flags, a
project can declare lane composition in a YAML file:

```yaml
lanes:
  web:    { harness: pi,     model: "" }
  infra:  { harness: claude, model: "" }
  docs:   { harness: opencode, model: "" }
  ops-top:
    harness: shell
    cmd: watch -n 10 curl -sf https://example.com/healthz
```

```bash
loop-tmux --project myproj --project-root ~/myproj \
          --lane-config ./loop-config.yaml
```

Each lane resolves through `lib/lane-config-resolver.sh`, which consults
`lib/harness-registry.sh` for the per-harness invocation contract
(`launch_cmd`, `auto_approve_flag`, etc.). See
`examples/lane-config.example.yaml` for the full schema, and run
`./lib/lane-config-resolver.sh print-resolved --lane-config <path>` to
preview resolution standalone.

**Precedence** (last wins): preset < `--*-cmd` < lane-config. A per-lane
`--web-cmd` overrides that lane's preset default, and a `web:` block in your
YAML overrides both — so if you set both `--web-cmd "foo"` and a `web:` block,
the YAML wins. To beat the YAML for one lane, edit the YAML.

**Host overrides** are auto-merged: a file at `<path-base>.<hostname -s>.yaml`
beside your primary config is layered on top (lane-level replace). Use it
for per-machine drift without forking the default.

**Relative paths**: `cmd: scripts/foo.sh` resolves against
`--project-root` (loop-tmux passes it through via the
`LANE_CONFIG_PROJECT_ROOT` env var).

#### Health probes

- `--boot-check [secs]` — after lanes launch, wait `<secs>` (default 8) and
  inspect each pane's current process. If an AI lane fell back to a bare
  shell, the lane prints `FAIL` and the whole script exits non-zero (under
  `--no-attach`). Catches silent boot failures: invalid model id, missing
  harness binary, `--flag` mismatch. Uses `lib/harness-registry.sh`'s
  `harness_is_bare_shell_process` as the source of truth for "AI exited".
- `--auto-restart` — spawn a detached background watchdog
  (`lib/lane-health.sh`) that polls each AI lane every 30s and re-issues
  the launch command if the pane has fallen back to a bare shell. Logs
  one-line JSON restart events to `<state-dir>/lane-restarts.jsonl`.
- `--state-dir <path>` — where the watchdog stores its log + state.
  Default: `<project-root>/.loop/sessions/<session>`. Env override:
  `LOOP_TMUX_STATE_DIR`.

Tip: `--no-attach --boot-check` is a useful CI / smoke pattern — bootstrap
a session, prove the harnesses came up, exit. The session stays running
for the operator to attach later.

#### Dynamic lanes (runtime add/drop)

Beyond the fixed six windows, you can grow and shrink a **running** session —
useful when a coordinator (or the coord lane's own agent) decides mid-flight
that it needs another implementation lane, or wants to retire one.

```bash
# Add a lane: a new window 'web2' running amp (auto-approved) on a sub-package
loop-tmux add-lane --session myproj --window web2 \
    --harness amp --auto-approve --repo ./packages/api --role impl

# Or run an arbitrary command instead of a registered harness
loop-tmux add-lane --session myproj --window watch --cmd 'npm run test:watch'

# List the session's windows and their lane metadata
loop-tmux list-lanes --session myproj

# Retire a lane (only kills windows add-lane created, unless --force)
loop-tmux drop-lane --session myproj --window web2
```

`--session` defaults to the current session when run inside tmux, so an agent
in the coord lane can simply call `loop-tmux add-lane --window … --harness …`.
The harness (`pi | claude | opencode | codex | cursor-agent | hermes | droid |
forge | amp | openclaw | mprocs | shell`) is resolved through the same registry
as `--lane-config`, with `--model` and `--auto-approve` applied per the harness
contract.

Lane metadata is stored as tmux `@loop_lane_*` window options, so it lives and
dies with the window — there is no external state file to leak or reconcile.
`drop-lane` refuses to kill a base lane (coord/web/infra/validate/ops/docs)
unless you pass `--force`.

The `--auto-restart` watchdog also discovers dynamically added AI lanes each
cycle (`add-lane` windows tagged `@loop_lane` whose command is an AI harness)
and restarts them with the same per-lane failure cap; shell/watcher lanes are
left alone. `loop-dispatch` and `loop-lane-status` likewise accept a dynamic
lane's window name directly, so dynamic lanes are first-class across the
toolchain.

## Generic lane role contracts

These scripts intentionally stay substrate-level, but the lane names imply a
recommended operating model that has worked across projects.

- `coord` — coordinator / checkpoint lane. Owns sequencing, lane assignment,
  escalation, and human checkpoints. Should not become the default coding lane.
- `web` / `infra` — implementation lanes. Own narrow scoped work in the
  primary and secondary repo respectively. Avoid broad self-directed scope
  expansion without a checkpoint.
- `docs` — synthesis / reconciliation lane. Consolidates findings, drafts next
  prompts, and records working notes. Should not become the default patch lane.
- `validate` — proving lane. Runs repeatable checks and separates validation
  output from implementation chatter. Green checks are not the same as canary
  proof.
- `ops` — downstream-truth lane. Confirms what is actually live on the target
  system via health/log/curl/SSH checks. A deploy API 200 is not enough.

Recommended generic rules:
- keep one explicit coordinator lane
- keep synthesis separate from implementation
- keep proving separate from implementation
- if two lanes may touch the same files, declare sequencing first
- before ship/canary/"done", run a critique pass asking what assumptions are
  still unproven and what downstream state has not been verified

### `loop-dispatch.sh` — paste commands/prompts into a lane

Uses a paste-buffer-then-Enter sequence (rather than literal keystrokes),
including the `LOOP_DISPATCH_PASTE_DELAY` knob for slow AI composers.
Auto-detects the current tmux session if invoked from inside tmux; otherwise
requires `--session`. Supports `--window <session:window.pane>` for
direct-addressed panes that don't match the default lane map.

Named lanes now resolve dynamically via `tmux list-panes`, so dispatch is
robust to pane numbering differences like `.0` vs `.1` across tmux versions
or wrapper-driven session creation. A lane name that isn't one of the eight
fixed lanes is resolved as an `add-lane` window, so dynamic lanes can be
dispatched to (and classified by `loop-lane-status`) by their window name.

`--wait-ready` polls `loop-lane-status` until the lane is `idle` before sending,
so a dispatch can't race a slow-booting TUI (e.g. Claude Code's welcome screen
swallowing the first paste). `loop-tmux add-lane --wait-ready` takes the same
flag to block until the new AI lane is input-ready — so you can compose a lane
and dispatch to it in sequence without a fixed sleep.

Some harnesses render a product-specific home/footer string when sitting idle at
their launch screen. Set `LOOP_LANE_IDLE_HOME_PATTERN` to an extended-regex
alternation matching that string so `loop-lane-status` recognizes those lanes as
`idle` (e.g. `LOOP_LANE_IDLE_HOME_PATTERN='MyHarness ready|press / for menu'`).

### `loop-digest.sh` — ASCII digest of orchestrator state

Reads `orchestrator-state.json` (schema v2, see below) and the mailbox
directory. Prints:

```
════ orchestrator loops @ HH:MM ════
schema_version=2  session=…  updated=…
LOOPS:
  loop-6a    implement   dev            canary:test-target   Verify checkout flow
  …
════ latest 4 messages ════
  20260415-112233-web-to-infra.md  Need staging token for canary
  …
════ unpushed commits ════
  my-app            3 (dev vs origin/dev)
  my-app-infra      0 (main vs origin/main)
```

Handles both the minimum schema (just `loops`) and an extended shape that also
carries `lanes` with live pane tails — both render.

## Invocation examples

```bash
# Two-repo project: primary app + infra repo, with a health probe and log tail
loop-tmux \
  --project my-app \
  --project-root ~/code/my-app \
  --infra-root  ~/code/my-app-infra \
  --validate-cmd 'npm run test:watch' \
  --gateway-health-cmd 'curl -s https://app.example.com/healthz' \
  --log-stream-cmd 'kubectl logs -f deploy/my-app' \
  --preset pi-claude
```

```bash
# Single repo, monitor-only (no AI lanes — just digest + health + logs)
loop-tmux \
  --project my-app \
  --project-root ~/code/my-app \
  --gateway-health-cmd 'curl -s https://app.example.com/healthz' \
  --log-stream-cmd 'docker compose logs -f --tail=50' \
  --preset monitor
```

### Dispatching to a lane

```bash
# Inside tmux (session auto-detected):
loop-dispatch --mode text web "Audit the dashboard for stale state."

# Outside tmux (name the session):
loop-dispatch --session my-app --mode text infra "Run the migration smoke test."

# Directly address a window + pane:
loop-dispatch --window my-app:validate.1 "npm run typecheck"
```

## Schema expectations

`loop-digest.sh` and the default `coord` pane expect the state file to match
this minimum schema:

```json
{
  "schema_version": 2,
  "updated_at": "<iso8601>",
  "loops": {
    "<loop_id>": {
      "name": "…",
      "status": "spec|plan|implement|verify|canary|shipped|reverted|blocked",
      "branch": "…",
      "deployed_to": ["…"],
      "canary_target": "…",
      "blast_radius": "…",
      "reconciliation_stance": "now|later|out-of-scope",
      "artifacts": { "spec": "…", "plan": "…", "verify_record": null,
                     "canary_record": null, "retro": null, "decision_record": null },
      "commits": ["…"],
      "updated_at": "<iso8601>",
      "updated_by": "…"
    }
  }
}
```

Mailbox messages must be `<mailbox-dir>/YYYYMMDD-HHMMSS-<from>-to-<to>.md`
with `subject:` in the frontmatter for the digest to extract a summary.

## Recommended session artifacts

For non-trivial multi-lane work, prefer durable artifacts over pane scrollback.
A lightweight convention that works well across projects is:

- `brief.md` — objective, blast radius, target repo(s), and what counts as done
- `working-note.md` — synthesis-in-progress from the docs lane
- `checkpoint.md` — current state + explicit decision needed from coord/human
- `summary.md` — what changed, what was proved, what remains
- `events.jsonl` — optional append-only handoff / checkpoint log

These are conventions, not hard requirements. Projects may already satisfy
these via loop charters, plans, verify records, retros, or other repo-specific
artifacts.

## Generic checkpoint / critique pattern

A simple pattern that complements the scripts well:

1. **Bootstrap** — create lanes and assign narrow responsibilities.
2. **Observe** — implementation, synthesis, validation, and ops collect signal.
3. **Checkpoint** — coord compares the lane outputs and narrows the next step.
4. **Critique** — ask what assumptions are still unproven, what downstream
   state is only inferred, and what would falsify confidence fastest.
5. **Advance or stop** — only after the checkpoint decides the next loop,
   canary, or follow-up action.

Projects with stronger lifecycle docs should treat this as a substrate-friendly
pattern, not a replacement for their own phase/gate contract.

## Decision records (MADR)

For decisions with lasting or irreversible consequences — choosing a queue,
changing the auth model or deploy architecture, a database-migration strategy,
swapping a vendor, accepting production risk — a lightweight ADR gate keeps
"safe scaling" honest without turning every commit into bureaucracy.

`loop-adr` is a standalone helper (no ADR logic is baked into `loop-tmux`):

```bash
loop-adr new "Choose the job queue: Redis vs SQS"   # -> docs/adr/0001-….md (Proposed)
loop-adr list                                        # the decision ledger
loop-adr accept 1                                    # gated — see below
```

**The gate.** `loop-adr accept <id>` refuses unless the ADR's frontmatter links
a `verify_record` **and** a `rollback` (or `canary_record`). Accepted decisions
must be both *proved* and *reversible*. Agents draft ADRs; a human/coord runs
`accept`.

| Change | ADR |
| ------ | --- |
| Normal bug | none |
| Reversible implementation choice | checkpoint note, no ADR |
| Lasting / irreversible / risky decision | `Proposed` ADR before implementation; `Accepted` (+ verify + rollback) before ship |

**Adapted to dynamic lanes.** Rather than committing to a fixed decision profile
up front, the coordinator opens decision-review lanes on demand when a lasting
decision surfaces, then retires them once the ADR is drafted:

```bash
loop-tmux add-lane --window opt-app   --harness pi     --role option-auditor-app-code
loop-tmux add-lane --window opt-infra --harness claude --role option-auditor-infra-risk --auto-approve
loop-tmux add-lane --window madr      --harness pi     --role madr-drafter
# … agents explore, validate proves, ops confirms, docs drafts the MADR …
loop-tmux drop-lane --window opt-app                   # clean up when done
```

A static `examples/madr-decision.lane-config.yaml` profile is also provided for
those who prefer to launch the whole decision layout at once.

**Digest ledger.** `loop-digest` (the coord pane) shows a `decisions (MADR)`
block scanning `<project-root>/docs/adr` (override with `--adr-dir`, env
`LOOP_DIGEST_ADR_DIR`), so the decision ledger sits alongside loops, mailbox,
and unpushed commits.

**Artifact convention.** A loop in the state file may link its decision via a
`decision_record` field in its `artifacts` object (next to `verify_record`,
`canary_record`, `retro`), pointing at the ADR path.

The shape that works: *agents explore → validate proves → ops confirms reality →
docs drafts the MADR → coord/human accepts → digest shows the ledger.* Don't make
ADRs mandatory for every loop, and never let an agent lane mark a decision
Accepted.

## Compiled coordinator: ops-wiki + scripts/

The coordinator's memory lives on disk, not in an agent's context window.
`ops-wiki/` is a small LLM-maintained wiki (index, append-only log, one page
per lane and loop, a compiled `checkpoint.md`) governed by single-writer
partitions in AGENTS.md: implementation lanes write only their own lane page
and mailbox messages, the docs lane is the sole compiler of loops/index/log,
and everything below the `<!-- coord-decisions -->` marker in checkpoint.md
is coordinator-owned. Mailbox messages are acked by moving them to
`.loop/messages/processed/`, so "pending" is always real.

The `scripts/` helpers operate that convention:

- `loop-wiki-pending.sh [--quiet]` — pending-mailbox summary (bare integer
  with `--quiet`, for prompts and cron).
- `loop-checkpoint.sh (--print | --dispatch [lane]) [--header-file p]` —
  assembles the stateless coordinator prompt (fixed header + checkpoint.md +
  index.md + pending summary). Prompt size is independent of session age;
  byte/token counts go to stderr with a warning above 24k tokens.
  `--header-file` swaps the header so an external engine can demand a
  side-effect-free decision block instead.
- `loop-task-lint.sh` — validates the tasks-as-files convention
  (`tasks/T<NNNN>-<slug>.md`, frontmatter, required sections, DAG of
  `depends_on`, status↔location).
- `loop-metrics.sh [--log]` — checkpoint tokens, pending count, lane
  restarts/giveups, ingest/lint/checkpoint counts; `--log` appends one
  metrics line to the wiki log. Feeds the experiment keep/discard gate:
  changes to the orchestration rules are kept only if these numbers do not
  regress over three cycles (AGENTS.md "Experiment protocol").
- `loop-wiki-lint.sh (--print | --dispatch)` — nightly wiki health pass:
  shuffled batches, five finding categories (CONTRADICTION / STALE / ORPHAN /
  MISSING-LINK / SUSPECT-INSTRUCTION), injection-aware quarantine; only the
  safe categories auto-fix, the rest queue for human review.
- `loop-jira-sync.sh (pull|push|both)` — thin shim onto `loop-pm sync
  --adapter jira`; exits 64 with a hint when the Python layer is absent.

## Preset reference

| Preset            | coord         | web   | infra  | validate-left | ops-top       | ops-bottom      |
| ----------------- | ------------- | ----- | ------ | ------------- | ------------- | --------------- |
| `pi-claude`       | loop-digest   | pi    | claude | --validate-cmd| --gateway-…   | --log-stream-…  |
| `all-pi`          | loop-digest   | pi    | pi     | --validate-cmd| --gateway-…   | --log-stream-…  |
| `all-claude`      | loop-digest   | claude| claude | --validate-cmd| --gateway-…   | --log-stream-…  |
| `validation-only` | —             | —     | —      | --validate-cmd| —             | —               |
| `monitor`         | loop-digest   | —     | —      | —             | --gateway-…   | --log-stream-…  |

Per-lane flags (e.g. `--web-cmd`, `--ops-top-cmd`) override the preset
defaults. Unset lanes stay empty — the window/pane is still created, just
idle.

## The Python layer: loop-engine, loop-deck, loop-pm

The bash scripts above are the substrate and need nothing but bash ≥ 3.2,
tmux ≥ 3.3, and python3 — that never changes (CI runs `make check` with no
Python packages installed to prove it). The Python package layers
orchestration on top; `substrate.py` is its single subprocess boundary, so
the whole layer tests without tmux against fakes-on-PATH.

### `loop-engine` — the orchestration loop as a program

One cycle: observe lanes → ingest pending mail → assemble the compiled
checkpoint prompt (`loop-checkpoint.sh --print --header-file …`) → call a
**swappable headless brain** (`claude -p`, `codex exec`, `amp -x`, … via the
registry's `oneshot_template`) → parse a fenced ` ```decision ` block
(critique + up to 8 typed actions) → **gate** each action by its *shape*
(safe / destructive / blocked — `loop-adr accept` and the coord lane are
never automatable; any action that runs a raw command — `mode: command`
dispatch/steer, or `add_lane` with a `cmd` — is destructive by shape and
needs a human, not just by a text blocklist) → execute through
`loop-dispatch` / `add-lane` / `drop-lane`. Decisions queue in
`.loop/sessions/<s>/engine/pending-decision.json` until resolved:

```bash
loop-engine --session s once --approval manual   # one cycle, decision queues
loop-engine --session s status                   # read the pending decision
loop-engine --session s approve d-…              # human gate; executes + archives
loop-engine --session s watch                    # the daemon
loop-engine --session s restart [--timeout 60]   # singleton-safe stop+start
```

`watch` polls and runs cycles on triggers — checkpoint interval, new mailbox
file, a lane finishing work (working→idle), state-file change, `cycle-now`,
or an **ask reply**: steered lanes can be told to answer via a mailbox
message (`subject: re:<ask-id>`), and the reply itself triggers the next
cycle. Debounce, a single-in-flight decision invariant, a brain-calls-per-hour
budget, pid-singleton, and pause/resume keep it boring. Ingest runs either by
nudging the docs lane or fully headless (a one-shot agent performs the
docs-lane protocol). Every step lands in an append-only `events.jsonl`;
brain transcripts are kept for provenance and written incrementally, so they
are live-tailable while the brain runs; approved decisions are filed below
the checkpoint marker, so the wiki accumulates the system's reasoning.
`brain.stream: true` streams claude token events into the live transcript —
the deck's `b` panel watches it.

`loop-engine improve` adapts *Self-Harness* (arXiv:2606.09498) to this
stack: it mines weakness clusters from the engine's own traces (brain
failures, rejected decisions, action failures, lane instability, ask
timeouts), has the brain propose at most three minimal edits to the
**declared surfaces only** (the checkpoint header, append-only AGENTS.md
experiment subsections, engine-config recommendations), and files them as
proposals — nothing applies without `--apply N`, and applied experiments
live or die by the `loop-metrics.sh` non-regression gate. Zero mined
weaknesses → zero proposals, by design.

The miner also learns from **human interventions**, not just internal
failures. The highest-leverage signal, `human:unsolicited-steer`, counts
unsolicited mailbox messages to the coordinator (`…-to-coord.md`, not from
coord, subject not `re:`) — a human steering the coordinator is the
coordinator failing to act autonomously, so the proposal teaches it to
self-discover that next step. `latency:regression` catches slow-but-succeeded
drift (brain-call→decision timings trending up) before it becomes a terminal
timeout, and `crash:<component>` mines unhandled engine cycle crashes plus
deck crashes (via a deck-owned `deck-crash.log`) under a report-only `none`
surface — a crash needs a code fix, so it is surfaced, never auto-applied.
Brain failures are classified (`quota` / `timeout` / `exit`) so a quota
lockout is never misdiagnosed as a slow generation. Operationally, the watch
loop applies **quota-aware backoff** (suppresses only the brain until the
limit resets, observation continues), a **stale-daemon guard** (`status`
warns when a reinstall landed after the daemon started — run `restart`), and
`restart` is **singleton-safe** (confirms the old daemon is dead before
starting a new one, never two at once).

### `loop-deck` — the flight deck

An interactive Textual TUI over the same files and CLIs: fleet table (lane,
harness, role, live status, restarts), loops, mailbox, the pending-decision
queue with `y`/`N` approve/reject, ADR ledger with human-gated accept, lane
detail with live pane tail. Actions: steer (`s`), dispatch (`d`), add-lane
(`n`), drop-lane (`x`, typed-name confirm for base lanes), jump-to-tmux
(`g`), run checkpoint (`c`), pause/resume the engine (`p`). The deck is a
strict non-writer — every mutation shells the same audited CLIs a human
would — and degrades to a live dashboard when the engine is off. Run it in
the coord pane (`--coord-cmd 'loop-deck --project-root . --session <s>'`)
or standalone.

### `loop-pm` — PM tools as plugins

Adapters are discovered via the `loop_orchestrator.pm_adapters` entry-point
group, so third parties ship them as pip packages. `tasks/` files are the
source of truth with a strict **file-wins** conflict rule; the PM tool is a
sync target, never the brain. Jira ships in-repo as the reference adapter
(REST v3 + Agile 1.0, env-only credentials: `JIRA_BASE_URL`, `JIRA_EMAIL`,
`JIRA_API_TOKEN`; optional scrum context: `JIRA_PROJECT_KEY` for issue
creation/epic search, `JIRA_BOARD_ID` for sprint lookups — `--project` /
`--board` override). `JIRA_BASE_URL` **must** be `https://` and carry no
embedded credentials (a `user@host` authority is rejected), so the API token
is never sent over plaintext or to a poisoned host; set
`JIRA_ALLOW_INSECURE_BASE_URL=1` to waive the https requirement for localhost
dev only. With zero adapters configured the engine behaves identically — PM
integration is optional by construction.

```bash
loop-pm list-adapters
loop-pm sync --adapter jira pull --dry-run
loop-pm sync --adapter jira push [--project K] [--epic KEY] [--sprint ID|active] [--board B]
loop-pm jira ensure-epic --name N [--project K]            # prints the epic key (found or created)
loop-pm jira sprint-status [--board B]                     # active sprint id/name, or 'no active sprint'
loop-pm jira move-to-sprint (--sprint ID | --active) KEY...
loop-pm jira start-sprint (--sprint ID | --next | --create NAME) [--board B]
                          [--duration-days N] [--goal TEXT]
loop-pm jira complete-sprint (--sprint ID | --active) [--board B]
loop-pm jira complete-epic KEY        # transition the epic to Done (no auto-rollup)
loop-pm jira retro --epic KEY [--title T] (--body-file F | --body TEXT) [--as-issue]
```

`sync push` also CREATES issues: open/in-progress tasks without a `jira:`
key become issues in `JIRA_PROJECT_KEY` (under `--epic` if given); the new
key is written back into the task frontmatter — the one sanctioned file
write — and `## [date] sync | <key> created from <task-id>` is appended to
ops-wiki/log.md. `retro` posts an ADF comment on the epic by default;
`--as-issue` creates a Task labeled `retrospective` instead. Epic linking
uses the team-managed `parent` field; company-managed projects reject it,
in which case the issue is created without the link and a warning is
surfaced (the epic-link customfield id varies per site — never guessed).
`start-sprint` activates a sprint (start now-UTC, end after
`--duration-days`, optional `--goal`; `--next` picks the board's earliest
future sprint, `--create NAME` makes one first); `complete-sprint` closes
the active sprint — Jira moves its incomplete issues back to the backlog,
and the CLI says so. Ceremony *decisions* stay with the human; these verbs
just make the full scrum cadence operator-invokable end-to-end.

### Install / develop

```bash
make install-python      # uv tool install (pip --user fallback; Python >= 3.10)
uv sync --group dev      # development
make check-python        # ruff + pytest (268 tests)
make check-all           # bash substrate + python layer
make install-skill       # link skills/loop-orchestrator into ~/.claude/skills
                         # (other harnesses: SKILLS_DIR=~/proj/.pi/skills etc.)
```

The repo ships an **operator skill** (`skills/loop-orchestrator/`) so any
agent harness that reads `SKILL.md` knows how to drive the whole system —
boot sessions, dispatch and steer lanes, run engine cycles, approve at the
gate, use improve and the PM adapters — with the safety rules (never
`--force`, never automate `loop-adr accept`, at-most-once dispatch) baked
in. Progressive references cover the substrate CLIs, the engine contract,
and the ops-wiki/mailbox/tasks conventions.

### Example prompts: adopting it in your project

With the skill installed (`make install-skill`), paste one of these into
your coding agent — Claude Code, pi, codex, or anything else that loads
`SKILL.md`. Each states the objective, the constraints, and where the human
gate sits; the skill supplies the mechanics.

**1. Wire it into an existing project**

> Using the loop-orchestrator skill: set up loop-orchestrator for this repo.
> Install the bash CLIs and Python layer (`make install` +
> `make install-python` from the loop-orchestrator checkout). Create
> `.loop/orchestrator-state.json` (schema v2, empty loops), the
> `.loop/messages/` mailbox, and `ops-wiki/` + `AGENTS.md` per the
> conventions reference. Write a `lane-config.yaml` for this stack —
> web=claude as the implementation lane, validate-left running our test
> watcher, ops-top probing our healthcheck URL — plus an `engine:` section
> with `approval_mode: manual` and a claude brain. Boot with
> `--no-attach --boot-check`, prove the JSON surfaces parse against the live
> session, then give me the attach command and STOP — dispatch no work yet.

**2. Run a real objective through the loop**

> Using the loop-orchestrator skill: session `myapp` is configured. Drop a
> mailbox message to coord with this objective: "<one paragraph: what,
> blast radius, what counts as done>". Start `loop-engine watch` and put
> `loop-deck` in the coord pane. When the first decision queues, summarize
> the brain's critique and proposed actions for me and WAIT — I approve
> from the deck. Never approve anything yourself; never steer shell lanes.

**3. Greenfield project, orchestrated from day one**

> Using the loop-orchestrator skill: create a new project at `~/code/<name>`
> (git init) and wire loop-orchestrator in before any code exists — state
> file, mailbox, ops-wiki, AGENTS.md, tasks/ convention, lane-config.yaml
> with an `engine:` section in manual mode. Seed `tasks/T0001-<slug>.md`
> with our first objective: "<objective>" — fully self-contained per the
> task-file convention and passing `scripts/loop-task-lint.sh`. Boot,
> verify, stop.

**4. Connect the task files to Jira**

> Using the loop-orchestrator skill: connect this project's `tasks/` to
> Jira. `JIRA_BASE_URL`, `JIRA_EMAIL`, and `JIRA_API_TOKEN` are in my
> environment. Run `loop-pm sync --adapter jira pull --dry-run` first and
> show me what would be created; if I say go, run the real pull, validate
> with `scripts/loop-task-lint.sh`, and enable `pm: {adapters: [jira]}` in
> the engine config. Task files win every conflict — never let the remote
> overwrite one.

**5. Let the system critique itself**

> Using the loop-orchestrator skill: session `myapp` has a few days of
> traffic. Run `loop-engine improve`, walk me through each filed proposal
> (surface, mined signature, the edit itself), and apply NOTHING without my
> explicit ok per proposal. If one targets the checkpoint header, show me a
> diff against the current header first. Remind me that applied experiments
> are judged by the `loop-metrics.sh` non-regression gate after three
> cycles — and revert any that regress.

Engine behavior is configured in an optional `engine:` section of
`lane-config.yaml` — invisible to the bash resolver, host-overridable, all
keys optional (see `examples/lane-config.example.yaml` for the annotated
defaults: approval mode, intervals, brain harness/budget, ingest mode,
destructive-action caps, PM adapters).

CI runs three jobs: the Python-free `make check` (the degradation contract),
ruff + pytest + boundary greps, and a real-tmux smoke that exercises the
CONTRACT.md JSON surfaces against a live session.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
