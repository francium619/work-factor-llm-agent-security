"""
report.py -- the dashboard renderer.

Reads one or more experiment databases THROUGH the guarded read API (one arm,
one stage, one model -- the same refusals apply here as everywhere else),
computes the statistics with wf.analysis, and renders one self-contained HTML
page: the survival curves, the censored quantiles, the log-rank tests against
the undefended baseline, the composition verdicts, and the two cost channels
kept apart.

Three things this file is careful about, because a plotting layer is the
easiest place for an apparatus to start believing things prematurely:

  1. Evidence class is read off adapter_registry and printed as the first thing
     on the page. A synthetic run renders with a banner saying it measures
     nothing; that banner cannot be turned off by an argument.
  2. Censored quantiles are rendered as "> ceiling", never as a number that
     looks like an event time.
  3. Attacker cost and defender cost are two tables. There is no combined
     column here either.

Several databases may be passed when one run was sharded across processes
(run_workfactor.py --shard). Their trial rows are unioned only if they share
the preregistration, model, and stage; anything else is refused.

Imports wf.analysis and wf.db only. Never wf.adapter or wf.runner.
"""

from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from wf.analysis import (
    KMCurve, bootstrap_quantile_ci, composition_interaction, kaplan_meier, log_rank,
)
from wf.db import ExperimentDB, HARNESS_STAGE

# Categorical palette, fixed slot order (dataviz reference instance). Light / dark.
SERIES_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
SERIES_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767")
MAX_SERIES = len(SERIES_LIGHT)


@dataclass
class Cell:
    label: str
    components: tuple[str, ...]
    config_id: str
    data: list[tuple[int, int]]
    n_errors: int = 0
    benign_mean: float | None = None
    uua_mean: float | None = None
    queries_total: int = 0
    wall_clock_total: float = 0.0
    attacker_usd: float = 0.0
    defender_usd: float = 0.0
    defender_tokens: int = 0
    template_breaks: dict[str, int] = field(default_factory=dict)
    km: KMCurve | None = None
    quantiles: dict[float, tuple[float, bool]] = field(default_factory=dict)
    q25_ci: dict | None = None
    logrank: Any = None            # vs the undefended baseline

    @property
    def n(self) -> int:
        return len(self.data)

    @property
    def n_censored(self) -> int:
        return sum(c for _, c in self.data)


class ReportError(RuntimeError):
    pass


def _label(components_json: str) -> tuple[tuple[str, ...], str]:
    comps = tuple(json.loads(components_json))
    return comps, ("+".join(comps) if comps else "none")


