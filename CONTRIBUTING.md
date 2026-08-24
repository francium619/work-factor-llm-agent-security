# Contributing

This repository is a measurement apparatus for a research study. Its correctness
requirements are unusual: most of the code exists to make specific mistakes *impossible*,
so a change that makes an error easier to commit is a regression even when every test
still passes.

Read this before changing anything under `wf/`.

---

## The one rule

> **If a change makes it easier to record, read, or report data in a way the apparatus
> currently refuses, it is a breaking change — regardless of whether tests pass.**

The guardrails are not defensive programming. They implement research-integrity rules that
would otherwise depend on researcher discipline, and discipline is exactly what fails
under deadline pressure. A guardrail that is relaxed "just for this run" has served no
purpose at all.

---

## Before you start

```bash
python -m venv .venv              # keep the real experimental venv outside any synced folder
source .venv/bin/activate         # Windows: .\.venv\Scripts\activate
pip install -r requirements.txt
python validate.py
```

Expected: `92/92 checks passed`, exit code 0. `validate.py` needs only numpy and scipy —
no network, no API key, no AgentDojo. If it does not pass on a clean checkout, stop and
fix that before doing anything else.

---

## The workflow

1. **Run `validate.py` first.** Establish a green baseline before you change anything.
2. **Make the change.**
3. **Run `validate.py` again.** It must still be `92/92`, or `N/N` with your new checks
   included — never fewer checks than before, and never a `FAIL`.
4. **If you changed anything under `wf/` or `run_slice.py`, add a check** (see below).
5. **Run the synthetic rehearsal** if you touched the runner, adapter, or database:
   ```bash
   python run_slice.py --adapter offline
   ```
   Expect `N attempted = N recorded = 144` with every case ending in exactly one of
   success/failure/error. This output is synthetic and measures the accounting only.
6. **Do not run a real slice to test a change.** It costs money and writes trials. Use the
   offline adapter and `--check-config`.

`validate.py` exits non-zero on any failure, so it works directly as a pre-commit or CI
gate.

---

## Never do these

