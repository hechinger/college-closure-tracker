"""
Generate the WordPress-ready landing page HTML.

Reads:  data/derived/fsa_closure_publication_table_<latest>.csv
Writes: docs/college_closure_tracker.html

Usage:  python scripts/generate_landing_page.py
"""
from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
DERIVED  = PROJECT / "data" / "derived"
OUT_PATH = PROJECT / "docs" / "college_closure_tracker.html"

MONTH_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

# US tile-grid positions: (state, row, col) — 1-indexed, 11 columns wide
TILE_GRID = [
    ("AK",1,1), ("ME",1,11),
    ("WA",2,2), ("MT",2,3), ("ND",2,4), ("MN",2,5), ("VT",2,10), ("NH",2,11),
    ("OR",3,2), ("ID",3,3), ("SD",3,4), ("WI",3,5), ("MI",3,6),
    ("NY",3,8), ("MA",3,9), ("RI",3,10),
    ("CA",4,2), ("NV",4,3), ("WY",4,4), ("IA",4,5), ("IL",4,6), ("IN",4,7),
    ("OH",4,8), ("PA",4,9), ("NJ",4,10), ("CT",4,11),
    ("UT",5,3), ("CO",5,4), ("NE",5,5), ("MO",5,6), ("KY",5,7),
    ("WV",5,8), ("VA",5,9), ("MD",5,10), ("DE",5,11),
    ("AZ",6,3), ("NM",6,4), ("KS",6,5), ("AR",6,6), ("TN",6,7),
    ("NC",6,8), ("SC",6,9), ("DC",6,10),
    ("TX",7,4), ("OK",7,5), ("LA",7,6), ("MS",7,7), ("AL",7,8), ("GA",7,9),
    ("HI",8,2), ("FL",8,9),
    ("PR",9,5),
]


def _latest(pattern: str) -> Path | None:
    files = sorted(DERIVED.glob(pattern), reverse=True)
    return files[0] if files else None


def _color(n: int, mx: int) -> tuple[str, str]:
    """Return (bg_hex, text_hex) for a closure count on a blue scale."""
    if n == 0:
        return "#e0e0e0", "#666"
    t = n / mx
    if t < 0.1:
        return "#c6dbef", "#333"
    elif t < 0.25:
        return "#6baed6", "#fff"
    elif t < 0.5:
        return "#2171b5", "#fff"
    elif t < 0.75:
        return "#084594", "#fff"
    else:
        return "#08306b", "#fff"


