# shellcheck shell=bash
# Architecture Decision Records: docs/adr/NNNN-slug.md.
#
# Sourced by check-records.sh, never executed. It supplies the record-kind knowledge the engine
# deliberately lacks: which directory, which sections, what a Status means, and the two rules
# that are specific to an ADR — the H1 carries the record's number, and a supersession banner
# names a sibling record by path rather than free text.
#
# Newline-delimited strings rather than arrays: bash 3.2 is the macOS system shell, where
# "${arr[@]}" on an empty array is fatal under `set -u`.
#
# Every value below is read by check-records.sh after it sources this file, never within it;
# linted standalone, shellcheck cannot see that use.
# shellcheck disable=SC2034

RECORD_DIR="docs/adr"

# Substituted into engine messages that name the record kind: "a record is resolved once, by one
# artifact" reads wrong for an ADR.
RECORD_LABEL="ADR"

# docs/adr/README.md lives inside the record directory, so the deferral gate's "nothing else in
# here" rule cannot carry over unchanged. Exempt means "not a record", not "invisible":
# profile_check_directory below reads both of these.
#
# 0000-template.md is exempt despite being record-shaped, because it is not a decision — it is
# the shape a decision is copied from. As a record it was immutable, so correcting it was the
# one edit the gate refused, and it sat teaching the pre-0504 shape while the gate rejected
# every record written from it. Exempting it moves it out of the immutability rules and under
# check_template_sections below, which holds it to REQUIRED_SECTIONS at full severity instead.
RECORD_EXEMPT_FILES="README.md
0000-template.md"

REQUIRED_SECTIONS="## Status
## Context
## Decision
## Consequences
## Considered & rejected"

# Every level-2 section the base ref had, except ## Status, rather than the required list. A
# fixed list protects only uniform records, and ADRs are not uniform — a pre-template record's
# extra sections would be freely guttable.
APPEND_ONLY_SECTIONS="*"

# Two patterns, not one: the loose prefix finds candidate banners so they can be counted and
# judged, and a malformed banner reports E-BANNER-FORM instead of falling through to E-STATUS.
BANNER_PREFIX='^> \*\*Superseded by'
BANNER_PATTERN='^> \*\*Superseded by \[[0-9]{4}\]\([0-9]{4}-[a-z0-9-]+\.md\)\*\* \([0-9]{4}-[0-9]{2}-[0-9]{2}\)$'
BANNER_HINT="> **Superseded by [NNNN](NNNN-slug.md)** (YYYY-MM-DD)"

# An ADR banner accompanies `Accepted (YYYY-MM-DD)`; it does not replace it. Every merged ADR
# here carries that line, and adding a banner to the same section is the sole edit the
# convention permits — a flag-free strict status check would fail exactly that edit.
BANNER_REPLACES_STATUS=no

# `Superseded` is accepted as a status word even though the banner is how a supersession is
# recorded: ADR 0003 names five status words including it, and the deployed reviewer prompt
# repeats that list verbatim, so an author following the instructions this repo ships would
# write it.
profile_check_status() {
  local file=$1 label=$2 body
  body=$(section_body "$file" "## Status" | grep -v '^>' | grep . | head -1)
  case "$body" in
  Proposed | Deferred) return 0 ;;
  esac
  if printf '%s' "$body" |
    grep -qE '^(Accepted|Rejected|Superseded) \([0-9]{4}-[0-9]{2}-[0-9]{2}\)$'; then
    return 0
  fi
  err "E-STATUS: $label: Status must be Proposed, Deferred, or Accepted/Rejected/Superseded (YYYY-MM-DD)"
}

