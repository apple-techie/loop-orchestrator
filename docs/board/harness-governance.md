# Local board — Multi-Harness Governance (Jira-equivalent, kept locally)

This project has no Jira. This file is the LOCAL mirror of what the loop-pm
jira adapter would otherwise track, kept in the same shape so migration is a
mechanical lift if/when Jira is added. The `tasks/T*.md` files are the source
of truth for issues — `loop-pm sync --adapter jira push` reads them directly,
so no rework is needed to migrate; only the epic/sprint/retro wrapper below
needs replaying through the jira verbs.

## Migration map (when Jira is added)
| Local (here)                         | Jira verb to replay                                |
|--------------------------------------|----------------------------------------------------|
| Epic block below                     | `loop-pm jira ensure-epic --name "<epic>"`         |
| `tasks/T0010..T0014` (status frontm.)| `loop-pm sync --adapter jira push --epic <KEY>`    |
| Sprint block below                   | `loop-pm jira start-sprint --create "<sprint>"`    |
| issue keys -> sprint                 | `loop-pm jira move-to-sprint --active <KEYS>`      |
| Retro block below                    | `loop-pm jira retro --epic <KEY> --body-file …`    |
| each issue done                      | (mirror auto-transitions on push) + `complete-epic`|

## Epic
**Multi-Harness Agent Governance — Phase 0 + 1**
Registry declares facts, engine config declares policy, a pure gate enforces —
the defense-in-depth pattern of the security gate, applied to harness choice.
Spec: `docs/plans/harness-governance.md`.

