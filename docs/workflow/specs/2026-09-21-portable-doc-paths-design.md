# Portable documentation path guard

Issue: #2627. Scope token: q2627-9a6bc042. Governing decision: ADR-0114.

## Problem and success

The guard depends on Bash 4 `mapfile`, GNU `grep -P`, and GNU `find -printf`.
Enumeration and extraction run in process substitutions, whose failures do not reach
the parent shell. A failed extractor can therefore print `doc paths resolve`.

C1 replaces these operations with Bash 3.2-compatible loops and POSIX AWK/find.
C2 requires enumeration/filtering/extraction failures to return nonzero, name the failed
operation and input, and omit the success message. C3 retains concrete missing-path
diagnostics and the current scan/exclusion/fence/token rules. C4 proves these behaviors
with subprocess tests, including partial output followed by failure.

## Scope and design

`scripts/check-doc-paths.sh` remains the sole guard; its just/CI callers need no migration.
`tests/scripts/test_check_doc_paths.py` remains its behavioral test owner. No new ADR is
needed: this repairs the existing ADR-0114 guarantee without changing its policy.
The required spec is committed; the implementation plan is transient per repository rules.

Use checked command substitutions to capture enumeration and per-file reference output
before parent-shell loops consume it through here-strings. Empty output is valid.
Keep tracked-file selection and the existing no-selected-files filesystem fallback.
Capture Git's `-z` output through POSIX `tr` (NUL to newline) under pipefail, so Git's
C-quoted filename representation never enters the consumer. This retains the stated
newline-filename limitation without creating scratch files. Test backslash/tab/quote names
in both Git and filesystem arms. Use portable find `-print`, removing its leading `./`
with AWK. Git outside a repository
is an expected fallback; distinguish its C-locale not-a-repository diagnostic from
unexpected Git failures, which fail closed. Preserve spaces and backslashes in file names.

One POSIX AWK program strips the existing triple-backtick fenced blocks and extracts
the existing ASCII path grammar with `match`, `RSTART`, `RLENGTH`, and `substr`.
Check the preceding byte against `[A-Za-z0-9_./-]`; do not convert a substring inside
another token into a reference. Deduplicate per file in AWK. Keep the existing shell
placeholder/ellipsis and trailing-period handling. Checked exit status precedes path
validation, so partial output from failed tools cannot certify a scan.

Alternatives: installing GNU tools violates the approved dependency exclusion;
adding a Python helper increases the executable surface when AWK already suffices.
Replacing only grep leaves enumeration failures and the Bash floor unresolved.

## Global Constraints

Bash 3.2 and POSIX AWK/find/tr on macOS/BSD and Linux/GNU; no added host dependency.
Keep docs-links behavior, scanned-file selection, and exclusions unchanged.
Do not modify ADR-0114 or just/CI callers. Use the repository's existing guardrail recipes.

## Failure model

- Actors and deployments: local developers and CI running the guard on repository
  checkouts or filesystem fixture roots, on Linux/GNU and macOS/BSD userlands.
- Invariants and assets at stake: a success exit certifies the selected concrete
  documentation references; tool errors and missing references cannot certify success.
- Accepted failure classes: concurrent source-tree mutation is outside this point-in-time
  guard; filenames containing newlines remain outside its existing line-based enumeration
  contract. Deliberately malicious replacement executables are trusted operator tooling.
- Covered elsewhere: Markdown links belong to docs-links; generated constant references
  belong to docs-check/config-docs-check, under ADR-0114 guard maintainers.

## Threat model

- Boundary inventory: existing repository text and filenames enter shell/AWK; no added
  or widened trust boundary, network, persistence, or privileges.
- Actor model: contributors control scanned text; installed executables and checkout
  ownership are trusted. This guard checks paths and does not execute documentation.
- Control per boundary: quoted shell expansions, `read -r`, fixed AWK code, and input
  redirection prevent source names/text from becoming command options or AWK assignments.
  Diagnostics disclose only the selected source/reference and failed operation.
- Out of scope: hostile installed tools and concurrent tree replacement, as above.

## Validation

Run the script against temporary Git and non-Git roots. Prove concrete missing paths,
ordinary empty/no-match cases, duplicates/multiple references, boundaries, fences,
exclusions, and names containing whitespace, backslashes, leading dashes or equals signs.
Inject failing Git enumeration, find, AWK filtering/extraction, including partial output.
Use controlled wrappers rejecting GNU-only options and disable mapfile in a shell arm;
this is a compatibility simulation, not a claim of execution on native macOS.
Run focused tests before and after the change, then lint, whole-tree types, shell lint,
and docs-paths. The installed pre-push hook owns the final full `just ci` run.