def collect(db_paths: Sequence[str], *, arm: str = "primary", stage: str | None = None,
            model_id: str | None = None, n_boot: int = 600) -> dict[str, Any]:
    """Gather everything the page needs from one or more databases."""
    dbs = [ExperimentDB(p) for p in db_paths]

    stages = sorted({r["stage"] for db in dbs
                     for r in db.conn.execute("SELECT DISTINCT stage FROM trial WHERE arm=?", (arm,))})
    models = sorted({r["model_id"] for db in dbs
                     for r in db.conn.execute("SELECT DISTINCT model_id FROM trial WHERE arm=?", (arm,))})
    if stage is None:
        if len(stages) > 1:
            raise ReportError(f"trials exist at several stages {stages}; pass --stage. "
                              "Screening and confirmatory data are never pooled.")
        stage = stages[0] if stages else HARNESS_STAGE
    if model_id is None:
        if len(models) > 1:
            raise ReportError(f"trials exist for several models {models}; pass --model. "
                              "Two models are two experiments.")
        model_id = models[0] if models else "n/a"

    preregs = {r["prereg_id"]: dict(r) for db in dbs for r in db.conn.execute("SELECT * FROM prereg")}
    if len(preregs) > 1:
        raise ReportError(f"databases carry different preregistrations {sorted(preregs)}; "
                          "shards of one run share one prereg_id, anything else is not one experiment")
    prereg = next(iter(preregs.values()), None)
    ladder = json.loads(prereg["budget_ladder"]) if prereg else []
    quantiles = json.loads(prereg["quantiles"]) if prereg else [0.10, 0.25, 0.50]

    adapters = {r["adapter_id"]: dict(r) for db in dbs
                for r in db.conn.execute("SELECT * FROM adapter_registry")}
    evidence = sorted({a["evidence_class"] for a in adapters.values()}) or ["none"]
    synthetic = "synthetic" in evidence or stage == HARNESS_STAGE
    view = "v_trial_harness" if stage == HARNESS_STAGE else "v_trial_clean"

    # configs, in a fixed order: baseline, singletons, pairs, ... alphabetical within.
    cfg_rows: dict[str, dict] = {}
    for db in dbs:
        for r in db.configs(arm):
            cfg_rows.setdefault(r["config_id"], dict(r))
    ordered = sorted(cfg_rows.values(), key=lambda r: (r["n_components"], r["components"]))

    method_ids = sorted({r["method_id"] for db in dbs
                         for r in db.conn.execute("SELECT DISTINCT method_id FROM trial WHERE arm=? AND stage=?",
                                                  (arm, stage))})
    method_id = method_ids[0] if len(method_ids) == 1 else None

    cells: list[Cell] = []
    for r in ordered:
        comps, lab = _label(r["components"])
        cell = Cell(label=lab, components=comps, config_id=r["config_id"], data=[])
        for db in dbs:
            cell.data += db.survival_data(arm, stage, r["config_id"], model_id=model_id,
                                          method_id=method_id)
            agg = db.conn.execute(
                f"SELECT AVG(benign_utility) b, AVG(utility_under_attack) u, "
                f"SUM(attacker_queries) q, SUM(wall_clock_s) w, SUM(attacker_usd) au, "
                f"SUM(defender_usd) du, SUM(defender_tokens_in + defender_tokens_out) dt "
                f"FROM {view} WHERE arm=? AND stage=? AND config_id=? AND model_id=?",
                (arm, stage, r["config_id"], model_id)).fetchone()
            if agg["q"] is not None:
                # weighted merge of means across shards
                n_here = db.conn.execute(
                    f"SELECT COUNT(*) n FROM {view} WHERE arm=? AND stage=? AND config_id=? AND model_id=?",
                    (arm, stage, r["config_id"], model_id)).fetchone()["n"]
                prev_n = cell.n - n_here
                for attr, key in (("benign_mean", "b"), ("uua_mean", "u")):
                    old = getattr(cell, attr)
                    new = agg[key] if agg[key] is not None else 0.0
                    setattr(cell, attr, new if old is None else (old * prev_n + new * n_here) / cell.n)
                cell.queries_total += int(agg["q"] or 0)
                cell.wall_clock_total += float(agg["w"] or 0.0)
                cell.attacker_usd += float(agg["au"] or 0.0)
                cell.defender_usd += float(agg["du"] or 0.0)
                cell.defender_tokens += int(agg["dt"] or 0)
            cell.n_errors += db.conn.execute(
                "SELECT COUNT(*) n FROM trial WHERE arm=? AND stage=? AND config_id=? AND model_id=? "
                "AND status='error'", (arm, stage, r["config_id"], model_id)).fetchone()["n"]
            for q in db.conn.execute(
                    "SELECT q.mutation_op op, COUNT(*) n FROM query_event q JOIN trial t ON t.trial_id=q.trial_id "
                    "WHERE t.arm=? AND t.stage=? AND t.config_id=? AND t.model_id=? AND t.status='ok' "
                    "AND q.programmatic_success=1 GROUP BY q.mutation_op",
                    (arm, stage, r["config_id"], model_id)):
                cell.template_breaks[q["op"] or "?"] = cell.template_breaks.get(q["op"] or "?", 0) + q["n"]
        if not cell.data and not cell.n_errors:
            continue
        cells.append(cell)

    ceiling = ladder[-1] if ladder else max((t for c in cells for t, _ in c.data), default=1)

    baseline = next((c for c in cells if not c.components), None)
    for c in cells:
        c.km = kaplan_meier(c.data)
        c.quantiles = {p: c.km.quantile(p) for p in quantiles} if c.data else {}
        c.q25_ci = bootstrap_quantile_ci(c.data, 0.25, n_boot=n_boot) if c.data else None
        if baseline is not None and c is not baseline and c.data and baseline.data:
            c.logrank = log_rank(c.data, baseline.data)

    by_comps = {c.components: c for c in cells}
    compositions = []
    if baseline is not None and baseline.data:
        for c in cells:
            if len(c.components) != 2 or not c.data:
                continue
            a, b = (by_comps.get((c.components[0],)), by_comps.get((c.components[1],)))
            if a is None or b is None or not a.data or not b.data:
                continue
            res = composition_interaction(baseline.data, a.data, b.data, c.data,
                                          budget=ceiling, n_boot=n_boot)
            compositions.append({"pair": c, "a": a, "b": b, "result": res})

    # run-level facts
    def scalar(sql, *params):
        out = 0
        for db in dbs:
            out += db.conn.execute(sql, params).fetchone()[0] or 0
        return out

    seeds = sorted({r[0] for db in dbs for r in db.conn.execute(
        "SELECT DISTINCT seed FROM trial WHERE arm=? AND stage=?", (arm, stage))})
    tasks = {r[0] for db in dbs for r in db.conn.execute(
        "SELECT DISTINCT task_id FROM trial WHERE arm=? AND stage=?", (arm, stage))}
    injs = {r[0] for db in dbs for r in db.conn.execute(
        "SELECT DISTINCT injection_task_id FROM trial WHERE arm=? AND stage=?", (arm, stage))}
    created = [r[0] for db in dbs for r in db.conn.execute(
        "SELECT MIN(created_utc) FROM trial WHERE arm=? AND stage=?", (arm, stage)) if r[0]]
    latest = [r[0] for db in dbs for r in db.conn.execute(
        "SELECT MAX(created_utc) FROM trial WHERE arm=? AND stage=?", (arm, stage)) if r[0]]
    snapshot = next((r[0] for db in dbs for r in db.conn.execute(
        "SELECT model_snapshot FROM trial WHERE model_id=? LIMIT 1", (model_id,))), model_id)
    bench = next((r[0] for db in dbs for r in db.conn.execute(
        "SELECT benchmark_version FROM trial WHERE arm=? AND stage=? LIMIT 1", (arm, stage))), "n/a")

    return {
        "arm": arm, "stage": stage, "model_id": model_id, "model_snapshot": snapshot,
        "benchmark_version": bench, "evidence": evidence, "synthetic": synthetic,
        "adapters": adapters, "prereg": prereg, "ladder": ladder, "quantiles": quantiles,
        "ceiling": ceiling, "method_id": method_id, "cells": cells, "baseline": baseline,
        "compositions": compositions,
        "n_trials": scalar("SELECT COUNT(*) FROM trial WHERE arm=? AND stage=?", arm, stage),
        "n_ok": scalar("SELECT COUNT(*) FROM trial WHERE arm=? AND stage=? AND status='ok'", arm, stage),
        "n_error": scalar("SELECT COUNT(*) FROM trial WHERE arm=? AND stage=? AND status='error'", arm, stage),
        "n_pending": scalar("SELECT COUNT(*) FROM trial WHERE arm=? AND stage=? AND status='pending'", arm, stage),
        "n_queries": scalar("SELECT COUNT(*) FROM query_event q JOIN trial t ON t.trial_id=q.trial_id "
                            "WHERE t.arm=? AND t.stage=?", arm, stage),
        "seeds": seeds, "n_tasks": len(tasks), "n_injections": len(injs),
        "first_utc": min(created) if created else None, "last_utc": max(latest) if latest else None,
        "n_dbs": len(dbs), "db_paths": list(db_paths),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _fmt_q(q: tuple[float, bool] | None) -> str:
    if q is None:
        return "–"
    v, cens = q
    return f"&gt; {v:.0f}" if cens else f"{v:.0f}"


def _pct(x: float | None) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.0f}%"


