# PLAN — Multi-Harness Agent Governance for loop-orchestrator

A decision-ready planning artifact. Not code. Repo root: `/Users/andrewpeltekci/Documents/1_Projects/loop-orchestrator`.

## TL;DR

The substrate can already *run* 12 harnesses; it cannot *govern* them. Selection today is declarative-inert (`role:` is documentation) plus dynamic-unchecked (the brain's `AddLaneAction`), with the gate explicitly harness-blind (`gate.py` comment: "The gate does not carry the per-lane harness"). The fix is one coherent move repeated at three layers: **the registry declares facts, the engine config declares policy, and a pure gate function enforces it** — the exact defense-in-depth pattern the security gate already uses. Around that, two genuinely new mechanisms: **conditional worktree isolation** keyed on concurrency (not opinion), and a **wiki-native lane-handoff contract** that bounds the in-session loss a harness swap causes. Start tiny (declare facts, then enforce policy in the gate); defer worktrees and handoff until concurrency actually exceeds 1.

The single hardest truth, which all four facets independently reach: **every non-claude harness carries vendor drift** (this session's 8 codex fixes are the proof), so governance is not "pick the best tool" — it is **pricing the risk of a tool you understand poorly running unattended on risky work.** Drift must become declared, probeable registry data, not heuristics patched after they break in production.

---

## (A) HARNESS GOVERNANCE MODEL + SELECTION RUBRIC

### A.1 The model: policy-constrained (brain proposes, declarative policy constrains, pure gate enforces)

Three candidate models, decided:

- **Model A — Declarative** (operator's YAML maps role→harness): auditable, host-overridable, zero injection surface — but static, and neuters the whole point of dynamic `AddLaneAction`. **Verdict: source of truth for the policy table, not the runtime decision-maker.**
- **Model B — Dynamic** (brain picks via `add_lane`, status quo): maximally adaptive, the right *interface* — but unconstrained; nothing stops the brain routing high-risk infra to a high-drift headless harness, and the gate is harness-blind by construction. **Verdict: right interface, wrong trust model.**
- **Model C — Policy-constrained** (brain proposes → declarative policy constrains → pure gate validates/overrides): **RECOMMENDED.** Same layering as the security gate's docstring philosophy ("Classification is defense in depth on top of decision validation").

**Where the two governance facets disagree, and the pick:** the *governance* facet puts policy in a new `engine.harness_policy.roles` block keyed on the **lane role** (infra/product/search/synthesis). The *engine-operability* facet puts policy in a `HarnessPolicy` dataclass keyed on **capability tags + cost tier + autonomy class**, with the registry carrying those tag facts. These are not really in conflict — they are the *policy layer* and the *fact layer* of the same thing. **Pick: adopt both, with a clean split.** The registry declares per-harness **facts** (`capability_tags`, `cost_tier`, `autonomy_class`, `auth_requirement`, `health_probe`, drift markers); the engine config declares **policy** (`HarnessPolicy`: allow/deny, cost ceiling, autonomy cap, and a `role_tag_map` that connects the role facet's `infra/product/search` to the operability facet's `code/ops/research` tags). Roles map to allowed tag-sets; tags are a registry fact; the allow/deny/ceiling is policy. One model, two declaration sites, each owning what it legitimately knows.

**Enforcement is a pure gate function**, not a brain-prompt instruction. The reasoning is identical to the security gate's own: a *prompt* telling the brain "pick safe harnesses" is the free-text-blocklist equivalent — unenforceable, bypassable, and silently broken by drift. When the brain itself is a drifting non-claude harness (codex-as-brain, this session), the only boundary that holds is a mechanical one. The brain proposing a bad harness must be correctable exactly as a brain proposing a `coord` target is mechanically `BLOCKED` today.

### A.2 Override semantics (the load-bearing design choice)

For an `AddLaneAction`, a new `classify_harness(action, config, roster)` pass runs **above** the existing SAFE/DESTRUCTIVE/BLOCKED logic (preserving `blocked > destructive > safe`):

| Condition | Verdict | Mechanism |
|---|---|---|
| Harness in role allowlist (or no policy) | `SAFE`, unchanged | pass-through (= today's behavior when policy empty) |
| Harness denied, or unknown to roster | `BLOCKED` | mirrors the `coord`-target block |
| Not allowed for role, but role default exists | **rewrite** `action.harness` → role default, emit governance event, proceed | analogous to how the gate *upgrades* classification |
| Over cost ceiling / autonomy cap; or `auto_approve=True` on a harness whose `autonomy_class` exceeds the cap; or roster says `missing`/`unauthenticated` | `DESTRUCTIVE` (human approves) | reuses the existing approval ladder — no new approval machinery |
| **High-drift harness + unattended (`auto_approve`) + high-risk role** | `DESTRUCTIVE` | direct mechanical encoding of this session's 8-fix lesson |
| `cmd` present (raw process) | `DESTRUCTIVE` | already true via the existing shape rule; left intact |

The gate stays **pure** (no IO, no substrate). The "gate can't see the harness" limitation is resolved by reading `action.harness` off the action itself (confirmed present in `AddLaneAction`), not off the live lane. The per-cycle `roster` is resolved by the loop *before* the gate call and threaded in as a parameter defaulting to `None` — so existing gate unit tests pass unchanged. **Additive.**

Two boot-time validations the engine lacks today: validate `config.brain.harness` and `config.ingest.harness` against a `brain_allow` list **and** against "has a non-empty `oneshot_template`." Today nothing checks `brain.harness` until the first cycle raises. Fail fast at boot.

### A.3 The per-harness profile matrix (governance inputs, all grounded in `lib/harness-registry.sh`)

| Harness | Autonomy modes | Can pin model? | Unattended-capable? | Brain/ingest-capable? | Drift | Best role |
|---|---|---|---|---|---|---|
| **claude** | interactive · oneshot · headless · **stream** | config | yes (`--dangerously-skip-permissions`) | **yes** | **low (baseline)** | Brain (default); high-risk infra; headless ingest |
| **codex** | interactive · oneshot · headless | via `--config model=` | yes (bypass flag) | yes | **high** | Autonomous coder once pinned; secondary brain w/ drift-watch |
| **pi** | interactive **only** | yes (`--model`) | no | **no** (empty oneshot) | med | Product / synthesis; never brain/ingest |
| **opencode** | interactive · oneshot | config | no | yes | med | Cheap bulk/parallel grunt |
| **cursor-agent** | interactive · oneshot | yes (`--model`) | no | yes | med (no skill_dir) | Cursor-model edits; not skill-dependent |
| **forge** | interactive · oneshot | config | no | yes | med | Fast one-shot bursts |
| **hermes** | interactive · oneshot · headless | yes (`--model`) | yes (`--yolo`) | yes | **high** (launch≠oneshot shape) | Specialized agent-platform; experimentation |
| **droid** | interactive · oneshot | config | no | yes | med (autonomy is exec-only) | Headless coding bursts (`droid exec`) |
| **amp** | interactive · oneshot · headless | **NO (`skip`)** | yes (`--dangerously-allow-all`) | yes | **high** | Agentic search; **never** reproducibility-critical |
| **openclaw** | interactive · oneshot | config (gateway-owned) | no (gateway owns) | yes | med (needs Gateway up) | Gateway-mediated fleet tasks |
| **mprocs** | interactive (not an LLM) | n/a | no | no | n/a | Ops dashboard |
| **shell** | bare command | n/a | no | no | n/a | Watchers, probes, log tails |

Three drift-derived axes this exposes: **model determinism** (amp can't pin → exclude from reproducibility-critical), **unattended-capability** (only claude/codex/hermes/amp have an auto-approve flag; the rest get a silent no-op warning — a *hard* constraint), and **brain/ingest-capability** (pi/mprocs/shell disqualified — empty `oneshot_template`).

### A.4 The "when we choose X" rubric (first match wins; every choice cites a registry fact)

| If the job is… | Choose | Because | Fallback |
|---|---|---|---|
| The brain (decision cycle) | **claude** | only zero-drift; only one that streams; default already claude | codex (`brain_allow`-gated, drift-watched) |
| Headless ingest (docs synthesis) | **claude** | needs non-empty oneshot + auto-approve; pi/mprocs/shell disqualified | codex / hermes |
| High-risk infra (deploys, migrations, gate-`destructive`) | **claude** interactive | best-understood approvals; example maps `infra: claude` | codex (pinned, drift-watch on) |
| Product reasoning / spec / UX | **pi** interactive | GSD lifecycle; real `--model`; example maps `web: pi`; human-watched | claude |
| Synthesis / docs / wiki | **pi** | example maps `docs: pi`; good prose | claude |
| Agentic codebase search | **amp** | `--mode` auto-model-selection built for search | claude (when amp's non-pinnable model is a problem) |
| Cheap bulk / parallel grunt edits | **opencode** | OSS models = lowest cost; `run` oneshot | forge (fast Rust startup) |
| Fast one-shot burst, latency-sensitive | **forge** | Rust binary, low startup; `-p` oneshot | droid (`droid exec`) |
| Headless autonomous coding burst | **droid** | `--auto low\|med\|high` via `droid exec` | codex exec |
| Cursor-model-specific edits | **cursor-agent** | real `--model`; skip if task needs loop skills (no skill_dir) | claude |
| Gateway-mediated / fleet task | **openclaw** | Gateway owns model+approvals | hermes |
| Specialized agent-platform / experiment | **hermes** | NousResearch fork, has skills; `-z` oneshot | claude |
| Watcher / health probe / log tail | **shell** | `cmd` IS the lane; expected_process covers watch/ssh | mprocs |
| Process-group ops dashboard | **mprocs** | not an LLM; cmd override supported | shell |

**Cross-cutting tie-breakers (after the table):** reproducibility required → exclude amp. Must run unattended-destructive → only claude/codex/hermes/amp qualify; otherwise the gate forces human approval rather than silently running attended. High drift + unattended + high risk → gate forces `DESTRUCTIVE`. Brain is non-claude this run → disable `auto`/`full` approval_mode for its `add_lane` harness choices (a drifting brain can't unattended-spawn a drifting worker).

---

## (B) COMPLEXITY ASSESSMENT — genuinely hard vs. cheap

**Cheap (additive, low-risk, mechanical):**
- **Registry governance fields** — same pattern as the existing 8. `harness_field` returns `""` for unset vars, so old registries, fakes, and partially-populated harnesses degrade gracefully. The frozen `probe`/`field`/`oneshot` CLI verbs are untouched. *This is the cheapest high-value work in the whole plan.*
- **`HarnessPolicy` dataclass on `EngineConfig`** — `_merge` already recurses into nested dataclasses and ignores unknown keys (verified). Defaults reproduce today's behavior exactly.
- **`classify_harness` in the gate** — pure function, `roster=None` default = no behavior change. The hard part is *policy design*, not code.
- **Brain roster block + selection rubric in the prompt** — append-only to `_assemble_prompt`; ~700 chars, well under the 24000-token warn threshold.
- **Boot-time `brain.harness`/`ingest.harness` validation** — a few lines, pure win (replaces a runtime raise with a boot failure).

**Genuinely hard (where the real engineering and risk live):**
- **The per-harness readiness/health contract.** This is the deepest item. Today readiness is one big heuristic in `loop-lane-status.sh`, hand-patched per harness (codex's `esc to interrupt` above the footer; Pi's braille spinner; themed prompts reading `unknown`). Generalizing this into declared `working_marker`/`idle_marker`/`health_probe` registry fields with the heuristic as fallback is the highest-value *and* highest-effort work, because **it must not regress the FROZEN single-word status contract** and every existing special case must keep passing. It is also the linchpin: continuity, swap-safety, and not-pasting-briefs-into-dead-shells all depend on trustworthy readiness.
- **`health` ≠ readiness, and both are needed.** `harness_binary_path` proves the binary is on PATH; it does NOT prove auth works or a gateway is up. Codex-on-PATH-but-unauthenticated is the exact silent-failure class — it passes the PATH check, spawns, dies to a bare shell, and reads as `idle`/`unknown`. A real `health` probe (auth/gateway) is new surface.
- **Worktree provisioning + teardown lifecycle** (facet C) — the integration story, the per-worktree `.venv` rebuild, the macOS UF_HIDDEN `.pth` gotcha (already in memory), and never-orphaning a tree. Hard, and deferrable.
- **The lane-handoff flush-out hook** (facet D) — a new action shape, gated on a *trustworthy* idle reading. It depends entirely on the readiness contract being solid first; building it on today's heuristic would flush against an unreliable status.

**Cheap-looking but actually a trap:** making `role` "governing." It is a one-line field today but flipping it from documentation to enforced policy changes the meaning of every existing `lane-config.yaml` and every `add_lane` the brain has ever emitted. Treat it as a **breaking-semantics change** (see roadmap), not a freebie.

---

## (C) BRANCH PRESSURE — quantified, with integration strategy

### C.1 The collision surface today

`add_lane` has **no worktree concept**: with no `--repo`, every dynamically-added lane inherits the base window's path = PROJECT_ROOT (verified at `loop-tmux.sh:304-313`). N added lanes land in the **same working tree**. The repo's write-safety model is **single-writer-per-file partitions on one shared tree** (`AGENTS.md`), which protects coordination artifacts (CRDT-style commuting writes) but **not source code** — because the git index is global per tree and source edits rarely commute.

**The killer is the shared index, not file overlap.** Even disjoint *edits* collide at commit: agent A's `git commit -am` sweeps agent B's saved-but-unstaged files into A's commit. And vendor drift makes the "stage narrowly" discipline unenforceable — a harness that defaults to `git add -A` silently violates the partition the instant a second writer exists. **Git-level isolation is the only enforcement that doesn't depend on per-harness good behavior.**

### C.2 The numbers (measured on this repo)

`git worktree add`: **170ms cold, 91/72ms warm.** `.git` is 2.6M and **shared** across worktrees (objects linked, not copied). Untracked heavyweights — `.venv`, `.pytest_cache`, `.ruff_cache`, `node_modules` — are **NOT carried.**

| Cost | Scaling | On this repo |
|---|---|---|
| Worktree setup | O(N) × ~100-200ms | 10 lanes ≈ 1-2s aggregate — negligible vs. think-time |
| Disk | O(N) × tracked tree (`.git` shared) | The real cost is N **`.venv` rebuilds** (`uv sync`), not the checkout — and the macOS UF_HIDDEN `.pth` gotcha makes each worse |
| Branches tracked | O(N) live refs | `loop-digest --json unpushed[]` grows to N rows; human digest not designed for N≥10 |
| Merge conflicts | **O(N²)** worst case | well-partitioned ≈ O(N); shared hot files (`harness-registry.sh`, `loop-tmux.sh`) → superlinear |
| Review | O(N) PRs or O(1) stacked w/ O(N) commits | this session shipped one stacked PR |
| CI | O(N) branches or O(1) at integration | |

**The decisive asymmetry:** setup is cheap, bounded, front-loaded (~200ms + a venv). Integration is expensive and up to O(N²). **You provision freely; you pay at reconciliation.** This inverts the naive intuition — the expensive part isn't spawning isolated trees, it's *collapsing them back*. It is also why this session's serialized-on-main was *correct*: concurrency was 1, each of the 8 fixes touched a different file, so N branches would have bought isolation serialized work never needed.

### C.3 Integration strategy

- **N ≤ 2-3 concurrent code-writers → sequential merge** (≈ the current model; each lane merges/rebases on done). No new infra. Degrades cleanly to serialized-on-main when concurrency = 1.
- **N ≥ 3 sustained → dedicated integration lane** (a sibling of `validate`, on its own integration worktree, the **sole writer of `main`**). This makes integration a *partition owner* — exactly the repo's existing single-writer instinct (docs is the sole writer of `ops-wiki/loops/`) — and contains O(N²) conflict cost in one place while giving `validate` a coherent tree to test against.
- **Merge queue** only once real CI exists and N is routinely high.
- **Stacked PRs are a dependency-chain tool, not a concurrency tool.** Don't reach for them just because this session did; they're pathological for N independent concurrent agents.

### C.4 The engine role: `add_lane` provisions a worktree — *conditionally*

Add an `isolation ∈ {shared, worktree}` field to the registry (same additive pattern). Add an `add_lane --worktree` path that, when isolation resolves to `worktree`: provisions `git worktree add .loop/worktrees/<session>/<window>`, sets cwd there, records the branch in `loops.<id>.branch` (so the digest and integration lane find it), and on `drop_lane` tears it down (extending the existing `@loop_lane` teardown guard so it never orphans a tree). coord/ops/docs stay on PROJECT_ROOT — generalizing the existing `--worktree-web` override from two fixed panes to any dynamic lane.

**Decision rule — provision a worktree if ANY holds:** another implementation lane currently holds dirty state (`unpushed[].count > 0`); the harness's `isolation` is `worktree` (treat **unverified non-claude harnesses as `worktree` by default**); or the lane must build/test while another edits. **Stay shared only when ALL hold:** concurrency provably 1, writer is a verified narrow-stager, no test needs a frozen tree — i.e. this session's exact profile.

The asymmetry decides the default: isolation costs ~200ms + a venv (bounded, front-loaded); shared-tree's failure is a silent cross-lane commit-sweep (unbounded, invisible until review). **The default flips from "shared unless asked" to "shared only while serialized" — concurrency, not operator opinion, is the trigger.**

---

## (D) CONTINUITY — what it PROPOSES and what it DECLINES

### D.1 The split (the thesis)

The loop **proposes total project continuity** across any swap, because every agent — brain or lane — is **stateless and boots from compiled disk state**, never its own transcript. `Brain.invoke` is a one-shot subprocess per cycle; the checkpoint header tells the brain "You hold no prior transcript." **Consequence: you can change `brain.harness` between any two cycles and lose zero project state** — this session ran codex as brain and worker for the first time on exactly this property. Continuity is total at the project level *because* it is zero at the session level.

The loop **declines** to preserve the agent's **in-session state**: context window, open files, reasoning scratch, the harness's own conversation rollout. Key negative finding (verified — grep returns empty): there is **no per-lane session-id, resume, or fork tracking anywhere**. The registry's 8 fields contain no session field; `substrate.add_lane` passes no resume id. Lane continuity is 100% wiki/mailbox/pane-text.

**This is by design, and it is the linchpin of harness-portability.** Cross-harness session transfer is impossible in principle — a claude rollout, a codex rollout, and an opencode thread are mutually unintelligible serializations of different internal states. There is no portable agent core-dump. And even *same-harness* resume is declined: wiring `claude --resume` would make a lane depend on an opaque off-disk file, breaking the "its memory is the disk" auditability invariant. **The loop trades native session continuity for harness-portability + auditability — exactly what a multi-harness governance layer needs.**

### D.2 The decline, and why it is currently silent

The only swap primitive is `add_lane` → `dispatch(brief, wait_ready=True)` → `drop_lane` (verified at `actions.py:104-115`). The departing agent gets **no shutdown hook**; `drop_lane` is a bare teardown. So if the outgoing agent was mid-task and hadn't filed an interim note, that progress is lost and invisible to the successor. The lane-page schema today (`Role / Current assignment / Last outcome / Open items`) is a *completed-work* schema, not an *in-flight* one.

Drift makes it worse at two points: **handoff-out** (was the lane really idle when we tore it down? — if a non-claude harness's "working" state is misread as "idle," the engine swaps mid-generation, maximizing loss at the worst moment) and **handoff-in** (did the new agent actually launch and consume the brief, or is it stuck at an approval prompt?). The handoff contract must make both observable, not assumed.

### D.3 The handoff contract (minimizes the declined loss; adds zero harness session coupling)

1. **New lane-page section `## Handoff state`** — append-only, lane-owned: `step` (the one concrete next action), `touched` (files + committed? yes/no/partial), `working-tree` (clean | dirty: paths), `blocked-on` (ask id | external | none), `assumptions` (≤3 load-bearing facts), `as-of` (UTC). This is the minimum serialization of in-session state into the one format every harness reads: plain markdown. It deliberately omits the unportable (reasoning trace, tool stack) — the honesty is in saying those die.
2. **Flush-out: a `drop_lane` pre-hook** for graceful swaps — dispatch a fixed handoff prompt ("write `## Handoff state`, then stop"), gated on a *verified-idle* reading. **A harness without a verified working-state entry is swapped `force` with no flush** (loss accepted explicitly, made visible at the swap site — not silent). A flush is `safe`; a forced no-flush swap of a *working* lane is `destructive`.
3. **Recovery brief** — on a swap (vs. a fresh lane), `add_lane.brief` must point the successor to read the lane page including `## Handoff state` first; inject the lane-scoped outstanding ask (the engine surfaces asks to the brain today but not to the lane — close this gap); and use the task file as backbone when one exists (`tasks/T<NNNN>.md` is already a self-contained dispatchable prompt by contract — the strongest existing continuity primitive).
4. **Handoff ack** — the incoming agent's first required action is to write an ack (mailbox or lane page), mirroring the mailbox-ack pattern, so handoff-in is observable. No ack within a cycle → the brain treats the swap as unconfirmed and probes, identically to unconfirmed dispatches.

**What the contract still declines, stated honestly:** reasoning trace, open-file mental model, undo stack (the `step`+`assumptions` fields are their lossy compression); native session resume (still declined even same-harness — governance may *log* a departed claude `session_id` as a forensic breadcrumb but must never depend on resuming it); uncommitted edits across a *forced* swap (the `working-tree: dirty` field warns; it does not transfer the diff).

**Governance hook:** swap cost is a function of **lane state, not harness identity.** Swapping an idle, filed-back lane costs ~0; swapping a working lane costs its entire in-session state. So governance prefers swapping at idle boundaries and treats mid-task swaps as destructive. **The brain can be swapped per cycle at zero continuity cost — it needs none of this handoff machinery; only lane swaps do.** A harness is "swap-safe" only once it has a verified working-state signature *and* a confirmed launch flag — make that a registry-level readiness gate before a harness is eligible as a lane target.

---

## (E) HOW THIS INCREASES MECHANICAL OPERABILITY

The throughline: **convert drift from "patch shared detection after it breaks in production" into "declare a field; let the probe catch the next harness's drift before it spawns."** Concretely, a human running the deck can see the fleet's governance state at a glance, spawn the right harness without typos, and never be surprised by a lane that silently died to drift or auth.

- **Roster as one source of truth.** A new `harness-registry roster [--json]` CLI verb (additive; existing `probe` untouched) emits every harness with governance fields + a `present` flag. The engine reads it to build the brain rubric; the deck reads it for a governance view; the gate reads a per-cycle snapshot. `contract_version: 1` per the additive contract.
- **`health` verb** (`ok | missing | unauthenticated | unhealthy`) is the out-of-band probe readiness can't do: it proves auth/gateway, not just PATH. Killing the codex-on-PATH-but-unauthenticated silent-failure class.
- **Brain physically cannot propose a bad harness.** The assembled prompt's roster block is filtered to *allowed + present + healthy* — so the gate's verdict becomes a backstop, not the primary funnel (same defense-in-depth as decision-validation + gate-reclassification).
- **Deck: a Roster screen** (bind `h`, mirroring the existing `AdrScreen`/`EventsScreen` pattern) — read-only governance dashboard, NON-WRITER boundary preserved. **A health glyph in the FleetTable** next to the harness name, so a dead/unauthenticated lane shows red *before* the operator wonders why it fell to a bare shell. **AddLaneModal becomes a `Select`** populated from the roster (allowed+present only) — a typo like `cluade` can no longer reach the bash boundary; the human, like the brain, picks only governed harnesses. The submit dict and spawn path stay byte-identical. **Retire-candidate indicator** on dynamic lanes idle past N cycles, so `drop_lane` has a visible target and lanes don't leak against `max_lanes`.
- **Health-aware `wait_ready`.** Today `add-lane`'s readiness poll times out and proceeds anyway, pasting the brief into a dead shell. On timeout it should run the harness `health` probe and emit `errored` instead of silently proceeding.

---

## PHASED ROADMAP — smallest-safe-highest-leverage first

Each phase notes fence/contract impact and additive-vs-breaking. "Additive" = backward-compatible per `CONTRACT.md` (new fields, empty-safe defaults, frozen verbs untouched).

### Phase 0 — Declare the facts (registry governance fields + roster)
**Additive.** Add `capability_tags`, `cost_tier`, `autonomy_class`, `auth_requirement`, `health_probe`, `drift_pins` to `HARNESS_REGISTRY_FIELDS` (empty-safe defaults). Add `roster`/`health` CLI verbs; `probe`/`field`/`oneshot` untouched (frozen). Teach the fake registry `roster`/`health` (unstubbed = empty = today). **Contract impact:** none (additive within major version). **Leverage:** unblocks everything else; zero runtime behavior change. **This is where you start.**

### Phase 1 — Enforce policy in the gate (the core governance value)
**Additive.** `HarnessPolicy` dataclass on `EngineConfig` (defaults = today). `classify_harness` in `gate.py` (`roster=None` default = today). Thread a per-cycle roster snapshot into `classify_batch`. Boot-time validation of `brain.harness`/`ingest.harness` (`brain_allow` + non-empty `oneshot_template`). Append the roster block + selection rubric to the brain prompt and checkpoint header. **Contract impact:** none — but the *first* time an operator writes a non-empty `harness_policy`, behavior changes for them by their own choice. **Leverage:** highest — this is the governance the user asked for, and it's almost all cheap. **Defer nothing here except** making `role` enforced (see Phase 1.5).

### Phase 1.5 — Make `role` governing (the one breaking-semantics step)
**Breaking-semantics (gated behind opt-in).** Flipping `role:` from documentation to enforced policy changes the meaning of existing configs and historical `add_lane`s. **Mitigation:** governance is *inert until an operator declares `engine.harness_policy.roles`* — empty policy = pass-through = today. So the breaking change is opt-in, not forced. Ship it in Phase 1 mechanically but document loudly that activating role policy is the semantic flip. **Contract impact:** semantic, not structural; opt-in.

### Phase 2 — Per-harness readiness/health contract (the hard, high-value generalization)
**Additive (output shape frozen).** Add `working_marker`/`idle_marker` registry fields; `loop-lane-status.sh` prefers them when `@loop_lane_harness` is set, keeping today's heuristics as fallback so the FROZEN single-word status contract is untouched and every existing special case still passes. Make `add-lane`'s `wait_ready` health-aware (emit `errored` on timeout instead of silent proceed). **Contract impact:** none structurally; this is the linchpin Phases 3-4 depend on. **Why here, not earlier:** it's the hardest item and gates the rest — but it must precede any swap-flush work.

### Phase 3 — Deck operability surface
**Additive, display-only (NON-WRITER preserved).** Roster screen (`h`); health glyph in FleetTable; AddLaneModal `Select` from roster; retire-candidate indicator. **Contract impact:** none. **Leverage:** high human-facing value, low risk, can land any time after Phase 0/1 — parallelizable with Phase 2.

### Phase 4 — Conditional worktree isolation (defer until concurrency > 1)
**Additive.** `isolation` registry field; `add_lane --worktree` provision/record/teardown; integration-lane pattern for N≥3. **Contract impact:** new flag + new `loops.<id>.branch` usage (already in schema). **Defer because:** at concurrency = 1 (today's dominant mode, and this session's exact profile) it is pure waste — isolation prevents conflicts that serialization already prevented. Build it *when the engine routinely runs ≥2 concurrent code-writers*, not before.

### Phase 5 — Lane-handoff contract (defer until both Phase 2 and Phase 4 land)
**Additive.** `## Handoff state` lane-page section; `drop_lane` flush pre-hook (gated on verified-idle); recovery-brief amendments; handoff ack. **Contract impact:** new action shape (flush), new lane-page section. **Defer because:** the flush-out hook is only safe on a *trustworthy* idle reading (Phase 2), and mid-task swaps are mostly a concurrency phenomenon (Phase 4). Building it on today's heuristic would flush against an unreliable status — actively harmful.

---

## RECOMMENDATION — where to start, what to defer

**Start with Phase 0 then Phase 1.** They deliver the governance the user explicitly asked for — the "when we choose X" rubric becomes enforceable, the brain can't propose a denied/missing/unauthenticated harness, and the high-drift-unattended-high-risk combination forces human sign-off — at almost entirely additive, low-risk cost. Crucially, the security gate already proves the pattern (pure-module defense-in-depth), so this is *extending a battle-tested mechanism*, not inventing one. Keep `role` enforcement opt-in (empty policy = today) so nobody's existing config breaks.

**Do Phase 2 next** despite its difficulty — it is the linchpin. Trustworthy readiness/health is what makes swap-safety, no-dead-shell-dispatch, and (later) handoff possible. Land Phase 3 (deck) opportunistically alongside it; it's cheap and high-visibility.

**Defer Phases 4 and 5 until concurrency actually exceeds 1.** This is the plan's most important restraint, and all four facets converge on it: at concurrency = 1, worktree isolation is pure overhead and the handoff flush has no trustworthy idle signal to fire on. This session's serialized-on-main approach was *correct for its workload*; the parallelism machinery should arrive exactly when — and not before — the engine starts running multiple concurrent code-writers. Build the governance now (cheap, additive, high-leverage); buy the parallelism infrastructure only when you're about to spend it.

**The one thing to get right regardless of phase:** drift is a first-class, declared, probeable property. Every harness added to governance ships a verified working-state signature, a confirmed launch flag, and a health probe — or it is not eligible as an unattended or swap-target lane. That single discipline is the generalized lesson of this session's 8 codex fixes, and it is what keeps the whole governance layer from silently degrading the moment vendor #13 drifts.