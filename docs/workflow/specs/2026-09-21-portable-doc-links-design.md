# Portable Markdown link guard

Issue: #2640. Scope token: q2640-72ab89ef. Governing decision: ADR-0114.

## Problem and success

The current guard uses Bash 4 mapfile and GNU find -printf. Git enumeration
errors are discarded, and process substitutions hide producer and extraction failures.
C1 makes enumeration portable to Bash 3.2 and POSIX userland.
C2 requires failed enumeration, filtering, or extraction to return nonzero with an
operation-specific diagnostic and no success message, including partial output.
C3 preserves the existing Markdown expression, fence/title/fragment handling,
missing-target diagnostics, tracked-file selection, fallback, and exclusions.
C4 proves these contracts with subprocess tests and controlled failure injection.
These criteria come from issue #2640 Expected and Proposed approach.

## Scope and ownership

The existing script remains the sole checking owner and its existing test module remains
the behavioral proof owner. Just and CI invoke that script unchanged; no callers migrate.
The tracked specification records this repair; the implementation plan remains uncommitted.
No new ADR is needed: this restores ADR-0114's existing guard rather than selecting new policy.

The operator approved these exclusions and owners on 2026-09-22 via campaign dispatch:
Markdown parsing, missing-target semantics, scanned files/exclusions — ADR-0114 guard
maintainers / separate issue; served-resource reachability — served-docs maintainers /
separate issue; docs-paths behavior — docs-paths guard maintainers; added host tools
where portable shell/AWK suffices — docs-links maintainers.

## Design

Use the merged #2627 / PR #2639 enumeration pattern. Capture Git's NUL-delimited
filename output through POSIX tr under pipefail, then filter in checked AWK.
Normalize a failed tr status to 1 so only Git can supply the accepted discovery status 128.
Accept only Git's C-locale not-a-repository discovery failure as expected fallback;
other Git/tr failures stop. Preserve the existing filesystem fallback when the filtered
tracked list is empty. Use find -print with AWK removing the leading ./.
Read captured lines through here-strings using IFS= and read -r.

Replace the extraction pipeline with one checked POSIX AWK program: retain the current
triple-backtick fence toggle, repeatedly match the same right-bracket/parenthesized
nonempty target expression using match/RSTART/RLENGTH/substr, and print the inner
target. Consume captured output only after AWK succeeds. Prefix dirname operands with ./.
Redirect each source to stdin
so source names cannot become AWK options or assignments. Keep the existing shell
external-link, mailto, anchor, title, fragment, and filesystem-existence checks.
Empty output is success; a selected missing/unreadable source fails extraction.
Diagnostics name the failed operation and source/root and point to tool diagnostics.

Alternatives: retaining the pipeline needs separate no-match handling and more subprocesses;
the equivalent AWK scan already supplies zero-match success. Installing GNU tools violates
the dependency exclusion. A new Python helper adds an entry point the existing shell/AWK
owner does not need.

## Global Constraints

Bash 3.2 and POSIX AWK/find/tr on macOS/BSD and Linux/GNU; no added host dependency.
Keep Markdown parsing, missing-target semantics, scanned files and exclusions unchanged.
Do not change docs-paths, served-resource checking, ADR-0114, or just/CI callers.
Use the repository's existing guardrail recipes. Keep the plan uncommitted.

## Failure model

- Actors and deployments: local developers and CI invoke the guard against repository
  checkouts or filesystem fixture roots on Linux/GNU and macOS/BSD userlands.
- Invariants and assets at stake: selected link targets retain existing checks; tool/read
  failure cannot certify successful completion, even after producing partial output.
- Accepted failure classes: filenames containing newlines retain the existing line-based
  limitation; concurrent checkout mutation is outside this point-in-time check; deliberately
  malicious installed tools are trusted operator tooling, not adversarial inputs.
- Covered elsewhere: Markdown grammar improvements and scan policy belong to ADR-0114
  guard maintainers; docs-paths and served-resource guards retain their respective owners.

## Threat model

- Boundary inventory: contributor-controlled Markdown and filenames enter existing shell/AWK
  processing. No added/widened boundary, network operation, persistence, or privilege.
- Actor model: contributors control files; installed tools and checkout ownership are trusted.
- Controls: fixed AWK programs, quoted expansions, read -r, and stdin redirection keep
  filenames/text as data. Failure diagnostics expose source/target and operation only.
- Out of scope: hostile installed executables and concurrent tree replacement, as above.

## Validation

Use Git and filesystem roots to test selected and excluded documents, literal filenames,
empty/no-match documents, multiple links, fences, fragments, titles, and broken targets.
Inject Git, tr, find, filter-AWK, fallback-normalizer, and extractor failure with empty and
partial output, including a pass-through converter failing with status 128 during Git discovery.
Disable mapfile and reject find -printf in compatibility simulations.
Run the focused tests red then green, plus lint, whole-tree types, shell lint, docs-links,
and relevant sibling guards. The installed pre-push hook owns the final full just ci.
Compatibility simulations do not establish native macOS execution; report proof arms.
