# ADR 0460 — Replace resource cordon/uncordon with one scheduling setter

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1587
- **Epic:** #1576
- **Implements:** [ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §3's retired-name
  search vocabulary, through the `RETIRED_TOOL_NAMES` mechanism
  [ADR-0458](0458-fold-postmortem-triage-into-postmortem-crash.md) §2 landed.
- **Amends:** [ADR-0062](0062-platform-operations.md) §3's two-tool schedulability surface. The
  orthogonal-axes decision itself is unchanged and reaffirmed below.

## Context

`resources.cordon` and `resources.uncordon` were the two values of one property. Every
precondition epic #1576 requirement 5 sets for a parameter consolidation matched:

- **authorization** — both `_PLAT_OP` in `exposure.py`, and one
  `require_platform_role(PLATFORM_OPERATOR)` already covered both wrappers;
- **annotations** — both `_docmeta.mutating()`, neither `destructive()`;
- **execution class** — both a synchronous single-statement `UPDATE`, no job handle;
- **result shape** — the same `resource_envelope(..., next_actions=["resources.describe"])`.

The shared `_set_cordoned` helper already parameterised the sole difference — the boolean it
wrote and the tool name it audited — so the split existed only in the catalog.

## Decision

### 1. One tool, `resources.set_scheduling(resource_id, state)`

`state` is `"cordoned"` or `"schedulable"`, mapped by `_SCHEDULING_STATES` to the `cordoned`
boolean. Both old wrappers and both names are removed in this change; there is no alias and no
deprecation period. The registry drops from 139 tools to 138.

`state` is validated before the role gate, matching `drain_resource`'s handling of `mode`. The
state vocabulary is public schema, so rejecting an unknown value first leaks nothing, and it keeps
an unrecognised argument out of the denial audit record. Authorization is uniform across both
branches, so — unlike `resources.drain` — there is no branch-dependent role to test.

Params are flat top-level arguments, not a `request` wrapper, per
[ADR-0372](0372-flat-params-for-mutation-tools.md). The wrapper docstring and both `Field`
descriptions carry the contract, because those are what an agent sees.

### 2. Schedulability stays orthogonal to health — this is not folded into `set_status`

`resources.set_status` and `resources.set_scheduling` write different columns and this ADR keeps
them apart. ADR-0062 §3's reasoning is unchanged: `cordoned` is a separate boolean rather than a
value in the `status` enum so that (a) a crashed-and-cordoned host reads as both, and (b)
`set_status offline` cannot clobber an operator's cordon. Folding the scheduling states into
`status` would collapse two orthogonal axes into one enum and silently reintroduce exactly the
clobber ADR-0062 rejected. A third tool name in this namespace is the cost of keeping the axes
separate, and it is the cheaper side of that trade.

### 3. `resources.drain` stays a separate destructive tool

`drain` is cordon *plus* reporting or force-releasing live allocations. It is
`destructive()`-annotated and carries this namespace's only branch-dependent authorization
(`passive` → `platform_operator`, `force_release` → `platform_admin`), so it fails requirement 5's
matching test on two of four preconditions. It keeps calling `_apply_cordon` internally and
therefore keeps writing the same `cordoned` boolean the setter owns; a test pins that a drained
host reads back cordoned and that `state="schedulable"` clears it.

### 4. Retired names as `tools.search` vocabulary

Two rows are added to `RETIRED_TOOL_NAMES`:

```python
"resources.cordon": "resources.set_scheduling",
"resources.uncordon": "resources.set_scheduling",
```

Both point at the same replacement, which the existing inversion into `_RETIRED_BY_REPLACEMENT`
already groups. `uncordon` is the half that needs this: unlike `cordon`, it appears nowhere in the
new tool's name, description, or schema, so without the vocabulary an agent that knows only the
old name has no path back. The mechanism's own guard — every key absent from the live registry,
every value present — and the parametrised search behaviour test are inherited unchanged.

## Consequences

- The `resources` namespace goes from 12 tools to 11; the live registry from 139 to 138.
- The curated `kdivectl resources cordon <id>` verb becomes
  `kdivectl resources set-scheduling <id> <state>`, and the generated verb descriptors are
  regenerated rather than hand-edited.
- The audit `scope` detail for a schedulability change reads `state=<state>` instead of
  `cordoned=<bool>`, matching `set_status`'s `status=<status>`. `drain` still audits
  `cordoned=true`, because there the cordon is a step inside a larger action.
- The override ledger is untouched. `prune_or_cordon_resource` /
  `prune_or_cordon_removed_resource` write `cordoned` directly, never through an MCP tool, so
  [ADR-0199](0199-seed-once-runtime-authoritative-inventory.md)'s removed→cordon-if-live contract
  still has the last word: setting a ledger-`removed` host `schedulable` succeeds and the pass
  cordons it again. A test pins that, since the setter is now the only agent-facing write to the
  column.
- An agent calling `resources.cordon` gets the unknown-tool `configuration_error`; `tools.search`
  is the recovery path.

## Rejected alternatives

- **Folding the states into `resources.set_status`.** See decision 2 — it collapses two
  orthogonal axes and reintroduces the `set_status offline` clobber.
- **A boolean `cordoned: bool` parameter instead of a `state` enum.** A boolean argument reads as
  a flag on a mutation whose name does not say what `true` means, and it cannot grow a third
  schedulability state without a breaking signature change. The two state names are also the
  vocabulary agents and operators already use.
- **Folding `resources.drain` in as a third `state`.** See decision 3 — different annotations and
  branch-dependent authorization; requirement 5 forbids it.
- **Keeping `resources.uncordon` as an alias.** The project is pre-release and follows
  replace-don't-deprecate; an alias keeps a second name in the catalog, the RBAC matrix, the
  generated CLI, and the served docs for no capability.
