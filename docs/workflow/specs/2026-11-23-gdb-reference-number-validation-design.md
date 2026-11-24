# Validate raw GDB breakpoint and watchpoint numbers

## Problem and outcome

Both command-family decoders use `str(entry.get("number"))`. Missing or null numbers become
`"None"` in typed references; malformed list entries appear alongside valid ones. The same
cause was reproduced for insert/list in both families before freezing this bounded sibling fix.

## Scope

At each existing decoder, require a raw string fully matching `[0-9]+(?:\.[0-9]+)?` before
redaction or model construction. Preserve its bytes until existing redaction. Use the existing
`DEBUG_ATTACH_FAILURE` malformed-response category with a static message advising debugger-state
inspection before retry; include no raw response in diagnostics. No new helper for two guards.
Keep optional fields, watchpoint expression/enabled handling, command strings, clear/location
validation and public models unchanged. Bare missing-number rows remain ignored by `breakpoint_rows`;
wrapped missing-number rows are forwarded and rejected. A later bad row rejects the whole list.
This applies existing missing-record policy at existing owners; no new ADR or migration is needed.

### Failure model and threat model

- Actors: malformed external MI records and adapters consuming their typed references. Assets:
  usable raw identities, mandatory secret redaction, response/error consistency and list integrity.
- Reject missing/null/non-string/empty, whitespace/newline, non-ASCII digits and malformed dotted
  numbers. Preserve numeric zero, leading zeros and numeric/dotted strings.
- Preserve the existing parser's ignored rows and watchpoint type filter. A malformed insert
  response does not prove the debugger failed to install it; diagnostics must not promise that.
- Existing mandatory redaction can mask an identifier coinciding with a registered secret.
  Preserve that policy rather than weaken it or add a public-model pattern. No new resource,
  timeout, concurrency, durable state or live-target policy is introduced.

## Validation and completion

Mode: focused-test. Extend the existing command-host tests across both families and insert,
wrapped-list and bare-list shapes. Explicit accepted/rejected examples must preserve original
strings, optional fields and secret redaction; validate all-or-error lists with a valid prefix.
Missing bare rows keep their valid prefix. Run the matrix before implementation for intended
missing-refusal RED results with positive/ignored rows already GREEN, then rerun GREEN.
Temporarily bypass one guard and weaken fullmatch/newline handling; prove intended failures
and restore exact source. Run command, MI-parser and engine regressions, lint, whole-tree types,
docs checks/hooks and bounded fresh code/security reviews. Preserve original tests, other method
AST, ports/parser/error helpers and SQL bytes. Commit locally and resolve only the exact finding.

## Global Constraints

Python3.14/uv, existing dependencies; Linux x86_64 host, x86_64 and ppc64le declared targets.
No live VM/hardware claim or external action. Public contracts remain protected; final full
campaign CI and whole-branch reviews remain required after the queue work.