def _asr_series(km: KMCurve, ceiling: int) -> list[float]:
    """ASR(q) for q = 0..ceiling as a step series."""
    return [km.asr_at(q) if q > 0 else 0.0 for q in range(ceiling + 1)]


def svg_curves(cells: list[Cell], ceiling: int, width: int = 780, height: int = 400) -> str:
    """Step curves of ASR(q) = 1 - S(q), one per configuration, with hover."""
    ml, mr, mt, mb = 48, 215, 18, 44
    pw, ph = width - ml - mr, height - mt - mb
    xs = lambda q: ml + pw * q / max(ceiling, 1)          # noqa: E731
    ys = lambda a: mt + ph * (1 - a)                       # noqa: E731
    parts = [f'<svg class="curves" viewBox="0 0 {width} {height}" width="100%" '
             f'role="img" aria-label="Work-factor curves: attack success rate against attacker query budget">']
    # grid + axes
    for frac in (0, .25, .5, .75, 1):
        y = ys(frac)
        parts.append(f'<line class="grid" x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{ml - 8}" y="{y + 4:.1f}" text-anchor="end">{int(frac * 100)}%</text>')
    step = 1 if ceiling <= 12 else max(1, ceiling // 10)
    for q in range(0, ceiling + 1, step):
        parts.append(f'<text class="tick" x="{xs(q):.1f}" y="{mt + ph + 18}" text-anchor="middle">{q}</text>')
    parts.append(f'<line class="axis" x1="{ml}" x2="{ml + pw}" y1="{mt + ph}" y2="{mt + ph}"/>')
    parts.append(f'<text class="axis-label" x="{ml + pw / 2:.0f}" y="{height - 8}" text-anchor="middle">'
                 f'attacker queries (budget)</text>')
    parts.append(f'<text class="axis-label" transform="translate(14 {mt + ph / 2:.0f}) rotate(-90)" '
                 f'text-anchor="middle">attack success rate  ASR(q) = 1 − S(q)</text>')

    series_json = []
    ends: list[tuple[float, int, str]] = []
    for i, c in enumerate(cells[:MAX_SERIES]):
        if not c.km or not c.data:
            continue
        asr = _asr_series(c.km, ceiling)
        d = [f"M{xs(0):.1f},{ys(0):.1f}"]
        for q in range(1, ceiling + 1):
            d.append(f"H{xs(q):.1f}")
            if asr[q] != asr[q - 1]:
                d.append(f"V{ys(asr[q]):.1f}")
        parts.append(f'<path class="s{i + 1} line" d="{" ".join(d)}"/>')
        series_json.append({"label": c.label, "asr": [round(a, 4) for a in asr], "slot": i + 1})
        ends.append((ys(asr[-1]), i, f"{c.label}  {100 * asr[-1]:.0f}%"))
    # direct labels, pushed apart
    ends.sort()
    placed: list[float] = []
    for y, i, text in ends:
        yy = y
        if placed and yy - placed[-1] < 13:
            yy = placed[-1] + 13
        placed.append(yy)
        parts.append(f'<text class="s{i + 1} label" x="{ml + pw + 8}" y="{yy + 4:.1f}">{html.escape(text)}</text>')
    parts.append(f'<rect class="hit" x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="transparent"/>')
    parts.append(f'<line class="crosshair" x1="0" x2="0" y1="{mt}" y2="{mt + ph}" style="display:none"/>')
    parts.append("</svg>")
    parts.append(f'<script type="application/json" id="curve-data">'
                 f'{json.dumps({"ceiling": ceiling, "ml": ml, "pw": pw, "series": series_json})}</script>')
    return "\n".join(parts)


def svg_composition(comp: dict, slot: int, width: int = 420) -> str:
    """One row: ASR axis 0..1 with ticks for A and B, hollow mark for the
    independence prediction, filled mark for the observed stack."""
    r = comp["result"]
    h, ml, mr = 46, 12, 12
    pw = width - ml - mr
    x = lambda a: ml + pw * min(max(a, 0), 1)             # noqa: E731
    y = 24
    s = [f'<svg viewBox="0 0 {width} {h}" width="100%" class="dumbbell" role="img" '
         f'aria-label="observed versus predicted stack ASR">',
         f'<line class="axis" x1="{ml}" x2="{ml + pw}" y1="{y}" y2="{y}"/>']
    for frac in (0, .5, 1):
        s.append(f'<text class="tick" x="{x(frac):.1f}" y="{h - 4}" text-anchor="middle">{int(frac * 100)}%</text>')
    s.append(f'<line class="pred" x1="{x(r.asr_ab_predicted):.1f}" x2="{x(r.asr_ab_observed):.1f}" y1="{y}" y2="{y}"/>')
    for a, lab in ((r.asr_a, "A"), (r.asr_b, "B")):
        s.append(f'<line class="member" x1="{x(a):.1f}" x2="{x(a):.1f}" y1="{y - 9}" y2="{y + 9}"/>')
        s.append(f'<text class="tick" x="{x(a):.1f}" y="{y - 12}" text-anchor="middle">{lab}</text>')
    s.append(f'<circle class="pred-mark" cx="{x(r.asr_ab_predicted):.1f}" cy="{y}" r="6"/>')
    s.append(f'<circle class="s{slot} obs-mark" cx="{x(r.asr_ab_observed):.1f}" cy="{y}" r="6"/>')
    s.append("</svg>")
    return "".join(s)


def _verdict_class(verdict: str) -> str:
    v = verdict.split(" ")[0]
    return {"additive": "neutral", "super-additive": "good", "sub-additive": "warning",
            "destructive": "critical"}.get(v, "neutral")


CSS = """
:root{--surface:#fcfcfb;--plane:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
--border:rgba(11,11,11,.10);--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--s8:#e34948;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--surface:#1a1a19;--plane:#0d0d0d;--ink:#f0efec;--ink2:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767;color-scheme:dark}}
:root[data-theme="dark"]{--surface:#1a1a19;--plane:#0d0d0d;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--plane);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:8px 0 2px}h2{font-size:17px;margin:0 0 10px}h3{font-size:15px;margin:0 0 6px;color:var(--ink2)}
.sub{color:var(--ink2);margin:0 0 14px}
.banner{border-radius:8px;padding:12px 16px;margin:12px 0 18px;font-weight:600;border:1px solid var(--border)}
.banner.synthetic{background:color-mix(in srgb,var(--serious) 18%,var(--surface));}
.banner.real{background:color-mix(in srgb,var(--good) 14%,var(--surface));}
.banner small{display:block;font-weight:400;color:var(--ink2);margin-top:4px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin:0 0 16px}
.grid2{display:grid;grid-template-columns:1fr;gap:16px}@media(min-width:900px){.grid2{grid-template-columns:1fr 1fr}}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.tile .v{font-size:24px;font-weight:600}.tile .k{color:var(--ink2);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--ink2);font-weight:600}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.s1{--c:var(--s1)}.s2{--c:var(--s2)}.s3{--c:var(--s3)}.s4{--c:var(--s4)}.s5{--c:var(--s5)}.s6{--c:var(--s6)}.s7{--c:var(--s7)}.s8{--c:var(--s8)}
.sw{background:var(--c)}
svg text{fill:var(--ink2);font:12px system-ui,-apple-system,"Segoe UI",sans-serif}
svg .grid{stroke:var(--grid);stroke-width:1}svg .axis{stroke:var(--axis);stroke-width:1}
svg .line{fill:none;stroke:var(--c);stroke-width:2;stroke-linejoin:round}
svg .label{fill:var(--ink);font-size:12px}svg .tick{fill:var(--muted);font-size:11px}
svg .crosshair{stroke:var(--ink2);stroke-dasharray:3 3}
svg .member{stroke:var(--axis);stroke-width:2}svg .pred{stroke:var(--axis);stroke-width:2}
svg .pred-mark{fill:var(--surface);stroke:var(--ink2);stroke-width:2}svg .obs-mark{fill:var(--c)}
.tip{position:fixed;pointer-events:none;background:var(--surface);border:1px solid var(--border);border-radius:8px;
padding:8px 10px;font-size:13px;box-shadow:0 4px 16px rgba(0,0,0,.12);display:none;z-index:9}
.tip b{display:block;margin-bottom:4px}.tip div{white-space:nowrap}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid var(--border)}
.badge.neutral{background:var(--grid);color:var(--ink)}.badge.good{background:var(--good);color:#fff}
.badge.warning{background:var(--warning);color:#0b0b0b}.badge.critical{background:var(--critical);color:#fff}
.note{color:var(--ink2);font-size:13px}.mono{font-family:ui-monospace,Consolas,monospace;font-size:13px}
footer{color:var(--muted);font-size:13px;margin:24px 0 8px}
"""

JS = """
(function(){
  var el=document.getElementById('curve-data'); if(!el) return;
  var D=JSON.parse(el.textContent), svg=document.querySelector('svg.curves');
  var hit=svg.querySelector('.hit'), cross=svg.querySelector('.crosshair');
  var tip=document.createElement('div'); tip.className='tip'; document.body.appendChild(tip);
  function pt(evt){var p=svg.createSVGPoint();p.x=evt.clientX;p.y=evt.clientY;return p.matrixTransform(svg.getScreenCTM().inverse());}
  hit.addEventListener('mousemove',function(e){
    var p=pt(e), q=Math.round((p.x-D.ml)/D.pw*D.ceiling); q=Math.max(0,Math.min(D.ceiling,q));
    var x=D.ml+D.pw*q/D.ceiling; cross.setAttribute('x1',x); cross.setAttribute('x2',x); cross.style.display='';
    var h='<b>budget q = '+q+'</b>';
    D.series.forEach(function(s){h+='<div><span class="sw s'+s.slot+'"></span>'+s.label+': '+Math.round(100*s.asr[q])+'%</div>';});
    tip.innerHTML=h; tip.style.display='block'; tip.style.left=(e.clientX+14)+'px'; tip.style.top=(e.clientY-10)+'px';
  });
  hit.addEventListener('mouseleave',function(){cross.style.display='none';tip.style.display='none';});
})();
"""


def render(summary: dict[str, Any], *, title: str = "Work Factor", refresh: int | None = None) -> str:
    S = summary
    cells: list[Cell] = S["cells"]
    ceiling = S["ceiling"]
    esc = html.escape
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if S["synthetic"]:
        banner = ('<div class="banner synthetic">SYNTHETIC REHEARSAL — stage harness_test. '
                  'These curves come from a seeded random number generator through the synthetic '
                  'adapter. They exercise the apparatus and measure no real defense.'
                  '<small>The database refuses to record this adapter at any evidence stage; '
                  'this banner is derived from adapter_registry.evidence_class and cannot be switched off.</small></div>')
    else:
        banner = (f'<div class="banner real">REAL RUN — stage {esc(S["stage"])}, target '
                  f'{esc(S["model_snapshot"])}, AgentDojo {esc(S["benchmark_version"])}.'
                  f'<small>Evidence class "real" per adapter_registry. '
                  f'{"Stage A screening: selects what to confirm; not itself a confirmatory result." if S["stage"] == "screen" else "Confirmatory stage."}</small></div>')

    pre = S["prereg"] or {}
    under_seeded = pre and len(S["seeds"]) < int(pre.get("n_seeds", 0))
    tiles = [
        ("trials finalized", f'{S["n_ok"]:,}'),
        ("attacker queries", f'{S["n_queries"]:,}'),
        ("configurations", f"{len(cells)}"),
        ("security cases", f'{S["n_tasks"]} × {S["n_injections"]}'),
        ("seeds" + (f' (preregistered: {pre["n_seeds"]})' if pre else ""), f'{len(S["seeds"])}'),
        ("errors / pending", f'{S["n_error"]} / {S["n_pending"]}'),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="v">{esc(v)}</div><div class="k">{esc(k)}</div></div>'
                         for k, v in tiles)

    # work-factor table
    qs = S["quantiles"]
    head = "".join(f'<th class="num">Q{int(p * 100)}</th>' for p in qs)
    rows = []
    for i, c in enumerate(cells):
        km = c.km
        asr = km.asr_at(ceiling) if km and c.data else None
        qcells = "".join(f'<td class="num">{_fmt_q(c.quantiles.get(p))}</td>' for p in qs)
        ci = c.q25_ci
        ci_txt = "–"
        if ci and ci["point"] is not None:
            ci_txt = f'[{ci["lo"]:.0f}, {ci["hi"]:.0f}]' + (f' cens {ci["censored_frac"]:.0%}' if ci["censored_frac"] > 0 else "")
        lr = c.logrank
        lr_txt = "baseline" if not c.components else (
            f'χ²={lr.chi2:.1f}, p={lr.p_value:.2g}' if lr else "–")
        rows.append(
            f'<tr><td><span class="sw s{min(i + 1, MAX_SERIES)}"></span>{esc(c.label)}</td>'
            f'<td class="num">{c.n}</td><td class="num">{c.n_censored}</td><td class="num">{c.n_errors}</td>'
            f'<td class="num">{_pct(asr)}</td>{qcells}<td class="num">{ci_txt}</td>'
            f'<td class="num">{_pct(c.benign_mean)}</td><td class="num">{_pct(c.uua_mean)}</td><td>{lr_txt}</td></tr>')
    table = (f'<table><thead><tr><th>defense configuration</th><th class="num">n</th><th class="num">censored</th>'
             f'<th class="num">errors</th><th class="num">ASR@{ceiling}</th>{head}<th class="num">Q25 95% CI</th>'
             f'<th class="num">benign utility</th><th class="num">utility under attack</th><th>log-rank vs none</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table>')

    # composition
    comp_rows = []
    for comp in S["compositions"]:
        r = comp["result"]
        pair: Cell = comp["pair"]
        slot = min(cells.index(pair) + 1, MAX_SERIES)
        comp_rows.append(
            f'<tr><td><span class="sw s{slot}"></span>{esc(pair.label)}<br>'
            f'<span class="note">A = {esc(comp["a"].label)}, B = {esc(comp["b"].label)}</span></td>'
            f'<td class="num">{_pct(r.asr_none)}</td><td class="num">{_pct(r.asr_a)}</td><td class="num">{_pct(r.asr_b)}</td>'
            f'<td class="num">{_pct(r.asr_ab_predicted)}</td><td class="num"><b>{_pct(r.asr_ab_observed)}</b></td>'
            f'<td class="num">{r.interaction:+.2f}<br><span class="note">[{r.ci_lo:+.2f}, {r.ci_hi:+.2f}]</span></td>'
            f'<td><span class="badge {_verdict_class(r.verdict)}">{esc(r.verdict.split(" (")[0])}</span>'
            f'<br><span class="note">{esc(r.verdict.split(" (")[1].rstrip(")")) if "(" in r.verdict else ""}</span></td>'
            f'<td>{svg_composition(comp, slot)}</td></tr>')
    if comp_rows:
        comp_html = (f'<table><thead><tr><th>stack</th><th class="num">ASR none</th><th class="num">ASR A</th>'
                     f'<th class="num">ASR B</th><th class="num">predicted A+B</th><th class="num">observed A+B</th>'
                     f'<th class="num">Δ log-risk</th><th>verdict</th><th>hollow = predicted, filled = observed</th></tr></thead>'
                     f'<tbody>{"".join(comp_rows)}</tbody></table>'
                     f'<p class="note">Null: if A and B block independently, log ASR(A+B) = log ASR(A) + log ASR(B) − log ASR(none) '
                     f'at budget {ceiling}. Δ &gt; 0 means the stack is weaker than independence predicts. '
                     f'"destructive" requires the stack to be worse than its own better member. '
                     f'Percentile bootstrap CI, 95%.</p>')
    else:
        comp_html = ('<p class="note">No stack has both of its members and the undefended baseline in this '
                     'database yet, so no composition test can be formed.</p>')

    # templates
    tmpl_rows = []
    for i, c in enumerate(cells):
        if not c.template_breaks:
            continue
        items = sorted(c.template_breaks.items(), key=lambda kv: -kv[1])
        tmpl_rows.append(f'<tr><td><span class="sw s{min(i + 1, MAX_SERIES)}"></span>{esc(c.label)}</td>'
                         f'<td>{", ".join(f"{esc(k)} ({v})" for k, v in items)}</td></tr>')
    tmpl_html = (f'<table><thead><tr><th>configuration</th><th>template that produced the first break (count)</th></tr></thead>'
                 f'<tbody>{"".join(tmpl_rows)}</tbody></table>' if tmpl_rows
                 else '<p class="note">No breaks recorded yet.</p>')

    # cost channels, two tables
    att = "".join(f'<tr><td><span class="sw s{min(i + 1, MAX_SERIES)}"></span>{esc(c.label)}</td>'
                  f'<td class="num">{c.queries_total:,}</td><td class="num">{(c.queries_total / c.n) if c.n else 0:.1f}</td>'
                  f'<td class="num">{c.wall_clock_total / 60:.1f}</td>'
                  f'<td class="num">{"n/a" if S["synthetic"] else f"${c.attacker_usd:.2f}"}</td></tr>'
                  for i, c in enumerate(cells))
    dfd = "".join(f'<tr><td><span class="sw s{min(i + 1, MAX_SERIES)}"></span>{esc(c.label)}</td>'
                  f'<td class="num">{c.defender_tokens:,}</td>'
                  f'<td class="num">{"n/a" if S["synthetic"] else f"${c.defender_usd:.2f}"}</td></tr>'
                  for i, c in enumerate(cells))
    cost_html = (f'<div class="grid2"><div><h3>Attacker side — what breaking it costs an adversary</h3>'
                 f'<table><thead><tr><th>configuration</th><th class="num">queries</th><th class="num">per trial</th>'
                 f'<th class="num">wall clock (min)</th><th class="num">USD</th></tr></thead><tbody>{att}</tbody></table></div>'
                 f'<div><h3>Defender side — what running it costs the operator</h3>'
                 f'<table><thead><tr><th>configuration</th><th class="num">defender tokens</th><th class="num">USD</th></tr></thead>'
                 f'<tbody>{dfd}</tbody></table>'
                 f'<p class="note">Two channels, never summed: no combined-cost column exists in the schema. '
                 f'Token and USD counters are 0 where the adapter does not yet capture usage from the benchmark; '
                 f'queries and wall clock are always recorded.</p></div></div>')

    curves = svg_curves(cells, ceiling) if any(c.data for c in cells) else \
        '<p class="note">No finalized trials yet. The page re-renders on refresh.</p>'

    legend = "".join(f'<span style="margin-right:14px"><span class="sw s{min(i + 1, MAX_SERIES)}"></span>{esc(c.label)}</span>'
                     for i, c in enumerate(cells))
    meta_bits = [
        f'prereg <span class="mono">{esc(pre.get("prereg_id", "–"))}</span>' if pre else "no preregistration row",
        f'plan sha <span class="mono">{esc(pre.get("plan_sha", "–"))}</span>' if pre else "",
        f'ladder {esc(str(S["ladder"]))}' if S["ladder"] else "",
        f'attack <span class="mono">{esc(S["method_id"] or "–")}</span>',
        f'{S["n_dbs"]} database{"s" if S["n_dbs"] != 1 else ""}',
        f'first trial {esc(S["first_utc"] or "–")}, latest {esc(S["last_utc"] or "–")}',
    ]
    warn = ('<p class="note"><b>Under-seeded:</b> the preregistration declares '
            f'{pre.get("n_seeds")} seeds per cell and this run has {len(S["seeds"])}. '
            'Curves are reported as such; seed-to-seed variability is not separated from defense effect.</p>'
            if under_seeded else "")

    refresh_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh_tag}<title>{esc(title)}</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>{esc(title)} — cost-to-break for LLM-agent defenses</h1>
