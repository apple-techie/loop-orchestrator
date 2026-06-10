# ops-wiki log

Append-only. Entries start `## [YYYY-MM-DD] <type> | <title>`. Never edit or reorder past entries.

## [2026-06-10] init | ops-wiki bootstrapped
Skeleton created per tasks/T0001-bootstrap-ops-wiki.md: index, log, checkpoint,
lane pages (coord, web, infra, validate, ops, docs), empty loops/ and decisions/.
AGENTS.md schema of record added at repo root.

## [2026-06-10] task | T0001 done
ops-wiki bootstrapped and verified in a Claude Code session; T0001 archived.

## [2026-06-10] schema | checkpoint.md ownership amended
T0002: docs lane now compiles checkpoint.md (compilation is docs' job); coord
owns only the decision-notes section below the `<!-- coord-decisions -->`
marker line. Docs recompiles must preserve the marker and everything below it
byte-for-byte. See AGENTS.md "Ingest protocol".

## [2026-06-10] ingest | 20260610-130500-web-to-docs.md
T0002 acceptance fixture. Created loops/loop-t0002-test.md (status implement,
branch feature/t0002-test), added index entry. Moved to processed/.

## [2026-06-10] ingest | 20260610-131200-validate-to-docs.md
T0002 acceptance fixture. loops/loop-t0002-test.md updated: validation green,
status implement -> verify. Moved to processed/.

## [2026-06-10] ingest | state sync
Diffed orchestrator-state.json loops against loop pages: loop-t0002-test is
verify (updated_at 2026-06-10T13:15:00Z, updated_by validate), matching the
page after the mailbox pass. No reconciliation needed; state citation added
to the page.

## [2026-06-10] ingest | test artifacts removed
T0002 acceptance pass verified; fixture retired: loops/loop-t0002-test.md
deleted, index entry reverted, fabricated loop removed from
orchestrator-state.json, checkpoint.md recompiled (coord-decisions section
preserved byte-for-byte). The two processed test messages stay in
.loop/messages/processed/ as worked examples.

## [2026-06-10] task | T0002 done
Ingest protocol + processed/ ack convention in AGENTS.md, coord-decisions
marker in checkpoint.md, scripts/loop-wiki-pending.sh added; acceptance ingest
pass run on 2 fixture messages and verified; T0002 archived.

## [2026-06-10] schema | coordinator contract added
T0003: AGENTS.md gains "### Coordinator contract" — coord is per-checkpoint
and stateless, boots from the constant-size compiled context assembled by
scripts/loop-checkpoint.sh, files all durable reasoning back, and writes only
its coord-decisions section of checkpoint.md plus appended decision notes on
loop pages (amending the docs-only ops-wiki/loops/ partition for appends).

## [2026-06-10] task | T0003 done
scripts/loop-checkpoint.sh added (--print / --dispatch [lane] / --header-file,
constant-size coord boot prompt, bytes+~tokens on stderr, 24k-token warning)
plus AGENTS.md "Coordinator contract"; verified and archived.

## [2026-06-10] schema | task-files convention added
T0004: AGENTS.md gains "### Task files" — tasks/T<NNNN>-<slug>.md is the
source of truth for atomic work items (Jira demoted to a sync target via
scripts/loop-jira-sync.sh, file wins on conflict); frontmatter, required
body sections, status<->location invariant, and acyclic depends_on enforced
by scripts/loop-task-lint.sh.

## [2026-06-10] task | T0004 done
scripts/loop-task-lint.sh (lints tasks/ + tasks/archive/, per-file findings,
non-zero exit) and scripts/loop-jira-sync.sh stub (pull/push/both contract,
exit 64) added; AGENTS.md "### Task files" appended; verified against the
live task pack and a broken /tmp fixture; T0004 archived.

## [2026-06-10] schema | substrate contract v1 (CONTRACT.md)
Machine-readable surfaces added additively: loop-lane-status --json/--all/
--print-target, loop-digest --json, loop-tmux list-lanes --json,
loop-dispatch --interrupt, harness-registry oneshot_template + oneshot verb.
All pre-existing outputs byte-identical (regression-diffed). scripts/ folded
into make check.

## [2026-06-10] schema | loop-engine landed (Python layer)
Deterministic engine over the substrate: stateless brain checkpoints via
oneshot templates, decision contract v1 (fenced decision YAML), gate
safe/destructive/blocked, pending-decision CAS + human approve/reject CLI,
events.jsonl audit. Live demo gate passed on a real tmux session. Engine
state lives in .loop/sessions/<s>/engine/; coord decisions file below the
checkpoint.md marker.

## [2026-06-10] schema | loop-deck landed (Textual flight deck)
Interactive non-writer deck: fleet/loops/mailbox/decision-queue/ADR views,
approve/reject, steer/add/drop lanes, jump-to-tmux. 132 tests green across
the Python layer. README + CONTRACT.md updated.
