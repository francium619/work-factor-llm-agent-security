"""
fill_template.py -- fill the organizers' Round 1 template with the Work Factor pitch.

    py -3.14 hackathon/fill_template.py <13TH_PROTOCOL_Ppt_Template.pptx> <out.pptx>

The template says not to change the colour theme or the logo placement, so
this script only replaces text inside the existing text boxes (keeping each
run's font, size and colour), widens one caption box inside its own panel,
removes the organizers' instruction note from slide 1, and adds speaker notes.
It also adds one slide after "Features & Innovation" showing the dashboard's
work-factor curves (synthetic rehearsal data, labelled as such); that slide is
a copy of slide 4's frame, so the logos, title style and footer are unchanged.

Every claim matches the repository as of 2026-11-17: 128 checks pass, 7
defense configurations (3 stacks), 144 banking cases, 11 attack templates,
the run_workfactor.py cost estimate, and no real trial written yet.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

from pptx import Presentation
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

# (slide number, shape id) -> new text. The template has one run per box.
TEXT: dict[tuple[int, int], str] = {
    # 1 Title
    (1, 89): "Work Factor",
    (1, 90): "Measuring how hard it is to break an AI agent's defenses",
    (1, 92): "Team Ligga",
    (1, 93): "Lunavath Ganesh",
    # 2 Problem & Audience
    (2, 118): "Agents obey text hidden in emails and pages.",
    (2, 119): "Defenses are scored on one attempt only.",
    (2, 120): "Nobody checks if stacked defenses help.",
    (2, 122): "Teams shipping tool-using AI agents.",
    (2, 123): "Red teams and security reviewers.",
    (2, 124): "Researchers building injection defenses.",
    # 3 Proposed Solution
    (3, 142): "Measure Security as Cost to Break",
    (3, 143): "We count the attack attempts each AI agent defense survives.",
    (3, 144): "Count the Attempts",
    (3, 145): "Survival time to the first successful attack.",
    (3, 146): "Keep the Survivors",
    (3, 147): "Unbroken defenses kept as lower bounds.",
    (3, 148): "Test the Stack",
    (3, 149): "Checks if stacking two defenses pays off.",
    # 4 Features & Innovation
    (4, 166): "Fixed Attack Schedule",
    (4, 167): "11 published injection templates, same order for every defense.",
    (4, 169): "Defense Stacking",
    (4, 170): "Multi-defense pipelines built from AgentDojo's own parts.",
    (4, 172): "Work-Factor Curves",
    (4, 173): "Survival curves and stack verdicts on a live dashboard.",
    # 5 Tech Stack & Architecture
    (5, 198): "Dashboard",
    (5, 199): "Self-contained HTML, Python server",
    (5, 200): "Benchmark",
    (5, 201): "AgentDojo 0.1.35, banking, 144 cases",
    (5, 202): "Agent & Defenses",
    (5, 203): "OpenAI gpt-4o-mini, AgentDojo defenses",
    (5, 204): "Data & Statistics",
    (5, 205): "SQLite with enforced rules, NumPy, SciPy",
    (5, 206): "Fixed Attacker",
    (5, 207): "Defense Stack",
    (5, 208): "Agent + Checker",
    (5, 209): "Live Dashboard",
    (5, 197): "Every attempt is checked by the benchmark and stored as one database row.",
    # 6 Impact & Scalability
    (6, 228): "Effort",
    (6, 229): "Defenses ranked by attacker effort.",
    (6, 231): "~$9",
    (6, 232): "First real slice, about 2 hours on 3 parallel runs.",
    (6, 234): "949",
    (6, 235): "Security cases across all four AgentDojo suites.",
    # 7 Roadmap & Conclusion
    (7, 256): "Prototype",
    (7, 257): "Built and rehearsed; 128 checks pass.",
    (7, 259): "Hackathon Run",
    (7, 260): "First real measurement, live on campus.",
    (7, 262): "Scale",
    (7, 263): "More suites, models and attackers.",
    (7, 250): "Price the defense, not just score it.",
    (7, 251): "Thank you.  github.com/francium619/work-factor-llm-agent-security",
}

DELETE = {(1, 97)}   # organizers' "don't change the colour theme" instruction

NOTES = {
    1: "Work Factor measures how much effort an attacker needs to break an AI agent's prompt-injection defenses, instead of only whether one attempt worked.",
    2: "A banking agent reads a transaction note that contains an instruction, and follows it. Defenses exist, but they are scored on a single attempt, which hides how hard the attacker had to try. Nobody measures whether stacking two defenses helps.",
    3: "Three ideas. Treat attacker effort as a survival time. Never drop the defenses that held, because dropping them biases every comparison toward the weak ones. Test stacks against an explicit independence prediction.",
    4: "The attacker is fixed so every defense faces the same instrument. Stacks are built from AgentDojo's own pipeline elements, because its public API takes only one defense. The dashboard shows curves, quantiles with intervals, and a verdict per stack.",
    5: "The success check is the benchmark's own code, not an LLM judge. The database enforces the integrity rules: survivors are never dropped, synthetic rehearsal data cannot be filed as evidence, and data from different models is never pooled.",
    6: "The cost is a planning estimate printed by the tool before it spends anything: about 5,500 agent runs in the worst case for the first slice. The method scales to all four AgentDojo suites, 949 cases.",
    7: "Honest status: the apparatus is built and rehearsed on synthetic data, and no real trial has been run yet. The first real run happens during the 28 hours on campus, with the dashboard on screen.",
}


GRAPH = Path(__file__).with_name("dashboard_synthetic_curves.png")
GRAPH_CROP_TOP = 0.145      # drop the PNG's own title and legend; the line labels remain
GRAPH_SLIDE_AT = 5          # inserted after "Features & Innovation"
GRAPH_NOTES = (
    "This is the live dashboard from the prototype, running on synthetic rehearsal data. "
    "Each line is one defense setup; the height at any point is the share of cases broken "
    "within that many attacker attempts, so lower and flatter is better. The stacks sit lower "
    "here because the rehearsal adapter is built that way; whether real stacks do is exactly "
    "what the on-campus run measures."
)


def _clone_text(slide, src_shape, left, top, width, height, text):
    """Copy a template text box (keeping its font, size, colour) to a new place and text."""
    el = copy.deepcopy(src_shape._element)
    slide.shapes._spTree.append(el)
    shape = slide.shapes[-1]
    shape.left, shape.top, shape.width, shape.height = left, top, width, height
    runs = [r for p in shape.text_frame.paragraphs for r in p.runs]
    runs[0].text = text
    for r in runs[1:]:
        r.text = ""
    shape.text_frame.word_wrap = True
    return shape


def add_graph_slide(prs) -> None:
    """Clone slide 4's frame (logos, title, footer) into a new slide that shows the dashboard curves."""
    src = prs.slides[3]
    new = prs.slides.add_slide(src.slide_layout)
    for ph in list(new.placeholders):
        ph._element.getparent().remove(ph._element)
    rid_map = {}
    for rel in src.part.rels.values():
        if rel.reltype.endswith("/image"):
            rid_map[rel.rId] = new.part.relate_to(rel._target, rel.reltype)
    by_id = {s.shape_id: s for s in src.shapes}
    for sid in (157, 158, 159, 163, 164, 174, 175, 176):   # footer bars, divider, title, footer text, logos
        el = copy.deepcopy(by_id[sid]._element)
        for node in el.iter():
            for attr in ("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed",):
                if node.get(attr) in rid_map:
                    node.set(attr, rid_map[node.get(attr)])
        new.shapes._spTree.append(el)
    title = [s for s in new.shapes if s.has_text_frame and s.text_frame.text.startswith("Features")][0]
    title.text_frame.paragraphs[0].runs[0].text = "Prototype Dashboard"

    # the chart, on the left, cropped to the plot and its line labels
    top, height = Inches(1.65), Inches(4.5)
    w_px, h_px = 2134, 1314
    crop_l, crop_r, crop_b = 0.006, 0.06, 0.004
    width = int(height * w_px * (1 - crop_l - crop_r) / (h_px * (1 - GRAPH_CROP_TOP - crop_b)))
    pic = new.shapes.add_picture(str(GRAPH), Inches(0.67), top, width, height)
    pic.crop_top, pic.crop_left, pic.crop_right, pic.crop_bottom = GRAPH_CROP_TOP, crop_l, crop_r, crop_b

    # a reading guide in the template's own card and text styles, on the right
    x = Inches(0.67) + width + Inches(0.3)
    w = Inches(12.67) - x
    card = copy.deepcopy(by_id[160]._element)
    new.shapes._spTree.append(card)
    c = new.shapes[-1]
    c.left, c.top, c.width, c.height = x, top, w, height
    pad = Inches(0.3)
    tx, tw = x + pad, w - 2 * pad
    _clone_text(new, by_id[165], tx, top + Inches(0.35), tw, Inches(0.2), "SYNTHETIC REHEARSAL")
    _clone_text(new, by_id[166], tx, top + Inches(0.65), tw, Inches(0.35), "Reading the Curves")
    lines = [
        "Each line is one defense setup.",
        "Height: share of cases broken within that many attempts. Lower is safer.",
        "Slope: how fast a defense gives way as the attacker keeps trying.",
        "Not a result. The real curves are drawn live during the on-campus run.",
    ]
    y = top + Inches(1.25)
    chars_per_line = int(tw / Inches(1) * 13)          # about 13 characters per inch at 12 pt Inter
    for line in lines:
        n = -(-len(line) // chars_per_line)
        h = Inches(0.21 * n)
        s = _clone_text(new, by_id[167], tx, y, tw, h, line)
        s.text_frame.paragraphs[0].runs[0].font.size = Pt(12)
        y += h + Inches(0.22)
    new.notes_slide.notes_text_frame.text = GRAPH_NOTES

    # move the new slide into position
    ids = prs.slides._sldIdLst
    el = ids[-1]
    ids.remove(el)
    ids.insert(GRAPH_SLIDE_AT - 1, el)


def main(src: str, out: str) -> None:
    prs = Presentation(src)
    seen: set[tuple[int, int]] = set()
    for n, slide in enumerate(prs.slides, 1):
        for shape in list(slide.shapes):
            key = (n, shape.shape_id)
            if key in DELETE:
                shape._element.getparent().remove(shape._element)
                seen.add(key)
                continue
            if key not in TEXT:
                continue
            runs = [r for p in shape.text_frame.paragraphs for r in p.runs]
            if len(runs) != 1:
                raise SystemExit(f"slide {n} shape {shape.shape_id}: expected one run, found {len(runs)}")
            runs[0].text = TEXT[key]
            seen.add(key)
            if key == (5, 197):
                # widen the caption to the width of its own panel (shape 182)
                shape.left, shape.width = Inches(5.72), Inches(6.42)
                shape.text_frame.paragraphs[0].alignment = PP_ALIGN.CENTER
        if n in NOTES:
            slide.notes_slide.notes_text_frame.text = NOTES[n]
    missing = (set(TEXT) | DELETE) - seen
    if missing:
        raise SystemExit(f"template shapes not found: {sorted(missing)}")
    add_graph_slide(prs)
    prs.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
