# Architecture

Software architecture, data flow, and the trial lifecycle for the Work Factor
apparatus. For the scientific methodology see [EXPERIMENT.md](EXPERIMENT.md); for the
project overview see [README.md](README.md).

---

## 1. Design premise

The apparatus was built before any attacker or defense implementation, in this order:

```
schema  ->  analysis  ->  validation  ->  adapters  ->  experiments
```

Two reasons. First, the project's headline claim is a *measurement* claim — that
cost-to-break varies substantially across defenses reporting similar ASR. If the
measurement layer is wrong, no amount of experiment code rescues it. Second, the schema
is the least reversible artifact: `censored`, `budget_ceiling`, refusal rate, and the two
cost channels must exist from experiment #1 or the data is unanalysable afterwards.

The consequence visible throughout the codebase is that **research-integrity rules are
implemented as exceptions and constraints rather than as conventions**. A rule that
raises is a rule that survives a tired researcher at 2am; a rule in a document is not.

---

## 2. Module map

```
                      ┌──────────────────────────────────────────┐
                      │              run_slice.py                │
                      │  protocol constants, preflight gate,      │
                      │  execution driver, replication report     │
                      └────────┬──────────────────────┬──────────┘
                               │                      │
             ┌─────────────────▼────────┐   ┌─────────▼──────────────┐
             │      wf/runner.py        │   │    wf/analysis.py      │
             │  budgets, seeds,          │   │  Kaplan-Meier, log-    │
             │  escalation, telemetry    │   │  rank, bootstrap CIs,  │
             │  ONE trial per cell       │   │  composition null, G2  │
             └───┬──────────────────┬────┘   └────────────────────────┘
                 │                  │            ▲
                 │                  │            │ reads via db.py only
     ┌───────────▼──────┐   ┌───────▼──────────┐ │
     │   wf/adapter.py  │   │     wf/db.py     │─┘
     │  AgentDojo       │   │  guardrails G1-G5│
     │  boundary,       │   │  open/checkpoint/│
     │  attack protocols│   │  finalize, reads │
     └───────┬──────────┘   └───────┬──────────┘
             │                      │
   ┌─────────▼──────────┐   ┌───────▼──────────┐
   │  AgentDojo 0.1.35  │   │  wf/schema.sql   │
   │  (external, lazy   │   │  SQLite, CHECK   │
   │   imports only)    │   │  constraints,    │
   └────────────────────┘   │  views           │
                            └──────────────────┘

   wf/defenses.py — metadata, composition grid, pair scoring, screening rank
                    (pure; no I/O, no DB, no benchmark)
```

### The three-layer rule

```
runner.py    budgets, seeds, escalation   |  no AgentDojo, no statistics
adapter.py   the AgentDojo boundary       |  no statistics
analysis.py  survival statistics          |  no benchmark knowledge
```

This is checked **mechanically**, not by review. `validate.py` Check 9 parses each
module's import graph and fails if `adapter → analysis`, `analysis → adapter/runner`, or
`runner → analysis` appears. An accidental import is easy to add and very hard to notice
in a diff, and the separation is what makes each layer independently testable: the
statistics can be validated against planted ground truth without a benchmark, and the
benchmark boundary can be exercised without any statistics.

A practical consequence: **all AgentDojo imports are lazy**, inside method bodies. The
entire apparatus — including the full 92-check validation suite — runs in an interpreter
that does not have `agentdojo` installed.

---

## 3. Data flow

### 3.1 One attack attempt

