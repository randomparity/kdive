# 0001 — The 483 pre-gate ADRs keep a non-conforming shape and are grandfathered

## Status

Open
review-by: 2027-01-29

## Concern

The records gate adopted alongside this record (ADR-0504) checks a record shape that none of
the 483 ADRs written before it use. Two differences, both structural:

- the H1 is `# ADR 0503 — …`, while `check_title_number` in
  `.github/scripts/profiles/adr.sh` requires the H1 to begin `# NNNN ` — the number alone;
- status is a metadata bullet **above** the first heading (`- **Status:** Accepted`,
  `- **Date:** 2026-07-29`), while the gate requires a `## Status` **section** whose body
  reads `Accepted (YYYY-MM-DD)`. `## Context`, `## Decision`, `## Consequences` and
  `## Considered & rejected` are also required and non-empty; older ADRs vary.

The gate does not fail on these. Conformance is computed from the base ref
(`check-records.sh`, `evaluate_base_conformance`), so a record that was already
non-conforming reports `W-LEGACY-SHAPE` instead of an error. Every one of the 483 is in
that position, so the gate passes and emits warnings.

The consequence is a **split convention**: records numbered ≤ 0503 use the old shape and
are permanently exempt from the structural rules, while 0504 and every later ADR are
checked at full severity and must use the canonical shape. A reader who copies a
neighbouring ADR as a template will copy the wrong one.

## Why deferred

Migrating is not a formatting pass this change can safely bundle.

The supplied `migrate-records.sh` fixes the H1s line-locally, but it **deliberately does
not** convert a status bullet into a `## Status` section — doing so moves a line from
outside every section into a new one, which is exactly the region the gate's append-only
rule protects. The skill states this: such a record "keeps its status as a metadata bullet
… gets its title fixed and stays grandfathered". So even a full migrator run leaves the
larger half of this concern open, and the migrator must be run from the skill, never in CI,
and committed on its own with `Migrated-markers:` trailers.

Hand-converting 483 records is also the highest-risk possible first use of an anti-erasure
gate: the gate's whole purpose is to fail a record that was rewritten in place, and a bulk
reshaping of every record is that operation performed 483 times. Doing it in the same
change that installs the gate would mean the gate has never once run green over the
untouched corpus, so a genuine erasure and a migration artifact would be
indistinguishable in review.

Adopting the gate now is still worth it without the migration: the rules that matter —
a record deleted, moved, symlinked away, or gutted in place — are anti-erasure findings
and are **never** downgraded by grandfathering. Those apply at full severity to all 483
records today.

## Non-regression boundary

- The count of `W-LEGACY-SHAPE` records must not grow. Any **new** ADR (0504 onward) is
  checked at full severity, so the legacy set is closed by construction — a new
  non-conforming record is an error, not a warning.
- `docs/adr/README.md` must not regain rows numbered like records; `profile_check_directory`
  reports `W-INDEX-TABLE` if it does.
- Anti-erasure findings must stay at full severity. Do not add a grandfathering path that
  relabels them.

## What would resolve it

Two commits, in order, each on its own:

1. Run `RECORD_PROFILES="adr debt" ~/.claude/skills/decision-records/assets/migrate-records.sh`
   (dry run, then `--write`) from a clean worktree, and commit only its marker changes with
   the `Migrated-markers:` trailers it prints. That closes the H1 half for all 483 and is a
   marker-only diff, which the gate permits explicitly.
2. Decide whether the status-bullet half is worth converting at all. Converting it buys
   full-severity structural checking on the back catalogue; leaving it costs a permanent
   two-shape corpus. If converting, do it in reviewable batches, and expect the
   append-only rule to require justifying each diff — that friction is the gate working,
   not a bug.

Done when `RECORD_PROFILES="adr debt" ./.github/scripts/check-records.sh` reports no
`W-LEGACY-SHAPE` for any ADR, or when a follow-up record accepts the split shape
permanently and this one is resolved by pointing at it.

Until then, the practical mitigation is `docs/adr/0000-template.md`: copy the template,
never a neighbouring ADR.

## Provenance

target: docs/adr
Found while adopting the `decision-records` gate in kdive on 2026-07-29 (ADR-0504), as the
direct and disclosed consequence of installing a shape check over a corpus that predates
the shape. Not a defect the gate introduced — a pre-existing divergence the gate made
visible and countable for the first time.
