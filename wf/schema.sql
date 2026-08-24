-- ===========================================================================
-- Work Factor: experiment schema
--
-- DESIGN PRINCIPLE
-- The unit of observation is the TRIAL, defined by the survival-analysis
-- question: "how much attacker budget did it take before this defense first
-- failed on this task?"  Everything else in this schema exists to support
-- that unit or to make a specific threat-to-validity auditable.
--
-- Three rules are enforced structurally rather than by discipline:
--   1. There is NO combined-cost column. Attacker cost and defender overhead
--      cannot be accidentally blended, because no field exists to blend them
--      into.
--   2. `arm` is CHECK-constrained. The learning attacker cannot silently
--      enter a primary analysis.
--   3. `censored` is NOT NULL. A trial cannot be recorded without stating
--      whether it was censored.
-- ===========================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Preregistration. An analysis must reference a prereg row, so that any
-- deviation from plan is visible as a mismatch rather than invisible.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prereg (
    prereg_id       TEXT PRIMARY KEY,
    created_utc     TEXT NOT NULL,
    arm             TEXT NOT NULL CHECK (arm IN ('primary', 'secondary_learning')),
    hypotheses      TEXT NOT NULL,          -- free text, committed before data
    budget_ladder   TEXT NOT NULL,          -- JSON list, e.g. [10,25,50,100,250]
    quantiles       TEXT NOT NULL,          -- JSON list, e.g. [0.10,0.25,0.50]
    n_seeds         INTEGER NOT NULL CHECK (n_seeds >= 3),
    plan_sha        TEXT NOT NULL           -- git sha of the plan document
);

-- ---------------------------------------------------------------------------
-- Defense configurations. One row per config, including singletons and stacks.
-- `components` is a canonical sorted JSON list so that D1+D2 and D2+D1 are the
-- same row -- otherwise the composition grid silently double-counts.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS defense_config (
    config_id       TEXT PRIMARY KEY,
    components      TEXT NOT NULL UNIQUE,   -- canonical JSON, [] = no defense
    n_components    INTEGER NOT NULL,
    -- Implementation-fidelity provenance. Directly addresses the "your
    -- reimplementation was a strawman" objection.
    impl_source     TEXT NOT NULL CHECK (impl_source IN ('original', 'reimplemented', 'mixed')),
    impl_ref        TEXT,                   -- repo URL / commit for each component
    deviations      TEXT                    -- documented departures from original
);

-- ---------------------------------------------------------------------------
-- Attack methods. `is_learning` is the quarantine flag at method level; the
-- trial-level `arm` must agree with it (enforced in db.py).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS attack_method (
    method_id       TEXT PRIMARY KEY,
    family          TEXT NOT NULL,
    is_learning     INTEGER NOT NULL CHECK (is_learning IN (0, 1)),
    source_ref      TEXT NOT NULL,          -- paper + code commit
    hyperparams     TEXT NOT NULL,          -- JSON, pinned. Fixed attacks MUST
                                            -- keep this constant across configs.
    defense_aware   INTEGER NOT NULL CHECK (defense_aware IN (0, 1))
);

