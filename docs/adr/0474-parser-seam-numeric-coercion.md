# ADR 0474 — Numeric CLI arguments are coerced at the parser seam, and a float must be finite

- **Status:** Accepted
- **Date:** 2026-07-28
- **Issue:** #1619
- **Epic:** #1576
- **Depends on:** [ADR-0469](0469-verb-shape-schema-guard.md), whose "read the argparse metadata
  off the generated verb at the same path rather than restating it" seam this extends by one
  more attribute.
- **Amends:** [ADR-0470](0470-positional-id-for-the-cli-point-read.md) decision 3, which recorded
  that protecting a curated numeric option from a non-numeric value is a *per-verb obligation*
  discharged in each handler. It stops being per-verb here; the two `wait` handlers keep only
  the part the parser cannot express.

## Context

A curated `Verb` declares its options as bare names. `_verb_parser` turned each into
`add_argument(f"--{option}", default=None, …)` with no `type=`, so argparse handed every curated
option to its handler as the raw `str` it read from `argv` — including the four whose tool
parameter is a JSON `number`. Three handlers coerced by hand; the two in `images` coerced with a
bare `int()` that catches nothing:

```console
$ kdivectl images extend img-1 --seconds abc --reason r
ValueError: invalid literal for int() with base 10: 'abc'   # exit 1, traceback
```

`kdive.cli.dispatch.run` catches only `ToolError` and `kdive.cli.__main__.main` catches nothing,
so the `ValueError` reached the operator as a traceback and a generic exit `1`, where every other
malformed argument on the same command line is an argparse usage error on exit `2`.
`images upload --lifetime-seconds` was the same defect.

This was structurally invisible to the guard that covers this surface: `test_verb_schema_guard.py`
drives each curated verb over its own descriptor, and its `_sample_value` synthesizes a value the
schema would *accept*. A guard that only ever feeds well-formed input cannot see a malformed-input
defect, so the coverage this needs is a new test, not a stronger sample.

The generated half of the parser never had the bug — `_add_generated_flag` has always passed
`type=_ARG_TYPES[flag.arg_type]`. The information the curated half was missing already existed, on
the same `GeneratedFlag` the curated parser was by then already reading `choices` and `help` from.

## Decision

### 1. The curated parser reads `arg_type` off the generated verb, like `choices` and `help`

`_derived_type(derived, name)` joins `_derived_choices` and `_derived_help` as a third accessor
over the `dict[str, GeneratedFlag]` that `_curated_flags` already computes once per verb, and
`_verb_parser` passes `type=` alongside `choices=` and `help=` for positionals, options, and
required options.

The coercion is therefore *derived*, not restated: it comes from the generated verb at the same
path, whose `arg_type` the generator reads off the live tool schema and `cli-verbs-check` keeps in
sync. A curated parameter with no generated counterpart yields `None`, which is exactly argparse's
own default for `type=` (its registry maps `None` to the identity function), so the seven
`accounting` options stay strings with no special case.

`type=` is deliberately **not** passed in the `verb.flags` loop. A `store_true` flag builds an
`argparse._StoreTrueAction`, whose `__init__` accepts neither `type` nor `choices`; passing either
raises `TypeError` while the parser is being built, which is why that loop already passed `help=`
alone. The four buckets keep four explicit `add_argument` calls rather than sharing a `**kwargs`
helper, because the buckets genuinely do not accept the same keywords.

Four curated parameters change behavior, and they are the whole set:
`jobs wait --timeout-s` and `allocations wait --timeout-s` (float),
`images upload --lifetime-seconds` and `images extend --seconds` (int).

### 2. `_ARG_TYPES["float"]` refuses `inf` and `nan`, for every float flag in the CLI

`float("inf")` and `float("nan")` succeed, so `type=float` accepted them. Neither has a JSON
encoding: pydantic serializes both to `null` without raising, so the value does not arrive at the
tool as a number the tool's own validation could reject — it arrives as a missing key, and the
tool applies its default. A caller who asked for an infinite timeout silently got 30 seconds.

