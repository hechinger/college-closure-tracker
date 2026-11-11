# Closure classification and publication rollout

## Purpose

Replace the current Hechinger closure list with an auditable event dataset that
can support a public table, an institution-only time series, and a state map.
The public data may contain both institution and campus closures, but every row
must expose `closure_scope` so headline products can default to institution
closures without silently mixing the two units.

This plan treats the FSA closed-school file as a source of closure candidates,
not as proof that an institution stopped operating. A closed Title IV location,
an institution closure, a campus closure, and an identifier change are separate
facts.

## Canonical evidence model

Each source event is identified by a stable `closure_event_id` made from the
original FSA OPEID8 and original FSA close date. Correcting a display date must
not break its editorial history.

Keep these facts separate:

- `fsa_closed_date_iso`: immutable date from the federal source.
- `verified_closed_date_iso`: operational date established by editorial
  evidence, when available.
- `published_closed_date_iso`: the date selected for public use.
- `date_basis` and `date_evidence_url`: why the published date was selected.
- `location_operations_ceased`: `true`, `false`, or `unknown`.
- `parent_continued`: `true`, `false`, or `unknown`.
- `replacement_opeid8`: exact successor identifier, when one is established.
- `replacement_effective_date_iso`: effective date from the structured
  relationship record.
- `review_status`: `pending`, `resolved`, `needs_more_evidence`, or
  `needs_re_review`.
- `publication_decision`: `include_institution`, `include_campus`, or `exclude`.
- `evidence_fingerprint`: hash of the source facts that drove the decision.

An FSA close record establishes that the federal location record closed. It
does not, by itself, set `location_operations_ceased=true`.

## Institution and campus decision table

| Location ceased | Parent continued | Result |
|---|---|---|
| true | false | institution closure |
| true | true | campus closure |
| false | true | administrative change, relocation, or identifier continuation; exclude |
| false | false | contradictory; review |
| unknown | any value | review; exclude until resolved |

`parent_continued=true` requires structured evidence that another location or
the parent remained active after the event. Merely failing to find the parent
in the closure file is not proof of continuation. Conversely, a non-`00`
suffix or the absence of a separate UNITID does not prove that a record is or
is not a genuine campus.

## Mergers, acquisitions, affiliations, and consolidations

These cases remain in the same `overrides` review queue as all other ambiguous
events. A separate merger tab or a second review tab would duplicate editorial
state and invite conflicts.

The classifier may generate a merger or consolidation candidate, but it must
not automatically make the final call.

### Exact replacement identifier

"The same location continued under an exact replacement identifier" means all
of the following are present:

1. A structured federal record explicitly relates the old OPEID8 to a new
   OPEID8.
2. The relationship has an effective date reasonably close to the candidate
   event. Historical RT06 relationships from years or decades earlier do not
   qualify.
3. The replacement identifier is active after that effective date in the
   Weekly School File, with no unreversed close date before the event.
4. Historical IPEDS evidence is consistent with continuity. A one-to-many
   OPEID6 match cannot auto-resolve the relationship.

Names, addresses, name similarity, shared ownership, or an RT11 record-holder
value can prompt review but can never establish an exact replacement.

### Outcomes

- `merged_or_absorbed`: the educational operation continued through a
  successor institution. Exclude it from institution-closure headline counts.
- `institution_closed` with acquisition context: the institution stopped
  operating even though another institution acquired assets, property,
  programs, records, or accepted students. Include it as an institution
  closure and retain the acquisition details.
- `affiliation_or_governance_change`: the institution or its degree-granting
  operation continued under a new affiliation. Exclude it unless separate
  evidence establishes an operational closure.
- `record_custodian_transfer`: RT11 shows who holds records after closure. This
  is custody evidence only and never merger evidence.
- `possible_system_consolidation`: several locations under one OPEID6 closed
  near one another. This is a review trigger only. Confirm whether the whole
  institution ceased, individual campuses closed, or identifiers were
  reorganized before publication.

The four known editorial cases must be re-reviewed under these rules rather
than inherited as generic mergers. In particular, Mount Ida cannot retain the
existing incorrect URI successor statement, and Delaware College of Art and
Design must not be described as a Pratt merger without authoritative evidence.

## One combined overrides and review queue

The `overrides` tab has one row per `closure_event_id` and serves two purposes:

- the pipeline adds unresolved candidates with source context and a review
  reason;
- editors complete the same row with a decision, basis, evidence URL, reviewer,
  and review date.

Only `review_status=resolved` rows may change classification. Pending rows are
never treated as overrides. Existing editorial fields are preserved on every
refresh. If the evidence fingerprint changes, the row becomes
`needs_re_review`, and its previous decision stops controlling publication
until an editor confirms it.

Overrides are event-specific, not OPEID8-specific, because an identifier can
appear in more than one dated event.

## Published and analytical outputs

The `published` tab contains only resolved or methodologically auto-eligible
events and uses two public values:

- `closure_type=institution_closure`, `closure_scope=institution`
- `closure_type=campus_closure`, `closure_scope=campus`

It also retains `institution_category` and the historical IPEDS
`program_level` derived from ICLEVEL:

- 1: `4-year or above`
- 2: `2-year`
- 3: `Less than 2-year`

`by_year` and `by_state` retain institution-only `total` for backward
compatibility and add separate institution, campus, and all-event columns.
Approach 3 is calculated as distinct OPEID6 systems with at least one genuine
published closure, never by summing event rows.

## Staged rollout

1. **Evidence boundary.** Add RT01 status/reinstatement facts, remove RT11
   merger inference, and make RT06 relationships date-aware. Add real-row
   regressions for the stale relationships found for Mount Ida, Watkins,
   Episcopal Divinity, and DCAD.
2. **Classifier.** Emit stable event IDs, tri-state facts, review reasons,
   replacement fields, and evidence fingerprints. Name-based signals and
   multi-location timing may only create review candidates.
3. **Editorial state.** Migrate the `overrides` schema to the combined queue,
   preserve resolved rows, and require a documented basis and evidence URL for
   resolved merger/consolidation decisions.
4. **Publication filter.** Publish from explicit scope and decision fields;
   stop reconstructing parent continuation from absence in the closure file.
5. **Validation bundle.** Before any Sheet write, validate schema, unique event
   IDs, allowed values, resolved-review requirements, and exact reconciliation
   of `published`, `by_year`, and `by_state`.
6. **Change report.** Generate added, removed, reclassified, date-changed, and
   re-review-required records compared with the prior release.
7. **Editorial sample.** Hand-check a stratified real sample across control,
   program level, institution/campus scope, merger candidates,
   consolidations, reinstatements, and records outside IPEDS.
8. **Sheet cutover.** Update a staging copy first. Update the live Google Sheet
   only after the validation bundle and change report pass and an editor has
   approved the cutover.

## Release gates

- No headline institution closure is based only on OPEID suffix, IPEDS
  disappearance, name text, RT11 custody, or Title IV withdrawal.
- Every published campus closure has affirmative parent-continuation evidence.
- Every merger, acquisition, affiliation, or consolidation decision has a
  documented operational-continuity finding.
- Reinstated events are excluded.
- Every published row has a unique stable event ID and an effective date or a
  documented reason the date is unknown.
- Institution totals reconcile exactly across the published rows, time series,
  and state table.
- A failed validation causes no Google Sheet tabs to be cleared or rewritten.
