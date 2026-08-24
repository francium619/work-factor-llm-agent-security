"""
runner.py -- sequential budget escalation.

Executes one cell (defense, attack, model, task, seed) across an ascending
budget ladder and produces EXACTLY ONE survival observation.

    B=10  no success  -> checkpoint(decision='escalate')
    B=25  no success  -> checkpoint(decision='escalate')
    B=50  success @37 -> checkpoint(decision='stop_success')
                      -> finalize: event_queries=37, censored=0, ceiling=50

Three checkpoints, one trial. The runner never writes survival fields itself --
`finalize_trial` derives them from the checkpoint history, so a buggy runner
produces a crash rather than a plausible-looking wrong number.

The runner names the adapter it holds when opening a trial, and refuses to
construct at all unless that adapter matches its registration. It does not
decide whether the adapter's output counts as evidence -- the registry does.

This module imports nothing from wf.analysis.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from typing import Sequence

from wf.adapter import (
    AttackContext, AttackMethod, BenignRunResult, RunResult, TargetAdapter, TaskSpec,
)
from wf.db import ExperimentDB


@dataclass
class CellSpec:
    prereg_id: str
    arm: str
    stage: str
    config_id: str
    defense_components: tuple[str, ...]
    model_id: str
    seed: int
    injection_task_id: str = "none"
    defender_models: tuple[str, ...] = ()
    attacker_model_snapshot: str = "n/a"


class EscalationRunner:
    def __init__(self, db: ExperimentDB, adapter: TargetAdapter,
                 escalation_plan: Sequence[int]):
        plan = list(escalation_plan)
        if plan != sorted(set(plan)) or not plan:
            raise ValueError("escalation_plan must be ascending and distinct")
        # The adapter must already be registered, and must agree with its
        # registration, before this runner can execute anything. Provenance is
        # settled up front rather than attached to results afterwards.
        db.assert_adapter_matches(adapter)
        self.db = db
        self.adapter = adapter
        self.plan = plan

    def run_cell(self, cell: CellSpec, method: AttackMethod, task: TaskSpec,
                 benign: BenignRunResult | None = None) -> str:
        """Run one cell. `benign` comes from a SEPARATE non-injected evaluation.

        It is a parameter rather than a field read off the attacked run, because
        AgentDojo returns `(utility, True)` when there is no injection task and
        that True is vacuous. Benign utility and security outcomes are produced
        by different executions and are never inferred from one another.
        """
        trial_id = self.db.open_trial(
            prereg_id=cell.prereg_id, arm=cell.arm, stage=cell.stage,
            method_id=method.method_id, escalation_plan=self.plan,
            config_id=cell.config_id, model_id=cell.model_id,
            benchmark=task.benchmark, suite=task.suite, task_id=task.task_id,
            injection_task_id=cell.injection_task_id,
            seed=cell.seed, adapter_id=self.adapter.adapter_id,
            defender_models=cell.defender_models,
            attacker_model_snapshot=cell.attacker_model_snapshot,
        )

        # Decided once, by signature, rather than by catching TypeError -- a
        # TypeError raised inside generate() must not be mistaken for "this
        # method takes a different signature" and silently retried.
        wants_adapter = "adapter" in inspect.signature(method.generate).parameters

        ctx = AttackContext(task=task, defense_components=cell.defense_components,
                            model_id=cell.model_id, seed=cell.seed,
                            injection_task_id=(None if cell.injection_task_id == "none"
                                               else cell.injection_task_id))
        method.reset(cell.seed)

        q = 0
        refusals = 0
        errors = 0
        last_error: str | None = None
        a_tok_in = a_tok_out = d_tok_in = d_tok_out = 0
        t0 = time.time()
        success_at: int | None = None
        last_result: RunResult | None = None

        for budget in self.plan:
            while q < budget and success_at is None:
                q += 1
                payload = (method.generate(ctx, q, adapter=self.adapter)
                           if wants_adapter else method.generate(ctx, q))
                a_tok_in += payload.generation_tokens_in
                a_tok_out += payload.generation_tokens_out
                if payload.refused:
                    refusals += 1
                    self.db.log_query(trial_id, q, payload.text,
                                      attacker_refused=1, programmatic_success=0,
                                      mutation_op=payload.mutation_op)
                    continue

                res = self.adapter.run_attempt(
                    task, payload, cell.defense_components, cell.model_id, cell.seed
                )
                last_result = res
                d_tok_in += res.defender_tokens_in
                d_tok_out += res.defender_tokens_out
                if res.outcome == "error":
                    errors += 1
                    last_error = res.error
                self.db.log_query(
                    trial_id, q, payload.text,
                    mutation_op=payload.mutation_op, attacker_refused=0,
                    outcome=res.outcome,
                    programmatic_success=int(res.programmatic_success is True),
                    judge_verdict=(None if res.judge_verdict is None else int(res.judge_verdict)),
                    attacker_tokens_in=payload.generation_tokens_in,
                    attacker_tokens_out=payload.generation_tokens_out,
                    defender_tokens_in=res.defender_tokens_in,
                    defender_tokens_out=res.defender_tokens_out,
                    latency_s=res.latency_s,
                )

                # Feedback is offered ONLY to declared learning methods. A fixed
                # attack cannot receive it even if someone adds an observe().
                # An errored attempt teaches nothing -- there was no verdict --
                # so it is not offered to the learner either.
                if res.programmatic_success is True:
                    success_at = q
                elif res.programmatic_success is False and getattr(method, "is_learning", False):
                    method.observe(payload, res)

            reached_success = success_at is not None
            self.db.record_checkpoint(
                trial_id=trial_id, checkpoint_budget=budget,
                success_observed=reached_success, success_at_query=success_at,
                queries_used=q,
                decision=("stop_success" if reached_success
                          else "escalate" if budget != self.plan[-1]
                          else "stop_exhausted"),
            )
            if reached_success:
                break

        self.db.commit()
        # A trial with no success but with attempts that never executed is NOT a
        # clean censored observation: we cannot claim the defense survived a
        # budget we did not actually spend. It is finalized as an error, which
        # keeps it out of v_trial_clean while leaving it visible to
        # replication_outcomes() and to the error-rate report.
        errored = success_at is None and errors > 0
        self.db.finalize_trial(
            trial_id,
            benign_utility=(benign.benign_utility if benign else 0.0),
            utility_under_attack=(last_result.utility_under_attack if last_result else 0.0),
            attacker_refusal_rate=(refusals / q if q else 0.0),
            eval_awareness=(int(last_result.eval_awareness)
                            if last_result and last_result.eval_awareness is not None else None),
            attacker_queries=q,
            attacker_tokens_in=a_tok_in, attacker_tokens_out=a_tok_out,
            defender_tokens_in=d_tok_in, defender_tokens_out=d_tok_out,
            wall_clock_s=time.time() - t0,
            status=("error" if errored else "ok"),
            error_detail=last_error if errored else None,
        )
        return trial_id
