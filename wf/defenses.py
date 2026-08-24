"""
defenses.py -- composable defense pipeline and the composition grid.

The pipeline wraps the target (AgentDojo), per the supervisor's correction. Each
defense is a stage that may transform the untrusted context, restrict the tool
set, or veto an action. Stages are pure with respect to configuration so that a
stack is fully described by its sorted component list.

IMPORTANT: no defense is implemented here. These are interface stubs plus
provenance metadata. Real behaviour comes from the ORIGINAL authors' code,
adapted at the boundary -- which is why `impl_source` and `impl_ref` are
required fields rather than optional documentation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Callable, Protocol, Sequence


class DefenseStage(Protocol):
    """A single defense. Implementations wrap upstream authors' code."""

    name: str
    layer: str          # 'input' | 'retrieval' | 'prompt' | 'tool' | 'flow' | 'egress'
    impl_source: str    # 'original' | 'reimplemented'
    impl_ref: str

    def apply(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Transform the execution context. May raise Veto to block."""
        ...


class Veto(Exception):
    """Raised by a stage to block execution. Counts as defense action, and --
    critically -- is recorded separately from a benign task failure so that
    'defense refused everything' cannot masquerade as 'defense was robust'."""


@dataclass
class DefenseSpec:
    """Metadata for one defense component."""

    name: str
    layer: str
    impl_source: str
    impl_ref: str
    deviations: str | None = None
    # Measured separately from attacker cost, always.
    expected_overhead_mult: float = 1.0


# The 6-defense core for the composition study. Layers are deliberately spread
# so that pairs test cross-layer interaction, which is where destructive
# interference is most plausible: a stage that rewrites untrusted text can
# destroy the provenance signal a later stage depends on.
CORE_DEFENSES: dict[str, DefenseSpec] = {
    "spotlight": DefenseSpec("spotlight", "prompt", "original", "TBD", None, 1.0),
    "sandwich": DefenseSpec("sandwich", "prompt", "original", "TBD", None, 1.1),
    "detector": DefenseSpec("detector", "input", "original", "TBD", None, 1.2),
    "toolfilter": DefenseSpec("toolfilter", "tool", "original", "TBD", None, 1.3),
    "dataflow": DefenseSpec("dataflow", "flow", "original", "TBD", None, 2.0),
    "egress_canary": DefenseSpec("egress_canary", "egress", "reimplemented", "TBD", None, 1.05),
}


@dataclass
class DefensePipeline:
    components: tuple[str, ...]
    stages: list[DefenseStage] = field(default_factory=list)

    @property
    def canonical(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.components)))

    def run(self, ctx: dict[str, Any]) -> dict[str, Any]:
        for stage in self.stages:
            ctx = stage.apply(ctx)
        return ctx


def composition_grid(
    core: Sequence[str] | None = None,
    include_triples: Sequence[tuple[str, str, str]] | None = None,
) -> list[tuple[str, ...]]:
    """Enumerate the configs for RQ2.

    Returns: no-defense, all singletons, all pairs, plus selected triples.
    Canonical (sorted, deduped) so the DB cannot double-count D1+D2 vs D2+D1.
    """
    names = sorted(core or CORE_DEFENSES.keys())
    grid: list[tuple[str, ...]] = [()]
    grid += [(n,) for n in names]
    grid += [tuple(sorted(c)) for c in combinations(names, 2)]
    if include_triples:
        grid += [tuple(sorted(t)) for t in include_triples]
    # dedupe, preserve order
    seen, out = set(), []
    for g in grid:
        if g not in seen:
            seen.add(g); out.append(g)
    return out


def grid_cost_estimate(
    grid: Sequence[tuple[str, ...]],
    n_methods: int,
    n_models: int,
    n_tasks: int,
    n_seeds: int,
    budget_ceiling: int,
) -> dict[str, float]:
    """Back-of-envelope query budget for the full grid.

    Worth computing before committing: the grid multiplies fast and the
    dominant project risk is API cost, not engineering.
    """
    trials = len(grid) * n_methods * n_models * n_tasks * n_seeds
    worst_case_queries = trials * budget_ceiling
    # Defender-side overhead is charged separately, never blended in.
    mean_overhead = sum(
        max((CORE_DEFENSES[c].expected_overhead_mult for c in g), default=1.0)
        for g in grid
    ) / len(grid)
    return {
        "n_configs": len(grid),
        "n_trials": trials,
        "worst_case_attacker_queries": worst_case_queries,
        "mean_defender_overhead_mult": round(mean_overhead, 3),
    }


# ===========================================================================
# Composition pair selection -- preregistered, mechanism-driven
# ===========================================================================
#
# The supervisor's rule:
#   "Select pairs whose security mechanisms depend on materially different
#    representations of the same untrusted information, prioritizing pairs
#    where the output of one defense can plausibly destroy, bypass, or
#    duplicate the signal required by the other."
#
# Implemented below as a scoring function over declared mechanism metadata, so
# that every included and excluded pair has a machine-checkable reason. This is
# an A PRIORI rule; it is preregistered and fixed before Stage A screening runs.

# What representation of the untrusted input each defense consumes, and what it
# mutates. Signal destruction is possible when A mutates something B consumes.
MECHANISM: dict[str, dict[str, Any]] = {
    "spotlight":     {"role": "text_transform", "consumes": {"raw_text"},
                      "mutates": {"raw_text", "prompt_structure"}},
    "sandwich":      {"role": "text_transform", "consumes": {"raw_text"},
                      "mutates": {"prompt_structure"}},
    "detector":      {"role": "detection",      "consumes": {"raw_text"},
                      "mutates": set()},
    "toolfilter":    {"role": "enforcement",    "consumes": {"user_intent", "tool_call"},
                      "mutates": {"tool_set"}},
    "dataflow":      {"role": "enforcement",    "consumes": {"provenance_tags", "raw_text"},
                      "mutates": {"data_flow", "tool_call"}},
    "egress_canary": {"role": "audit",          "consumes": {"output_stream"},
                      "mutates": set()},
}


def pair_mechanism_score(a: str, b: str) -> dict[str, Any]:
    """A priori interference potential for one pair. Fully auditable."""
    ma, mb = MECHANISM[a], MECHANISM[b]
    sa, sb = CORE_DEFENSES[a], CORE_DEFENSES[b]
    reasons: list[str] = []
    score = 0

    # C1 -- signal destruction: one defense mutates what the other reads.
    destroys = (ma["mutates"] & mb["consumes"]) | (mb["mutates"] & ma["consumes"])
    if destroys:
        score += 3
        reasons.append(f"signal destruction via {sorted(destroys)}")

    # C2 -- cross-layer: different points in the pipeline.
    if sa.layer != sb.layer:
        score += 2
        reasons.append(f"cross-layer ({sa.layer} x {sb.layer})")

    # C3 -- at least one enforcement mechanism. Destructive interference
    # requires something that can actually block; two advisory defenses can
    # underperform but not make the system worse than either alone.
    roles = {ma["role"], mb["role"]}
    if "enforcement" in roles:
        score += 2
        reasons.append("includes an enforcement mechanism")

    # C4 -- duplicated enforcement: two independent enforcers may contend.
    if ma["role"] == "enforcement" and mb["role"] == "enforcement":
        score += 1
        reasons.append("duplicated enforcement")

    # C5 -- detection paired with enforcement: does detection add anything once
    # action is independently constrained?
    if roles == {"detection", "enforcement"}:
        score += 1
        reasons.append("detection vs independent enforcement")

    return {
        "pair": tuple(sorted((a, b))),
        "score": score,
        "reasons": reasons,
        "roles": tuple(sorted(roles)),
        "layers": tuple(sorted({sa.layer, sb.layer})),
    }


def rank_pairs(core: Sequence[str] | None = None) -> list[dict[str, Any]]:
    names = sorted(core or CORE_DEFENSES.keys())
    scored = [pair_mechanism_score(a, b) for a, b in combinations(names, 2)]
    return sorted(scored, key=lambda r: (-r["score"], r["pair"]))


def select_pairs(n: int = 8, core: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Top-n pairs by a priori mechanism score. Ties broken alphabetically so
    the selection is deterministic and reproducible from the rule alone."""
    return rank_pairs(core)[:n]


# ---------------------------------------------------------------------------
# Stage A screening rank -- ALSO preregistered, and deliberately blind to
# significance. Screening selects which pairs to study; if it could see the
# confirmatory test statistic, selection would be conditioned on the outcome.
# ---------------------------------------------------------------------------
def screening_rank(
    stage_a: dict[tuple[str, ...], dict[str, float]],
    mechanism_scores: dict[tuple[str, ...], int],
    w_mechanism: float = 1.0,
    w_effect: float = 1.0,
    w_utility: float = 0.5,
) -> list[dict[str, Any]]:
    """Rank pairs for promotion to the confirmatory study.

    `stage_a[pair]` must contain 'abs_interaction', 'utility_drop', and
    'worse_than_best_member' (0/1). It must NOT contain p-values or CIs, and
    this function will refuse them -- ranking on significance is exactly the
    cherry-picking the supervisor flagged.
    """
    banned = {"p", "p_value", "ci_lo", "ci_hi", "significant"}
    out = []
    for pair, m in stage_a.items():
        illegal = banned & set(m)
        if illegal:
            raise ValueError(
                f"screening metrics for {pair} contain inference outputs {sorted(illegal)}; "
                "screening must not rank on significance"
            )
        s = (w_mechanism * mechanism_scores.get(tuple(sorted(pair)), 0)
             + w_effect * m["abs_interaction"]
             + w_utility * m["utility_drop"]
             + 2.0 * m.get("worse_than_best_member", 0.0))
        out.append({"pair": tuple(sorted(pair)), "screen_score": round(s, 3)})
    return sorted(out, key=lambda r: (-r["screen_score"], r["pair"]))
