# College Closure Tracker

Data pipeline behind The Hechinger Report's
[Counting college closures](https://hechingerreport.org/college-closures/)
tracker. It turns the U.S. Department of Education's weekly closed-school data
into a reviewed list of college closures, published to a Google Sheet that
feeds the tracker's tables and Datawrapper charts.

- **Weekly:** `.github/workflows/weekly-refresh.yml` downloads the FSA files,
  rebuilds the IPEDS match, classifies every closure and refreshes the Sheet.
  New closures go to editors in the Sheet's `overrides` tab.
- **On demand:** `.github/workflows/publish-editorial-overrides.yml` pulls
  editors' decisions from the `overrides` tab and republishes `published`,
  `by_year` and `by_state`.

# How we count college closures

*Methods note for editors and readers. Figures are from the 2026-11-11 dry run;
the pipeline reruns weekly and every count below is regenerated from source data.
The published table covers closures from 1996 onward.*

## In one sentence

A college counts as closed when the federal record for its **main campus**
closes **and** there is structured evidence that the **institution** itself
stopped operating, not just that one location or federal identifier ended.

## Sources

| Source | What we use it for |
|---|---|
| FSA closed-school file (weekly) | Every closed Title IV location, with its close date. This is where closures come from. |
| FSA Weekly School File (RT01, RT06, RT11) | Whether other locations of the same school were still listed as open; dated identifier changes; who holds student records |
| IPEDS directory files, 1995–2025 | Matching each closure to a real institution; sector, level, highest degree, enrollment; whether the institution kept reporting after the closure; IPEDS "combined with another institution" status |
| Financial Health tracker (one-time snapshot, base table only) | Editor-confirmed closure announcements for recent closures that IPEDS cannot test yet |
| Editors (overrides tab) | Final say on any case, with a documented basis and source URL |

## The rule

**Institution closure.** All of the following:

1. FSA closed the main-campus record (OPEID ending in `00`).
2. The record matches a single historical IPEDS institution: a public or
   nonprofit college of any level except public under-two-year (see below),
   or a for-profit of any level.
3. The institution stopped operating, shown by **one** of these:
   - no other location of the school was open in FSA's records on the close date;
   - no institution under the same OPEID6 reported to IPEDS as active after
     the closure year (FSA keeps stale "open" rows for schools that are gone,
     such as ITT Tech, Brightwood and Bethany);
   - for the frozen historical base table only, an editor-confirmed closure in
     the Financial Health tracker.
4. No sign it was really a merger. If IPEDS marks the school as combined with
   another institution, the case goes to editors.
5. It was not later reinstated, and no exact replacement identifier exists.

**Campus closure.** A non-main location with **its own IPEDS record** (its own
OPEID8 in IPEDS, not just the parent's) closed
while the school kept operating: another location was open on the date, or
the school kept reporting to IPEDS as active. If the whole chain shut down
instead (main campus closed within a year and the school disappeared from
IPEDS), the location is counted once, through the institution closure, and
is **not** a separate campus closure.

**Excluded automatically, with the reason recorded and open to editor override:**

| Excluded | Why |
|---|---|
| Locations with no IPEDS record of their own, including those matched only through the parent college | They cannot be verified as campuses. Most are extension or teaching sites, and at least 1,474 are K-12 dual-enrollment or cohort sites, such as "Viterbo University – Portage High School". |
| Locations of chains that shut down entirely | Already counted once, through the institution closure |
| Foreign locations | Out of scope |
| Exact FSA identifier replacements | Operations continued under the new ID |
| Locations IPEDS marks as combined into another institution | The location likely continued |
| Public under-two-year programs (IPEDS sector 7), plus district programs IPEDS codes as public 2-year, by editorial override | Overwhelmingly adult programs run by K-12 districts, BOCES and career-tech centers, some housed in high schools; not colleges. Kelchen et al. note the same pattern but count them. |
| IPEDS administrative units | Not instructional institutions |

**Never counted on their own:** location-name patterns, OPEID suffixes, RT11
record custodians, or Title IV withdrawal by itself.

**Human review.** Ambiguous cases go to editors: possible mergers, closures
with no usable IPEDS match, and schools that kept reporting after their main
campus closed (about 280 rows). **Every closure that appears after the base
table was frozen also goes to an editor**, even if it meets the rule. A
suggested exclusion is attached where one applies.

## How this compares with other counts

| | SHEEO method | Kelchen et al. (Fed, 2025) | Ours |
|---|---|---|---|
| A closure means | every location under the OPEID6 closed | main campus closed (first closure) | main campus closed **and** evidence the institution stopped |
| Uses IPEDS to | — | link closures to institutions and add characteristics | link closures **and** test whether the school kept operating |
| Stale "open" FSA rows | school silently dropped | ignored | resolved with IPEDS |
| Mergers | counted as closures | counted (paper says it can't track them yet) | flagged or excluded |
| District career-tech programs | counted | counted | excluded |

**Kelchen**, main campuses, Jan 1996 – Nov 2023: his paper reports 1,661–1,671 closures; we count **1,751**.

| Sector | Kelchen | Ours |
|---|---|---|
| For-profit 2-year | 1,222 | 1,305 |
| For-profit 4-year | 100 | 105 |
| Nonprofit 4-year | 142 | 150 |
| Nonprofit 2-year | 152 | 178 |
| Public 4-year | 2 | 2 |
| Public 2-year | 45 | 11 |

Our "2-year" includes under-two-year schools, as Kelchen's does. The public
2-year gap is the district career-tech programs we exclude.

**SHEEO method**, rerun on current FSA data by
`scripts/benchmark_sheeo_method.py`, 2008–2023:

- The method gives **848** closures. We count **1,038**, and **816** are on both lists.
- It reproduces the earlier Hechinger figure exactly: **287** institutions offering an associate degree or higher, 62% for-profit. Our count is **417**, 66% for-profit.
- **222 we count that SHEEO doesn't:**
  - 220: one location still listed as open in FSA's records, which makes SHEEO drop the school (Presentation College, Brightwood, Bramson ORT);
  - 2: gaps in location numbering.
- **32 SHEEO counts that we don't:**
  - 12 are held for our editors because the IPEDS match is unresolved;
  - 11 are district career-tech programs or administrative offices;
  - 3 are FSA satellite IDs that belong to colleges still open, Olivet Nazarene and Lindenwood among them;
  - 3 we exclude on purpose (Watkins, a merger; Episcopal Divinity, an affiliation; one reinstatement);
  - 2 are possible mergers, including Mills College;
  - 1 has no main-campus record.

**SHEEO / Hechinger "Proof Points" (2004–2021):** 861 institutions and 9,499
campuses. We count 1,083 institutions and 594 campuses. SHEEO counted every closed
location, including sites with no IPEDS record of their own, which we exclude, and
about 500 merger and consolidation "campuses".

## Limitations

1. **Disappearing from IPEDS is evidence, not proof.** A school absorbed by
   another can simply vanish from IPEDS, as University of the Rockies did when
   it was absorbed into Ashford. The "combined" status catches some mergers, not all.
2. **Recent years lag.** IPEDS runs a year or more behind, so 2025–26 closures
   rely on editor confirmation.
3. **Our thresholds are judgment calls**: "main campus closed within a year,"
   and "never reports after the closure year." Different windows would shift
   counts slightly.
4. **Campus counts cover IPEDS-reported campuses only.** Extension and
   teaching sites without their own IPEDS record are left out, so our campus
   count is lower than SHEEO's location count.
5. **District career-tech programs are excluded**, unlike SHEEO and Kelchen.
   This explains most of the public 2-year gap with Kelchen.
6. **"Associate degree or higher"** uses IPEDS highest degree offered in the
   last year before closure. SHEEO used FSA program length, so the two
   definitions differ slightly.
7. **This is our method, not a published standard.** We publish the rule and
   the reconciliation so it can be checked.

## Reproducing it

```text
python scripts/build_ipeds_history_cache.py --source-dir data/inputs/ipeds/nces_raw
python scripts/build_ipeds_crosswalk.py
python scripts/classify_closure_events.py
python scripts/build_publication_bundle.py      # local tables + validation, no Sheet writes
python scripts/benchmark_sheeo_method.py        # SHEEO replication + reconciliation
```
