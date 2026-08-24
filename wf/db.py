"""
db.py -- database access with guardrails.

The guardrails exist because the supervisor's hard rule ("the learning
attacker's results cannot be used to select, tune, or redefine the fixed-attack
experiments") is a rule about researcher behaviour, and rules about researcher
behaviour are best implemented as exceptions.

Three invariants enforced here that SQL alone cannot express:

  G1  A trial's `arm` must agree with its attack method's `is_learning` flag.
      A learning method cannot be logged into the primary arm, or vice versa.

  G2  Any query for analysis must specify exactly one arm, one stage, AND one
      model. There is no way to ask this module for data pooled across arms,
      across screening and confirmatory stages, or across models. Two models are
      two experiments: a survival curve pooled over them describes no system
      that exists.

  G3  Cost is only ever returned as separate attacker/defender channels.
      `total_cost` does not exist as a concept anywhere in the codebase.

  G4  A sequential-escalation run yields exactly ONE survival observation.
      Checkpoints are written to a separate table, survival fields are DERIVED
      at finalize() rather than supplied, and there is no API that writes a
      survival outcome directly.

  G5  Synthetic-adapter output cannot become evidence, and real runs cannot
      hide outside the preregistered stages. `evidence_class` and `stage` are
      held in bijection by `_assert_stage_matches_adapter`, and every trial
      freezes the adapter/benchmark/model/implementation provenance it ran
      against, so "which experimental object produced this number?" is answered
      by the row itself rather than by a lab notebook.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

VALID_ARMS = ("primary", "secondary_learning")

# 'harness_test' is not a weaker experiment; it is not an experiment. It exists
# so that synthetic runner-mechanics runs have somewhere legitimate to go that
# is not 'screen' or 'confirm'.
HARNESS_STAGE = "harness_test"
EVIDENCE_STAGES = ("screen", "confirm")
VALID_STAGES = (*EVIDENCE_STAGES, HARNESS_STAGE)

VALID_EVIDENCE_CLASSES = ("real", "synthetic")


class QuarantineViolation(RuntimeError):
    """Raised when an operation would mix primary and secondary-arm data."""


class ConfirmatoryRunViolation(QuarantineViolation):
    """Raised when the evidence class of an adapter disagrees with the stage.

    Subclasses QuarantineViolation because it is the same category of error:
    data that is not admissible for a purpose being routed to that purpose.
    """


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_components(components: Sequence[str]) -> str:
    """Canonical JSON for a defense stack.

    Sorting matters: without it, D1+D2 and D2+D1 become distinct config rows and
    the composition grid double-counts half its cells.
    """
    return json.dumps(sorted(set(components)), separators=(",", ":"))


def config_id_for(components: Sequence[str]) -> str:
    canon = canonical_components(components)
    if canon == "[]":
        return "none"
    return "d_" + hashlib.sha256(canon.encode()).hexdigest()[:12]


class ExperimentDB:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_PATH.read_text())
        self.conn.commit()

    # -- registration -----------------------------------------------------
    def register_prereg(
        self,
        arm: str,
        hypotheses: str,
        budget_ladder: Sequence[int],
        quantiles: Sequence[float],
        n_seeds: int,
        plan_sha: str,
        prereg_id: str | None = None,
    ) -> str:
        if arm not in VALID_ARMS:
            raise ValueError(f"arm must be one of {VALID_ARMS}")
        if list(budget_ladder) != sorted(budget_ladder):
            raise ValueError("budget_ladder must be ascending")
        pid = prereg_id or f"pre_{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            "INSERT INTO prereg VALUES (?,?,?,?,?,?,?,?)",
            (
                pid, _utc(), arm, hypotheses,
                json.dumps(list(budget_ladder)), json.dumps(list(quantiles)),
                n_seeds, plan_sha,
            ),
        )
        self.conn.commit()
        return pid

    def register_defense(
        self,
        components: Sequence[str],
        impl_source: str,
        impl_ref: str | None = None,
        deviations: str | None = None,
    ) -> str:
        cid = config_id_for(components)
        canon = canonical_components(components)
        self.conn.execute(
            "INSERT OR IGNORE INTO defense_config VALUES (?,?,?,?,?,?)",
            (cid, canon, len(json.loads(canon)), impl_source, impl_ref, deviations),
        )
        self.conn.commit()
        return cid

    def register_method(
        self,
        method_id: str,
        family: str,
        is_learning: bool,
        source_ref: str,
        hyperparams: dict[str, Any],
        defense_aware: bool,
    ) -> str:
        self.conn.execute(
            "INSERT OR IGNORE INTO attack_method VALUES (?,?,?,?,?,?)",
            (
                method_id, family, int(is_learning), source_ref,
                json.dumps(hyperparams, sort_keys=True), int(defense_aware),
            ),
        )
        self.conn.commit()
        return method_id

    def register_model(
        self, model_id: str, provider: str, snapshot: str, is_open_weight: bool
    ) -> str:
        self.conn.execute(
            "INSERT OR IGNORE INTO model_version VALUES (?,?,?,?,?)",
            (model_id, provider, snapshot, int(is_open_weight), _utc()),
        )
        self.conn.commit()
        return model_id

    def register_adapter(self, adapter: Any, impl_ref: str) -> str:
        """Register the harness that will execute trials.

        Takes the adapter OBJECT, not a bag of strings, so the registry cannot
        describe something other than what will run: `evidence_class` is read
        off the class that produces the outcomes. Claiming a synthetic adapter
        is real therefore requires editing the adapter's source, which is
        visible in review, rather than passing a different argument here.
        """
        missing = [f for f in ("adapter_id", "adapter_version", "benchmark",
                               "benchmark_version", "evidence_class")
                   if not getattr(adapter, f, None)]
        if missing:
            raise ValueError(
                f"adapter {type(adapter).__name__} is missing provenance fields "
                f"{missing}; an adapter must declare what it is before it can run"
            )
        if adapter.evidence_class not in VALID_EVIDENCE_CLASSES:
            raise ValueError(
                f"evidence_class must be one of {VALID_EVIDENCE_CLASSES}, "
                f"got {adapter.evidence_class!r}"
            )
        if not impl_ref:
            raise ValueError("impl_ref is required: an adapter with no provenance "
                             "cannot support a replication claim")
        self.conn.execute(
            "INSERT OR IGNORE INTO adapter_registry VALUES (?,?,?,?,?,?,?)",
            (adapter.adapter_id, adapter.adapter_version, adapter.benchmark,
             adapter.benchmark_version, adapter.evidence_class, impl_ref, _utc()),
        )
        self.conn.commit()
        return adapter.adapter_id

    def adapter_record(self, adapter_id: str) -> sqlite3.Row:
        if not adapter_id:
            raise ValueError(
                "adapter_id is required: every trial must name the harness that "
                "produced it"
            )
        row = self.conn.execute(
            "SELECT * FROM adapter_registry WHERE adapter_id = ?", (adapter_id,)
        ).fetchone()
        if row is None:
            raise ValueError(
                f"unknown adapter_id {adapter_id!r}; register it first. Every trial "
                "must name the harness that produced it."
            )
        return row

    def assert_adapter_matches(self, adapter: Any) -> None:
        """Check a live adapter object against its registration.

        Called by the runner at construction. Catches the case where the DB was
        registered by one session and the object swapped in another -- the
        registry and the code must agree before anything executes.
        """
        row = self.adapter_record(getattr(adapter, "adapter_id", None))
        for field_ in ("adapter_version", "benchmark", "benchmark_version",
                       "evidence_class"):
            declared = getattr(adapter, field_, None)
            if declared != row[field_]:
                raise ConfirmatoryRunViolation(
                    f"adapter {row['adapter_id']!r} declares {field_}={declared!r} "
                    f"but is registered with {field_}={row[field_]!r}. Re-register "
                    "the adapter, or fix the code -- provenance must not be "
                    "ambiguous at the moment data is produced."
                )

    # -- G5: evidence class / stage bijection ----------------------------
    def _assert_stage_matches_adapter(self, stage: str, adapter: sqlite3.Row) -> None:
        synthetic = adapter["evidence_class"] == "synthetic"
        if synthetic and stage != HARNESS_STAGE:
            raise ConfirmatoryRunViolation(
                f"adapter {adapter['adapter_id']!r} has evidence_class='synthetic', "
                f"so its output is not a measurement of anything and cannot be "
                f"recorded at stage {stage!r}. Use stage={HARNESS_STAGE!r} for "
                "runner-mechanics runs."
            )
        if not synthetic and stage == HARNESS_STAGE:
            raise ConfirmatoryRunViolation(
                f"adapter {adapter['adapter_id']!r} has evidence_class='real', so it "
                f"may not run at stage {HARNESS_STAGE!r}. A real run parked outside "
                "the preregistered stages is an unblinded look at the data."
            )

    # -- G1: arm/method agreement ----------------------------------------
    def _assert_arm_matches_method(self, arm: str, method_id: str) -> None:
        row = self.conn.execute(
            "SELECT is_learning FROM attack_method WHERE method_id = ?", (method_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown method_id {method_id!r}; register it first")
        is_learning = bool(row["is_learning"])
        expected = "secondary_learning" if is_learning else "primary"
        if arm != expected:
            raise QuarantineViolation(
                f"method {method_id!r} has is_learning={is_learning}, so it belongs "
                f"in arm {expected!r}, but arm {arm!r} was supplied. "
                "Primary and secondary arms are preregistered separately."
            )

    # -- escalation-aware trial lifecycle --------------------------------
    #
    # A trial is opened once, passes through N budget checkpoints, and is
    # finalized once. It yields exactly ONE survival observation regardless of
    # how many checkpoints it survived. There is deliberately no API that
    # writes a survival outcome directly from a checkpoint.

    def open_trial(
        self,
        prereg_id: str,
        arm: str,
        stage: str,
        method_id: str,
        escalation_plan: Sequence[int],
        **cell: Any,
    ) -> str:
        if arm not in VALID_ARMS:
            raise ValueError(f"arm must be one of {VALID_ARMS}")
        if stage not in VALID_STAGES:
            raise ValueError(f"stage must be one of {VALID_STAGES}")
        self._assert_arm_matches_method(arm, method_id)
        plan = list(escalation_plan)
        if plan != sorted(set(plan)) or not plan:
            raise ValueError("escalation_plan must be a non-empty ascending list of distinct budgets")

        adapter = self.adapter_record(cell.get("adapter_id"))
        self._assert_stage_matches_adapter(stage, adapter)
        if cell["benchmark"] != adapter["benchmark"]:
            raise ValueError(
                f"trial declares benchmark {cell['benchmark']!r} but adapter "
                f"{adapter['adapter_id']!r} runs {adapter['benchmark']!r}; a task set "
                "and the harness that executes it must be the same object"
            )
        provenance = self._freeze_provenance(cell["config_id"], method_id,
                                             cell["model_id"], adapter)

        tid = f"t_{uuid.uuid4().hex[:12]}"
        columns = (
            "trial_id", "prereg_id", "arm", "stage",
            "config_id", "method_id", "model_id",
            "benchmark", "suite", "task_id", "injection_task_id", "seed",
            "adapter_id", "adapter_version", "benchmark_version",
            "model_snapshot", "defense_impl_ref", "attack_source_ref",
            "defender_models", "attacker_model_snapshot",
            "censored", "event_queries", "budget_ceiling",
            "attack_success", "benign_utility", "utility_under_attack",
            "attacker_refusal_rate", "eval_awareness",
            "attacker_queries", "attacker_tokens_in", "attacker_tokens_out",
            "attacker_usd", "defender_tokens_in", "defender_tokens_out",
            "defender_usd", "wall_clock_s",
            "escalation_plan", "checkpoints_run", "status", "error_detail",
            "created_utc",
        )
        values = (
            tid, prereg_id, arm, stage,
            cell["config_id"], method_id, cell["model_id"],
            cell["benchmark"], cell["suite"], cell["task_id"],
            cell.get("injection_task_id", "none"), cell["seed"],
            *provenance,
            canonical_components(cell.get("defender_models", ())),
            cell.get("attacker_model_snapshot", "n/a"),
            0, 0, plan[-1],           # censored, event_queries, budget_ceiling (provisional)
            0, 0.0, 0.0, 0.0, None,   # outcomes, filled at finalize
            0, 0, 0, 0.0, 0, 0, 0.0, 0.0,   # cost channels
            json.dumps(plan), 0, "pending", None, _utc(),
        )
        # Named columns rather than positional VALUES: adding a column to the
        # schema should not silently shift every field one place to the left.
        self.conn.execute(
            f"INSERT INTO trial ({','.join(columns)}) "
            f"VALUES ({','.join('?' * len(columns))})",
            values,
        )
        self.conn.commit()
        return tid

    def _freeze_provenance(self, config_id: str, method_id: str, model_id: str,
                           adapter: sqlite3.Row) -> tuple[str, ...]:
        """Snapshot the experimental object at the moment the trial opens.

        Everything here is available by join, and is copied anyway. The point is
        that the registries are mutable and the trial is the record of what ran;
        if someone re-points a defense at a new commit, finished trials must keep
        naming the commit they actually used.
        """
        model = self.conn.execute(
            "SELECT snapshot FROM model_version WHERE model_id = ?", (model_id,)
        ).fetchone()
        if model is None:
            raise ValueError(f"unknown model_id {model_id!r}; register it first")
        defense = self.conn.execute(
            "SELECT impl_ref FROM defense_config WHERE config_id = ?", (config_id,)
        ).fetchone()
        if defense is None:
            raise ValueError(f"unknown config_id {config_id!r}; register it first")
        if not defense["impl_ref"]:
            raise ValueError(
                f"defense config {config_id!r} has no impl_ref. An unattributed "
                "defense implementation cannot answer 'was your version a strawman?', "
                "so it may not produce trials."
            )
        method = self.conn.execute(
            "SELECT source_ref FROM attack_method WHERE method_id = ?", (method_id,)
        ).fetchone()
        return (
            adapter["adapter_id"], adapter["adapter_version"],
            adapter["benchmark_version"], model["snapshot"],
            defense["impl_ref"], method["source_ref"],
        )

    def record_checkpoint(
        self,
        trial_id: str,
        checkpoint_budget: int,
        success_observed: bool,
        queries_used: int,
        decision: str,
        success_at_query: int | None = None,
        attacker_usd: float = 0.0,
        defender_usd: float = 0.0,
    ) -> str:
        """Log one budget checkpoint. This is resource-allocation metadata.

        It is NOT a survival observation and nothing in analysis.py reads it.
        """
        prior = self.conn.execute(
            "SELECT checkpoint_index, checkpoint_budget FROM budget_checkpoint "
            "WHERE trial_id=? ORDER BY checkpoint_index DESC LIMIT 1", (trial_id,)
        ).fetchone()
        idx = (prior["checkpoint_index"] + 1) if prior else 1
        if prior and checkpoint_budget <= prior["checkpoint_budget"]:
            raise ValueError(
                f"checkpoint budgets must strictly increase; got {checkpoint_budget} "
                f"after {prior['checkpoint_budget']}"
            )
        plan = json.loads(self.conn.execute(
            "SELECT escalation_plan FROM trial WHERE trial_id=?", (trial_id,)
        ).fetchone()["escalation_plan"])
        if checkpoint_budget not in plan:
            raise ValueError(
                f"budget {checkpoint_budget} is not in the preregistered escalation "
                f"plan {plan}; escalation policy deviations must be preregistered"
            )
        if success_observed and success_at_query is None:
            raise ValueError("success_observed requires success_at_query")

        cid = f"cp_{uuid.uuid4().hex[:10]}"
        self.conn.execute(
            "INSERT INTO budget_checkpoint VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, trial_id, idx, checkpoint_budget, 1, int(success_observed),
             success_at_query, queries_used, decision, attacker_usd, defender_usd, _utc()),
        )
        self.conn.execute(
            "UPDATE trial SET checkpoints_run=? WHERE trial_id=?", (idx, trial_id)
        )
        self.conn.commit()
        return cid

    def finalize_trial(self, trial_id: str, **outcomes: Any) -> None:
        """Collapse the checkpoint history into ONE survival observation.

        The survival fields are DERIVED here, not supplied by the caller, so a
        runner cannot report a censoring status inconsistent with what actually
        happened.
        """
        cps = list(self.conn.execute(
            "SELECT * FROM budget_checkpoint WHERE trial_id=? ORDER BY checkpoint_index",
            (trial_id,),
        ))
        if not cps:
            raise ValueError("cannot finalize a trial with no checkpoints")

        final_ceiling = cps[-1]["checkpoint_budget"]
        hits = [c for c in cps if c["success_observed"]]
        if hits:
            first = hits[0]
            censored, event_q, success = 0, first["success_at_query"], 1
            if event_q > first["checkpoint_budget"]:
                raise ValueError(
                    f"success at query {event_q} exceeds the checkpoint budget "
                    f"{first['checkpoint_budget']} at which it was observed"
                )
        else:
            censored, event_q, success = 1, final_ceiling, 0

        cols = (
            "benign_utility", "utility_under_attack", "attacker_refusal_rate",
            "eval_awareness", "attacker_queries", "attacker_tokens_in",
            "attacker_tokens_out", "attacker_usd", "defender_tokens_in",
            "defender_tokens_out", "defender_usd", "wall_clock_s",
        )
        sets = ", ".join(f"{c}=?" for c in cols)
        self.conn.execute(
            f"UPDATE trial SET censored=?, event_queries=?, budget_ceiling=?, "
            f"attack_success=?, {sets}, status=?, error_detail=? WHERE trial_id=?",
            (censored, event_q, final_ceiling, success,
             *(outcomes.get(c, 0) for c in cols),
             outcomes.get("status", "ok"), outcomes.get("error_detail"), trial_id),
        )
        self.conn.commit()

    def checkpoint_audit(self, trial_id: str) -> list[dict]:
        """For auditing that escalation followed the preregistered plan."""
        return [dict(r) for r in self.conn.execute(
            "SELECT checkpoint_index, checkpoint_budget, success_observed, "
            "success_at_query, queries_used, decision FROM budget_checkpoint "
            "WHERE trial_id=? ORDER BY checkpoint_index", (trial_id,)
        )]

    def log_query(self, trial_id: str, query_index: int, payload: str, **kw: Any) -> str:
        outcome = kw.get("outcome", "failure")
        if outcome not in ("success", "failure", "error"):
            raise ValueError(
                f"outcome must be 'success', 'failure', or 'error'; got {outcome!r}"
            )
        qid = f"q_{uuid.uuid4().hex[:12]}"
        self.conn.execute(
            "INSERT INTO query_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                qid, trial_id, query_index,
                hashlib.sha256(payload.encode()).hexdigest(),
                kw.get("mutation_op"),
                int(kw.get("attacker_refused", 0)),
                outcome,
                int(kw.get("programmatic_success", 0)),
                kw.get("judge_verdict"), kw.get("judge_id"),
                kw.get("attacker_tokens_in", 0), kw.get("attacker_tokens_out", 0),
                kw.get("defender_tokens_in", 0), kw.get("defender_tokens_out", 0),
                kw.get("latency_s", 0.0),
            ),
        )
        return qid

    def commit(self) -> None:
        self.conn.commit()

    # -- G2: no pooled reads ---------------------------------------------
    def survival_data(
        self,
        arm: str,
        stage: str,
        config_id: str,
        *,
        model_id: str,
        method_id: str | None = None,
        clean_only: bool = True,
    ) -> list[tuple[int, int]]:
        """Return [(event_queries, censored), ...] for one cell.

        `arm`, `stage`, and `model_id` are all required and single-valued by
        design. There is no API here that pools across arms, across
        screening/confirmatory stages, or across models -- pooling the first two
        would be selection on the outcome, and pooling the third would average
        over systems that differ in the very property being measured.
        """
        if arm not in VALID_ARMS:
            raise ValueError(f"arm must be one of {VALID_ARMS}")
        if stage not in VALID_STAGES:
            raise ValueError(f"stage must be one of {VALID_STAGES}")
        # The analysis view is real-evidence-only, so asking it for harness rows
        # would silently return nothing. Route explicitly instead: a caller that
        # wants apparatus data has to name the harness stage to get it.
        if stage == HARNESS_STAGE:
            table = "v_trial_harness"
        else:
            table = "v_trial_clean" if clean_only else "trial"
        sql = (f"SELECT event_queries, censored FROM {table} "
               f"WHERE arm=? AND stage=? AND config_id=?")
        params: list[Any] = [arm, stage, config_id]
        if method_id:
            sql += " AND method_id=?"
            params.append(method_id)
        if model_id:
            sql += " AND model_id=?"
            params.append(model_id)
        return [(r["event_queries"], r["censored"]) for r in self.conn.execute(sql, params)]

    def replication_outcomes(
        self,
        arm: str,
        stage: str,
        config_id: str,
        *,
        model_id: str,
        method_id: str | None = None,
    ) -> list[str]:
        """Three-valued outcome vector for a replication slice.

        Deliberately reads the base table rather than `v_trial_clean`: errored
        trials are excluded from survival analysis but are REQUIRED here, because
        the reference ASR track has to reproduce the benchmark's own error
        semantics and the error rate has to be reportable. Evidence class and
        arm/stage quarantine still apply.

        The three values are derived from `status` and `attack_success`; there is
        no separate outcome column on `trial` that could disagree with them.
        """
        if arm not in VALID_ARMS:
            raise ValueError(f"arm must be one of {VALID_ARMS}")
        if stage not in VALID_STAGES:
            raise ValueError(f"stage must be one of {VALID_STAGES}")
        sql = ("SELECT t.status, t.attack_success FROM trial t "
               "JOIN adapter_registry a ON a.adapter_id = t.adapter_id "
               "WHERE t.arm=? AND t.stage=? AND t.config_id=? "
               "AND t.status IN ('ok', 'error')")
        params: list[Any] = [arm, stage, config_id]
        if method_id:
            sql += " AND t.method_id=?"
            params.append(method_id)
        if model_id:
            sql += " AND t.model_id=?"
            params.append(model_id)
        return [
            "error" if r["status"] == "error"
            else ("success" if r["attack_success"] else "failure")
            for r in self.conn.execute(sql, params)
        ]

    def cost_channels(self, arm: str, stage: str, config_id: str, *,
                      model_id: str) -> dict[str, float]:
        """G3: cost is returned as separate channels, never summed.

        `model_id` is required for the same reason `arm` and `stage` are: defender
        overhead averaged across two different models is not a quantity that
        describes anything.
        """
        if arm not in VALID_ARMS:
            raise ValueError(f"arm must be one of {VALID_ARMS}")
        if stage not in VALID_STAGES:
            raise ValueError(f"stage must be one of {VALID_STAGES}")
        if stage == HARNESS_STAGE:
            raise ValueError(
                "cost_channels() is not defined for the harness stage: synthetic "
                "trials spend nothing and measure nothing"
            )
        row = self.conn.execute(
            """SELECT AVG(attacker_usd) a_usd, AVG(defender_usd) d_usd,
                      AVG(attacker_queries) a_q,
                      AVG(attacker_tokens_in + attacker_tokens_out) a_tok,
                      AVG(defender_tokens_in + defender_tokens_out) d_tok
               FROM v_trial_clean
               WHERE arm=? AND stage=? AND config_id=? AND model_id=?""",
            (arm, stage, config_id, model_id),
        ).fetchone()
        return {
            "attacker_usd": row["a_usd"], "defender_usd": row["d_usd"],
            "attacker_queries": row["a_q"], "attacker_tokens": row["a_tok"],
            "defender_tokens": row["d_tok"],
        }

    def configs(self, arm: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT DISTINCT c.* FROM defense_config c
               JOIN trial t ON t.config_id = c.config_id
               WHERE t.arm = ? ORDER BY c.n_components, c.components""",
            (arm,),
        ))

    def refusal_audit(self, arm: str) -> dict[str, Any]:
        """How much data the refusal rule discards. Must be reported, not hidden."""
        # Counted over real-evidence trials only; a synthetic run's refusal rate
        # is a property of the mock, not of any attacker model.
        base = ("FROM trial t JOIN adapter_registry a ON a.adapter_id = t.adapter_id "
                "WHERE t.arm=? AND t.status='ok' AND a.evidence_class='real'")
        total = self.conn.execute(
            f"SELECT COUNT(*) n {base}", (arm,)
        ).fetchone()["n"]
        dropped = self.conn.execute(
            f"SELECT COUNT(*) n {base} AND t.attacker_refusal_rate >= 0.5", (arm,)
        ).fetchone()["n"]
        return {
            "trials_ok": total,
            "dropped_high_refusal": dropped,
            "dropped_frac": (dropped / total) if total else 0.0,
        }
