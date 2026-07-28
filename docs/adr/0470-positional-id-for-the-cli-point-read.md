# ADR 0470 — The CLI point read takes its id positionally

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1616
- **Epic:** #1576
- **Amends:** [ADR-0468](0468-wait-as-the-single-point-read.md) §5, which left the CLI point
  read on the schema-generated flag form. Decision 2 of that ADR — `timeout_s` keeps its
  30-second default, and the single-read form is `timeout_s=0` spelled out rather than
  defaulted — is **not** amended and is upheld here on the CLI surface as well.
- **Depends on:** [ADR-0469](0469-verb-shape-schema-guard.md), whose schema guard is what makes
  a hand-written payload over `jobs.wait` / `allocations.wait` safe to add.

## Context

ADR-0468 removed `jobs.get` and `allocations.get`, making `wait(timeout_s=0)` the point read.
Its §5 removed the curated `kdivectl jobs get` / `allocations get` verbs along with them,
because `add_subparsers()` emits a curated `Verb` only at a path a *generated* verb already
occupies and neither `get` path survived. The point read stayed reachable, but only in the
generated shape:

```bash
kdivectl jobs wait --job-id <id> --timeout-s 0
kdivectl allocations wait --allocation-id <id> --timeout-s 0
```

That is the only single-record read in the CLI that names its id with a flag. Every other one
takes it positionally: `systems get <id>`, `runs get <id>`, `resources describe <id>`,
`images describe <id>`. ADR-0468 §5 called the flag form "a breaking CLI change, stated plainly
rather than shimmed", which it was — but the breakage it accepted was losing the `get` *verb*,
not losing the positional id. The positional id is recoverable without resurrecting anything,
because a curated `Verb` at `jobs wait` overrides the generated shape at the same path.

The reason this did not simply land inside #1592 is #1611. Two curated verbs over
`jobs.wait` / `allocations.wait` are exactly the shape that drifted three times inside epic
#1576: a hand-written payload over a tool whose schema can move underneath it. With ADR-0469's
guard merged, every payload these verbs can emit is validated against the live tool schema, so
the shape cannot ship broken or rot silently.

## Decision

### 1. Curated `jobs wait` and `allocations wait` verbs take the id positionally

Two `Verb` entries, two handlers:

```bash
kdivectl jobs wait <job_id> [--timeout-s N]
kdivectl allocations wait <allocation_id> [--timeout-s N]
```

This is an override at an existing path, not a new path and not a resurrected tool. The
registry stays at 123 tools, `_generated_verbs.py` is unchanged, and the generated descriptors
for both tools stay in the committed artifact — they are simply shadowed at parse time by
`add_subparsers()`, which prefers the curated shape wherever one exists.

The direct consequence, and the reason this ADR exists rather than being a one-line diff: a
curated `Verb` **replaces** the generated parser at its path. `--job-id` and `--allocation-id`
therefore cease to exist the moment these verbs land. This is a second breaking change to the
same two command lines inside one *unreleased range*, and it is the correction of the first
rather than a new direction. The distinction matters for what operators are told: ADR-0468
landed after `v0.4.0`, so the flag form has only ever existed on unreleased `main`. Relative to
the last release there is exactly one change — `jobs get <id>` became
`jobs wait <id> --timeout-s 0` — which is what the runbook's migration note states. The
intermediate flag form is recorded here, and only here, so the operator contract does not
enshrine a spelling no released artifact ever taught.

### 2. `--timeout-s` mirrors the tool default; the point read stays an explicit `--timeout-s 0`

An omitted `--timeout-s` sends **no** `timeout_s` key at all, leaving the tools' own 30-second
default authoritative. The CLI does not restate `30.0`, and it does not substitute `0`.

Defaulting the curated verb to `0` was considered and rejected by the user. It would make the
bare `kdivectl jobs wait <id>` a point read at the cost of reversing ADR-0468 decision 2 for
the CLI surface only, leaving `kdivectl jobs wait <id>` and `jobs.wait(job_id=…)` — the same
tool, one layer apart — waiting for different lengths of time. There is no precedent in the
repo for a curated verb diverging from its tool's default, and the in-tree convention is the
opposite: an omitted curated option sends no key and the server default wins
(`images list --scope`, `_payload()` in `reads.py`). The documented point read is therefore
unchanged in substance and only loses its flag:

