# 0564 — Status rules read the status region, and a status value is never protected

## Status

Accepted (2026-08-17)

## Context

The records gate ([ADR-0504](0504-no-hand-maintained-adr-index.md)) treats a record's status as
the one part a merged record may change. `protected_shape` in `.github/scripts/check-records.sh`
drops the `## Status` body before the three anti-rewrite rules compare anything, so a conforming
record can freely rewrite `Accepted (2026-01-01)` into `Superseded (2026-08-17)`, and
`APPEND_ONLY_SECTIONS` excludes `## Status` for the same reason.

That allowance is written against one *location* rather than against the status. The 483
grandfathered records ([debt 0001](../debt/0001-legacy-adr-shape-is-grandfathered.md)) have no
`## Status` section at all — they keep their status as a preamble bullet, `- **Status:** Accepted`,
in the region between the H1 and the first `## ` heading. Every status rule in the gate reads
`section_body "$file" "## Status"`, which for those records returns nothing, and every
protection rule reads the preamble as ordinary prose. Two failures follow, and #1976 hit both
when ADR-0563 superseded ADR-0430:

**The prescribed edit is refused.** `docs/adr/README.md` says to move a superseded ADR to
`Superseded by NNNN`, and the four supersessions the corpus already carries (ADR-0137, 0161,
0265, 0282) do exactly that in the bullet. Making that edit today rewrites a preamble line.
`canonicalise` has marker cases for a `target:`/`review-by:` bullet and for a heading, and none
for a status bullet, so `protected_shape` differs, `marker_only_change` is false, and
`check_preamble_intact` reports `E-PREAMBLE-REWRITTEN` through `err_full` — which
`W-LEGACY-SHAPE` deliberately cannot downgrade. The gate forbids the status update, so ADR-0430
kept reading `Accepted` while its successor recorded that its decision no longer stands. The
status drift the gate exists to prevent was the state it forced.

**The banner is not validated.** `check_supersede_link` reads the same empty `## Status` body,
finds no link, and returns at its `[ -n "$link" ]` guard, so `E-SUPERSEDE-DANGLING` cannot fire
for any pre-0504 record. `check_status`'s `E-BANNER-COUNT`, `E-BANNER-FORM` and
`E-BANNER-FUTURE` never see those banners either. ADR-0430's banner link was checked by a human
in review, not by the gate, and `docs/adr/README.md` told readers the gate checked it.

Neither is a grandfathering decision. Grandfathering says a legacy record's *shape* is a warning
until it is migrated; it does not say the gate stops reading a legacy record's status, and it
must not make the one edit that keeps status honest unreachable.

## Decision

Name the region a record keeps its status in, and read every status rule from it:

```sh
status_region() {   # check-records.sh
  local file=$1
  preamble "$file"
  section_body "$file" '## Status'
}
```

Both regions, unconditionally, rather than a branch on which shape the record is in. A branch
needs a predicate, and every cheap one is forgeable from inside the record it judges: a
`## Status` line quoted in a fenced example satisfies `grep -qxF`, `section_body` then returns the
fence, and the preamble's real banner goes unread — this defect, reopened by a documentation
snippet. The union has no such input, and for the two shapes records actually come in it costs
nothing: a conforming record's preamble is empty, and a pre-0504 record has no `## Status` body.

It does cost something for a third, constructible shape — content in both regions — and that has
to be paid rather than assumed away. `BANNER_REPLACES_STATUS=yes` (the deferral profile) makes a
well-formed banner stand in for the status word, so a banner one line above the first heading
would skip `profile_check_status` altogether; in the base pass that flips the record's verdict to
conforming, and one line would both stop its status being validated and revoke its grandfathering.
That early return is therefore keyed to the `## Status` body, not to the whole region: the body is
the only place a record of such a kind declares a status. The form, count and date rules stay on
the union, because judging a banner wherever it sits is the point.

`check_status` (engine) and `check_supersede_link` (`profiles/adr.sh`) both read `status_region`
instead of `section_body … "## Status"`, so banner form, banner count, banner date and banner link
resolution apply to a legacy record's banner the same way they apply to a conforming one.
`check_supersede_link` checks every banner link it finds rather than the first, because
`E-BANNER-COUNT` is downgradable on a grandfathered record and a second dangling link would
otherwise pass.

`profile_check_status` keeps reading `## Status` and is deliberately left alone. It is what makes
a pre-0504 record non-conforming at the base ref, and therefore what grandfathers it; routing it
through `status_region` would flip most of the corpus to conforming and hold 483 records to full
severity on every other rule at once.

A status **value** is not a protected region, wherever it lives. `protected_shape` already drops
the `## Status` body, and `check_preamble_intact` — the one rule that examines a preamble — now
compares both sides through a reduction that does the same for a status bullet:

