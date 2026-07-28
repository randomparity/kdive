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
same two command lines inside one release, and it is the correction of the first rather than a
new direction.

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

The handler also rejects a **non-finite** value, which is not redundant with the tools' own
`math.isfinite` guard. `float()` accepts `inf`, `-inf` and `nan`; JSON can encode none of them,
so pydantic serializes all three to `null` on the way out. The tool would be handed `null` for
a property it declares as a `number`, and the transport raises before the handler's
`configuration_error` can be returned — the CLI's own `main()` has no exception handling, so
the operator would get a traceback and exit `1` rather than the exit `2` every other malformed
`--timeout-s` produces. This is also the one payload defect ADR-0469's guard cannot see, since
it validates the Python payload before serialization and `inf` passes `"type": "number"`. The
CLI therefore refuses it up front rather than relying on a server-side check that this path
can never reach.

Teaching the `Verb` dataclass a per-option `type=` was rejected as premature: two options
across the whole registry need coercion today, and a general mechanism would have to answer
where the type comes from (the schema, presumably) — which is a different change, and one the
generated verbs already solve for themselves via `GeneratedFlag.arg_type`.

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
  stop parsing**, with no deprecation period, one release after they became the documented
  form. Scripts written against ADR-0468's runbook line must drop the flag name.
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
- **Default the curated `--timeout-s` to `0`.** Rejected by the user; see decision 2.
- **Add a curated `jobs get` verb over `jobs.wait` with a hardcoded `timeout_s=0`.** Rejected
  for the same reason ADR-0468 rejected it: it puts a `get` verb and a `wait` verb on one tool
  with different implied timeouts, reintroducing one layer down exactly the duplication
  ADR-0468 removed.
- **Give `Verb` a per-option `type=`.** Rejected as premature; see decision 3.
