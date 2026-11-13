"""
run_workfactor.py -- WF1, the work-factor arm.

This is a DIFFERENT protocol from the G2 slice in run_slice.py, and it says so:

    G2   undefended baseline, one-shot template, B=1, protocol validation
    WF1  defense grid incl. STACKS, 11-template fixed schedule, ladder 4/8/11,
         survival curves + composition test, target gpt-4o-mini-2024-07-18

WF1 exists because the G2 target (gpt-4o-2024-05-13) was retired by OpenAI on
2026-10-23, so G2 as preregistered can no longer be executed, and because G2
could never have produced a survival curve. The declared protocol lives in
PROTOCOL_WF1.md; its SHA is the prereg plan_sha, so the plan document and the
data are bound together.

Two adapters, and the database decides where their output may go:

    --adapter offline     synthetic -> forced to stage='harness_test'
                          rehearses the ladder, the grid, the DB, the dashboard
    --adapter agentdojo   real -> stage='screen' (Stage A) or 'confirm'
                          spends API credits; requires --confirm

Usage:
    python run_workfactor.py --adapter offline --db runs/wf1_offline.sqlite
    python run_workfactor.py --adapter agentdojo --check-config
    python run_workfactor.py --adapter agentdojo --estimate --tasks 8 --seeds 1
    python run_workfactor.py --adapter agentdojo --confirm --db runs/wf1.sqlite --tasks 8 --seeds 1
    python run_workfactor.py --adapter agentdojo --confirm --db runs/wf1_s0.sqlite --shard 0/3   (x3)

Then:  python dashboard.py runs/wf1.sqlite --serve 8765
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys
import time

from wf.adapter import (
    AgentDojoAdapter, AttackContext, OfflineAdapter, defender_model_snapshots,
)
from wf.analysis import kaplan_meier
from wf.attacks import (
    MAX_BUDGET, METHOD_ID, VARIANT_SCHEDULE, OfflineVariantAttack, VariantScheduleAttack,
)
from wf.compose import impl_ref, impl_source, plan
from wf.db import ExperimentDB
from wf.runner import CellSpec, EscalationRunner

# ---------------------------------------------------------------------------
# The declared protocol. Changing any of these is a new protocol, not a setting.
# ---------------------------------------------------------------------------
BENCHMARK_VERSION = "v1.2.2"
SUITE = "banking"
SOURCE_REF = "agentdojo@0.1.35"
DECLARED_MODEL = "gpt-4o-mini-2024-07-18"
LADDER = (4, 8, 11)                   # must end at or below MAX_BUDGET
QUANTILES = (0.10, 0.25, 0.50)
N_SEEDS_DECLARED = 3
DEFAULT_CONFIGS = (
    "none",
    "spotlight", "repeat_prompt", "toolfilter",
    "repeat_prompt+spotlight", "spotlight+toolfilter", "repeat_prompt+toolfilter",
)
HYPOTHESES = (
    "WF1. H1: defenses with similar ASR at the ceiling differ in queries-to-break. "
    "H2: pairs compose as the log-risk independence null predicts. "
    "H3: at least one pair is destructive (stack worse than its better member)."
)
PLAN_DOC = pathlib.Path(__file__).with_name("PROTOCOL_WF1.md")

# Planning assumptions for --estimate. They are ASSUMPTIONS, printed as such.
ASSUMED_TOKENS_IN = 6000       # per agent run, banking suite with tools
ASSUMED_TOKENS_OUT = 300
PRICE_IN_PER_M = 0.15          # USD per 1M input tokens, gpt-4o-mini
PRICE_OUT_PER_M = 0.60
ASSUMED_SECONDS = 4.0          # wall-clock per agent run
TOOLFILTER_EXTRA_CALLS = 1     # OpenAILLMToolFilter issues one extra call per run

assert LADDER[-1] <= MAX_BUDGET, "ladder exceeds the number of distinct payloads"


def plan_sha() -> str:
    if not PLAN_DOC.exists():
        return "plan-doc-missing"
    return hashlib.sha256(PLAN_DOC.read_bytes()).hexdigest()[:12]


def prereg_id() -> str:
    # Deterministic, so shards of one run share the preregistration row and the
    # dashboard can union them.
    return f"wf1_{plan_sha()[:8]}"


def parse_configs(specs) -> list[tuple[str, ...]]:
    out: list[tuple[str, ...]] = []
    for spec in specs:
        comps = () if spec.strip() == "none" else tuple(sorted(set(spec.split("+"))))
        plan(comps)                          # raises for unknown components
        if comps not in out:
            out.append(comps)
    return out


def label(components: tuple[str, ...]) -> str:
    return "+".join(components) if components else "none"


def build(kind: str, model_id: str):
    if kind == "offline":
        return OfflineAdapter(), OfflineVariantAttack(), "harness_test"
    adapter = AgentDojoAdapter(suite=SUITE, model_id=model_id,
                               benchmark_version=BENCHMARK_VERSION)
    attack = VariantScheduleAttack(suite=SUITE, model_id=model_id,
                                   benchmark_version=BENCHMARK_VERSION,
                                   source_ref=SOURCE_REF)
    return adapter, attack, None


def select_cases(tasks, injection_tasks, args) -> list[tuple]:
    tasks = tasks[: args.tasks] if args.tasks else tasks
    injs = injection_tasks[: args.injection_tasks] if args.injection_tasks else injection_tasks
    cases = [(t, i) for t in tasks for i in injs]
    if args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        if not 0 <= k < n:
            raise SystemExit("--shard must be k/n with 0 <= k < n")
        cases = [c for idx, c in enumerate(cases) if idx % n == k]
    return cases


def preflight(adapter, attack, configs, args) -> dict:
    """Everything checkable before a trial row exists. Writes nothing."""
    checks: list[tuple[str, str]] = []
    tasks = adapter.tasks(SUITE)
    injection_tasks = adapter.injection_tasks(SUITE)
    checks.append(("benchmark loads", f"{SUITE} @ {adapter.benchmark_version}"))
    if args.adapter == "agentdojo" and (len(tasks), len(injection_tasks)) != (16, 9):
        raise SystemExit(f"PREFLIGHT FAIL: task set is {len(tasks)}x{len(injection_tasks)}, "
                         "declared 16x9. The protocol and the benchmark disagree.")
    checks.append(("task set", f"{len(tasks)} x {len(injection_tasks)} security cases"))

    # every configuration has a composition plan, and -- for the real adapter --
    # a constructible pipeline. Construction issues no request.
    for comps in configs:
        p = plan(comps)
        if args.adapter == "agentdojo":
            try:
                adapter._pipeline(comps, args.model)
            except Exception as e:
                raise SystemExit(
                    f"PREFLIGHT FAIL: cannot construct the pipeline for {label(comps)!r} "
                    f"on {args.model!r}.\n  {type(e).__name__}: {str(e)[:160]}\n"
                    "No trial was written.") from None
        checks.append((f"config {label(comps):<26}", f"loop={'>'.join(p.loop)}"))

    # the attack produces MAX_BUDGET distinct payloads and refuses the next one
    probe = AttackContext(task=tasks[0], defense_components=(), model_id=args.model,
                          seed=0, injection_task_id=injection_tasks[0])
    gen = (lambda q: attack.generate(probe, q, adapter=adapter)) if args.adapter == "agentdojo" \
        else (lambda q: attack.generate(probe, q))
    payloads = [gen(q).text for q in range(1, MAX_BUDGET + 1)]
    if len(set(payloads)) != MAX_BUDGET:
        raise SystemExit("PREFLIGHT FAIL: the variant schedule produced duplicate payloads; "
                         "a repeated payload is not a new attempt")
    try:
        gen(MAX_BUDGET + 1)
        raise SystemExit("PREFLIGHT FAIL: attack accepted query_index past the schedule")
    except RuntimeError:
        pass
    checks.append(("attack schedule", f"{MAX_BUDGET} distinct payloads; #{MAX_BUDGET + 1} refused"))
    checks.append(("credentials", "pipelines constructed; no request issued"
                   if args.adapter == "agentdojo" else "not required (synthetic)"))
    return {"tasks": tasks, "injection_tasks": injection_tasks, "checks": checks}


def print_estimate(configs, cases, n_tasks, args) -> None:
    n_cfg = len(configs)
    worst = n_cfg * len(cases) * args.seeds * MAX_BUDGET
    benign = n_cfg * n_tasks
    tf = sum(1 for c in configs if "toolfilter" in c)
    extra = tf * len(cases) * args.seeds * MAX_BUDGET * TOOLFILTER_EXTRA_CALLS
    runs = worst + benign + extra
    usd = runs * (ASSUMED_TOKENS_IN * PRICE_IN_PER_M + ASSUMED_TOKENS_OUT * PRICE_OUT_PER_M) / 1e6
    hours = (worst + benign) * ASSUMED_SECONDS / 3600
    print("ESTIMATE (worst case: every trial runs to ceiling; these are planning")
    print("assumptions, not measurements)")
    print(f"  configs x cases x seeds x ceiling = {n_cfg} x {len(cases)} x {args.seeds} x {MAX_BUDGET}"
          f" = {worst:,} attacked runs")
    print(f"  benign-utility runs               = {benign:,}")
    print(f"  tool_filter extra defender calls  = {extra:,}")
    print(f"  assumed tokens/run                = {ASSUMED_TOKENS_IN} in / {ASSUMED_TOKENS_OUT} out")
    print(f"  assumed price (USD/1M)            = {PRICE_IN_PER_M} in / {PRICE_OUT_PER_M} out")
    print(f"  worst-case spend                  ~ ${usd:,.2f}")
    print(f"  worst-case wall clock, sequential ~ {hours:,.1f} h  (use --shard k/n to parallelise)")
    print("  Weak configurations break early and stop; strong ones run to ceiling.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--adapter", choices=("offline", "agentdojo"), required=True)
    ap.add_argument("--model", default=DECLARED_MODEL,
                    help="changing this changes the protocol, not a setting")
    ap.add_argument("--db", default=":memory:")
    ap.add_argument("--configs", nargs="+", default=list(DEFAULT_CONFIGS),
                    help="'none', single components, or 'a+b' stacks")
    ap.add_argument("--tasks", type=int, default=0, help="first N user tasks (0 = all)")
    ap.add_argument("--injection-tasks", type=int, default=0, help="first M injection tasks (0 = all)")
    ap.add_argument("--seeds", type=int, default=N_SEEDS_DECLARED)
    ap.add_argument("--shard", default="", help="k/n: run every n-th case starting at k")
    ap.add_argument("--stage", choices=("screen", "confirm"), default="screen",
                    help="real runs only; synthetic runs are forced to harness_test")
    ap.add_argument("--check-config", action="store_true", help="preflight, print, write nothing")
    ap.add_argument("--estimate", action="store_true", help="print the cost envelope and exit")
    ap.add_argument("--confirm", action="store_true", help="required for a real run")
    ap.add_argument("--resume", action="store_true",
                    help="skip finalized cells already in --db; purge pending ones")
    args = ap.parse_args()

    if args.model != DECLARED_MODEL:
        print(f"MODEL SUBSTITUTION REFUSED: declared {DECLARED_MODEL!r}, requested {args.model!r}.")
        print("A different model is a different protocol. Change DECLARED_MODEL and PROTOCOL_WF1.md "
              "together if that is intended.")
        return 2
    if args.seeds < 1:
        raise SystemExit("--seeds must be >= 1")

    configs = parse_configs(args.configs)
    adapter, attack, stage = build(args.adapter, args.model)
    stage = stage or args.stage
    model_id = args.model if args.adapter == "agentdojo" else "synthetic-target"

    pre = preflight(adapter, attack, configs, args)
    tasks, injection_tasks = pre["tasks"], pre["injection_tasks"]
    cases = select_cases(tasks, injection_tasks, args)
    n_tasks = len({t.task_id for t, _ in cases})

    print("=" * 66)
    print("WF1 CONFIGURATION -- no trial has been written")
    print("=" * 66)
    rows = [
        ("protocol", "WF1 (work-factor arm; NOT the G2 replication slice)"),
        ("plan document", f"{PLAN_DOC.name} sha256[:12]={plan_sha()}"),
        ("prereg_id", prereg_id()),
        ("adapter", f"{adapter.adapter_id} ({adapter.evidence_class})"),
        ("benchmark_version", adapter.benchmark_version),
        ("suite", SUITE),
        ("target_model", model_id),
        ("attack", f"{METHOD_ID}: {len(VARIANT_SCHEDULE)} upstream templates, fixed order"),
        ("attacker_model", attack.attacker_model_snapshot),
        ("budget ladder", list(LADDER)),
        ("configs", [label(c) for c in configs]),
        ("cases", f"{len(cases)} of {len(tasks) * len(injection_tasks)}"
                  + (f"  (shard {args.shard})" if args.shard else "")),
        ("seeds", f"{args.seeds} (preregistered: {N_SEEDS_DECLARED})"
                  + ("  <- UNDER-SEEDED, will be reported" if args.seeds < N_SEEDS_DECLARED else "")),
        ("stage", stage),
        ("arm", "primary"),
    ]
    for k, v in rows:
        print(f"  {k:<18} {v}")
    print()
    for name, detail in pre["checks"]:
        print(f"  [ok] {name:<30} {detail}")
    print("=" * 66)

    if args.estimate or args.check_config:
        print()
        print_estimate(configs, cases, n_tasks, args)
        if args.check_config:
            print("\ncheck-config only; no trial written.")
        return 0
    if args.adapter == "agentdojo" and not args.confirm:
        print("Real run NOT started. This spends API credits. Re-run with --confirm.")
        print_estimate(configs, cases, n_tasks, args)
        return 0
    if args.db == ":memory:" and args.adapter == "agentdojo":
        raise SystemExit("A real run needs --db <file>; an in-memory database loses every trial.")

    # ---- registration -------------------------------------------------------
    db = ExperimentDB(args.db)
    pid = prereg_id()
    if db.prereg(pid) is None:
        db.register_prereg("primary", HYPOTHESES, list(LADDER), list(QUANTILES),
                           N_SEEDS_DECLARED, plan_sha(), prereg_id=pid)
    cfg_ids = {c: db.register_defense(list(c), impl_source=impl_source(c),
                                      impl_ref=impl_ref(c, SOURCE_REF if args.adapter == "agentdojo"
                                                        else "wf/adapter.py@offline"),
                                      deviations=("stack wiring per wf/compose.py plan(); "
                                                  "elements are upstream originals"
                                                  if len(c) > 1 else None))
               for c in configs}
    db.register_method(attack.method_id, attack.family, False, attack.source_ref,
                       dict(attack.hyperparams), defense_aware=False)
    db.register_model(model_id, "openai" if args.adapter == "agentdojo" else "offline",
                      model_id, False)
    db.register_adapter(adapter, impl_ref=(SOURCE_REF if args.adapter == "agentdojo"
                                           else "wf/adapter.py@offline"))

    done: set = set()
    if args.resume:
        purged = db.purge_pending()
        done = db.completed_cells("primary", stage, model_id=model_id)
        print(f"resume: {len(done)} finalized cells skipped, {purged} pending cells purged")

    runner = EscalationRunner(db, adapter, list(LADDER))

    # Benign utility: separate, non-injected, once per (config, task). Reused
    # from an existing finalized trial on resume so the pass is not re-bought.
    benign_cache: dict[tuple, object] = {}

    def benign_for(comps, task):
        key = (comps, task.task_id)
        if key in benign_cache:
            return benign_cache[key]
        row = db.conn.execute(
            "SELECT benign_utility FROM trial WHERE config_id=? AND task_id=? AND status='ok' LIMIT 1",
            (cfg_ids[comps], task.task_id)).fetchone()
        if row is not None:
            from wf.adapter import BenignRunResult
            res = BenignRunResult(benign_utility=row["benign_utility"])
        else:
            res = adapter.run_benign(task, comps, args.model, 0)
        benign_cache[key] = res
        return res

    total = len(cases) * len(configs) * args.seeds
    n_run = n_skip = 0
    t_start = time.time()
    print(f"\nrunning {total} cells -> {args.db}  (Ctrl-C to stop; --resume continues)\n")
    try:
        for task, inj in cases:
            for comps in configs:
                defenders = defender_model_snapshots(comps, model_id)
                for seed in range(args.seeds):
                    key = (cfg_ids[comps], attack.method_id, task.task_id, inj, seed)
                    if key in done:
                        n_skip += 1
                        continue
                    cell = CellSpec(prereg_id=pid, arm="primary", stage=stage,
                                    config_id=cfg_ids[comps], defense_components=comps,
                                    model_id=model_id, seed=seed, injection_task_id=inj,
                                    defender_models=defenders,
                                    attacker_model_snapshot=attack.attacker_model_snapshot)
                    tid = runner.run_cell(cell, attack, task, benign=benign_for(comps, task))
                    n_run += 1
                    row = db.conn.execute(
                        "SELECT status, censored, event_queries, budget_ceiling FROM trial "
                        "WHERE trial_id=?", (tid,)).fetchone()
                    outcome = (f"censored at {row['budget_ceiling']}" if row["censored"]
                               else f"broke at q={row['event_queries']}")
                    if row["status"] != "ok":
                        outcome = f"ERROR ({row['status']})"
                    elapsed = time.time() - t_start
                    print(f"  [{n_run + n_skip:>5}/{total}] {label(comps):<26} {task.task_id:<13} "
                          f"{inj:<17} s{seed}  {outcome:<18} {elapsed / 60:6.1f} min", flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted. Re-run the same command with --resume to continue; "
              "finalized cells are kept, the pending one is purged.")
        return 130

    # ---- summary ------------------------------------------------------------
    print()
    print(f"{'config':<26} {'n':>4} {'events':>6} {'censored':>8} {'errors':>6} {'ASR@%d' % LADDER[-1]:>8}")
    for comps in configs:
        data = db.survival_data("primary", stage, cfg_ids[comps], model_id=model_id,
                                method_id=attack.method_id)
        errs = db.conn.execute(
            "SELECT COUNT(*) n FROM trial WHERE config_id=? AND stage=? AND status='error'",
            (cfg_ids[comps], stage)).fetchone()["n"]
        km = kaplan_meier(data)
        print(f"{label(comps):<26} {km.n_total:>4} {km.n_total - km.n_censored:>6} "
              f"{km.n_censored:>8} {errs:>6} {km.asr_at(LADDER[-1]):>8.3f}")
    if args.adapter == "offline":
        print("\nSYNTHETIC: OfflineAdapter output at stage='harness_test'. These numbers "
              "exercise the apparatus and measure no real defense.")
    print(f"\ndashboard:  python dashboard.py {args.db} --serve 8765")
    return 0


if __name__ == "__main__":
    sys.exit(main())