```sh
mask_status_bullet() {   # fed `preamble` output, which already stops at the first `## `
  LC_ALL=C awk '
    !seen && /^(- )?(\*\*Status:\*\*|Status:)/ { seen = 1; print "<status>"; next }
    { print }
  '
}
```

The sentinel is a line, not a deletion, so the bullet still has to be *there*: removing it drops a
line from both sides of the comparison and `E-PREAMBLE-REWRITTEN` still fires. A `Status:` line
inside a section is body content of an append-only section, stays byte-protected, and never
reaches this filter.

The first match only, anchored at column one. A record has one status, and every record in the
corpus carries exactly one such line, so a second one is prose that happens to start with the
label. Masking every match would let a change add a `Status:` bullet carrying a paragraph in one
commit and gut it in the next, both green — an erasure route straight through the allowance, since
`check_preamble_intact` counts removals and never objects to the addition.

First by position, not by which line is really the status. A `Status:`-prefixed prose line placed
above the status bullet consumes the allowance and leaves the bullet protected. That is the loud
direction — the prescribed supersession edit then reports `E-PREAMBLE-REWRITTEN` and the author
moves the line — so position is enough, and a rule that tried to identify the *real* status line
would need a heuristic over prose.

One call site, not two. `marker_only_change` — the shortcut in front of the three anti-rewrite
rules — is left as strict as it was, because a status-bullet edit it declines simply arrives at
`check_preamble_intact` and is accepted there. Masking in `protected_shape` as well was
measurably redundant: neutralising it left the suite green.

This does not go in `canonicalise`. `canonicalise` is the definition of a *marker*, and it is also
what `renumbered_elsewhere` compares two records' content with; a status value is content, and
discarding it there would let a deleted record be excused by a sibling that differs from it only
in status.

## Consequences

- The supersession `docs/adr/README.md` prescribes is reachable for a pre-0504 record: set the
  status bullet to `Superseded by` followed by a link to the superseding record, and add the
  banner beneath it, in one commit. ADR-0430 takes that shape in this change, so its status now
  agrees with ADR-0563.
- A dangling supersession banner on a pre-0504 record is `E-SUPERSEDE-DANGLING` at full severity,
  as it already was on a conforming one — `err_full`, not downgradable.
- The four grandfathered supersessions (ADR-0137, 0161, 0265, 0282) write their banner as
  `[ADR-0316]` rather than `[0316]`, with prose appended after the date, so their banner is not
  `BANNER_PATTERN`. Their one `W-LEGACY-SHAPE` line changes from `(E-STATUS)` to
  `(E-BANNER-FORM)`, since `check_status` returns after reporting a malformed banner. The corpus
  gains no warning and loses none — a measured 1720 before and after — and no error is
  introduced, because the link extractor does not recognise that spelling either. The banner
  form the README prescribes stays the single canonical one.
- A merged record may now rewrite the value of the **first** column-one `Status:`-labelled line
  in its preamble to anything. That is the same latitude the `## Status` body has always had, and
  the same argument covers it: a status is state, not a claim the record makes. Nothing else
  moves — a `Date:` or `Deciders:` bullet, any *later* `Status:`-labelled preamble line, an
  indented one, and a `Status:` line inside any section all stay byte-protected.
- A deferral record's status still comes from its `## Status` section. A resolution banner in the
  preamble is judged for form, count and date, but does not stand in for the status word, so such
  a record cannot become conforming by growing one line above its first heading.
- `migrate-records.sh` checks its own output with `marker_only_change`, which this change leaves
  untouched, so the migrator gains no permission it did not have: a transform that rewrote a status
  bullet would still fail its `E-SELF-CHECK` and refuse to write. It has no such transform —
  `migrate_status` only edits inside `## Status`, and its header states it will not lift a preamble
  bullet into a section — so nothing about the migrator changes.
- ADR-0504's "that banner is the only edit a merged ADR permits" describes the convention, not
  the gate: the gate has never protected the `## Status` body, and this change makes the pre-0504
  shape behave the way the current shape already did. No amendment to 0504 is needed for a
  sentence about the convention that this change does not alter.

## Considered & rejected

- **Add a `Status:` case to `canonicalise`'s marker table**, which is the first remedy #1976
  names. It reaches `protected_shape` for free, but `canonicalise` is also
  `renumbered_elsewhere`'s content comparison, so it would widen the look-alike-sibling hole the
  `<n>` sentinel already had to be fenced against. It also misnames the thing: a status value is
  not a marker, and `canonicalise`'s header calls itself the single definition of one.
- **Branch `status_region` on whether the record has a `## Status` heading**, which reads as the
  narrower rule. The predicate is forgeable from inside the record being judged — a fenced
  `## Status` example anywhere in the body satisfies it — and the failure is silent: the gate reads
  the wrong region and reports nothing. The union needs no predicate and no transition rule for a
  record that grows or loses the heading mid-change.
- **Grant a one-time allowance for the ADR-0430 edit** — a path exemption, an env flag, or a
  commit-message escape. The gate's own header rejects this shape of fix: the marker-only
  allowance is a property of the diff precisely so there is no escape hatch to forget to remove,
  and the defect is general to 483 records rather than specific to one.
- **Leave the gate alone and record the supersession only as a banner**, which is what PR #1972
  did. It is the only shape the gate permits today, and it is why #1976 exists: the status line a
  reader and `scripts/check_adr_status.py` both read keeps saying `Accepted`.
- **Route `profile_check_status` through `status_region` too**, so a legacy record's status word
  is checked by the records gate at full severity. It un-grandfathers most of the corpus as a
  side effect, since base-ref conformance is computed from the same rules. The status *keyword* of
  every ADR is already checked at full severity by `scripts/check_adr_status.py`, in exactly the
  bullet form these records use, so nothing is unguarded.
- **Migrate the pre-0504 records to the `## Status` shape instead.** That is
  [debt 0001](../debt/0001-legacy-adr-shape-is-grandfathered.md)'s resolution and it remains the
  right end state, but `migrate-records.sh` explicitly refuses to lift a preamble bullet into a
  section because that is a relocation rather than a marker fix. Blocking a correct supersession
  on a 483-record migration inverts the priority.
- **Reword `docs/adr/README.md` to admit the gate checks neither half** and change no code. It
  makes the documentation honest at the cost of leaving the prescribed edit unreachable, which is
  the defect rather than a description of it.