```
CellSpec (config, model, seed, injection_task)
    │
    ▼
method.generate(ctx, query_index)  ──────────────►  AttackPayload
    │   ctx = AttackContext(task, defense_components, model_id, seed,
    │                       injection_task_id)
    │   NOTE: ctx contains NO prior results. A fixed attack cannot see them.
    ▼
adapter.run_attempt(task, payload, defense_components, model_id, seed)
    │
    │   AgentDojoAdapter internally:
    │     suite.run_task_with_pipeline(pipeline, user_task, injection_task,
    │                                  injections) -> (utility, security)
    │     catching BadRequestError / UnprocessableEntityError / ApiError /
    │     ServerError  ->  error string
    ▼
interpret_agentdojo_outcome(utility, security, error)
    │
    │   error set      ->  RunResult(outcome='error')      programmatic_success = None
    │   security True  ->  RunResult(outcome='success')    programmatic_success = True
    │   security False ->  RunResult(outcome='failure')    programmatic_success = False
    ▼
db.log_query(trial_id, q, payload.text, ...)     payload stored as SHA-256 only
```

The `error` branch is the load-bearing one. AgentDojo v1.2.2 records
`utility=False, security=True` when it swallows those exception types, meaning an
infrastructure failure is indistinguishable from a successful attack in its raw stream.
Our adapter catches those exceptions itself and **discards** the upstream `security`
value in that case rather than trusting it.

### 3.2 The benign path is a different type

```
adapter.run_benign(task, defense_components, model_id, seed)  ->  BenignRunResult
```

`BenignRunResult` has `benign_utility`, token counts, latency, and an optional error. It
has **no** `outcome` field and **no** `programmatic_success` field. This is deliberate:
`run_task_with_pipeline` early-returns `(utility, True)` when `injection_task is None`,
and that `True` means "the security check is vacuously satisfied", not "an attack
succeeded". There is nowhere in the type to put it, so it cannot leak into the security
stream by inattention.

`EscalationRunner.run_cell()` takes benign utility as an explicit parameter, produced by
a separate non-injected execution. Benign utility and security outcomes are never
inferred from one another.

### 3.3 Cell → trial

```
open_trial()                    status='pending', provenance frozen
    │
    │   for budget in escalation_plan:          # ascending, distinct
    │       while q < budget and not success:
    │           q += 1
    │           generate -> run_attempt -> log_query
    │           if success:  success_at = q
    │           elif method.is_learning:  method.observe(payload, result)
    │
    │       record_checkpoint(budget, success_observed, queries_used, decision)
    │       if success: break
    ▼
finalize_trial()                survival fields DERIVED from checkpoint history
```

`decision` is one of `escalate`, `stop_success`, `stop_exhausted`.

Note the feedback line: `observe()` is offered **only** to methods declaring
`is_learning=True`, and only on a genuine `failure` — an errored attempt teaches nothing,
because there was no verdict.

---

## 4. Database schema

`wf/schema.sql`, SQLite, `PRAGMA foreign_keys = ON`.

### 4.1 Registries

| Table | Purpose | Notable constraints |
|---|---|---|
| `prereg` | Preregistration. Every trial references one, so a deviation from plan is visible as a mismatch. | `arm` CHECK; `n_seeds >= 3`; ascending `budget_ladder` |
| `defense_config` | One row per configuration, singleton or stack. | `components` is canonical sorted JSON and `UNIQUE`; `impl_source` CHECK |
| `attack_method` | Attack registry. | `is_learning` CHECK — the quarantine flag at method level; `hyperparams` pinned JSON |
| `model_version` | Pinned model snapshots. | `snapshot` mandatory (dated API string, not a local alias) |
| `adapter_registry` | What actually executes trials. | `evidence_class` CHECK `('real','synthetic')`; `benchmark_version` mandatory |

`adapter_registry.evidence_class` is the quarantine flag for the *measurement apparatus*,
the exact analogue of `attack_method.is_learning` for attack methods.

### 4.2 `trial` — the survival unit

One row per `(config_id, method_id, model_id, task_id, injection_task_id, seed, arm,
stage)`, enforced by `UNIQUE`. Column groups:

