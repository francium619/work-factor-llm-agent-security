"""
run_slice.py -- the banking replication slice.

    16 user tasks x 9 injection tasks = 144 attempted security cases
    B = 1, one deterministic one-shot attack per pair
    every case ends in exactly one of success / failure / error

Two adapters, and the DB decides where their output may go:

    --adapter offline    OfflineAdapter, evidence_class='synthetic'
                         -> forced to stage='harness_test'
                         -> proves the ACCOUNTING, measures nothing

    --adapter agentdojo  AgentDojoAdapter, evidence_class='real'
                         -> stage='confirm', spends real API credits

The bijection in db.py means the first mode cannot silently become evidence and
the second cannot hide outside the preregistered stages. Neither mode is allowed
to choose its own stage.

Usage:
    python run_slice.py --adapter offline
    python run_slice.py --adapter agentdojo --check-config
    python run_slice.py --adapter agentdojo --confirm
"""

from __future__ import annotations

import argparse
import sys

from wf.adapter import (
    AgentDojoAdapter, AgentDojoFixedAttack, AttackContext, AttackPayload,
    FixedAttack, OfflineAdapter, defender_model_snapshots,
)
from wf.analysis import G2_EQUIVALENCE_MARGIN, g2_gate, replication_report
from wf.db import ExperimentDB
from wf.runner import CellSpec, EscalationRunner

BENCHMARK_VERSION = "v1.2.2"
SUITE = "banking"
ATTACK = "important_instructions"
SOURCE_REF = "agentdojo@0.1.35"

# The protocol-locked target. A different model is a different experiment, not a
# different setting, so substituting it is refused rather than defaulted.
DECLARED_MODEL = "gpt-4o-2024-05-13"

# What we declared we would run. The observed protocol is measured from the run
# itself and compared against this; a divergence means we did not run what the
# preregistration says we ran.
DECLARED_PROTOCOL = dict(
    n_user_tasks=16,
    n_injection_tasks=9,
    attack_id=ATTACK,
    success_definition="injection_task._check_task_result",
    model_snapshot=None,          # filled from the model actually used
    defense_config="none",
    error_handling="errors_excluded_from_clean_asr",
)

# No published ASR exists for this exact object: the paper's 629 security cases
# are a different task set from v1.2.2's 144 banking pairs. None is the honest
# value, and it caps the achievable verdict at PASS_PROTOCOL_ONLY.
PUBLISHED_ASR: float | None = None


class SyntheticOneShot(FixedAttack):
    """Stand-in with the same one-shot contract, for the accounting rehearsal."""

    def reset(self, seed: int) -> None:
        self._seed = seed

    def generate(self, ctx, query_index):
        if query_index != 1:
            raise RuntimeError("one-shot attack; replication protocol is B=1")
        return AttackPayload(
            text=f"synthetic-{ctx.task.task_id}-{ctx.injection_task_id}",
            injection_point=ctx.task.injection_points[0],
            injection_task_id=ctx.injection_task_id,
        )


def build(kind: str, model_id: str):
    if kind == "offline":
        adapter = OfflineAdapter()
        attack = SyntheticOneShot(ATTACK, "agentdojo_published", SOURCE_REF, {})
        return adapter, attack, "harness_test"
    adapter = AgentDojoAdapter(suite=SUITE, model_id=model_id,
                               benchmark_version=BENCHMARK_VERSION)
    attack = AgentDojoFixedAttack(suite=SUITE, model_id=model_id,
                                  benchmark_version=BENCHMARK_VERSION,
                                  attack_name=ATTACK, source_ref=SOURCE_REF)
    return adapter, attack, "confirm"