<p class="sub">Attacker effort as a survival time: how many published injection templates does it take before each defense first fails?
Right-censored where the defense held for the whole budget. Rendered {now}.</p>
{banner}
<div class="tiles">{tiles_html}</div>
<div class="card"><h2>Work-factor curves</h2>
<p class="note">Kaplan–Meier estimate of ASR(q), the probability the defense has been broken by query q. Hover for values. {legend}</p>
{curves}</div>
<div class="card"><h2>Quantiles of the work factor</h2>
<p class="note">Q<sub>p</sub> is the smallest budget at which ASR reaches p. "&gt; {ceiling}" means the curve never reached p inside the budget: a lower bound, not a missing value.</p>
{table}{warn}</div>
<div class="card"><h2>Does stacking compose?</h2>{comp_html}</div>
<div class="grid2">
<div class="card"><h2>Which template broke it first</h2>{tmpl_html}
<p class="note">Per-query telemetry from query_event; the attack schedule plays the 11 upstream templates in a fixed order, so position in the schedule is the budget.</p></div>
<div class="card"><h2>Run facts</h2><p class="note">{"<br>".join(b for b in meta_bits if b)}</p>
<p class="note">Model <span class="mono">{esc(S["model_snapshot"])}</span> · arm {esc(S["arm"])} · stage {esc(S["stage"])} · evidence {esc(", ".join(S["evidence"]))}</p></div>
</div>
<div class="card"><h2>Cost, in two channels</h2>{cost_html}</div>
<footer>Work Factor · github.com/francium619/work-factor-llm-agent-security · statistics: Kaplan–Meier, log-rank, percentile bootstrap, log-risk composition null (wf/analysis.py).
Nothing on this page pools across models, arms, or stages; the read API refuses to.</footer>
</div><script>{JS}</script></body></html>"""