# The H1 is the decision statement and it carries the record's number, which E-HEADING-REWRITTEN
# protects from rewording and this rule ties to the filename. Blob-local — decidable from the
# bytes and the path — so it runs in both passes, which is why the hook takes the logical path
# separately from the file it reads.
check_title_number() {
  local file=$1 label=$2 name num title
  name=${label##*/}
  num=${name%%-*}
  title=$(grep -m1 '^# ' "$file" || true)
  if ! printf '%s' "$title" | grep -qE "^# ${num} "; then
    err "E-TITLE-MISMATCH: $label: title '$title' does not begin '# $num ' — the H1's number is the record's number"
  fi
}

# A deferral banner names free text, which is unverifiable; an ADR banner names a sibling record
# by path, so it can be resolved. Not blob-local — it reads the sibling set — so it is a
# tree-pass rule, and it reports through err_full because a grandfathered record must not soften
# it: the likeliest record to acquire a banner here is a pre-template ADR.
#
# status_region rather than the `## Status` body, because that is precisely the record kind this
# rule exists for and a pre-template one has no such section — the body came back empty, the
# guard returned, and the banner on every one of them went unchecked (ADR 0564, #1976).
#
# The README prescribes recording a supersession twice: a banner, and the status itself naming
# the superseding ADR. The extractor therefore reads every `Superseded by [link](target)` in
# the status region, not only banner lines: the banner shape (`> **Superseded by …`) and the
# status-line shape (`- **Status:** Superseded by …`, or its unbolded preamble twin
# `- Status: Superseded by …`), in both the `[NNNN]` and `[ADR-NNNN]` link spellings the
# corpus carries (#1988).
#
# Every link, not the first: E-BANNER-COUNT is downgradable, so on a grandfathered record a
# second banner is a warning, and reading one link would let its dangling target through. Fed by
# a process substitution rather than a pipe, so err_full runs in the current shell where the
# assignment to `failed` survives.
check_supersede_link() {
  local file=$1 label=$2 link
  while IFS= read -r link; do
    [ -n "$link" ] || continue
    # The target is checked against the record grammar before it is joined to RECORD_DIR, not
    # after. The extractor captures anything but `)`, so a banner may name `../../README.md` — a
    # path that exists, resolves, and is not a record. BANNER_PATTERN would refuse it, but through
    # `err`, which downgrades on exactly the grandfathered records this rule was widened to reach,
    # so the form check cannot be what stands between a traversal and a green run.
    if ! printf '%s' "$link" | grep -qE '^[0-9]{4}-[a-z0-9-]+\.md$'; then
      err_full "E-SUPERSEDE-DANGLING: $label: supersession link names '$link', which is not a record filename in $RECORD_DIR"
    elif [ ! -f "$RECORD_DIR/$link" ]; then
      err_full "E-SUPERSEDE-DANGLING: $label: supersession link names $RECORD_DIR/$link, which is not a record here"
    fi
  done < <(status_region "$file" |
    sed -n 's/^\(> \*\*\|- \*\*Status:\*\* \|- Status: \)Superseded by \[\(ADR-\)\{0,1\}[0-9]\{4\}\](\([^)]*\)).*/\3/p')
}

profile_check_extra() {
  local file=$1 label=$2 pass=$3
  check_title_number "$file" "$label"
  [ "$pass" = tree ] || return 0
  check_supersede_link "$file" "$label"
}

# The status vocabulary migrate-records.sh case-corrects, split the way profile_check_status
# above splits it: three words that carry a date, two that stand alone. Read by the migrator
# only — the checker never sources these for anything.
MIGRATE_STATUS_DATED="Accepted Rejected Superseded"
MIGRATE_STATUS_BARE="Proposed Deferred"

# The marker transforms, in the order migrate-records.sh applies them. Called only by the
# migrator, which defines every function here; the checker sources this file and never runs
# it. An ADR carries no `target:` or `review-by:` field, but migrate_fields stays in the
# pipeline anyway: canonicalise treats such a line as a marker wherever it appears, and the
# migrator's output has to agree with canonicalise rather than with a record kind's habits.
profile_migrate_markers() {
  local file=$1 num=$2
  migrate_h1 "$num" <"$file" | migrate_headings | migrate_status | migrate_fields
}

# The exempt file every new record is copied from. Its section list is the one thing in this
# directory that has to agree with REQUIRED_SECTIONS above, because an author who follows the
# documented process writes whatever shape it teaches — and if that shape is wrong, the gate
# rejects the record rather than the template.
#
# Exempting it from the record rules is what makes it fixable; this is what keeps it correct
# afterwards. Nothing else does: the exemption also takes it out of check_sections, so without
# this rule a template could declare any sections at all, or none.
RECORD_TEMPLATE="0000-template.md"

# Exactly REQUIRED_SECTIONS, in the same order — a superset is drift too, since a record copied
# from the template inherits the extra heading and APPEND_ONLY_SECTIONS then pins it there for
# the life of the record.
#
# err_full for the same reason W-INDEX-TABLE uses warn_full: a directory-level rule reports at
# full severity, so a `downgrade` leaking past the record loop's last record cannot soften it.
check_template_sections() {
  local template="$RECORD_DIR/$RECORD_TEMPLATE" declared got want
  # A profile need not ship a template, and this rule does not invent the requirement that it
  # must. Deleting one that a repo does have is a plain content deletion, visible in the diff.
  [ -f "$template" ] || return 0
  declared=$(grep '^## ' "$template" || true)
  if [ "$declared" != "$REQUIRED_SECTIONS" ]; then
    got=$(printf '%s' "$declared" | tr '\n' ' ')
    want=$(printf '%s' "$REQUIRED_SECTIONS" | tr '\n' ' ')
    err_full "E-TEMPLATE-DRIFT: $template declares [$got] but must declare exactly [$want] — a record copied from it has to pass this gate on the first run"
  fi
}

# Once per profile, for rules whose subject is not a record. W-INDEX-TABLE is heuristic — a prose
# list of ADRs is not detected — so it warns and never fails. warn_full/err_full because no
# record's base-ref verdict has any bearing on a file that is not a record. The record list the
# engine passes is unused here; the hook takes it because a directory rule may need it.
profile_check_directory() {
  check_template_sections
  local readme="$RECORD_DIR/README.md"
  [ -f "$readme" ] || return 0
  if grep -qE '^\|[[:space:]]*\[?[0-9]{4}' "$readme"; then
    warn_full "W-INDEX-TABLE: $readme has table rows numbered like records — the directory listing is the index; see ADR 0006"
  fi
}