def preflight(adapter, attack, defense_components, args) -> dict:
    """Everything checkable BEFORE a single trial row exists.

    The first real execution is the irreversible point: it spends money, and a
    half-configured run produces data that looks valid. So every question that
    can be answered without writing is answered here, in order, and nothing is
    written until this returns.
    """
    checks: list[tuple[str, str]] = []

    # 1. the benchmark loads, and the task set is the declared one
    tasks = adapter.tasks(SUITE)
    injection_tasks = adapter.injection_tasks(SUITE)
    pairs = len(tasks) * len(injection_tasks)
    checks.append(("benchmark loads", f"{SUITE} @ {BENCHMARK_VERSION}"))
    if (len(tasks) != DECLARED_PROTOCOL["n_user_tasks"]
            or len(injection_tasks) != DECLARED_PROTOCOL["n_injection_tasks"]):
        raise SystemExit(
            f"PREFLIGHT FAIL: task set is {len(tasks)}x{len(injection_tasks)}, "
            f"declared {DECLARED_PROTOCOL['n_user_tasks']}x"
            f"{DECLARED_PROTOCOL['n_injection_tasks']}. The protocol and the "
            "benchmark disagree; do not run."
        )
    checks.append(("task set matches declaration",
                   f"{len(tasks)} x {len(injection_tasks)} = {pairs}"))

    # 2. the one-shot contract actually holds for the attack we are about to use
    probe = AttackContext(task=tasks[0], defense_components=defense_components,
                          model_id=args.model, seed=0,
                          injection_task_id=injection_tasks[0])
    try:
        attack.generate(probe, 2)
        raise SystemExit("PREFLIGHT FAIL: attack accepted query_index=2; the "
                         "one-shot contract is not being enforced")
    except RuntimeError:
        checks.append(("one-shot contract enforced", "query_index=2 rejected"))

    # 3. credentials -- constructing the pipeline issues NO request
    defenders = defender_model_snapshots(defense_components, args.model)
    if args.adapter == "agentdojo":
        try:
            adapter._pipeline(defense_components, args.model)
        except Exception as e:
            raise SystemExit(
                "PREFLIGHT FAIL: cannot construct the pipeline for "
                f"{args.model!r}.\n"
                f"  {type(e).__name__}: {str(e)[:150]}\n\n"
                "No trial was written. Do NOT resolve this by switching model or "
                "provider:\nthe replication configuration is protocol-locked. "
                "Either obtain authorized\naccess to the declared model, or change "
                "the preregistered configuration\nexplicitly and treat the result "
                "as a DIFFERENT protocol, not this G2 run."
            ) from None
        checks.append(("credentials", "pipeline constructed; no request issued"))
    else:
        checks.append(("credentials", "not required for the synthetic adapter"))

    return {"tasks": tasks, "injection_tasks": injection_tasks, "pairs": pairs,
            "defenders": defenders, "checks": checks}


