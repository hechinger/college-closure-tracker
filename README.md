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
  editors' decisions from the `overrides` tab and republishes:
  - `published`: the tracker's list, institution (main-campus) closures only;
  - `campus_closures`: closed campuses that report separately to IPEDS, as
    context, not a headline;
  - `by_year` and `by_state`: institution counts, with campus counts alongside;
  - `mergers`: editor-confirmed mergers and affiliations, which are excluded
    from closure counts.

# How we count college closures

*Figures as of the 2026-11-11 build. The pipeline reruns weekly, so they move
slightly as federal data updates. The published table covers 1996 onward.*

## The rule in brief

- **A college counts as closed** when the federal record for its main campus
  closes **and** federal data point to the institution itself having stopped:
  federal records don't show any other location still open, or the school
  stopped reporting to IPEDS as an active institution.
- **We count schools the way the federal government does.** Chains that held
  separate federal approvals for several schools count once per school. Triangle
  Tech's Pittsburgh, Greensburg and DuBois schools are three closures, for
  example. Branches under one approval count once, through their main campus.
- **Mergers and affiliations are not closures.** When a school's operations
  continued under another institution, an editor confirms it and it is listed
  on the `mergers` tab instead.
- **Out of scope:** campuses with no IPEDS record of their own (mostly
  extension and teaching sites, including high-school dual-enrollment sites),
  K-12 district and career-tech programs, and locations outside the U.S. and
  its territories. Puerto Rico, Guam, the U.S. Virgin Islands, American Samoa
  and the Northern Marianas are included.
- **Every decision is structured or editorial, never a name match.** Each row
  records the evidence used, and editors can override any of them with a
  documented source.
- **New closures are reviewed by an editor** before they publish.

## Why our numbers differ from SHEEO's and Kelchen's

