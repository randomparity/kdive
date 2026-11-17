# Guard unaliased pydantic dumps

## Problem

Pydantic 2.14.3 rejects an input key spelled as an aliased field's Python name (`schema_` for
`Field(alias="schema")`) in `extra="forbid"` models; 2.14.0 dropped it silently. #3674 found
two tests that dumped without `by_alias=True` and audited production by reading. Nothing
fails mechanically when a new unaliased dump reaches a closed model or the wire (#3676).

Evidence at f191e6def: 104 alias-bearing model fields in 12 modules; an AST scan of `src/` and
`deploy/` finds 73 `model_dump`/`model_dump_json` calls with no `by_alias` keyword in 43 files
(the issue's 71 is a same-line count). A tracer wrapping `BaseModel.model_dump` over 11.6k
non-DB tests saw an alias-bearing receiver dumped without `by_alias` at four `src/` lines, all
`anchor.model_dump(exclude={"schema_"})`-style calls that want Python names on purpose.

## Scope

Charter: WORK:SCOPE q3676-86642b29 on #3676. One new test file,
`tests/guards/test_unaliased_dumps_are_reviewed.py`; no `src/` change, no ownership move.

1. An AST scanner walks `*.py` under `src/` and `deploy/` (a file that fails to parse fails the guard) and collects calls to `model_dump`,
   `model_dump_json`, `dump_python` and `dump_json` that carry neither a `by_alias=` keyword
   nor a `**` argument (a `**` argument may carry it and is not judged; none exists at this base).
2. A reviewed allowlist maps repo-relative file path to `(count, reason)`. The guard fails for
   a file with collected calls and no entry, for a count that differs in either direction, and
   for an entry whose file is gone or has no collected call. A new dump in an allowlisted file
   therefore reddens until a reviewer adjusts the count and reads the reason again.
3. Reasons: the receivers and the models nested in them declare no alias, or the
   call dumps by Python names on purpose (feeding a same-class `model_validate`). An explicit
   `by_alias=False` passes the scanner because it states the choice.

Rejected: `serialize_by_alias=True` model config and a shared dump helper. Both change what
dumps emit or rewrite the 73 sites, which the charter excludes.

### Failure model

- Actors: a contributor adding a dump; CI running the `tests/guards` tier.
- Invariants: a dump that feeds a closed model or the wire spells aliases; wire shapes and
  validation are unchanged by this work.
- Accepted: the scanner is syntactic, so it cannot see the receiver's type; the reviewed reason
  carries that judgment, and the tracer evidence above is how it was formed. A dump built by
  `getattr` or `functools.partial` is not seen; none exists at this base. Adding an alias to a
  model dumped in a `no alias` file, or swapping one dump for another in the same file, leaves
  the count unchanged and is not caught; keying by function would cost more than that risk.
  The `no alias` reasons were checked against the alias-bearing model set when written.
- Covered elsewhere: runtime rejection by pydantic itself (`extra_forbidden`).

## Success

On main the guard passes. Adding `x.model_dump()` to a file with no entry, or a second call to
an allowlisted file, fails with the file, line and instruction. Scanner behavior is
checked on synthetic snippets.

## Validation

- Scanner contract: focused-test, `tests/guards/test_unaliased_dumps_are_reviewed.py` scanner
  cases (flags bare dump, `**opts`, `model_dump_json`; ignores `by_alias=True/False`).
- Allowlist contract: focused-test, same file's tree test; controlled fault by injecting a bare
  dump into one `src/` file.
