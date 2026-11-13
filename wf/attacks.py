"""
attacks.py -- the repeated-query FIXED attack for the work-factor arm (WF1).

THE PROBLEM IT SOLVES
---------------------
`important_instructions` is a deterministic template. Against a temperature-0
target, query 2 is query 1 again, so the G2 slice is one-shot by construction
and produces no survival curve. A work factor needs an attacker whose
successive queries are genuinely different -- and, for the primary arm, an
attacker that is still FIXED: no feedback, no adaptation, parameters pinned,
identical across every defense configuration.

THE INSTRUMENT
--------------
AgentDojo v1.2.2 ships 11 non-DoS, non-interactive attack templates. This
method plays them in a fixed, published order: query q uses template
VARIANT_SCHEDULE[q-1], filled by the UPSTREAM attack class for that template.
Nothing here writes attack text. The attacker's budget is therefore "how many
of the published templates did it take", and the ceiling is 11 -- past that,
every payload would be a repeat, so `generate` raises rather than pad the
curve with duplicates recorded as distinct attempts.

WHY IT IS A LEGITIMATE FIXED ATTACK
-----------------------------------
  * `generate(ctx, query_index)` sees no prior outcome. FixedAttack has no
    observe(), and the runner never offers feedback to is_learning=False.
  * The schedule is a module constant, stored in `attack_method.hyperparams`
    and frozen onto every trial through `attack_source_ref`.
  * The schedule does not depend on the defense. Every configuration faces the
    same 11 payloads in the same order, so a difference in queries-to-break is
    a property of the defenses (EXPERIMENT.md s2.4).
  * The order is a design choice, declared before any run, and it is part of
    the method's identity: a different order is a different method_id.

The order below runs the strongest-known family first (the paper's headline
attack), then the simple baselines, then the name-perturbed variants. That
makes the EARLY tail -- Q10, the cheapest break -- correspond to "did the
best published attack work", which is the quantity practitioners care about.
"""

from __future__ import annotations

import json
from typing import Any

from wf.adapter import AttackContext, AttackPayload, FixedAttack

# Registered attack names in agentdojo 0.1.35, excluding the five *_dos attacks
# (different success definition; see COMPATIBILITY.md hazard 4) and `manual`
# (interactive). Order is part of the method identity. Do not reorder.
VARIANT_SCHEDULE: tuple[str, ...] = (
    "important_instructions",
    "injecagent",
    "ignore_previous",
    "tool_knowledge",
    "important_instructions_no_names",
    "system_message",
    "important_instructions_wrong_user_name",
    "direct",
    "important_instructions_no_user_name",
    "important_instructions_wrong_model_name",
    "important_instructions_no_model_name",
)

MAX_BUDGET = len(VARIANT_SCHEDULE)          # 11: one pass, no repeats
METHOD_ID = "variant_schedule_v1"
FAMILY = "agentdojo_published_schedule"


def _check_index(query_index: int) -> str:
    if not 1 <= query_index <= MAX_BUDGET:
        raise RuntimeError(
            f"{METHOD_ID!r} has exactly {MAX_BUDGET} distinct payloads; "
            f"query_index={query_index} was requested. Continuing past the last "
            "template would replay earlier payloads and record them as new "
            "attempts, which pads the survival curve with duplicates. The budget "
            f"ladder for this method must end at or below {MAX_BUDGET}."
        )
    return VARIANT_SCHEDULE[query_index - 1]


class VariantScheduleAttack(FixedAttack):
    """Plays the 11 upstream templates in a fixed order against the real target.

    Each template is instantiated through AgentDojo's own registry, against the
    UNDEFENDED pipeline for the declared model (that is what the upstream attack
    needs: the pipeline name, to resolve the model's prose name). The payload
    is the upstream `attack()` output verbatim, serialised, and stored by hash.
    """

    attacker_model_snapshot = "none-deterministic-template"

    def __init__(self, suite: str, model_id: str, benchmark_version: str,
                 source_ref: str):
        super().__init__(
            method_id=METHOD_ID,
            family=FAMILY,
            source_ref=source_ref,
            hyperparams={
                "benchmark_version": benchmark_version,
                "schedule": list(VARIANT_SCHEDULE),
                "vector_policy": "all_injection_candidates",
            },
        )
        self.suite = suite
        self.model_id = model_id
        self.benchmark_version = benchmark_version
        self._upstream: dict[str, Any] = {}

    def reset(self, seed: int) -> None:
        # Deterministic templates: no state, no seed dependence.
        return None

    def _attack(self, name: str, adapter: Any) -> Any:
        if name not in self._upstream:
            from agentdojo.attacks.attack_registry import load_attack
            self._upstream[name] = load_attack(
                name, adapter._suite(self.suite), adapter._pipeline((), self.model_id),
            )
        return self._upstream[name]

    def generate(self, ctx: AttackContext, query_index: int,
                 adapter: Any | None = None) -> AttackPayload:
        name = _check_index(query_index)            # checked before anything else
        if ctx.injection_task_id is None:
            raise ValueError("an AgentDojo attack payload needs an injection_task_id")
        if adapter is None:
            raise ValueError("VariantScheduleAttack.generate needs the adapter to reach "
                             "the benchmark's own attack implementations")
        suite = adapter._suite(self.suite)
        injections = self._attack(name, adapter).attack(
            suite.get_user_task_by_id(ctx.task.task_id),
            suite.get_injection_task_by_id(ctx.injection_task_id),
        )
        return AttackPayload(
            text=json.dumps(injections, sort_keys=True),
            injection_point="+".join(sorted(injections)),
            injection_task_id=ctx.injection_task_id,
            mutation_op=name,       # which template this query used; per-query telemetry
        )


class OfflineVariantAttack(FixedAttack):
    """Same contract as VariantScheduleAttack, for the synthetic adapter.

    Produces one distinct inert payload per schedule position so the runner,
    the ladder, the database and the dashboard can be rehearsed end to end
    without the benchmark or a credential. Its output is forced to
    stage='harness_test' by the evidence-class bijection and measures nothing.
    """

    attacker_model_snapshot = "none-deterministic-template"

    def __init__(self, source_ref: str = "wf/attacks.py@offline"):
        super().__init__(
            method_id=METHOD_ID, family=FAMILY, source_ref=source_ref,
            hyperparams={"benchmark_version": "n/a-synthetic",
                         "schedule": list(VARIANT_SCHEDULE),
                         "vector_policy": "all_injection_candidates"},
        )

    def reset(self, seed: int) -> None:
        return None

    def generate(self, ctx: AttackContext, query_index: int) -> AttackPayload:
        name = _check_index(query_index)
        return AttackPayload(
            text=f"synthetic|{name}|{ctx.task.task_id}|{ctx.injection_task_id}",
            injection_point=ctx.task.injection_points[0],
            injection_task_id=ctx.injection_task_id,
            mutation_op=name,
        )