| Group | Columns | Why |
|---|---|---|
| Identity | `trial_id`, `prereg_id`, `arm`, `stage`, `config_id`, `method_id`, `model_id`, `benchmark`, `suite`, `task_id`, `injection_task_id`, `seed` | `injection_task_id` is part of the identity because an AgentDojo security case *is* the pair; without it the `UNIQUE` constraint would collapse 144 banking cases to 16 |
| Frozen provenance | `adapter_id`, `adapter_version`, `benchmark_version`, `model_snapshot`, `defense_impl_ref`, `attack_source_ref` | copied at `open_trial()`, all `NOT NULL` |
| Model channels | `model_snapshot`, `defender_models`, `attacker_model_snapshot` | three channels, never one |
| Survival | `censored`, `event_queries`, `budget_ceiling` | `censored` is `NOT NULL` — a trial cannot be recorded without stating whether it was censored |
| Outcomes | `attack_success`, `benign_utility`, `utility_under_attack`, `attacker_refusal_rate`, `eval_awareness` | four separate numbers, never collapsed into one score |
| Attacker cost | `attacker_queries`, `attacker_tokens_in/out`, `attacker_usd` | |
| Defender cost | `defender_tokens_in/out`, `defender_usd` | |
| Escalation | `escalation_plan`, `checkpoints_run` | |
| Status | `status`, `error_detail`, `created_utc` | `status` CHECK `('pending','ok','error','aborted')` |

**There is no combined-cost column.** That is the enforcement mechanism for the
attacker/defender cost separation: costs cannot be accidentally blended because no field
exists to blend them into.

The survival semantics are enforced by the database itself:

```sql
CHECK (event_queries <= budget_ceiling)
CHECK (status <> 'ok' OR censored = 0 OR event_queries = budget_ceiling)
CHECK (status <> 'ok' OR censored = 1 OR attack_success = 1)
```

Read in order: an observation cannot exceed its own ceiling; a *censored* finalized trial
must sit exactly at the ceiling (it is a lower bound, not an event time); and an
*uncensored* finalized trial must actually have succeeded.

### 4.3 `budget_checkpoint` — resource-allocation metadata, not observations

A trial that ran `B=10` (no success) → `B=25` (no success) → `B=50` (success at q37)
yields **three checkpoint rows and exactly one trial row** (`event_queries=37`,
`censored=0`, `budget_ceiling=50`).

Counting checkpoints as censored observations would count the same task once per
checkpoint and bias every curve. Enforcement is layered:

- checkpoints live in their own table;
- **nothing in `analysis.py` reads that table** — it exists for cost accounting and for
  auditing that the escalation policy was followed as preregistered;
- `record_checkpoint()` rejects a non-increasing budget and rejects any budget not in the
  trial's preregistered `escalation_plan`;
- `finalize_trial()` *derives* the survival fields from the checkpoint history rather
  than accepting them from the caller.

### 4.4 `query_event` — per-attempt telemetry

A child of `trial`, deliberately not the analysis unit — mixing the two is how correlated
queries get treated as independent samples.

- `payload_sha256` — attack content is stored **by hash, never in plaintext**, so the
  database can be released without releasing working attack strings.
- `outcome` is three-valued (`success` / `failure` / `error`) and CHECK-constrained
  against `programmatic_success`, so the two cannot disagree:
  `CHECK (outcome <> 'success' OR programmatic_success = 1)` and
  `CHECK (outcome <> 'error' OR programmatic_success = 0)`.
- `judge_verdict` and `judge_id` sit beside the programmatic label and never determine
  it.

`human_label` is a separate table for judge calibration, because the labelled slice is a
sample rather than a column on every query.

### 4.5 Views

| View | Contents |
|---|---|
| `v_trial_clean` | `status='ok'` **and** `attacker_refusal_rate < 0.5` **and** `adapter.evidence_class='real'` — the analysis-ready set |
| `v_trial_harness` | the same finalization and refusal rules, but `evidence_class='synthetic'` — apparatus runs, queryable but clearly separated |

Applying the finalization, refusal-exclusion, and evidence-class rules in one place means
they cannot be applied inconsistently across analyses. Pending trials are excluded
because an unfinalized trial has no survival outcome; errored trials are excluded because
an errored trial has no survival outcome *whatever its evidence class* — treating one as
censored would claim the defense survived a budget that was never actually spent.