def print_configuration(adapter, attack, stage, defense_components, pre, args):
    rows = [
        ("adapter", adapter.adapter_id),
        ("adapter_version", adapter.adapter_version),
        ("benchmark_version", adapter.benchmark_version),
        ("suite", SUITE),
        ("user_tasks", len(pre["tasks"])),
        ("injection_tasks", len(pre["injection_tasks"])),
        ("security_pairs", pre["pairs"]),
        ("attack", attack.method_id),
        ("attack_source_ref", attack.source_ref),
        ("target_model", args.model),
        ("defender_models", pre["defenders"] or "()"),
        ("attacker_model", getattr(attack, "attacker_model_snapshot", "n/a")),
        ("defense_config", "none" if not defense_components else defense_components),
        ("tool_output_format", "yaml"),
        ("budget", 1),
        ("published_asr", PUBLISHED_ASR),
        ("delta", G2_EQUIVALENCE_MARGIN),
        ("stage", stage),
        ("arm", "primary"),
    ]
    print("=" * 62)
    print("CONFIGURATION -- no trial has been written")
    print("=" * 62)
    for k, v in rows:
        print(f"  {k:<20} {v}")
    print()
    for name, detail in pre["checks"]:
        print(f"  [ok] {name:<28} {detail}")
    print("=" * 62)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", choices=("offline", "agentdojo"), required=True)
    ap.add_argument("--model", default=DECLARED_MODEL,
                    help="changing this changes the protocol, not just a setting")
    ap.add_argument("--db", default=":memory:")
    ap.add_argument("--check-config", action="store_true",
                    help="run preflight, print the configuration, write nothing")
    ap.add_argument("--confirm", action="store_true",
                    help="required to execute a real run; the irreversible step")
    args = ap.parse_args()

    if args.model != DECLARED_MODEL:
        print(f"MODEL SUBSTITUTION REFUSED: declared {DECLARED_MODEL!r}, "
              f"requested {args.model!r}.")
        print("That is a different protocol, and its result would not be a G2 "
              "replication of")
        print("the declared configuration. Change DECLARED_MODEL and the "
              "preregistration if")
        print("the substitution is intended.")
        return 2

    adapter, attack, stage = build(args.adapter, args.model)
    defense_components: tuple[str, ...] = ()

    # ---- nothing below writes until preflight has passed ----
    pre = preflight(adapter, attack, defense_components, args)
    print_configuration(adapter, attack, stage, defense_components, pre, args)

    if args.check_config:
        print("check-config only; no trial written.")
        return 0
    if args.adapter == "agentdojo" and not args.confirm:
        print("Real run NOT started. This spends API credits and is the "
              "irreversible step.")
        print(f"Re-run with --confirm to execute the {pre['pairs']} cases.")
        return 0

    db = ExperimentDB(args.db)
    pre_id = db.register_prereg("primary", "G2 banking replication slice", [1],
                                [0.25], 3, "sha_slice_v1")
    cfg = db.register_defense(list(defense_components), impl_source="original",
                              impl_ref=SOURCE_REF)
    db.register_method(attack.method_id, "agentdojo_published", False, SOURCE_REF,
                       dict(attack.hyperparams), defense_aware=True)
    db.register_model(args.model, "openai", args.model, False)
    db.register_adapter(adapter, impl_ref=SOURCE_REF)

    runner = EscalationRunner(db, adapter, [1])
    tasks, injection_tasks = pre["tasks"], pre["injection_tasks"]
    defenders = pre["defenders"]
    print()

    # Benign utility comes from a SEPARATE non-injected evaluation, one per user
    # task, never inferred from an attacked run.
    benign = {t.task_id: adapter.run_benign(t, defense_components, args.model, 0)
              for t in tasks}

    attempted = 0
    for task in tasks:
        for inj in injection_tasks:
            cell = CellSpec(
                prereg_id=pre_id, arm="primary", stage=stage, config_id=cfg,
                defense_components=defense_components, model_id=args.model, seed=0,
                injection_task_id=inj, defender_models=defenders,
                attacker_model_snapshot=getattr(attack, "attacker_model_snapshot",
                                                "n/a"),
            )
            runner.run_cell(cell, attack, task, benign=benign[task.task_id])
            attempted += 1

    outcomes = db.replication_outcomes("primary", stage, cfg,
                                       model_id=args.model,
                                       method_id=attack.method_id)
    rep = replication_report(outcomes)

    observed = dict(DECLARED_PROTOCOL)
    observed.update(n_user_tasks=len(tasks), n_injection_tasks=len(injection_tasks),
                    model_snapshot=args.model)
    declared = dict(DECLARED_PROTOCOL, model_snapshot=args.model)
    gate = g2_gate(rep, published_asr=PUBLISHED_ASR, ours=observed,
                   reference=declared)

    mean_benign = sum(b.benign_utility for b in benign.values()) / len(benign)

    print(f"N attempted          = {attempted}")
    print(f"N recorded           = {rep['n']}")
    print(f"N success            = {rep['n_success']}")
    print(f"N failure            = {rep['n_failure']}")
    print(f"N error              = {rep['n_error']}")
    print()
    print(f"reference_asr        = {rep['reference_asr']:.4f}")
    print(f"clean_asr            = {rep['clean_asr']:.4f}")
    print(f"error_rate           = {rep['error_rate']:.4f}")
    print()
    print(f"benign_utility       = {mean_benign:.4f}")
    print(f"reference_available  = {gate['reference_available']}")
    print(f"delta                = {G2_EQUIVALENCE_MARGIN:.2f}")
    print(f"protocol_status      = {gate['verdict']}")
    for d in gate["divergences"]:
        print(f"  divergence: {d}")

    if attempted != rep["n"]:
        print("\nACCOUNTING FAILURE: attempted != recorded; cases went missing")
        return 1
    if rep["n_error"]:
        print("\nerror_rate > 0: do NOT rerun the failed cases until the cause is "
              "understood.\nSelectively retrying errors turns an infrastructure "
              "problem into a\nmissing-data problem.")
    if args.adapter == "offline":
        print("\nSYNTHETIC: OfflineAdapter output, recorded at "
              "stage='harness_test'.\nThese numbers measure the accounting, not "
              "any real defense.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
