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
| T0010 | registry governance fields            | open |
| T0011 | roster + health verbs                 | open |
| T0012 | HarnessPolicy config                  | open |
| T0013 | classify_harness gate                 | open |
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
