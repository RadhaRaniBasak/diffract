
from html import escape

BUDGET_TOLERANCE_NOTE = "over the line budget"


def slice_color(number: int, total: int) -> str:
    hue = 265 if total <= 1 else round(265 - 260 * (number - 1) / (total - 1))
    return f"hsl({hue}, 68%, 50%)"


def _status_text(item: dict) -> tuple[str, str]:
    if item["status"] == "pass":
        return ("passed on retry", "warn") if item.get("flaky") else ("passed", "pass")
    if item["status"] == "unverified":
        return "not verified", "muted"
    return item["status"], "fail"


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _prism_svg(view: dict) -> str:
    slices = view["slices"]
    total = len(slices)
    row_height = 46
    height = max(300, total * row_height + 80)
    middle = height / 2
    widest = max([s["lines"] for s in slices] + [1])
    parts = [
        f'<svg class="prism" viewBox="0 0 1000 {height}" role="img" aria-labelledby="prism-title">',
        f'<title id="prism-title">{escape(view["head"])}, {view["original"]["lines"]:,} lines, split into '
        f'{_plural(total, "slice")}</title>',
        f'<rect x="24" y="{middle - 17}" width="296" height="34" rx="3" class="beam"/>',
        f'<text x="28" y="{middle - 30}" class="beam-label">{escape(view["head"])}</text>',
        f'<text x="28" y="{middle + 44}" class="beam-sub">{view["original"]["lines"]:,} lines in one PR</text>',
    ]
    ray_start_x = 372
    label_x = 584
    for index, item in enumerate(slices):
        y = 50 + index * (height - 100) / max(total - 1, 1) if total > 1 else middle
        thickness = max(3.0, min(20.0, 20.0 * item["lines"] / widest))
        dash = ' stroke-dasharray="10 6"' if item["over_budget"] else ""
        color = slice_color(item["number"], total)
        exit_y = middle + (index - (total - 1) / 2) * 3
        parts.append(f'<path d="M {ray_start_x} {exit_y:.1f} L {label_x - 24} {y:.1f} L {label_x - 8} {y:.1f}" '
                     f'stroke="{color}" stroke-width="{thickness:.1f}" fill="none" stroke-linecap="round"{dash}/>')
        status, tone = _status_text(item)
        title = item["title"] if len(item["title"]) <= 46 else item["title"][:45].rstrip() + "…"
        budget = f", {BUDGET_TOLERANCE_NOTE}" if item["over_budget"] else ""
        parts.append(f'<text x="{label_x}" y="{y - 3:.1f}" class="ray-title">'
                     f'<tspan class="ray-number" fill="{color}">{item["number"]}</tspan> {escape(title)}</text>')
        parts.append(f'<text x="{label_x}" y="{y + 15:.1f}" class="ray-sub">{item["lines"]:,} lines{budget}, '
                     f'<tspan class="{tone}">{status}</tspan></text>')
    parts.append(f'<polygon points="340,{middle - 86} 282,{middle + 62} 398,{middle + 62}" class="glass"/>')
    parts.append("</svg>")
    return "\n".join(parts)


def _prism_compact_svg(view: dict) -> str:
    slices = view["slices"]
    total = len(slices)
    height = max(260, total * 52 + 60)
    middle = height / 2
    widest = max([s["lines"] for s in slices] + [1])
    parts = [f'<svg class="prism-compact" viewBox="0 0 640 {height}" role="img" aria-hidden="true">',
             f'<rect x="12" y="{middle - 16}" width="236" height="32" rx="3" class="beam"/>']
    for index, item in enumerate(slices):
        y = 34 + index * (height - 68) / max(total - 1, 1) if total > 1 else middle
        thickness = max(4.0, min(22.0, 22.0 * item["lines"] / widest))
        dash = ' stroke-dasharray="12 7"' if item["over_budget"] else ""
        exit_y = middle + (index - (total - 1) / 2) * 3
        parts.append(f'<path d="M 292 {exit_y:.1f} L 548 {y:.1f} L 572 {y:.1f}" stroke="{slice_color(item["number"], total)}" '
                     f'stroke-width="{thickness:.1f}" fill="none" stroke-linecap="round"{dash}/>')
        parts.append(f'<text x="588" y="{y + 11:.1f}" class="ray-number-compact">{item["number"]}</text>')
    parts.append(f'<polygon points="264,{middle - 80} 212,{middle + 58} 316,{middle + 58}" class="glass"/>')
    parts.append("</svg>")
    return "\n".join(parts)