```bash
kdivectl jobs wait <job_id> --timeout-s 0
kdivectl allocations wait <allocation_id> --timeout-s 0
```

### 3. `--timeout-s` is coerced to a *finite* `float` in the handler

`_verb_parser` declares curated options with `default=None` and no `type=`, so every curated
option value arrives on the namespace as a string. Both tools declare `timeout_s` as a JSON
`number`, so `{"timeout_s": "0"}` is a schema violation — ADR-0469's guard drives
`--timeout-s 1.5` at these verbs and validates what they emit, so an uncoerced pass-through
fails CI rather than reaching a server. The handler coerces with `float()`, matching the
existing hand-coercion in `images.py` (`int(args.seconds)`, `int(lifetime)`), and a
non-numeric value is a usage error (exit `2`) instead of an uncaught `ValueError`.

The accepted range is **non-negative and finite**. Both refused classes would otherwise be
silently reinterpreted rather than rejected, which is the failure mode this decision is about;
zero stays legal, because it is the documented point read.

*Non-finite.* `float()` accepts `inf`, `-inf` and `nan`; JSON can encode none of them, so
pydantic serializes all three to `null` on the way out — silently, without raising. The tool is
handed `null` for a property it declares as a `number`, so its own `math.isfinite` check never
runs and the `configuration_error` it would have returned never happens. What the operator gets
instead is the opaque failure of a rejected request at exit `1`, rather than the explicit exit
`2` and named cause every other malformed `--timeout-s` produces. This is also the one payload
defect ADR-0469's guard cannot see, since it validates the Python payload before serialization,
where `inf` satisfies `"type": "number"`.

*Negative.* Both handlers compute `min(max(timeout_s, 0.0), MAX_WAIT_S)`, so a negative is
clamped to `0` — by ADR-0468's own construction, exactly one query and an immediate return.
Nothing reports this: the schema carries no `minimum`, so ADR-0469's guard is silent, and no
envelope field distinguishes a clamped negative from a requested point read. A wrapper that
computes its wait arithmetically (`--timeout-s $(( deadline - now ))`) therefore turns a
bounded server-side long-poll into an unthrottled client-side spin the moment it passes its
deadline, and sees only a loop that appears to be working. Refusing it follows the convention
already in this module, where a given-but-empty `--projects` is exit `2` rather than a value
whose effect differs from what was asked.

*Above `MAX_WAIT_S`.* Deliberately **not** refused, even though the same silent clamp applies
at the upper bound and the schema carries no `maximum` either. The asymmetry is in the
consequence, not the mechanism: over-clamping shortens a bounded wait, which a correct poll
loop simply re-issues, whereas under-clamping to zero converts a wait into an unthrottled
spin. Refusing it would also mean restating `MAX_WAIT_S` in the CLI — a second copy of a server
constant that goes stale the moment the server raises the cap, which is exactly what
`_curated_choices` exists to avoid for enums. The cap is stated in the runbook instead — one hand-copied figure, which with the default makes several restatements of two literals that no guard binds; that drift surface is tracked as #1622.