def build(pub_csv: Path) -> str:
    rows = list(csv.DictReader(pub_csv.open(encoding="utf-8")))
    today_str = date.today().strftime("%B %-d, %Y") if sys.platform != "win32" \
        else date.today().strftime("%B %d, %Y").replace(" 0", " ")

    # --- Time series data ---
    monthly_raw: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        d = r.get("closed_date_iso", "")
        if len(d) >= 7:
            ctrl = r.get("control_label", "") or "other"
            if ctrl not in ("for-profit", "nonprofit", "public"):
                ctrl = "other"
            monthly_raw[d[:7]][ctrl] += 1

    years = sorted({ym[:4] for ym in monthly_raw})
    year_totals: dict[str, Counter] = defaultdict(Counter)
    for ym, c in monthly_raw.items():
        year_totals[ym[:4]].update(c)

    annual = {
        "labels": years,
        "fp":  [year_totals[y].get("for-profit", 0) for y in years],
        "np":  [year_totals[y].get("nonprofit",  0) for y in years],
        "pub": [year_totals[y].get("public",     0) for y in years],
    }
    monthly: dict[str, dict] = {}
    for yr in years:
        monthly[yr] = {
            "labels": MONTH_NAMES,
            "fp":  [monthly_raw.get(f"{yr}-{m:02d}", Counter()).get("for-profit", 0) for m in range(1, 13)],
            "np":  [monthly_raw.get(f"{yr}-{m:02d}", Counter()).get("nonprofit",  0) for m in range(1, 13)],
            "pub": [monthly_raw.get(f"{yr}-{m:02d}", Counter()).get("public",     0) for m in range(1, 13)],
        }

    # --- State counts ---
    state_counts: Counter = Counter(r.get("state", "") for r in rows if r.get("state"))
    max_count = max(state_counts.values(), default=1)

    # --- Names by period (for chart tooltip) ---
    names_annual: dict[str, list[str]] = defaultdict(list)
    names_monthly: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        d = r.get("closed_date_iso", "")
        name = r.get("school_name", "")
        if name and len(d) >= 4:
            names_annual[d[:4]].append(name)
        if name and len(d) >= 7:
            names_monthly[d[:7]].append(name)

    # --- Table data ---
    table_rows = []
    for r in sorted(rows, key=lambda x: x.get("closed_date_iso", ""), reverse=True):
        table_rows.append([
            r.get("closed_date_iso", ""),
            r.get("school_name", ""),
            (r.get("city", "") + ", " + r.get("state", "")).strip(", "),
            r.get("control_label", "") or "—",
            r.get("iclevel_label", "") or "—",
            r.get("enrollment_band", "") or "—",
            "1" if r.get("review_flag", "") else "",
        ])

    # --- Tile map HTML ---
    tile_rows = max(r for _, r, _ in TILE_GRID)
    tile_cols = max(c for _, _, c in TILE_GRID)
    grid_cells: dict[tuple[int,int], str] = {}
    for st, row, col in TILE_GRID:
        n = state_counts.get(st, 0)
        bg, fg = _color(n, max_count)
        grid_cells[(row, col)] = (
            f'<div class="tile" style="background:{bg};color:{fg};'
            f'grid-row:{row};grid-column:{col};" '
            f'title="{st}: {n} closure{"s" if n != 1 else ""}">'
            f'{st}</div>'
        )
    tile_html = "\n".join(v for v in grid_cells.values())

    # --- Embed data ---
    js_data = json.dumps(
        {
            "annual": annual,
            "monthly": monthly,
            "table": table_rows,
            "names_annual":  dict(names_annual),
            "names_monthly": dict(names_monthly),
        },
        separators=(",", ":"), ensure_ascii=False
    )

    # --- Stat blurbs ---
    total = len(rows)
    by_level = Counter(r.get("iclevel_label", "") for r in rows)
    by_ctrl  = Counter(r.get("control_label", "") for r in rows)
    n_4yr   = by_level.get("4yr+", 0)
    n_2yr   = by_level.get("2-4yr", 0)
    n_cert  = by_level.get("<2yr", 0)
    n_fp    = by_ctrl.get("for-profit", 0)
    n_np    = by_ctrl.get("nonprofit", 0)
    n_pub   = by_ctrl.get("public", 0)

    _SPEC = ("seminary","theolog","bible","divinity","rabbinical","talmudic","yeshiva",
             "art instit","arts instit","music","culinary","acupuncture","chiropractic",
             "massage","cosmetol","barber","esthetics","nail tech","court report",
             "aviation","fashion","design school","language","english as")
    n_spec = sum(1 for r in rows if any(k in r.get("school_name","").lower() for k in _SPEC))

    top_state = state_counts.most_common(1)[0] if state_counts else ("N/A", 0)
    year_peak = max(years, key=lambda y: sum(year_totals[y].values()), default="2016")
    n_peak    = sum(year_totals[year_peak].values())

    # ===========================================================================
    # HTML
    # ===========================================================================
    return f"""<style>
/* ---- Wrapper ---- */
.cct-wrap {{ font-family: Georgia, serif; color: #222; max-width: 900px; margin: 0 auto; }}
.cct-wrap h2 {{ font-size: 1.9em; line-height: 1.25; margin-bottom: .5em; }}
.cct-wrap h3 {{ font-size: 1.3em; border-bottom: 2px solid #222; padding-bottom: .3em; margin: 2em 0 .6em; }}
.cct-wrap p  {{ line-height: 1.65; margin: .8em 0; }}

/* ---- Table ---- */
#cct-search {{ width: 100%; padding: 8px 10px; font-size: 14px; border: 1px solid #bbb;
               border-radius: 3px; margin-bottom: 8px; box-sizing: border-box; }}
#cct-ctrl-btns {{ margin-bottom: 10px; }}
#cct-ctrl-btns button {{
  margin: 0 4px 4px 0; padding: 4px 10px; border: 1px solid #aaa;
  background: #f5f5f5; cursor: pointer; border-radius: 3px; font-size: 13px; }}
#cct-ctrl-btns button.active {{ background: #1a3f6f; color: #fff; border-color: #1a3f6f; }}
#cct-table-wrap {{ overflow-x: auto; }}
#cct-table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
#cct-table th {{ background: #1a3f6f; color: #fff; padding: 7px 8px; text-align: left;
                 cursor: pointer; white-space: nowrap; user-select: none; }}
#cct-table th:after {{ content: ' ⇅'; opacity: .5; }}
#cct-table th.asc:after {{ content: ' ↑'; opacity:1; }}
#cct-table th.desc:after {{ content: ' ↓'; opacity:1; }}
#cct-table td {{ padding: 6px 8px; border-bottom: 1px solid #eee; vertical-align: top; }}
#cct-table tr:nth-child(even) {{ background: #f9f9f9; }}
#cct-table tr:hover {{ background: #eef4ff; }}
.flag-warn {{ color: #c0392b; font-weight: bold; cursor: help; }}
#cct-pagination {{ margin-top: 8px; display: flex; gap: 6px; align-items: center;
                   flex-wrap: wrap; font-size: 13px; }}
#cct-pagination button {{ padding: 3px 9px; border: 1px solid #aaa; background:#fff;
                          cursor:pointer; border-radius:2px; }}
#cct-pagination button.active {{ background:#1a3f6f; color:#fff; border-color:#1a3f6f; }}
#cct-pagination span {{ color:#555; }}

/* ---- Charts ---- */
#cct-year-btns {{ margin-bottom: 10px; display: flex; flex-wrap: wrap; gap: 4px; }}
#cct-year-btns button {{
  padding: 4px 10px; border: 1px solid #aaa; background:#f5f5f5;
  cursor:pointer; border-radius:3px; font-size:13px; }}
#cct-year-btns button.active {{ background:#1a3f6f; color:#fff; border-color:#1a3f6f; }}
#cct-chart-wrap {{ position:relative; max-width:860px; height:320px; }}

/* ---- Tile map ---- */
#cct-tile-grid {{
  display: grid;
  grid-template-columns: repeat({tile_cols}, 44px);
  grid-template-rows: repeat({tile_rows}, 44px);
  gap: 3px;
  margin: 1em 0;
}}
.tile {{
  display: flex; align-items: center; justify-content: center;
  font-size: 10px; font-weight: bold; border-radius: 3px;
  text-align: center; line-height: 1.2;
  cursor: default;
}}
#cct-legend {{ display:flex; gap:6px; align-items:center; font-size:12px; margin-top:6px; }}
#cct-legend span {{ display:inline-block; width:18px; height:18px; border-radius:2px; }}

/* ---- Methodology ---- */
#cct-methodology {{ background:#f7f7f7; padding:16px 20px; border-left:4px solid #1a3f6f;
                    margin-top:2em; font-size:.92em; line-height:1.6; }}
#cct-methodology h3 {{ font-size:1.1em; border:none; margin-top:0; }}
#cct-methodology ol {{ margin:.5em 0 0 1.2em; padding:0; }}
#cct-methodology li {{ margin-bottom:.4em; }}
</style>

<div class="cct-wrap">

<h2>At Least {total:,} Colleges Have Closed Since 2015. Here&#8217;s the Data.</h2>

<p>Since 2015, at least {total:,} colleges and universities across the United States have permanently
closed their doors, forcing hundreds of thousands of students to scramble for new programs or
abandon their education entirely. This database tracks full institutional closures&#8202;&#8212;&#8202;defined
as the last or only campus of a college system shutting down&#8202;&#8212;&#8202;using federal financial aid
records matched to federal enrollment data. The peak came in {year_peak}, when {n_peak} institutions
closed in a single year, driven largely by the collapse of large for-profit chains.</p>

<p>Of those {total:,} closures, <strong>{n_4yr:,} institutions primarily awarded bachelor&#8217;s degrees
or higher</strong>, including liberal arts colleges, regional universities, and professional schools
that left students midway through four-year programs. <strong>Another {n_2yr:,} primarily awarded
associate degrees</strong>, the two-year credentials that community college students rely on for
workforce training and transfer pathways. The largest single category&#8202;&#8212;&#8202;<strong>{n_cert:,}
institutions&#8202;&#8212;&#8202;primarily awarded certificates and other credentials lasting less than two
years</strong>, most of them vocational programs in fields like healthcare, technology, and the trades.
At least {n_spec:,} were <strong>specialized institutions</strong>, including cosmetology schools,
culinary institutes, massage therapy programs, and theological seminaries.</p>

<p>By far the most common type of closure has been for-profit colleges: {n_fp:,} of the {total:,}
closures&#8202;&#8212;&#8202;roughly {round(n_fp/total*100)}&#8201;percent&#8202;&#8212;&#8202;involved for-profit
institutions. That includes large chains like ITT Technical Institute and Corinthian Colleges, as
well as thousands of smaller vocational schools. Among nonprofit and public institutions, the picture
is more varied: {n_np:,} nonprofit colleges closed, as did {n_pub:,} public institutions.
{top_state[0]} recorded the most closures of any state or territory, with {top_state[1]}.</p>

<p><em>Note: This database covers institutional closures reported to federal financial aid authorities
from January&#160;2015 through the most recent data download ({today_str}). Closures before 2015 are not
included because consistent federal enrollment data for matching is only available from that year
onward. The full federal list extends back to the 1980s. Figures shown reflect main-campus or
only-campus closures (&#8220;Approach&#160;2&#8221; in SHEEO methodology); branches of still-operating
systems are excluded from totals but are available in the underlying data.</em></p>

<!-- ============================================================ TABLE -->
<h3>Search the Database</h3>

<input id="cct-search" type="text" placeholder="Search by school name, city, or state&hellip;" />
<div id="cct-ctrl-btns">
  <button class="active" data-ctrl="">All</button>
  <button data-ctrl="for-profit">For-profit</button>
  <button data-ctrl="nonprofit">Nonprofit</button>
  <button data-ctrl="public">Public</button>
</div>
<div id="cct-table-wrap">
  <table id="cct-table">
    <thead>
      <tr>
        <th data-col="0">Close date</th>
        <th data-col="1">Institution</th>
        <th data-col="2">Location</th>
        <th data-col="3">Control</th>
        <th data-col="4">Level</th>
        <th data-col="5">Enrollment</th>
        <th data-col="6">Flag</th>
      </tr>
    </thead>
    <tbody id="cct-tbody"></tbody>
  </table>
</div>
<div id="cct-pagination"></div>

<!-- ============================================================ TIME SERIES -->
<h3>Closures Over Time by Type of Institution</h3>

<p>The chart below tracks college closures by year and by the type of institution. Click a year
button to zoom into the monthly breakdown for that year. The sharp spike in 2016 reflects the
collapse of ITT Technical Institute and other large for-profit chains following federal enforcement
actions. The trend has moderated since 2020, though for-profit closures continue and a small but
growing number of nonprofit institutions have begun to close as enrollment pressure
intensifies.</p>

<div id="cct-year-btns">
  <button class="active" data-yr="all">All years</button>
  {''.join(f'<button data-yr="{y}">{y}</button>' for y in years)}
</div>
<div id="cct-chart-wrap">
  <canvas id="cct-ts-chart"></canvas>
</div>

<!-- ============================================================ STATE MAP -->
<h3>Closures by State (2015&#8211;Present)</h3>

<p>The tile map below shows the total number of institutional closures in each state since 2015.
California leads with {state_counts.get("CA",0)} closures, reflecting both its large higher-education sector and
the concentration of for-profit chains in the state. New York ({state_counts.get("NY",0)}),
Florida ({state_counts.get("FL",0)}), Pennsylvania ({state_counts.get("PA",0)}), and
Illinois ({state_counts.get("IL",0)}) follow. Puerto Rico ({state_counts.get("PR",0)}) is included in
the federal data and shown at bottom. States with no color had no closures in this dataset.</p>

<div id="cct-tile-grid">
{tile_html}
</div>
<div id="cct-legend">
  <span style="background:#e0e0e0"></span>0&nbsp;&nbsp;
  <span style="background:#c6dbef"></span>1&#8211;12&nbsp;&nbsp;
  <span style="background:#6baed6"></span>13&#8211;31&nbsp;&nbsp;
  <span style="background:#2171b5"></span>32&#8211;63&nbsp;&nbsp;
  <span style="background:#084594"></span>64&#8211;95&nbsp;&nbsp;
  <span style="background:#08306b"></span>96+
</div>

<!-- ============================================================ METHODOLOGY -->
<div id="cct-methodology">
<h3>Methodology</h3>
<ol>
  <li><strong>Source:</strong> U.S. Department of Education Federal Student Aid Closed School Search
  (weekly file, as of {today_str}). The list includes institutions that withdrew from federal
  financial aid programs, which in most but not all cases corresponds to permanent closure.</li>
  <li><strong>Counting method:</strong> This database uses &#8220;Approach&#160;2&#8221; from the SHEEO methodology
  (Burns 2024, &#8220;Measuring College Closures with Federal Education Data&#8221;): only the last or only
  campus of a system is counted, so branch-campus closures where the parent institution remains open
  are excluded. This produces the most conservative and defensible count of full institutional
  closures. The underlying data contains all closures; the full dataset is available on request.</li>
  <li><strong>Enrollment data:</strong> Headcount enrollment figures come from the federal IPEDS
  year-cache (2014&#8211;2025) and reflect the last year of data available before the reported closure
  date. Enrollment bands are shown rather than raw numbers to account for data-year lag. Institutions
  that closed before 2015, or for which no IPEDS match could be made, show no enrollment figure.</li>
  <li><strong>Not inferred:</strong> This tracker does not infer closure from a dead website,
  IPEDS disappearance, removal from federal monitoring lists, or Title&#160;IV withdrawal alone.
  Closure dates come from the federal FSA list only.</li>
  <li><strong>Flags:</strong> Records marked &#8220;&#9888;&#8221; in the table are flagged for editorial
  review&#8202;&#8212;&#8202;either because the location name matches patterns the SHEEO methodology identifies
  as possible sub-unit or administrative records (not standalone campuses), or because no federal
  enrollment record could be matched. These records are included in counts but should be
  verified before citing individually.</li>
  <li><strong>Methodology version:</strong> 1.0. Changes to inclusion criteria will be versioned
  and noted here.</li>
</ol>
</div>

</div><!-- .cct-wrap -->

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"
        crossorigin="anonymous" referrerpolicy="no-referrer"></script>
<script>
(function(){{
const D = {js_data};

/* ==================== TABLE ==================== */
const PER_PAGE = 25;
let filtered = D.table.slice();
let sortCol = 0, sortDir = -1, page = 0, ctrlFilter = "";

function applyFilters() {{
  const q = (document.getElementById("cct-search").value || "").toLowerCase();
  filtered = D.table.filter(r => {{
    if (ctrlFilter && r[3] !== ctrlFilter) return false;
    if (!q) return true;
    return (r[1]+r[2]).toLowerCase().includes(q);
  }});
  filtered.sort((a,b) => {{
    const av = a[sortCol]||"", bv = b[sortCol]||"";
    return av < bv ? sortDir : av > bv ? -sortDir : 0;
  }});
  page = 0;
  render();
}}

function lvlLabel(v) {{
  return v === "4yr+" ? "Bachelor&#x2019;s+" : v === "2-4yr" ? "Associate" : v === "<2yr" ? "Certificate" : v||"&#x2014;";
}}
function ctrlLabel(v) {{
  return v === "for-profit" ? "For-profit" : v === "nonprofit" ? "Nonprofit" : v === "public" ? "Public" : v||"&#x2014;";
}}

function render() {{
  const tbody = document.getElementById("cct-tbody");
  const start = page * PER_PAGE;
  const slice = filtered.slice(start, start + PER_PAGE);
  tbody.innerHTML = slice.map(r =>
    `<tr>
      <td>${{r[0]}}</td>
      <td><strong>${{r[1]}}</strong></td>
      <td>${{r[2]}}</td>
      <td>${{ctrlLabel(r[3])}}</td>
      <td>${{lvlLabel(r[4])}}</td>
      <td>${{r[5]||"&#x2014;"}}</td>
      <td>${{r[6] ? '<span class="flag-warn" title="Flagged for editorial review">&#9888;</span>' : ""}}</td>
    </tr>`
  ).join("");

  const total_pages = Math.ceil(filtered.length / PER_PAGE);
  const pg = document.getElementById("cct-pagination");
  let html = `<span>Showing ${{start+1}}&#x2013;${{Math.min(start+PER_PAGE, filtered.length)}} of ${{filtered.length.toLocaleString()}} results</span>`;
  if (page > 0) html += `<button onclick="goPage(${{page-1}})">&#8592; Prev</button>`;
  for (let i = Math.max(0, page-2); i <= Math.min(total_pages-1, page+2); i++) {{
    html += `<button class="${{i===page?"active":""}}" onclick="goPage(${{i}})">${{i+1}}</button>`;
  }}
  if (page < total_pages-1) html += `<button onclick="goPage(${{page+1}})">Next &#8594;</button>`;
  pg.innerHTML = html;
}}

window.goPage = p => {{ page = p; render(); document.getElementById("cct-table-wrap").scrollIntoView({{behavior:"smooth"}}); }};

document.getElementById("cct-search").addEventListener("input", applyFilters);

document.getElementById("cct-ctrl-btns").addEventListener("click", e => {{
  if (e.target.tagName !== "BUTTON") return;
  ctrlFilter = e.target.dataset.ctrl;
  document.querySelectorAll("#cct-ctrl-btns button").forEach(b => b.classList.remove("active"));
  e.target.classList.add("active");
  applyFilters();
}});

document.getElementById("cct-table").querySelector("thead").addEventListener("click", e => {{
  const th = e.target.closest("th");
  if (!th) return;
  const col = +th.dataset.col;
  if (col === sortCol) sortDir *= -1; else {{ sortCol = col; sortDir = -1; }}
  document.querySelectorAll("#cct-table th").forEach(t => t.classList.remove("asc","desc"));
  th.classList.add(sortDir === 1 ? "asc" : "desc");
  applyFilters();
}});

applyFilters();

/* ==================== CHART ==================== */
const COLORS = {{
  fp:  {{ line:"#c0392b", bg:"rgba(192,57,43,0.15)"  }},
  np:  {{ line:"#2980b9", bg:"rgba(41,128,185,0.15)" }},
  pub: {{ line:"#27ae60", bg:"rgba(39,174,96,0.15)"  }},
}};

function makeDatasets(d) {{
  return [
    {{ label:"For-profit", data:d.fp, borderColor:COLORS.fp.line, backgroundColor:COLORS.fp.bg, tension:.3, fill:true }},
    {{ label:"Nonprofit",  data:d.np, borderColor:COLORS.np.line, backgroundColor:COLORS.np.bg, tension:.3, fill:true }},
    {{ label:"Public",     data:d.pub,borderColor:COLORS.pub.line,backgroundColor:COLORS.pub.bg,tension:.3, fill:true }},
  ];
}}

const ctx = document.getElementById("cct-ts-chart").getContext("2d");
// Current view: "all" = annual, or a year string = monthly
let currentView = "all";

function namesForLabel(label) {{
  // label is a year ("2016") in annual view, or month name ("Jan") in monthly view
  if (currentView === "all") {{
    return D.names_annual[label] || [];
  }} else {{
    const mi = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"].indexOf(label) + 1;
    if (!mi) return [];
    const key = `${{currentView}}-${{String(mi).padStart(2,"0")}}`;
    return D.names_monthly[key] || [];
  }}
}}

const TOOLTIP_MAX = 12;

let chart = new Chart(ctx, {{
  type: "bar",
  data: {{ labels: D.annual.labels, datasets: makeDatasets(D.annual) }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    interaction: {{ mode:"index", intersect:false }},
    plugins: {{
      legend: {{ position:"top" }},
      tooltip: {{
        callbacks: {{
          afterBody: function(items) {{
            if (!items.length) return [];
            const label = items[0].label;
            const names = namesForLabel(label);
            if (!names.length) return [];
            const lines = ["", "Institutions:"];
            const show = names.slice(0, TOOLTIP_MAX);
            show.forEach(n => lines.push("  " + n));
            if (names.length > TOOLTIP_MAX) lines.push(`  … and ${{names.length - TOOLTIP_MAX}} more`);
            return lines;
          }}
        }}
      }}
    }},
    scales: {{
      x: {{ stacked: true }},
      y: {{ stacked: true, beginAtZero: true, title: {{ display:true, text:"Closures" }} }}
    }}
  }}
}});

document.getElementById("cct-year-btns").addEventListener("click", e => {{
  if (e.target.tagName !== "BUTTON") return;
  const yr = e.target.dataset.yr;
  document.querySelectorAll("#cct-year-btns button").forEach(b => b.classList.remove("active"));
  e.target.classList.add("active");
  currentView = yr;
  if (yr === "all") {{
    chart.data.labels = D.annual.labels;
    chart.data.datasets = makeDatasets(D.annual);
    chart.options.scales.x.stacked = true;
    chart.options.scales.y.stacked = true;
  }} else {{
    const m = D.monthly[yr];
    chart.data.labels = m.labels;
    chart.data.datasets = makeDatasets(m);
  }}
  chart.update();
}});

}})();
</script>
"""


def main():
    pub_csv = _latest("fsa_closure_publication_table_*.csv")
    if pub_csv is None:
        print("ERROR: no publication table CSV found", file=sys.stderr)
        return 1
    print(f"Building page from {pub_csv.name} ...")
    html = build(pub_csv)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"Written: {OUT_PATH}  ({len(html):,} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