def _ray_list(view: dict) -> str:
    total = len(view["slices"])
    items = []
    for item in view["slices"]:
        status, tone = _status_text(item)
        budget = f", {BUDGET_TOLERANCE_NOTE}" if item["over_budget"] else ""
        items.append(f'<li><span class="dot" style="background:{slice_color(item["number"], total)}"></span>'
                     f'<span><b>{item["number"]}</b> {escape(item["title"])}<br>'
                     f'<span class="quiet">{item["lines"]:,} lines{budget}, <span class="{tone}">{status}</span></span></span></li>')
    return f'<ol class="ray-list" aria-label="Slices">{"".join(items)}</ol>'


def _worth_a_look(view: dict) -> str:
    items = list(view["gaps"])
    items += [f"Slice {s['number']} passed only on retry: a test there is flaky." for s in view["slices"] if s.get("flaky")]
    items += [f"Slice {s['number']} has {s['lines']:,} lines, over the {view['budget']:,}-line budget."
              for s in view["slices"] if s["over_budget"]]
    if not items:
        return '<p class="quiet">Nothing stands out: every requirement with behavioural changes also changes tests.</p>'
    return "<ul class=\"look\">" + "".join(f"<li>{escape(item)}</li>" for item in items) + "</ul>"


def _requirements_table(view: dict) -> str:
    requirements = view["requirements"]
    if not requirements:
        return ('<p class="quiet">No requirements were recorded. Give Bob the design doc or ticket and it will map '
                'every slice to it.</p>')
    total = len(view["slices"])
    head = "".join(f'<th scope="col" class="num" style="border-top-color:{slice_color(n, total)}">{n}</th>'
                   for n in range(1, total + 1))
    rows = []
    for requirement in requirements:
        cells = []
        for number in range(1, total + 1):
            roles = requirement["per_slice"].get(number, set())
            if "code" in roles or "tests" in roles:
                mark = "●" + ("<sub>T</sub>" if "tests" in roles else "")
                label = "code and tests" if {"code", "tests"} <= roles else "tests" if "tests" in roles else "code"
                cells.append(f'<td class="num" style="color:{slice_color(number, total)}" '
                             f'aria-label="slice {number}: {label}">{mark}</td>')
            elif "docs" in roles:
                cells.append(f'<td class="num docs" aria-label="slice {number}: docs">○</td>')
            else:
                cells.append('<td class="num"></td>')
        tone = {"no test changes": "warn", "not in this PR": "fail", "code + tests": "pass"}.get(requirement["status"], "muted")
        rows.append(f'<tr><th scope="row"><span class="rid">{escape(requirement["id"])}</span> '
                    f'{escape(requirement["text"])}</th>{"".join(cells)}'
                    f'<td class="{tone}">{escape(requirement["status"])}</td></tr>')
    source = f' from {escape(view["requirements_source"])}' if view["requirements_source"] else ""
    return (f'<p class="quiet">Requirements{source}. ● changes code, <sub>T</sub> includes tests, ○ docs only.</p>'
            f'<div class="scroll" tabindex="0" role="region" aria-label="Requirements across the stack"><table class="grid"><thead><tr><th scope="col">Requirement</th>{head}'
            f'<th scope="col">Coverage</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def _size_bar(item: dict, scale: int, budget: int, color: str) -> str:
    def width(lines: int) -> float:
        return 100 * lines / scale if scale else 0
    other = max(0, item["lines"] - item["mechanical_lines"] - item["behavioral_lines"])
    return (f'<div class="bar" role="img" aria-label="{item["lines"]} lines: {item["behavioral_lines"]} behavioural, '
            f'{item["mechanical_lines"]} mechanical">'
            f'<span style="width:{width(item["behavioral_lines"]):.1f}%;background:{color}"></span>'
            f'<span style="width:{width(item["mechanical_lines"]):.1f}%;background:{color};opacity:.35"></span>'
            f'<span class="unsorted" style="width:{width(other):.1f}%"></span>'
            f'<i class="budget" style="left:{width(budget):.1f}%"></i></div>')


def _slices_table(view: dict) -> str:
    total = len(view["slices"])
    scale = max([s["lines"] for s in view["slices"]] + [view["budget"]])
    rows = []
    for item in view["slices"]:
        color = slice_color(item["number"], total)
        status, tone = _status_text(item)
        note = f'<p class="note">{escape(item["note"])}</p>' if item["note"] else ""
        requirement = f'<span class="rid">{escape(item["requirement"])}</span> ' if item["requirement"] else ""
        rows.append(
            f'<tr><td class="num"><span class="swatch" style="background:{color}"></span>{item["number"]}</td>'
            f'<td>{requirement}<strong>{escape(item["title"])}</strong>{note}'
            f'<code class="branch">{escape(item["branch"] or "")}</code></td>'
            f'<td class="size"><span class="count">{item["lines"]:,}</span>'
            f'{_size_bar(item, scale, view["budget"], color)}</td>'
            f'<td class="num">{item["files"]}</td><td class="{tone}">{status}</td></tr>')
    return (f'<p class="quiet">Solid bar: behavioural lines to read closely. Faded: mechanical lines to skim. '
            f'The tick marks the {view["budget"]:,}-line budget.</p>'
            f'<div class="scroll" tabindex="0" role="region" aria-label="Slices, in review order"><table class="slices"><thead><tr><th scope="col" class="num">#</th>'
            f'<th scope="col">Slice</th><th scope="col">Lines</th><th scope="col" class="num">Files</th>'
            f'<th scope="col">Tests</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def _timeline(view: dict) -> str:
    if not view["timeline"]:
        return ""
    items = "".join(f'<li><time>{int(entry["seconds"] // 60)}:{int(entry["seconds"] % 60):02d}</time>'
                    f'<span>{escape(entry["text"])}</span></li>' for entry in view["timeline"])
    return f'<h2>How it got here</h2><ol class="timeline">{items}</ol>'


def _proof(view: dict) -> str:
    proof = view["proof"]
    checks = [
        ("The last slice's tree equals the original branch's tree", proof["trees_identical"]),
        ("The slices form one straight chain from the base", proof["linear_chain"]),
        ("Every change is in exactly one slice", proof["units_once"]),
    ]
    items = "".join(f'<li class="{"pass" if ok else "fail"}">{"✓" if ok else "✕"} {escape(text)}</li>'
                    for text, ok in checks)
    relation = "equals" if proof["trees_identical"] else "differs from"
    return (f'<h2>Proof that nothing was lost</h2><ul class="checks">{items}</ul>'
            f'<p class="trees">Stack tip tree <code>{escape(proof["stack_tree"][:12])}</code> {relation} original tree '
            f'<code>{escape(proof["head_tree"][:12])}</code>.</p>')


STYLE = """
:root { --ink:#14213d; --paper:#f6f7fb; --panel:#ffffff; --muted:#5b6479; --rule:#d5dae5;
        --pass:#1f7a50; --fail:#b8322a; --warn:#9a6414; --glass:rgba(120,140,190,.16); }
@media (prefers-color-scheme: dark) {
  :root { --ink:#e6e9f2; --paper:#0f1424; --panel:#151c30; --muted:#9aa3b8; --rule:#2a3350;
          --pass:#4cc38a; --fail:#f07469; --warn:#e3a94f; --glass:rgba(160,180,230,.14); } }
* { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink);
       font:16px/1.55 "Avenir Next","Segoe UI","Helvetica Neue",system-ui,sans-serif; }
main { max-width:1080px; margin:0 auto; padding:48px 24px 64px; }
h1 { font-size:2.2rem; line-height:1.15; letter-spacing:-.015em; margin:0 0 14px; max-width:28ch; }
h2 { font-size:1.3rem; margin:52px 0 12px; }
p { max-width:72ch; }
.verdict { font-size:1.15rem; margin:0 0 6px; }
.summary { color:var(--muted); }
.quiet { color:var(--muted); font-size:.95rem; }
.prism { width:100%; height:auto; margin:28px 0 8px; }
.beam { fill:var(--ink); opacity:.9; }
.beam-label { font-size:18px; font-weight:700; fill:var(--ink); }
.beam-sub, .ray-sub { font-size:13px; fill:var(--muted); }
.ray-title { font-size:15px; fill:var(--ink); }
.ray-number { font-weight:800; }
.glass { fill:var(--glass); stroke:var(--ink); stroke-width:2; }
.prism-compact, .ray-list { display:none; }
.prism-compact { width:100%; height:auto; margin:24px 0 4px; }
.ray-number-compact { font-size:30px; font-weight:800; fill:var(--ink); }
.ray-list { list-style:none; padding:0; margin:0 0 8px; }
.ray-list li { display:flex; gap:12px; align-items:baseline; padding:8px 0; border-bottom:1px solid var(--rule); }
.ray-list .dot { flex:none; width:12px; height:12px; border-radius:50%; transform:translateY(1px); }
.ray-list .quiet { font-size:.92rem; }
@media (max-width:640px) { .prism { display:none; } .prism-compact { display:block; } .ray-list { display:block; } }
tspan.pass { fill:var(--pass); } tspan.warn { fill:var(--warn); } tspan.fail { fill:var(--fail); }
.pass { color:var(--pass); } .warn { color:var(--warn); } .fail { color:var(--fail); } .muted { color:var(--muted); }
.look { padding-left:1.2em; margin:0; } .look li { margin:6px 0; max-width:72ch; }
.scroll { overflow-x:auto; }
.scroll:focus-visible { outline:3px solid var(--ink); outline-offset:2px; }
table { border-collapse:collapse; width:100%; background:var(--panel); }
th, td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--rule); vertical-align:top; }
thead th { font-size:.9rem; color:var(--muted); font-weight:600; }
.num { text-align:center; font-variant-numeric:tabular-nums; }
.grid thead th.num { border-top:4px solid; }
.grid td.num { font-size:1.1rem; }
.grid td.docs { color:var(--muted); }
.grid sub { font-size:.65rem; font-weight:700; }
.rid { font-weight:700; margin-right:.3em; }
.grid tbody th { font-weight:400; }
.slices td.num { white-space:nowrap; }
.swatch { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:8px; }
.note { margin:4px 0 0; color:var(--muted); font-size:.95rem; }
.branch { display:block; margin-top:4px; font-size:.8rem; color:var(--muted); }
.size { min-width:200px; }
.count { font-variant-numeric:tabular-nums; font-weight:600; }
.bar { position:relative; display:flex; height:10px; margin-top:6px; background:var(--rule); border-radius:5px; }
.bar span { display:block; height:100%; }
.bar span:first-child { border-radius:5px 0 0 5px; }
.bar .unsorted { background:var(--muted); opacity:.35; }
.bar .budget { position:absolute; top:-4px; bottom:-4px; width:2px; background:var(--ink); }
.timeline { list-style:none; padding:0; margin:0; border-left:2px solid var(--rule); }
.timeline li { display:flex; gap:16px; padding:6px 0 6px 16px; }
.timeline time { color:var(--muted); font-variant-numeric:tabular-nums; min-width:3.5em; }
.checks { list-style:none; padding:0; } .checks li { margin:6px 0; font-weight:600; }
.trees { color:var(--muted); }
code { font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace; font-size:.9em; }
footer { margin-top:56px; padding-top:16px; border-top:1px solid var(--rule); color:var(--muted); font-size:.9rem; }
@media (max-width:640px) { main { padding:32px 16px; } h1 { font-size:1.7rem; } .size { min-width:140px; } }
"""


def render_report(view: dict) -> str:
    slices = view["slices"]
    largest = max((s["lines"] for s in slices), default=0)
    green = all(s["status"] == "pass" for s in slices)
    verdict = (f"{view['original']['lines']:,} lines became {_plural(len(slices), 'slice')}. The largest is "
               f"{largest:,} lines. " + ("Every slice passes its tests on its own. " if green else
                                         "Some slices do not pass yet. ")
               + ("The stack adds up to exactly the original branch." if view["proof"]["zero_drift"] else
                  "The stack does not match the original branch yet."))
    facts = (f"{_plural(view['original']['files'], 'file')} changed. Split in {view['elapsed_minutes']:g} minutes "
             f"with {_plural(view['repairs'], 'repair')} and {_plural(view['verify_runs'], 'verify run')}.")
    summary = f'<p class="summary">{escape(view["summary"])}</p>' if view["summary"] else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(view["title"])}</title>
<style>{STYLE}</style></head>
<body><main>
<h1>{escape(view["title"])}</h1>
<p class="verdict">{escape(verdict)}</p>
<p class="quiet">{escape(facts)}</p>
{summary}
{_prism_svg(view)}
{_prism_compact_svg(view)}
{_ray_list(view)}
<h2>Worth a look</h2>
{_worth_a_look(view)}
<h2>Requirements across the stack</h2>
{_requirements_table(view)}
<h2>Slices, in review order</h2>
{_slices_table(view)}
{_timeline(view)}
{_proof(view)}
<footer>Every number here comes from git, computed by the Diffract engine; the notes come from Bob.
Verified with <code>{escape(view["verify_command"] or "no command")}</code>. Generated {escape(view["generated_at"])}.</footer>
</main></body></html>
"""