| | Their count | Ours |
|---|---|---|
| SHEEO method, closures 2008–2023 | 848 | 1,036 |
| Same, colleges offering an associate degree or higher | 287 (Hechinger's earlier "nearly 300") | 415 |
| Kelchen et al. (Federal Reserve, 2025), main campuses, Jan 1996 – Nov 2023 | 1,661–1,671 | 1,748 |

**Higher than SHEEO, even though our checks are tighter in most ways.** The
two rules are strict in different places. SHEEO's method counts a college only
when every one of its federal location records is marked closed. That
requirement mostly catches FSA bookkeeping, not schools still operating. FSA often leaves one location marked "open" years after a school
shut. Examples are ITT Tech, Brightwood College and Presentation College. That
drops 219 closed colleges from the SHEEO count; IPEDS shows each of them
stopped operating. Going the other way, we leave out 33 schools SHEEO counts:

- 12 district or career-tech programs, or administrative offices;
- 3 satellite IDs belonging to colleges that are still open, such as Olivet
  Nazarene;
- 3 mergers, affiliations or reinstatements;
- 15 still unresolved (awaiting an editor, or no main-campus record).

**Higher than Kelchen because the federal file has grown, not because our
rule is looser.** Applied to today's federal file, Kelchen's own rule (main
campus closed, matched to IPEDS) gives 1,825, more than our 1,748. The file now
lists 1,973 main-campus closures for his period, about 250 more than the
roughly 1,726 in his November 2023 source (1,671 matched plus 55 he couldn't
match). We publish 1,748 of those 1,973. The rest are excluded or
awaiting an editor:

- 37 district programs or administrative offices;
- 6 mergers, affiliations or identifier changes;
- 6 reinstated schools;
- 176 awaiting editors.

His list isn't public, so we can't match the gap school by school. Adding
confirmed mergers back changes almost nothing (1,752).

**Campuses are context, not part of the list.** The published list is
institution closures only. Separately, at least 781 campuses that report to the
federal government on their own closed while their college kept operating. That
is a floor, not a full count.
Since 1996, about 14,000 locations closed while their college kept operating,
and most report under their parent. That group includes real branch campuses but
also extension, teaching and high-school sites, and federal data can't separate
them. We also exclude the district programs and mergers that both SHEEO
and Kelchen count.

**A sentence for the story's introduction**, sourced from the
`campus_closures` and `by_year` tabs: "Beyond the [N] colleges that closed
outright, at least 781 branch campuses that reported to the federal government
separately closed while their college carried on, and federal records list
roughly 14,000 closed locations of continuing colleges since 1996, many of them
extension sites and classrooms in high schools."

## The rule in detail

**Institution closure** requires all of the following:

1. FSA closed the main-campus record (OPEID ending in `00`).
2. It matches one IPEDS institution: public or nonprofit at any level except
   public under-two-year, or for-profit at any level.
3. Evidence the institution stopped. That means **one** of:
   - federal records don't show any other location still open on the close date;
   - no institution under the same OPEID6 reported to IPEDS as active after the
     closure year;
   - for the frozen historical base table only, an editor-confirmed closure in
     the Financial Health tracker.
4. No sign of a merger. If IPEDS marks the school "combined with another
   institution," the case goes to an editor.
5. Not later reinstated, and no exact replacement identifier.

**Campus closure:** a location with **its own** IPEDS record closed while the
school kept operating. When a whole chain shut down, its locations are counted
once, through the institution closure.

| Excluded automatically (reason recorded; editors can override) | Why |
|---|---|
| Locations without their own IPEDS record | Can't be verified as campuses. Most are extension or teaching sites, and at least 1,474 are K-12 sites such as "Viterbo University – Portage High School". |
| Locations of chains that shut down entirely | Already counted once, through the institution |
| Public under-two-year programs (IPEDS sector 7), plus district programs coded public 2-year (by override) | Adult programs run by K-12 districts, BOCES and career-tech centers; not colleges |
| IPEDS administrative units | Not instructional |
| Exact FSA identifier replacements | Operations continued under the new ID |
| Locations IPEDS marks as combined into another institution | Likely continued |
| Foreign locations, including the freely associated states (Micronesia, Marshall Islands, Palau) | Out of scope |

**Human review.** Possible mergers, closures without a usable IPEDS match, and
schools that kept reporting after their main campus closed go to editors on the
`overrides` tab (about 240 rows). Each row explains why it was flagged, what to
check and a suggested decision. It also carries the evidence: sector, level,
enrollment, College Navigator and news-search links, what federal records show
about other locations, and who holds the student records. Suggestions are never
applied; an editor's decision is. Every closure first seen after the base table
was frozen also goes to an editor, with a suggested decision where one
applies. Whole-school suggestions use `institutional_closure` for every type of
school. Public, nonprofit or for-profit ownership stays in the separate
`control` field. Campus closures remain a separate category.

## Limitations

1. **Dropping out of IPEDS is strong evidence, not proof.** Some merged schools
   simply disappear from IPEDS (University of the Rockies did). Editors confirm
   those, and they move to the `mergers` tab.
2. **Both signals depend on federal-aid participation.** A school that left
   the federal aid program but kept teaching would look closed to us. That's
   rare, and editors check flagged cases.
3. **Recent years lag.** IPEDS runs a year or more behind, so 2025–26 closures
   rely on editors.
4. **Some thresholds are judgment calls,** such as "main campus closed within a
   year" and "never reports again after the closure year."
5. **Campus counts are a floor.** Only separately reported campuses count.
6. **"Associate degree or higher"** uses the highest degree a school offered in
   IPEDS before closing. SHEEO used FSA program length.
7. **This is our method, not a federal standard.** We publish the rule and this
   reconciliation so it can be checked.

## Sources

FSA closed-school file and Weekly School File (weekly); IPEDS directory and
enrollment files, 1995–2025; a one-time snapshot of the Hechinger Financial
Health tracker's confirmed closures (base table only); and editors' decisions,
each with a source link.
[Kelchen et al., FEDS 2025-003](https://www.federalreserve.gov/econres/feds/files/2025003pap.pdf).
[SHEEO college closures project](https://sheeo.org/project/college-closures/).
`scripts/benchmark_sheeo_method.py` reruns the SHEEO comparison each week.

## Reproducing it

```text
python scripts/build_ipeds_history_cache.py --source-dir data/inputs/ipeds/nces_raw
python scripts/build_ipeds_crosswalk.py
python scripts/classify_closure_events.py
python scripts/build_publication_bundle.py      # local tables + validation, no Sheet writes
python scripts/benchmark_sheeo_method.py        # SHEEO replication + reconciliation
```