Placing this at the handler rather than at the parser seam is a deliberate but narrow choice,
and the reason is not that no seam exists. `_curated_choices` (ADR-0469) established exactly
the mechanism — per-parameter argparse metadata read off the *generated* verb at the same path
— and `GeneratedFlag.arg_type` already carries `"float"` for both `timeout_s` flags, so the
generalization is ten lines above `_verb_parser` rather than a different change. It is
deferred because it is not a refactor: argparse's own `type=float` accepts `inf` and `nan`
too, so the seam would need a shared finite-number type, and adopting it would change the
failure mode of `images extend --seconds` and `images upload --lifetime-seconds` — two
mutating verbs outside this issue — from an uncaught `ValueError` to an argparse usage error.
That is a fix those verbs need (#1619), not one to smuggle in here. Until then this ADR states
plainly that the protection is a per-verb obligation over a defect class CI cannot detect.

### 4. The invocation pin moves rather than being dropped

ADR-0468 §5 added `test_documented_point_read_invocation_parses` to
`tests/cli/test_dispatch_wiring.py` so "the runbook cannot drift back to a command that does
not parse". That obligation outlives the shape it pinned: the runbook is still hand-written
markdown that no generator checks. The test is rewritten against the new positional form —
asserting the id lands on the curated dest, that `--timeout-s 0` is accepted, and that the
removed `--job-id` / `--allocation-id` flags are now rejected — rather than deleted along with
the invocation it described. `test_removed_getter_verbs_are_gone` is unaffected: `jobs get`
still does not exist.

## Consequences

- **`kdivectl jobs wait --job-id <id>` and `kdivectl allocations wait --allocation-id <id>`
  stop parsing**, with no deprecation period. Nobody upgrading from a release is affected —
  the flag form never shipped — but anyone tracking `main` since ADR-0468 must drop the flag
  name. The break an operator on `v0.4.0` actually sees is the `get` removal, which is what the
  runbook's migration note and this branch's `BREAKING CHANGE:` footer are anchored to.
- **No tool change and no migration.** The registry stays at 123, both tools keep their
  schemas and their `read_only()` annotations, and nothing is persisted.
- **The verbs are covered by the schema guard the day they land.** ADR-0469's matrix is
  parametrized over `REGISTRY` and derives its argv rows from each verb's own descriptor, so
  there was nothing to register and no exclusion to add.
- **Two things the generated shape carried for free move or disappear.** `type=float` came
  from `GeneratedFlag.arg_type`, so a bad value failed at parse time; the curated shape fails
  in the handler instead, one layer later, with an explicit message rather than argparse's.
  Per-flag `--help` text is simply lost: `_verb_parser` declares curated positionals and
  options with no `help=`, so `kdivectl jobs wait --help` prints a bare `--timeout-s
  TIMEOUT_S` where the generated verb rendered the schema's own description. This is not new
  with these verbs — every curated verb has always dropped it — but it costs more here,
  because the timeout is the verb's whole semantic surface. The 30-second default and the
  300-second clamp are stated in the runbook instead; teaching `_curated_choices` to pass
  `GeneratedFlag.help` through as well would fix it for all 23 curated verbs at once; that is
  tracked as #1618 rather than folded in here.
- **The dated design record for #1592 is not rewritten.**
  `docs/specs/2026-07-27-wait-as-single-point-read-1592-design.md` still names the flag form.
  It is a record of what was designed then, and this ADR is the record of what changed.

## Alternatives considered

- **Leave the flag form and change nothing.** Rejected: it makes the point read the one
  single-record read in the CLI that spells its id with a flag, for no reason other than that
  the tool it now dispatches to happens to have a second parameter.
- **Teach `gen_cli_verbs` to emit a sole required scalar id as a positional.** This is the only
  option that reaches the goal with *no* hand-written payload — the thing the Context above
  names as what kept drifting through epic #1576 — and it would keep both things decision 1
  gives up (parse-time `type=float`, and the schema-derived per-flag `--help`) while fixing the
  same flag-vs-positional inconsistency for every by-id generated verb rather than two.
  Rejected on blast radius, not on merit: it changes the command line of every generated verb
  with a single required id across the whole surface, which is a third breaking change to
  unrelated commands inside one unreleased range and far outside #1616. The hand-written
  payload it would have avoided is the risk ADR-0469's guard was built to carry, and that guard
  is now merged — which is why the narrower option is acceptable here and was not before.
- **Default the curated `--timeout-s` to `0`.** Rejected by the user; see decision 2.
- **Add a curated `jobs get` verb over `jobs.wait` with a hardcoded `timeout_s=0`.** Rejected
  for the same reason ADR-0468 rejected it: it puts a `get` verb and a `wait` verb on one tool
  with different implied timeouts, reintroducing one layer down exactly the duplication
  ADR-0468 removed.
- **Move the coercion to a shared `type=` on the curated parser seam.** Deferred, not
  rejected: the seam exists (`_curated_choices`) and the type is already derivable, but taking
  it changes two mutating verbs outside this issue. See decision 3 and #1619.
- **Let the server clamp a negative `--timeout-s`.** Rejected: the clamp is correct as a server
  behavior and silent as a client one, and silence is what makes the poll-loop hot spin
  invisible.
