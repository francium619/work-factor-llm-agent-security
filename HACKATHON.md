# Work Factor at 13th Protocol (HackIndia, 28–29 Nov 2026)

This page is for judges and teammates. It says what the project is, what was built before the
event, what gets built and measured during the 28 hours on campus, and how to run the demo.

## The problem in one paragraph

An LLM agent that reads a web page, an email, or a bank statement can be hijacked by
instructions hidden inside it. The whole field scores defenses against this with one number,
attack success rate: run an attack once per task, count the hits. That number cannot tell a
defense that folds on the first try from one that holds for ten, because it never asks how
much work the attacker had to do. Real attackers retry. We measure the missing dimension:
**how many attempts does it take before a defense first fails**, treated as a survival time,
with defenses that never fail inside the budget kept as right-censored observations instead
of being thrown away. On top of that we ask the question operators actually face: **when you
stack two defenses, do you get what you paid for, or can the stack be worse than its better
half?**

## What the demo shows

A live page (`dashboard.py --serve`) that re-renders from the experiment database every 30
seconds while trials run. On it:

1. **Work-factor curves**: one Kaplan–Meier curve per defense configuration, attack success
   against attacker budget. Two defenses with the same end-point can have visibly different
   shapes. That shape is the result.
2. **Quantiles**: Q10, Q25, Q50 per configuration with bootstrap intervals, and "> 11" where
   the defense was never broken. The ranking by Q25 is compared against the ranking by ASR.
3. **Composition verdicts**: for each defense pair, the independence prediction versus the
   observed stack, with an interval and one of four verdicts: additive, super-additive,
   sub-additive, destructive.
4. **Which template broke it**: the attacker is the 11 published AgentDojo injection templates
   played in a fixed order, so the page shows which template first defeated each defense.
5. **Cost in two channels**: attacker side and defender side, never summed.

The page refuses to pool across models, stages, or arms, and prints a banner derived from the
database that says whether what you are looking at is real or a synthetic rehearsal.

## What existed before the event, and what is new

| Built before (Aug 2026, frozen) | Built for the event (Nov 2026) |
|---|---|
| Survival statistics: Kaplan–Meier, censored quantiles, log-rank, bootstrap, log-risk composition null | `wf/attacks.py`: repeated-query **fixed** attack from the 11 upstream templates (the first attack that can produce a curve) |
| SQLite schema with the integrity rules as constraints | `wf/compose.py`: defense **stacks** built from AgentDojo's own pipeline elements (upstream cannot express a stack) |
| Adapter to AgentDojo v1.2.2, single defense only | `run_workfactor.py`: protocol WF1, resumable, shardable, with a pre-spend estimate |
| 92 automated checks against planted truth | `wf/report.py` + `dashboard.py`: the live page |
| | `PROTOCOL_WF1.md`: the preregistration, SHA-bound to the data |
| | 36 further checks (128 total); `validate.py` now runs the whole WF1 pipeline offline |

The earlier protocol (G2) never ran: its one attempt died on an API quota error with zero
trials, and its target model was retired by OpenAI on 2026-10-23. WF1 is the first protocol
under which this apparatus can produce a measurement. **As of this writing no real trial has
been written.** The on-campus run is the first.

## The on-campus plan (28 h)

| When | What | Command |
|---|---|---|
| T+0:00 | Validate the apparatus on the event machine | `python validate.py` |
| T+0:10 | Preflight against the real benchmark, no spend | `python run_workfactor.py --adapter agentdojo --check-config --tasks 8 --seeds 1` |
| T+0:20 | Start the screening run, three shards in parallel | `python run_workfactor.py --adapter agentdojo --confirm --db runs/wf1_s0.sqlite --tasks 8 --seeds 1 --shard 0/3` (and `1/3`, `2/3`) |
| T+0:25 | Start the live page | `python dashboard.py runs/wf1_s0.sqlite runs/wf1_s1.sqlite runs/wf1_s2.sqlite --serve 8765` |
| T+6:00 | First slice complete (8 tasks × 9 injections × 7 configs); decide whether to extend to 16 tasks or 3 seeds with the remaining credits | `--resume` on each shard with the wider arguments |
| T+24:00 | Freeze the databases, export the page | `python dashboard.py runs/wf1_s*.sqlite -o runs/wf1.html` |

The pre-spend estimate for the first slice is about 5,500 attacked runs worst case, roughly
$9 at gpt-4o-mini prices and about 6 hours sequential (2 hours over three shards). The $150
OpenAI credit in the prize pool covers the full 144-case, 3-seed protocol with room to spare.

If a run dies, `--resume` continues it: finalized trials are kept, the one pending trial is
purged, and no observed outcome is ever re-run (selective retry is forbidden by the protocol).

## Why this is honest work, in case a judge asks

- The attacker is a fixed instrument. It sees no feedback and is identical for every defense,
  so differences in queries-to-break belong to the defenses.
- Nothing is reimplemented. Every defense is AgentDojo's own code; the stacks reuse the same
  element classes in the same pipeline shape, and `validate.py` proves the single-defense
  shape equals the upstream factory's.
- The success check is the benchmark's own programmatic checker, not an LLM judge.
- The plan is written down first. The preregistration SHA is stored with the data.
- Synthetic data cannot become evidence: the database enforces that the synthetic adapter can
  only write to a non-evidence stage, and the page says which you are looking at.
- Limits are stated: one suite, one model, 11-query ceiling, no multiple-comparison correction
  across pairs, and a lower-bound attacker.

## Team

Team Ligga, HackIndia 2026. Repository:
<https://github.com/francium619/work-factor-llm-agent-security>. License: MIT.