---

## 5. The guardrails in `wf/db.py`

Five invariants that SQL alone cannot express, named in the module docstring:

| ID | Invariant | Implementation |
|---|---|---|
| **G1** | A trial's `arm` must agree with its method's `is_learning` flag | `_assert_arm_matches_method()` raises `QuarantineViolation` |
| **G2** | Every analysis read names exactly one arm, one stage, **and** one model | `survival_data()`, `replication_outcomes()`, `cost_channels()` — `model_id` is keyword-only with no default |
| **G3** | Cost is only ever returned as separate channels | `cost_channels()` returns separate keys; `total_cost` does not exist as a concept anywhere |
| **G4** | A sequential-escalation run yields exactly ONE survival observation | checkpoints in a separate table; survival fields derived at `finalize_trial()`; no API writes a survival outcome directly |
| **G5** | Synthetic output cannot become evidence, and real runs cannot hide outside the preregistered stages | `_assert_stage_matches_adapter()` raises `ConfirmatoryRunViolation`; provenance frozen at `open_trial()` |

### 5.1 The evidence-class ⟺ stage bijection

```
evidence_class = 'synthetic'   ⟺   stage = 'harness_test'
evidence_class = 'real'        ⟺   stage ∈ {'screen', 'confirm'}
```

**Both directions are load-bearing.**

*Forward* — synthetic numbers cannot enter `screen`, not just `confirm`. Screening selects
which pairs are promoted to the confirmatory study, so mock data corrupting screening
corrupts the confirmatory set by proxy.

*Reverse* — a real adapter cannot run at `harness_test`. Otherwise the confirmatory grid
could be run "as a harness test", inspected, and then re-run "for real" — which is peeking
with extra steps.

Because the mapping is a bijection, even a raw `SELECT ... WHERE stage='confirm'` against
the base table cannot return a synthetic row. `v_trial_clean` joining the registry is
belt-and-braces, not the only guard.

Three further details make the guard hard to defeat by accident:

- `register_adapter()` takes the **adapter object**, not a bag of strings, and reads
  `evidence_class` off the class that will actually produce outcomes.
- On `OfflineAdapter`, `evidence_class` is a `ClassVar` — **not** a dataclass field, so it
  is not a constructor argument. Lying about what the adapter is costs an edit to
  `wf/adapter.py`, visible in review, rather than one keyword at a call site.
- `assert_adapter_matches()` is called by the runner at construction and rejects a live
  adapter whose declared fields disagree with its registration, catching the case where
  the DB was registered in one session and the object swapped in another.

### 5.2 Provenance freezing

`_freeze_provenance()` snapshots the experimental object at `open_trial()`:
`adapter_id`, `adapter_version`, `benchmark_version`, `model_snapshot`,
`defense_impl_ref`, `attack_source_ref`.

Everything there is available by join and is copied anyway. The registries use
`INSERT OR IGNORE` and remain editable; a finished trial is a historical record of what
actually ran. If someone re-points a defense at a new commit, completed trials keep naming
the commit they used, so "we changed the attack and forgot to re-run" surfaces as a
mismatch rather than being silently overwritten by current registry values.