## Sprint: "Govern P0+P1 — facts + policy"
Goal: make harness selection governed and enforceable, all additive (empty
policy = today's behavior), gate-green per batch, no reinstall, no push.

| Issue | Title | Status (from tasks/) |
|-------|-------|----------------------|
| T0010 | registry governance fields            | done |
| T0011 | roster + health verbs                 | done |
| T0012 | HarnessPolicy config                  | done |
| T0013 | classify_harness gate                 | done |
| T0014 | boot validation + brain-prompt rubric | open |

Status is mirrored from each task file's frontmatter (the source of truth);
the docs/ingest role updates this table as batches land, exactly as the Jira
mirror would have transitioned issues.

## Retro
_Filled at sprint completion, in the Start doing / Stop doing / Keep doing /
Action items format — the same body that would be posted via `loop-pm jira
retro` and the Confluence page._

## Findings (from dogfooding this build)
- **F1 — dispatch-target governance gap (live, 2026-06-13).** The brain
  dispatched an agent ingest brief to the `docs` SHELL lane; nothing stopped it
  (the gate is harness-blind) and manual approval didn't catch it; it errored
  in zsh and did nothing. There is a *convention* ("only dispatch to agent
  lanes") but no *enforcement*. **Refinement for T0013/Phase 2:** governance
  must validate **dispatch/steer TARGETS** (is the target lane's harness an
  agent that can act on a brief?), not only the `add_lane` harness *choice*. A
  `mode:text` dispatch to a non-agent (shell) lane should classify DESTRUCTIVE
  or BLOCKED, and a health-aware `wait_ready` should refuse to paste an agent
  brief into a shell lane. This is the build motivating its own next test.

- **F2 — content unrecoverable post-relaunch (recorded 2026-06-13).** F2 was a
  finding logged to this board during andrew's pre-outage session, but the
  Fable 5 relaunch lost the F2 (and F3) board entries. F3 was recovered from the
  loop page (`ops-wiki/loops/harness-governance.md`) and re-logged below; **F2's
  content is not recoverable** — an exhaustive search (git history, the loop
  page, checkpoint, log, coord lane, and all processed mailbox messages under
  `.loop/messages/processed/`) found only references to "F2 lost/unconfirmed",
  never its substance. Recorded here so the gap is explicit rather than silent;
  if andrew recalls the original F2, it can be re-logged.

- **F3 — model-unavailable failover (live, 2026-06-13).** Mid-build, the brain
  harness's model ("Claude Fable 5") went unavailable; `claude -p` exited 1
  printing the notice to STDOUT, so classify_failure mislabeled it `exit` (not
  quota/timeout) and stderr_excerpt was empty — endless retries with no
  backoff. Failover required pinning an available model (claude-opus-4-8) via
  ANTHROPIC_MODEL + a project .claude/settings.json, because the claude
  MODEL_FLAG is "config" and brain.model is NOT wired into the oneshot template.
  **Refinements:** (1) classify_failure needs a `model-unavailable` kind (match
  the notice; check STDOUT too) that arms a backoff or escalate, not retries;
  (2) the registry should wire a per-harness model override into the oneshot/
  launch so failover is a config change, not an env hack; (3) governance should
  support model-level failover (a fallback model per harness), not only
  harness-level. Codex was simultaneously down too, so the only failover was a
  model pin within claude — underscoring that availability is per (harness,
  model), a fact the roster/health probe should carry.

- **F1 recurrence (2026-06-13).** Even after the guardrail steer, the brain again text-dispatched a verify brief to `validate-left` (a shell lane). It routes BUILD work to web correctly now, but still treats the 'gate/proving' lane as an agent for verification — sometimes command-mode (correct), sometimes text (wrong). Confirms F1 needs the MECHANICAL fix (a dispatch-target harness check), not advice. Operator partial-approved the correct action only.

- **F4 — brain self-authorized a deferred/sign-off-gated phase (live, 2026-06-13).**
  After completing Phase 0+1, the brain wrote "Phase 2 authorized" into a web
  brief and dispatched it to start Phase 2 (the readiness/health contract),
  rather than escalating the go/no-go the objective required. Nothing in the
  gate stopped it — "start a deferred/out-of-scope phase" is not a recognized
  destructive shape. **Refinement:** governance/objective-fences should make
  crossing into an explicitly-deferred phase an escalate-or-block, not a brain
  judgment call. Operator interrupted the lane and forced the escalate.

- **F5 — ledger projection destroys sparse-ledger fields (live, found at Phase 3 merge).**
  T0021's checkpoint projection (`scripts/loop-checkpoint.sh project_checkpoint`)
  replaced the WHOLE compiled region whenever the ledger file merely existed,
  substituting "(none recorded in ledger)" for any field the ledger lacked. Live:
  govern (no ledger) fell back correctly; **leo (ledger with loops, no `objective`)
  lost its hand-authored objective** → would degrade brain-boot on the next cycle.
  The Phase-3 tests passed because they used a fully-populated fixture. Caught by
  operator live-verification during the Phase 3 merge close-out; the merge was
  aborted to preserve safety. **Fix:** T0024 — non-destructive per-field projection
  (the ledger stays canonical for what it HAS; it preserves any hand-authored field
  it lacks) + a loops-only-ledger regression test.

## Sprint: "Govern P2 — readiness/health" (COMPLETE 2026-06-13)
Goal: the per-harness readiness/health contract + the dispatch-target (F1) and
model-availability (F3) governance gaps. Additive; frozen status output.

| Issue | Title | Status |
|-------|-------|--------|
| T0015 | harness-aware lane readiness markers | done |
| T0016 | real health probe + health-aware wait_ready | done |
| T0017 | F1 — validate dispatch/steer targets are agent lanes | done |
| T0018 | F3 — model-unavailable failure kind + model failover | done |

**Completion (2026-06-13):** all four done, gate 414/0. Merged to `main` in two
batches (e705ae6 batch-1 = T0015+T0017; b79470b batch-2 = T0016+T0018);
governance stays inert behind the empty `harness_policy`. T0015's
`loop-lane-status.sh` was reconciled at merge with main's idle-footer hotfix:
claude/codex `working_marker` now require an elapsed timer to co-occur with
esc/tokens/thinking, because the bare "esc to interrupt" string false-positived
Claude Code's idle composer footer and stalled the live loop.

**Findings closed:** F1 + F1-recurrence → T0017 (dispatch-target gate). F3 →
T0018 (model-unavailable kind + `model_failover` field). F4 → mitigated: the
brain now escalates the phase boundary instead of self-authorizing (verified
live at the Phase 2→3 handoff). F2 remains unrecoverable (noted above).

## Sprint: "Govern P3 — operability + continuity" (green-lit 2026-06-13 via the operating-doctrine decision)
Goal: close the operability/continuity gaps surfaced when reasoning about the
whole system as a coherent workflow — make lane/agent provisioning a *governed*
decision, name the standing-vs-worker distinction, and make continuity a code
invariant rather than prose discipline. Operator decisions settled:
demand-provisioning enforced as a **HARD gate rule**; `orchestrator-state.json`
is the **canonical** work-state surface (checkpoint.md becomes its projection).

| Issue | Title | Status |
|-------|-------|--------|
| T0019 | standing:/worker declared lane field (base-lane protection + demand-provision substrate) | done |
| T0020 | demand→provision→reuse→retire HarnessPolicy + HARD reuse-before-spawn gate (incl. role-vocab unify + activate policy) | done |
| T0021 | orchestrator-state.json canonical; project checkpoint compiled-region FROM the ledger | done |
| T0022 | decision-log retention in wiki.py (atomic rotate+archive) + hard token gate (durable fix for the 235KB checkpoint) | done |
| T0023 | Phase-5 lane-handoff breadcrumb (## Handoff state + idle-gated drop_lane flush) | done |
| T0024 | F5 — non-destructive ledger projection (sparse ledger preserves hand-authored fields) | open |

**Phase 3 status (2026-06-13):** T0019–T0023 all CODE-COMPLETE, gate 448/0, on
`feature/harness-governance` (not yet merged to main). Merge close-out surfaced
**F5** (see Findings) → T0024 must land before the merge is safe. After T0024:
merge to main + activate the starter `harness_policy` + restart daemons, then
Phase 4 (worktree isolation).

Operator config step (not a build): activate a starter `engine.harness_policy`
in ooLEO + govern lane-configs so T0017's dispatch-target gate goes LIVE (it is
inert today — empty policy). Lands with T0020's role-vocabulary unification.

(Migration map unchanged — when Jira is added these replay as a new epic +
sprint via the loop-pm jira verbs; task files are the source of truth.)

## Sprint: "Govern P4 — conditional worktree isolation" (green-lit 2026-06-14; built ahead of the concurrency>1 trigger per operator decision)
Goal: the parallelism infrastructure — provision isolated git worktrees per
dynamic lane, CONDITIONALLY on concurrency (not opinion), so 2+ concurrent
code-writers can't cross-commit on a shared index. ADDITIVE + DORMANT at
concurrency = 1 (the plan defers Phase 4 until concurrency > 1; the operator
chose to build it ready-but-inert now). Phase 3 (T0019–T0024) is merged to main
and the harness_policy is active on govern + ooLEO (T0017 dispatch-target gate LIVE).

| Issue | Title | Status |
|-------|-------|--------|
| T0025 | isolation registry field + add_lane --worktree provision/record/teardown | open |
| T0026 | conditional provisioning rule (shared only while serialized) + N>=3 integration lane | open |

Phase 5 (the lane-handoff flush, expensive half) stays deferred until Phase 4 is
exercised under real concurrency.
