# Operating doctrine — running a portfolio of loops

The engine already bought durable continuity (stateless one-shot brain,
ledger-canonical checkpoint, graceful degrade, singleton + restart, lane-handoff
breadcrumbs — proven live through a dual-vendor outage). So scaling is now a
**policy** problem, not an engineering one. This is how to run many loops without
fragmenting.

## The barbell portfolio

Run a **wide base of cost-capped AUTO janitors** + a **thin top of 1–3 MANUAL
high-stakes loops**, with **almost nothing in the half-watched middle**. A
half-watched loop is a fragmentation trap: either tighten its policy + caps until
it is a safe janitor, or promote it to manual and commit the attention.

- **Janitors** (`config-templates/janitor.engine.yaml`) — lint / deps / metrics /
  doc-recompile / stale-branch. AUTO, cheap model, tight caps, worktree-isolated.
  Limited by how many you can glance at in a daily deck pass, not by compute.
- **High-stakes** (`config-templates/partner.engine.yaml`) — live partner/customer
  work, or anything touching a shared `origin/main`. MANUAL, opus brain, PM wired.
  Each consumes real per-decision attention; past ~3 at once your latency is the
  stall.

## The rules

1. **Operator attention is the only real ceiling** — compute is cheap to cap per
   brain (`brain.max_calls_per_hour`). Structure every loop to minimize
   attention-per-unit-value.
2. **Set `approval_mode` to blast radius, not to anxiety.** Reviewing a lint fix is
   the anti-pattern. Janitors auto; isolated worktree build loops auto-with-caps;
   shared-main / live work manual.
3. **Verification-before-trust BUYS autonomy.** The ADR gate refuses `accept`
   without a `verify_record` AND (`rollback` OR `canary`). Every autonomy expansion
   is conditional on a verification hook existing — no hook, no auto.
4. **Escalate ONLY judgment / scope / spend.** Everything deterministic-or-low-risk-
   and-verifiable runs autonomously. Mine your own `human:unsolicited-steer` mailbox
   messages via `loop-engine improve` — they are the loop telling you it should have
   self-acted.
5. **Two walls never tunable, even in full-auto:** targeting the `coord` lane, and
   any `loop-adr accept` (ADR acceptance is human-only, period).

## Mechanics (one loop = one `(project_root, session)`)

One tmux session + one singleton engine daemon (pid-file guarded) + one state dir
`.loop/sessions/<session>/engine/`. Run N loops via N distinct `--session` names.
Caps are PER-LOOP (`max_dispatches_per_cycle`, `max_lanes`, ≤8 actions/decision,
ONE in-flight decision = the serialization point). The brain budget is per-loop and
ADDITIVE across loops against the shared upstream account — only `cost_ceiling` /
`autonomy_cap` protect you, and only if you set them. Parallel git writers are
isolated by BRANCH (`--worktree` lanes), dormant at concurrency=1. Watch every loop
in one read-only view: `loop-deck --all --roots <r1,r2,…>`.

## Operational hygiene (the gotchas that bite)

- **Restart only via `bin/loop-restart <session>`** for any PM-syncing loop — it
  re-sources `~/.loop-secrets/<session>.env`, reinstalls, restarts, and asserts the
  PM adapter is live. A bare `loop-engine restart` from a fresh shell drops the env
  → the adapter silently goes unavailable → pull/push become no-ops.
- **Bare `loop-engine` / substrate commands default `project_root` to CWD** — always
  pass `--project-root <root>` (or run from it), especially for a worktree.
- **Single-pusher discipline** — when a loop shares `origin/main` with a human or a
  2nd agent, only one pushes at a time (the engine does not enforce it).
- **Seed `jira:` BLANK** for new tasks — let `loop-pm` push create + backfill the
  key; a guessed key 404s every push cycle forever.
- **A `stop` on a healthy-looking fleet is a suspected idle-stall** — the engine now
  re-probes before honoring it (B2), but treat it with suspicion.
- **Wind a finished/stuck loop down by PAUSE, not kill** — zero attention, zero
  compute, clean resume, tmux intact. Reserve teardown for certain-done.