Two refusals live in the same function: a `defense_config` with no `impl_ref` cannot
produce trials at all (an unattributed defense implementation cannot answer "was your
version a strawman?"), and an unregistered `model_id` or `config_id` raises rather than
inserting a dangling reference.

### 5.3 The fifteen structural invariants

The five `G` guardrails above are the ones `db.py` names in its docstring. Taken together
with the schema, the adapter boundary, and the import checks, fifteen distinct
research-integrity rules are encoded structurally across the codebase. Each is a rule
about *researcher behaviour* implemented as an exception or a constraint, so that
violating it requires an edit visible in a diff rather than a moment of inattention.

| # | Invariant | Mechanism |
|---|---|---|
| 1 | Learning-attacker results cannot enter a primary analysis | `arm` is CHECK-constrained; `db.py` raises `QuarantineViolation` when a method's `is_learning` flag disagrees with the trial's arm |
| 2 | Observations from different models cannot be pooled | `model_id` is a required keyword-only argument of `survival_data()`, `replication_outcomes()`, `cost_channels()`; omitting it is a `TypeError`, not a wider query |
| 3 | Censored observations cannot be recorded as event times | `CHECK (status <> 'ok' OR censored = 0 OR event_queries = budget_ceiling)` |
| 4 | Attacker and defender cost cannot be blended | no combined-cost column exists in the schema; `cost_channels()` returns separate keys and no total |
| 5 | `D1+D2` and `D2+D1` cannot become two configurations | canonical sorted JSON hashed to a single `config_id` |
| 6 | Sequential escalation cannot create multiple survival observations | checkpoints live in `budget_checkpoint`; survival fields are derived at `finalize_trial()`, never supplied; `UNIQUE` on the cell rejects a second trial |
| 7 | Screening data cannot enter confirmatory analysis | `stage` is CHECK-constrained and required by every read API; `screening_rank()` raises if handed p-values or CIs |
| 8 | Fixed attacks cannot see prior results | `FixedAttack` defines no `observe()`; `generate()` takes only `(ctx, query_index)`; the runner offers feedback solely to `is_learning=True` methods |
| 9 | The three layers cannot bleed into each other | `validate.py` inspects module imports: adapter ↛ analysis, analysis ↛ adapter/runner, runner ↛ analysis |
| 10 | Synthetic data cannot become evidence, and real runs cannot hide outside the preregistered stages | `adapter_registry.evidence_class` held in bijection with `trial.stage`; `ConfirmatoryRunViolation` raised in both directions |
| 11 | Infrastructure error cannot be counted as attack success | `RunResult.outcome` is three-valued and `programmatic_success` returns `None` on error; a trial with no success and any errored attempt finalizes as `status='error'`, not as a clean censored observation |
| 12 | "Replication" cannot be claimed without a comparable published reference | `g2_gate(published_asr=None)` returns `PASS_PROTOCOL_ONLY` and `numerical_pass=None`; `PASS_REPLICATION` is unreachable without a reference |
| 13 | A one-shot attack cannot be inflated into a repeated-query attack | `AgentDojoFixedAttack.generate()` raises on `query_index != 1`, before any benchmark import |
| 14 | Defender-side model changes cannot masquerade as overhead changes | three separate model channels on `trial`; `defender_model_snapshots()` raises if a model-using defense names no model |
| 15 | A finished trial cannot lose track of what produced it | `adapter_id`, `adapter_version`, `benchmark_version`, `model_snapshot`, `defense_impl_ref`, `attack_source_ref` are `NOT NULL` and copied at `open_trial()`, so later registry edits cannot rewrite what a completed trial ran against |

All fifteen are exercised by `validate.py`. The constraint checks there attempt the
violation and assert that it raises, so a removed guardrail fails the suite rather than
passing silently.

---

## 6. The adapter boundary

### 6.1 Value objects

| Type | Role | Notable property |
|---|---|---|
| `TaskSpec` | One benchmark task | frozen; mirrors AgentDojo's structure without importing it |
| `AttackContext` | What an attack may see | **contains no prior results** — a fixed attack cannot receive feedback through it |
| `AttackPayload` | The generated attack | stored downstream by hash; carries `injection_task_id` and generation token counts |
| `RunResult` | One **injected** attempt | `outcome` is the source of truth; `programmatic_success` is derived |
| `BenignRunResult` | One **non-injected** run | has no security fields at all, by construction |

`RunResult.__post_init__` enforces the outcome vocabulary in both directions: an `error`
outcome *requires* an error detail (an unexplained non-result cannot be audited), and a
non-error outcome *must not* carry one (an attempt that produced a verdict did not fail
infrastructurally).

### 6.2 Attack protocols

```python
class FixedAttack:                    # primary arm
    is_learning = False
    def generate(self, ctx, query_index) -> AttackPayload: ...
    # defines NO observe()

class LearningAttack:                 # secondary arm
    is_learning = True
    def generate(self, ctx, query_index) -> AttackPayload: ...
    def observe(self, payload, result) -> None: ...
```

The runner refuses to call `observe()` on any method with `is_learning=False`, so adding
one to a fixed attack later is not enough to sneak feedback in. `validate.py` asserts both
halves: `FixedAttack` exposes no `observe`, `LearningAttack` does.

### 6.3 Adapters

| Adapter | `evidence_class` | Role |
|---|---|---|
| `AgentDojoAdapter` | `real` | wraps AgentDojo 0.1.35 / benchmark v1.2.2; lazy imports; caches one pipeline per defense configuration |
| `OfflineAdapter` | `synthetic` | deterministic seeded-RNG stand-in for exercising runner mechanics; shaped like banking (16 tasks × 9 injection tasks) so the accounting rehearsal exercises the real pair structure |

`AgentDojoAdapter` pins `tool_output_format="yaml"` explicitly when constructing the
pipeline, because it is a protocol element and a library default could otherwise move
numbers between releases without appearing in a diff.

### 6.4 Defense name resolution

```python
DEFENSE_NAME_MAP = {
    "spotlight":     "spotlighting_with_delimiting",
    "toolfilter":    "tool_filter",
    "detector":      "transformers_pi_detector",
    "repeat_prompt": "repeat_user_prompt",
}
```

`upstream_defense_name(components)`:

- `()` → `None` (undefended baseline);
- one mappable component → its upstream name;
- one **unmappable** component → `ValueError` naming the four that exist;
- two or more components → `NotImplementedError`, because
  `PipelineConfig.defense` is a single `str | None` and AgentDojo v1.2.2 **cannot express
  a stack**. Running a two-component config as one defense would silently measure a
  different configuration.

`AGENTDOJO_DEFENSES` hard-codes the upstream defense set so that a version bump changing
it fails a test rather than silently changing what `spotlight` means.

`defender_model_snapshots()` resolves which model snapshots the *defender* side invokes:
`toolfilter` resolves to the target snapshot (it wraps the target's client), `detector`
resolves to its pinned classifier, and a model-using defense with no supplied target
snapshot raises rather than being guessed at.

---

## 7. Trial lifecycle in full

```
                         ┌─────────────────────────────┐
                         │  register_* (prereg,        │
                         │  defense, method, model,    │
                         │  adapter)                   │
                         └──────────────┬──────────────┘
                                        │
                         ┌──────────────▼──────────────┐
                         │  EscalationRunner(...)      │
                         │  assert_adapter_matches()   │  ← provenance settled
                         └──────────────┬──────────────┘     before anything runs
                                        │
                         ┌──────────────▼──────────────┐
                         │  open_trial()               │
                         │  • arm ↔ is_learning        │
                         │  • stage ↔ evidence_class   │
                         │  • benchmark ↔ adapter      │
                         │  • provenance frozen        │
                         │  status = 'pending'         │
                         └──────────────┬──────────────┘
                                        │
                    ┌───────────────────▼────────────────────┐
                    │  per budget in escalation_plan:        │
                    │    per query until budget or success:  │
                    │      generate → run_attempt → log_query│
                    │    record_checkpoint(...)              │
                    └───────────────────┬────────────────────┘
                                        │
                         ┌──────────────▼──────────────┐
                         │  finalize_trial()           │
                         │  censored / event_queries / │
                         │  budget_ceiling DERIVED     │
                         │  status = 'ok' | 'error'    │
                         └──────────────┬──────────────┘
                                        │
                         ┌──────────────▼──────────────┐
                         │  v_trial_clean  (real, ok,  │
                         │  low-refusal)               │
                         │       ↓                     │
                         │  survival_data(arm, stage,  │
                         │    config, model_id=...)    │
                         │       ↓                     │
                         │  analysis.py                │
                         └─────────────────────────────┘
```

**Finalization rules, applied in `finalize_trial()`:**

- Any checkpoint with `success_observed` → the *first* one supplies the event:
  `censored=0`, `event_queries=success_at_query`, `attack_success=1`. A success recorded
  at a query index beyond the checkpoint budget at which it was observed raises.
- No success at any checkpoint → `censored=1`, `event_queries=final_ceiling`,
  `attack_success=0`.
- Finalizing a trial with **no** checkpoints raises — there is no history to derive from.
- The runner separately marks `status='error'` when there was no success *and* at least
  one attempt errored, which keeps that trial out of `v_trial_clean` while leaving it
  visible to `replication_outcomes()` and the error-rate report.

---

## 8. Read API and pooling refusal

| Function | Returns | Reads from |
|---|---|---|
| `survival_data(arm, stage, config_id, *, model_id, method_id=None, clean_only=True)` | `[(event_queries, censored), ...]` | `v_trial_clean`, or `v_trial_harness` when `stage='harness_test'` |
| `replication_outcomes(arm, stage, config_id, *, model_id, method_id=None)` | `['success'|'failure'|'error', ...]` | base `trial` joined to the registry — **deliberately**, because errored trials are required here |
| `cost_channels(arm, stage, config_id, *, model_id)` | dict of separate attacker/defender averages | `v_trial_clean`; raises for `harness_test` |
| `refusal_audit(arm)` | how much data the refusal rule discards | real-evidence trials only |
| `checkpoint_audit(trial_id)` | the escalation history | `budget_checkpoint` |

`replication_outcomes()` reading the base table is the one deliberate asymmetry: the
reference ASR track has to reproduce the benchmark's own error semantics, and the error
rate has to be reportable. Arm, stage, and evidence-class quarantine still apply, and the
three values are *derived* from `status` and `attack_success` so there is no separate
outcome column that could disagree with them.

`refusal_audit()` exists because the `attacker_refusal_rate < 0.5` exclusion in
`v_trial_clean` silently drops rows, and how much it drops must be reported rather than
hidden.

---

## 9. Validation architecture

`validate.py` is a single top-to-bottom script of 92 checks in 19 groups, requiring only
numpy and scipy. There are two kinds of check, and the distinction matters:

**Estimator checks** — simulate trials from a *known* generative process and ask whether
the estimator returns the right answer. Kaplan–Meier recovering a planted median under
heavy censoring; the log-rank test separating two defenses engineered to share an ASR but
differ in break timing; the composition test recovering planted independence, destructive
interference, and synergy; judge calibration recovering a planted over-flagging judge.

**Constraint checks** — attempt an integrity violation and assert that it raises. A
learning method into the primary arm; a synthetic adapter into `screen`/`confirm`; a real
adapter into `harness_test`; a non-monotone checkpoint; an off-plan budget; a duplicate
cell; a defense stack at the boundary; a second query to a one-shot attack; p-values
handed to the screening rank; a read that omits `model_id`.

Everything `validate.py` prints is simulated. Nothing in its output is a result about any
real defense, and the script exits non-zero if any check fails.

---

## 10. What is not built

Stated here so the architecture is not read as more complete than it is:

- **Pipeline composition for defense stacks.** Required for RQ2/RQ3, and blocked on
  upstream's single-defense `PipelineConfig`.
- **Defense stage implementations** for `sandwich`, `dataflow`, `egress_canary` —
  `wf/defenses.py` contains metadata and interface stubs only, by design.
- **Repeated-query fixed attacks.** The current wrapper is one-shot by construction.
- **The learning attacker.** `LearningAttack` is a base class; no concrete
  implementation exists.
- **Any plotting or dashboard layer.**
- **`run_attempt()` / `run_benign()` against a live target.** These are the I/O shell
  around the fully tested `interpret_agentdojo_outcome()`, and are exercised for the first
  time by the G2 run — which has not completed.