| Don't | Why |
|---|---|
| Add a combined-cost column or a `total_cost` helper | Attacker cost and defender overhead answer different questions; the absence of a field to blend them into *is* the enforcement |
| Give `model_id`, `arm`, or `stage` a default in any read API | Silent pooling across models, arms, or stages produces curves describing no system that exists |
| Add an `observe()` method to `FixedAttack` or a subclass | A fixed attack that sees prior results is no longer a stationary instrument, and RQ1 stops being identified |
| Make `evidence_class` a constructor argument | It is a `ClassVar` so that misrepresenting a synthetic adapter requires a visible source edit, not one keyword at a call site |
| Let a runner write `censored`, `event_queries`, or `budget_ceiling` directly | These are derived in `finalize_trial()` from the checkpoint history, so a buggy runner crashes instead of producing a plausible wrong number |
| Read `budget_checkpoint` from `wf/analysis.py` | Checkpoints are resource-allocation metadata; treating them as observations triple-counts tasks and biases every curve |
| Relax a `CHECK` constraint in `schema.sql` to make a test pass | The constraint is almost certainly correct and the test is wrong |
| Change `G2_EQUIVALENCE_MARGIN` | δ was frozen before the first run. Changing it after seeing a result is the exact researcher degree of freedom this project removes |
| Change `DECLARED_MODEL`, `BENCHMARK_VERSION`, `SUITE`, or `ATTACK` in `run_slice.py` | That is a different protocol, not a different setting. It needs an explicit preregistration change, not an edit |
| Set `PUBLISHED_ASR` to a number from a different task set | v1.2.2's 144 banking pairs are not the paper's 629 cases. A numerical verdict against a quantity never measured here is worse than no verdict |
| Approximate a defense that has no upstream implementation | A hand-rolled substitute is the "your version was a strawman" objection this project exists to avoid |
| Add an import that crosses the three-layer boundary | `adapter ↛ analysis`, `analysis ↛ adapter/runner`, `runner ↛ analysis`. Check 9 fails if you do |
| Commit an API key, `.env`, experiment database, run log, venv, or `__pycache__` | See [Secrets and artifacts](#secrets-and-artifacts) |

If you believe one of these is genuinely wrong, that is a discussion to have explicitly —
open an issue arguing the case. Do not route around it in a commit.

---

## Adding a check

Every change to `wf/` or `run_slice.py` should come with a check in `validate.py`. There
are two kinds, and picking the right one matters:

**Estimator checks** plant a known truth and assert the estimator recovers it.

```python
data = sim_trials(p_per_query=0.02, n=4000, ceiling=150)
q50, censored = kaplan_meier(data).quantile(0.50)
check("KM recovers median", abs(q50 - truth_median) <= 6,
      f"est {q50:.0f} vs truth {truth_median}")
```

**Constraint checks** attempt a violation and assert it raises. Note the shape — the
"no exception raised" branch must fail the check, otherwise a removed guardrail passes
silently:

```python
try:
    db.open_trial(..., arm="primary", method_id="learner", ...)
    check("learning method blocked from primary arm", False, "NO EXCEPTION RAISED")
except QuarantineViolation:
    check("learning method blocked from primary arm", True)
```

Guidelines:

- Name the check after the *property*, not the function — "errored trials never enter
  survival data", not "test_survival_data_3".
- Keep checks deterministic. `validate.py` uses a fixed seed (`RNG`); do not introduce
  unseeded randomness.
- Never make a check depend on network access, an API key, or AgentDojo being installed.
- Add new checks at the end of the relevant `CHECK N` group, or open a new group with the
  same header format.

---

## Changing the schema

`wf/schema.sql` is the least reversible artifact in the project. Before editing it:

1. **Is the new field a survival observation?** If it can be derived at finalization,
   derive it — do not let a caller supply it.
2. **Does it create a way to blend attacker and defender cost?** If so, don't add it.
3. **Does it need to be frozen provenance?** If a later registry edit could change what a
   finished trial appears to have run against, it belongs in the frozen block and must be
   copied in `_freeze_provenance()`.
4. **Can the constraint be expressed as a `CHECK`?** Prefer that over validation in
   Python — the database is the last line of defence and applies to every writer.
5. `open_trial()` inserts with **named columns**, not positional `VALUES`. Keep it that
   way: adding a column should not silently shift every field one place to the left.

---

## Secrets and artifacts

- **Credentials go in environment variables only** (`OPENAI_API_KEY`). Never in a file,
  never in a default argument, never in a docstring example.
- **Never commit**: `.env`, API keys or tokens, virtual environments, SQLite experiment
  databases (`*.sqlite`, `*.db`), run logs, `__pycache__`, or any generated artifact. The
  `.gitignore` covers these, but treat it as a convenience rather than a control — check
  `git status` before committing.
- If a credential is ever committed, **rotate it**. Removing it from a later commit does
  not remove it from history.
- The experiment database stores attack payloads as SHA-256 hashes rather than plaintext,
  so a database *could* be published without publishing working attack strings. That is a
  property worth preserving — do not add a plaintext payload column.

---

## Commit conventions

- Small, single-purpose commits with a typed subject line.
- Say what changed and, for anything touching `wf/`, **why the invariant still holds**. A
  commit that relaxes a guardrail must say so in its message rather than burying it.
- Include the validation result in the message when it changed, e.g. `validate: 92/92 →
  94/94`.
- Do not commit experiment output or a modified `runs/` directory.

---

## Reporting results

If you produce experimental data with this apparatus, the following distinctions must
survive into whatever you write:

| Label | Means |
|---|---|
| **implemented** | code exists and is exercised by `validate.py` |
| **validated synthetically** | an estimator recovers a planted truth, or a guardrail raises as designed. **Not a result about any real defense** |
| **experimentally completed** | a real run finished, against a named model snapshot and benchmark version, with the error rate reported |
| **proposed / future work** | designed, not executed |

Specifically:

- Never describe synthetic-adapter output as a measurement. It is manufactured from a
  seeded RNG, and the database records it at `stage='harness_test'` for exactly that
  reason.
- Never report `clean_asr` where `reference_asr` is the comparable quantity, or vice
  versa. Report both, with the error rate.
- Never report a survival quantile without its censored flag, and never report a bootstrap
  interval without `censored_frac`.
- Never pool across models, arms, or stages — and if you find yourself wanting to, the
  read API will stop you.
- A failed or aborted run is **not** a result. A run that terminated on quota, rate limit,
  or a crash produced no data, and must be described that way.