`_ARG_TYPES["float"]` therefore becomes `_finite_float`, a `str -> float` callable raising
`argparse.ArgumentTypeError` for a non-finite result. Because `_ARG_TYPES` is the single map both
halves of the parser read, this fixes the curated and generated surfaces in one place.

**This is repo-wide, and the blast radius is stated rather than scoped away.** It changes the
accepted input of all six generated float flags, not only the two reachable through a curated
verb: `jobs wait --timeout-s`, `allocations wait --timeout-s`, `control watch-for-crash
--deadline-s`, `debug advance --timeout-sec`, `debug continue --timeout-sec`, and
`introspect script --timeout-sec`. Every one is a timeout or a deadline in seconds, where an
infinite or undefined value has no meaning that the wire format could carry anyway. The narrower
alternative — a finite check only on the four curated parameters — was rejected because it would
leave the identical hole open on the four generated float flags with no curated counterpart, and
would put the check somewhere other than the one map that already answers "how is this argument
consumed".

`_ARG_TYPES["int"]` is left as the builtin `int`, which already rejects `inf`, `nan`, and `1.5`
with the `ValueError` argparse renders as a usage error. Adding a wrapper there would change no
outcome.

### 3. A range bound stays the handler's obligation

`GeneratedFlag` carries `arg_type` and `choices`; it carries no numeric bounds, because the
generator does not project JSON Schema `minimum`/`maximum`. So the parser can say
*"this is a finite number"* and cannot say *"this is a non-negative number"*.

`reads._wait` accordingly drops the `float()` / `math.isfinite` half of the check ADR-0470
decision 3 introduced — the parser now guarantees a finite `float` reached it — and keeps the
negative rejection, unchanged in message and in exit code. A negative `--timeout-s` is clamped to
`0` server-side, turning a requested wait into a point read with no error anywhere, which is how a
deadline-arithmetic poll loop becomes a hot spin; that is a domain bound, not a type.

`images_upload` and `images_extend` drop their `int()` calls outright and pass the namespace value
through, having no range bound of their own to enforce.

## Consequences

- `images extend <id> --seconds abc --reason r` and `images upload --lifetime-seconds abc` now
  exit `2` with an argparse usage error naming the option, instead of exit `1` with a traceback.
  Both are mutating verbs, and both now fail *before* any tool call rather than during payload
  assembly.
- The error text for a malformed `--timeout-s` changes. `abc` and `inf` are now refused by
  argparse (`argument --timeout-s: invalid float value` / `must be a finite number`), not by the
  handler's own message. The handler's remaining message drops the word "finite", which it no
  longer checks, and echoes the parsed number rather than the raw string:
  `error: --timeout-s must be non-negative, not -5.0`. The exit code is `2` in all cases, before
  and after.
- `1e400` overflows to `inf` and is reported as `must be a finite number, not '1e400'`. The value
  looks finite as written; the message names what it parsed to, which is the accurate answer.
- A bare `--timeout-s -inf` is rejected by argparse as an *unknown option* rather than by
  `_finite_float`, because argparse's negative-number heuristic recognizes only a leading digit
  or `.`. The exit code is `2` either way. `--timeout-s=-inf` reaches the type callable and gets
  the finite-number message; the tests use the `=` form for exactly this reason, since the bare
  form would pass no matter what the callable did.
- Anything scripting `--timeout-s inf` or a non-finite value on any of the six float flags now
  gets a usage error where it previously got the tool's default. This is the intended correction;
  it is called out here because it is the one input that used to "work".
- The four buckets in `_verb_parser` still repeat their `add_argument` calls. Folding them is a
  standing temptation and remains wrong for the reason in decision 1.
- Coverage is a new test that drives malformed values through the real `build_parser`, since the
  schema guard cannot reach this class by construction.
