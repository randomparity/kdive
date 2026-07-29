# ADR 0476 — One home for the wait timeout default and cap

- **Status:** Accepted
- **Date:** 2026-07-28
- **Issue:** #1622
- **Amends:** [ADR-0470](0470-positional-id-for-the-cli-point-read.md)'s rejected-alternative
  note that "the 30-second default and the 300-second clamp are stated in the runbook instead",
  which #1618 (`deb06a558`, no ADR of its own) made obsolete by rendering both figures into
  `kdivectl … wait --help` from the tool schema.
- **Relates to:** [ADR-0410](0410-code-derived-doc-constant-guard.md) (`gen_doc_constants`),
  [ADR-0138](0138-transport-reset-retry-contract.md) (why the ceiling is 300 s),
  [ADR-0468](0468-wait-as-the-single-point-read.md) §2 (`timeout_s` keeps its default).

## Context

The `wait` timeout default and cap existed as **four** unrelated literals:

| Site | Literal |
|---|---|
| `mcp/tools/jobs.py` | `DEFAULT_WAIT_S = 30.0` |
| `mcp/tools/jobs.py` | `MAX_WAIT_S = 300.0` |
| `mcp/tools/lifecycle/allocations/common.py` | a second `MAX_WAIT_S = 300.0` |
| `mcp/tools/lifecycle/allocations/registrar.py` | a bare inline `] = 30.0` — no named constant |

Nothing bound them. ADR-0470 decision 2's whole argument is that `kdivectl jobs wait <id>` and
`jobs.wait(job_id=…)` must not wait for different lengths of time — an argument that rests on
it being *one* number, which it was not.

The split had already produced a live inconsistency in the agent-facing schema:
`allocations.wait`'s `timeout_s` description said only "capped at 300" and never stated its
default, while `jobs.wait`'s stated both. Two tools, one parameter, two different contracts
for what an omitted argument does.

The existing guard did not cover any of it. `doc-constants-check` reported "2 in sync" —
the single-PUT upload ceiling and the approximate tool count — and would have kept reporting
that while all four literals moved.

## Decision

### 1. The shared home is `mcp/tools/_common.py`

`DEFAULT_WAIT_S` and `MAX_WAIT_S` move to `kdive.mcp.tools._common`, which both `jobs.py` and
`lifecycle/allocations/registrar.py` **already import from** (`DEFAULT_LIST_LIMIT`,
`MAX_LIST_LIMIT`). No new import edge is created.

This matters because the obvious alternative — have the allocations package import from
`jobs.py`, which already owned both constants — is rejected: the allocations package
deliberately does not depend on `tools/jobs.py`, and a shared *value* is not a reason to make
one tool module depend on a sibling tool module. `_common.py` is where the tool boundary keeps
exactly this class of thing; it already holds the paired default-and-cap for list pagination,
which is the same shape of contract.

`POLL_INTERVAL_S` stays duplicated (0.5 in both modules) **by decision, not omission**. It is
local tuning: the two loops poll different tables and are free to diverge. The default and the
cap are the opposite — a contract the CLI and both tools must agree on — and that difference is
recorded in both modules' docstrings so the asymmetry is not read as an oversight.

### 2. `allocations.wait` states its default

Its `timeout_s` default becomes `DEFAULT_WAIT_S` rather than a bare `30.0`, and its `Field`
description gains the "defaults to N" clause `jobs.wait` already carried, interpolated from the
constant. Both tools now surface both figures, and a test asserts that over *both* tools plus
the schema `default` itself — the description could interpolate the constant while the
signature still hardcoded `30.0`, which is precisely the pre-existing state.

### 3. The runbook figures are deleted, not bound

`docs/operating/runbooks/kdivectl.md` restated "default of **30 seconds**" and "clamps any
larger value to **300 seconds**". These are **removed** rather than given a generated binding.

ADR-0470 put them there because at the time neither figure reached the user anywhere else.
#1618 changed that: per-flag `--help` is now derived from the tool schema, so
`kdivectl jobs wait --help` and `kdivectl allocations wait --help` both render both numbers
from the constants themselves. The runbook sentence became a hand-maintained copy of text the
CLI already prints.

Binding it was rejected: a generated binding that keeps a redundant sentence in sync is
machinery preserving a duplicate rather than removing it. The sentence's non-redundant content
— *the CLI never picks the timeout for you*, which is ADR-0470 decision 2 and is not something
`--help` conveys — is kept, and the page now points at `--help` for the figures.

### 4. The `gen_doc_constants` binding covers the one prose copy that remains

`src/kdive/cli/commands/reads.py`'s `_wait` docstring says "the tool's own 30-second default".
That is kept — it explains a non-obvious control-flow decision (send no `timeout_s` key at all)
and the number is what lets a reader check the CLI is not secretly different — and gains a
**guarded** (non-writable) binding, the kind ADR-0410 defines for "a figure embedded in a
hand-authored source docstring whose surrounding sentence carries nuance a generator must not
rewrite". `doc-constants-check` goes from 2 bindings to 3.

## Consequences

Every live statement of either figure is now covered by exactly one mechanism:

| Surface | Figures | Mechanism |
|---|---|---|
| MCP `Field` descriptions, `@app.tool` docstrings | default + cap | interpolation, enforced by `test_agent_facing_numeric_bounds_are_interpolated_not_hardcoded` (a hand-typed `capped at 300` is a test failure) |
| `kdivectl … wait --help` | default + cap | derived from the schema (#1618, extending [ADR-0469](0469-verb-shape-schema-guard.md)'s `_curated_flags` seam), pinned for **both** verbs against the constants |
| `reads.py` `_wait` docstring | default | `gen_doc_constants` guarded binding |
| `kdivectl.md` runbook | — | deleted; defers to `--help` |

**Disclosed asymmetry.** After the runbook deletion the cap has *no* hand-written prose copy
anywhere live, so `doc-constants-check` has nothing to bind for it and moving `MAX_WAIT_S`
alone does not redden that check. This is the intended end state, not a gap in it: zero copies
is strictly stronger than one copy plus a guard. The cap's exposure is the *reintroduction* of
a hand-copy, which the AST interpolation guard already refuses on the agent-facing surface.
The residual, stated plainly: a newly hand-written `300` in an operator doc or in CLI prose
would be caught by neither mechanism, because a binding must name a string that exists.

Test assertions on these figures read the constants instead of retyping them, including the
two `\b30\b` / `\b300\b` literals #1618's help test had hand-typed — a hand-typed expectation
reddens on a deliberate bump and proves nothing about whether the rendered help tracked it.

No schema, migration, config setting, tool-surface, or behavior change: every value is
unchanged and every tool still waits and clamps exactly as before.

## Alternatives considered

- **Keep the constants in `jobs.py` and import them into the allocations package.** Rejected:
  creates the tool-module-to-tool-module dependency the package structure avoids, to share a
  value that belongs to neither tool in particular.
- **A new `mcp/tools/_wait_constants.py`.** Rejected as premature: `_common.py` exists for this
  and already carries the analogous pagination pair. Two constants do not need a module.
- **Bind the runbook figures with a generated binding** (what #1622 proposed). Rejected in
  favour of deleting them — see decision 3. The issue was filed before #1618 landed, so its
  premise that the runbook was the only operator-facing home no longer held.
- **Also collapse `POLL_INTERVAL_S`.** Rejected: equal values today, but no contract requires
  the two poll loops to stay equal, and collapsing them would assert one that does not exist.