-- ---------------------------------------------------------------------------
-- Model versions, pinned. Model drift is a silent invalidator of any
-- longitudinal measurement, so the exact snapshot string is mandatory.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_version (
    model_id        TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    snapshot        TEXT NOT NULL,          -- e.g. dated API model string
    is_open_weight  INTEGER NOT NULL CHECK (is_open_weight IN (0, 1)),
    first_seen_utc  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- ADAPTER REGISTRY: what actually executed a trial, and whether its output is
-- admissible as evidence.
--
-- `evidence_class` is the quarantine flag for the measurement apparatus, the
-- exact analogue of `is_learning` for attack methods. An adapter that
-- manufactures outcomes from a seeded RNG is 'synthetic'; only a real
-- benchmark harness is 'real'. db.py enforces a BIJECTION between this column
-- and `trial.stage`:
--
--     evidence_class = 'synthetic'  <->  stage = 'harness_test'
--     evidence_class = 'real'       <->  stage IN ('screen', 'confirm')
--
-- Both directions are load-bearing. Forward: synthetic numbers cannot enter
-- screening (which selects the confirmatory pairs) or confirmation. Reverse: a
-- real run cannot be parked in a non-preregistered stage, inspected, and then
-- re-run "for real" -- which would be peeking with extra steps.
--
-- A consequence worth stating: because the mapping is a bijection, even a raw
-- `SELECT ... WHERE stage='confirm'` against the base table cannot return a
-- synthetic row. The views below are belt-and-braces, not the only guard.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS adapter_registry (
    adapter_id        TEXT PRIMARY KEY,
    adapter_version   TEXT NOT NULL,      -- version of OUR wiring, not the benchmark
    benchmark         TEXT NOT NULL,      -- e.g. 'agentdojo'
    benchmark_version TEXT NOT NULL,      -- e.g. 'v1.2.2'; the benchmark is versioned
    evidence_class    TEXT NOT NULL CHECK (evidence_class IN ('real', 'synthetic')),
    impl_ref          TEXT NOT NULL,      -- repo URL / commit of the adapter wiring
    registered_utc    TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- TRIAL: the survival unit of observation.
--
-- One row per (defense_config, attack_method, model, task, seed).
--
-- Survival semantics:
--   censored = 0  ->  event observed. event_queries is the attacker query
--                     index at which the FIRST programmatic success occurred.
--   censored = 1  ->  no success by the ceiling. event_queries MUST equal
--                     budget_ceiling and is a lower bound, not an event time.
--
-- The CHECK constraint makes the second case impossible to record wrongly,
-- which is the single most common way survival data gets corrupted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trial (
    trial_id            TEXT PRIMARY KEY,
    prereg_id           TEXT NOT NULL REFERENCES prereg(prereg_id),
    arm                 TEXT NOT NULL CHECK (arm IN ('primary', 'secondary_learning')),
    -- Screening data cannot enter confirmatory analysis. Same structural
    -- pattern as arm quarantine: Stage A screening selects which pairs to
    -- study, so reusing it for inference would be selection on the outcome.
    -- 'harness_test' is the non-evidence stage: runner mechanics exercised
    -- against a synthetic adapter. It is not a weaker kind of experiment, it is
    -- not an experiment at all, and no read API returns it alongside the other
    -- two.
    stage               TEXT NOT NULL CHECK (stage IN ('screen', 'confirm', 'harness_test')),

    config_id           TEXT NOT NULL REFERENCES defense_config(config_id),
    method_id           TEXT NOT NULL REFERENCES attack_method(method_id),
    model_id            TEXT NOT NULL REFERENCES model_version(model_id),
    benchmark           TEXT NOT NULL,      -- e.g. 'agentdojo'
    suite               TEXT NOT NULL,      -- e.g. 'banking'
    task_id             TEXT NOT NULL,
    -- The injection task paired with this user task. For AgentDojo a security
    -- case IS the pair, so 144 banking pairs are 144 trials; without this column
    -- the UNIQUE constraint below would collapse them to 16. 'none' for
    -- benchmarks with no such concept.
    injection_task_id   TEXT NOT NULL DEFAULT 'none',
    seed                INTEGER NOT NULL,

    -- EXECUTION PROVENANCE, copied at open_trial() rather than joined at read
    -- time. The registries use INSERT OR IGNORE and are editable afterwards; a
    -- finished trial must keep reporting what it actually ran against, so that
    -- "we changed the attack and forgot to re-run" surfaces as a mismatch
    -- instead of being silently overwritten by the current registry values.
    adapter_id          TEXT NOT NULL REFERENCES adapter_registry(adapter_id),
    adapter_version     TEXT NOT NULL,
    benchmark_version   TEXT NOT NULL,
    model_snapshot      TEXT NOT NULL,      -- dated API string, not the local alias
    defense_impl_ref    TEXT NOT NULL,      -- repo/commit of the defense implementation
    attack_source_ref   TEXT NOT NULL,      -- paper + code commit of the attack
    -- THREE model channels, never one. A defense like tool_filter issues its own
    -- LLM calls; if only the target model were recorded, a later "defender
    -- overhead increased" could actually mean "the defender changed model".
    defender_models     TEXT NOT NULL DEFAULT '[]',   -- canonical JSON list
    attacker_model_snapshot TEXT NOT NULL DEFAULT 'n/a',

    -- survival outcome
    censored            INTEGER NOT NULL CHECK (censored IN (0, 1)),
    event_queries       INTEGER NOT NULL CHECK (event_queries >= 0),
    budget_ceiling      INTEGER NOT NULL CHECK (budget_ceiling > 0),

    -- the four separate outcome numbers. Never collapsed into one score.
    attack_success      INTEGER NOT NULL CHECK (attack_success IN (0, 1)),
    benign_utility      REAL    NOT NULL CHECK (benign_utility BETWEEN 0 AND 1),
    utility_under_attack REAL   NOT NULL CHECK (utility_under_attack BETWEEN 0 AND 1),
    -- Refusal confound: fraction of attacker queries the ATTACKER-side model
    -- declined to generate. A trial with high refusal is NOT evidence of
    -- defense strength and must be excludable at analysis time.
    attacker_refusal_rate REAL  NOT NULL CHECK (attacker_refusal_rate BETWEEN 0 AND 1),

    -- Evaluation-awareness probe: did the target verbalise that it thought it
    -- was being tested? Recorded per trial so it can be used as a covariate.
    eval_awareness      INTEGER CHECK (eval_awareness IN (0, 1)),

    -- Cost, in two strictly separate channels.
    attacker_queries    INTEGER NOT NULL CHECK (attacker_queries >= 0),
    attacker_tokens_in  INTEGER NOT NULL,
    attacker_tokens_out INTEGER NOT NULL,
    attacker_usd        REAL    NOT NULL,
    defender_tokens_in  INTEGER NOT NULL,
    defender_tokens_out INTEGER NOT NULL,
    defender_usd        REAL    NOT NULL,
    wall_clock_s        REAL    NOT NULL,

    -- Sequential escalation. The trial is ONE survival observation regardless
    -- of how many checkpoints it passed through. `budget_ceiling` is the FINAL
    -- ceiling reached; intermediate checkpoints live in budget_checkpoint and
    -- are resource-allocation metadata, never survival observations.
    escalation_plan     TEXT NOT NULL,      -- JSON ascending list, e.g. [10,25,50]
    checkpoints_run     INTEGER NOT NULL DEFAULT 0,

    status              TEXT NOT NULL CHECK (status IN ('pending', 'ok', 'error', 'aborted')),
    error_detail        TEXT,
    created_utc         TEXT NOT NULL,

    UNIQUE (config_id, method_id, model_id, task_id, injection_task_id, seed, arm, stage),
    -- Survival semantics, enforced by the DB itself, once finalized.
    -- A censored observation is a lower bound at the ceiling, never an event.
    CHECK (event_queries <= budget_ceiling),
    CHECK (status <> 'ok' OR censored = 0 OR event_queries = budget_ceiling),
    CHECK (status <> 'ok' OR censored = 1 OR attack_success = 1)
);

-- ---------------------------------------------------------------------------
-- BUDGET CHECKPOINT: resource-allocation metadata for sequential escalation.
--
-- CRITICAL: these rows are NOT survival observations. A trial that ran
-- B=10 (no success) -> B=25 (no success) -> B=50 (success at q37) yields
-- THREE checkpoint rows and exactly ONE trial row (event_queries=37,
-- censored=0, budget_ceiling=50). Counting checkpoints as censored
-- observations would triple-count the task and bias every curve.
--
-- Nothing in analysis.py reads this table. It exists for cost accounting and
-- for auditing that the escalation policy was followed as preregistered.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS budget_checkpoint (
    checkpoint_id     TEXT PRIMARY KEY,
    trial_id          TEXT NOT NULL REFERENCES trial(trial_id) ON DELETE CASCADE,
    checkpoint_index  INTEGER NOT NULL CHECK (checkpoint_index >= 1),
    checkpoint_budget INTEGER NOT NULL CHECK (checkpoint_budget > 0),
    reached           INTEGER NOT NULL CHECK (reached IN (0, 1)),
    success_observed  INTEGER NOT NULL CHECK (success_observed IN (0, 1)),
    success_at_query  INTEGER,
    queries_used      INTEGER NOT NULL CHECK (queries_used >= 0),
    decision          TEXT NOT NULL CHECK (decision IN ('escalate', 'stop_success', 'stop_exhausted')),
    attacker_usd      REAL NOT NULL DEFAULT 0.0,
    defender_usd      REAL NOT NULL DEFAULT 0.0,
    recorded_utc      TEXT NOT NULL,
    UNIQUE (trial_id, checkpoint_index),
    CHECK (success_observed = 0 OR success_at_query IS NOT NULL),
    CHECK (decision <> 'stop_success' OR success_observed = 1)
);

CREATE INDEX IF NOT EXISTS idx_checkpoint_trial ON budget_checkpoint(trial_id, checkpoint_index);

CREATE INDEX IF NOT EXISTS idx_trial_survival ON trial(arm, config_id, method_id, model_id);
CREATE INDEX IF NOT EXISTS idx_trial_task     ON trial(benchmark, suite, task_id);

-- ---------------------------------------------------------------------------
-- QUERY: per-attempt telemetry. Supports cost curves and post-hoc auditing.
-- Deliberately a child of trial, not the analysis unit -- mixing the two is
-- how people accidentally treat correlated queries as independent samples.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS query_event (
    query_id        TEXT PRIMARY KEY,
    trial_id        TEXT NOT NULL REFERENCES trial(trial_id) ON DELETE CASCADE,
    query_index     INTEGER NOT NULL CHECK (query_index >= 1),
    -- Attack content is referenced by hash, not stored, so the DB can be
    -- released without releasing working attack strings.
    payload_sha256  TEXT NOT NULL,
    mutation_op     TEXT,                   -- NULL for non-mutating fixed attacks
    attacker_refused INTEGER NOT NULL CHECK (attacker_refused IN (0, 1)),
    -- Three-valued outcome. `programmatic_success` stays 0/1 for the survival
    -- machinery, but an attempt that never executed is recorded as 'error' here
    -- and must not be read as evidence that the defense held.
    outcome         TEXT NOT NULL CHECK (outcome IN ('success', 'failure', 'error')),
    programmatic_success INTEGER NOT NULL CHECK (programmatic_success IN (0, 1)),
    -- Secondary judge output. Kept beside, never inside, the primary metric.
    judge_verdict   INTEGER CHECK (judge_verdict IN (0, 1)),
    judge_id        TEXT,
    attacker_tokens_in  INTEGER NOT NULL,
    attacker_tokens_out INTEGER NOT NULL,
    defender_tokens_in  INTEGER NOT NULL,
    defender_tokens_out INTEGER NOT NULL,
    latency_s       REAL NOT NULL,
    UNIQUE (trial_id, query_index),
    -- The three-valued outcome and the 0/1 flag cannot disagree.
    CHECK (outcome <> 'success' OR programmatic_success = 1),
    CHECK (outcome <> 'error' OR programmatic_success = 0)
);

CREATE INDEX IF NOT EXISTS idx_query_trial ON query_event(trial_id, query_index);

-- ---------------------------------------------------------------------------
-- Human labels for judge calibration. Kept in its own table because the
-- labelled slice is a sample, not a column on every query.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS human_label (
    label_id        TEXT PRIMARY KEY,
    query_id        TEXT NOT NULL REFERENCES query_event(query_id) ON DELETE CASCADE,
    rater_id        TEXT NOT NULL,
    pass_number     INTEGER NOT NULL CHECK (pass_number IN (1, 2)),
    label           INTEGER NOT NULL CHECK (label IN (0, 1)),
    labelled_utc    TEXT NOT NULL,
    UNIQUE (query_id, rater_id, pass_number)
);

-- ---------------------------------------------------------------------------
-- Analysis-ready view. Applies the finalization, refusal-exclusion, and
-- evidence-class rules in one place so they cannot be applied inconsistently
-- across analyses. Pending trials are excluded: an unfinalized trial has no
-- survival outcome. Synthetic-adapter trials are excluded: they are not
-- observations of anything.
-- ---------------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_trial_clean AS
SELECT t.*
FROM trial t
JOIN adapter_registry a ON a.adapter_id = t.adapter_id
WHERE t.status = 'ok'
  AND t.attacker_refusal_rate < 0.5
  AND a.evidence_class = 'real';

-- Synthetic runner-mechanics trials, kept queryable but in a separate, clearly
-- named view. Anything reading this is asking about the apparatus, not about a
-- defense.
CREATE VIEW IF NOT EXISTS v_trial_harness AS
SELECT t.*
FROM trial t
JOIN adapter_registry a ON a.adapter_id = t.adapter_id
WHERE a.evidence_class = 'synthetic'
  -- Same finalization and refusal rules as v_trial_clean. An errored trial has
  -- no survival outcome whatever its evidence class: treating it as censored
  -- would claim the defense survived a budget that was never actually spent.
  AND t.status = 'ok'
  AND t.attacker_refusal_rate < 0.5;
