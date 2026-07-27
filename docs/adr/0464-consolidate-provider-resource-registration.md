# ADR 0464 — Consolidate provider resource registration into one kind-discriminated tool

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1588
- **Epic:** #1576
- **Implements:** [ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §3's retired-name
  search vocabulary, through the `RETIRED_TOOL_NAMES` mechanism
  [ADR-0458](0458-fold-postmortem-triage-into-postmortem-crash.md) §2 landed.
- **Constrained by:** [ADR-0372](0372-flat-params-for-mutation-tools.md) — flat top-level params
  on every mutation tool. See decision 1, which departs from the issue's proposed design because
  of it.
- **Amends:** [ADR-0112](0112-systems-inventory-config.md)'s three-tool runtime registration
  surface. The ownership model (`managed_by='runtime'` rows, config-name rejection, per-identity
  lock, lease) is unchanged.

## Context

`resources.register_remote_libvirt`, `resources.register_local_libvirt`, and
`resources.register_fault_inject` were the same platform-admin operation repeated per provider
kind. Every precondition epic #1576 requirement 5 sets for a parameter consolidation matched:

- **authorization** — all three `_PLAT_ADMIN` in `exposure.py`, over one
  `_authorize_registration` requiring `PLATFORM_ADMIN`. There is **no** branch-dependent
  authorization: no kind is more privileged than another;
- **annotations** — all three `_docmeta.mutating()`, maturity `implemented`, none destructive;
- **execution class** — all three synchronous, returning a `ToolResponse`, no job handle;
- **result shape** — one `_insert_registered_resource` returning `data={id, name, kind}` with
  `suggested_next_actions=["resources.list", "resources.renew"]`.

The three wrappers already converged on one spine, `_register_with_plan`. The only real
differences were which provider fields each accepted, and one *side effect*: the two libvirt kinds
run a bounded in-request TCP reachability probe against the host URI before the insert, and
fault-inject does not (it is synthetic; its host URI is the server constant
`fault-inject://local`).

## Decision

### 1. One tool with a flat scalar `kind` discriminator — not the issue's `request` union

The issue proposed `resources.register(request=<provider-discriminated union>)`. That design
cannot ship here, for two independent reasons, so this ADR records the deviation:

1. **ADR-0372 forbids a `request` wrapper on any mutation tool.** The repo-wide guard
   `test_every_mutation_tool_takes_flat_top_level_params` flags any tool with
   `readOnlyHint=False` that has `request` in its schema properties. Epic requirement 8 forbids
   exclusion lists, so it could not be waived.
2. **The generated CLI cannot express a union.** `scripts/gen_cli_verbs.py` unwraps a sole
   `request` parameter via `_object_body`, which returns `None` for a genuine multi-member union;
   the generator then falls back to zero flags and zero `--*-json` escapes. `kdivectl resources
   register` would have been **silently uninvokable**, and `cli-verbs-check` would not have caught
   it — that gate compares the committed output against a fresh generation, and both would have
   been empty and equal.

The shipped signature is therefore flat, every parameter a scalar:

```python
resources.register(
    kind: ResourceKind,          # the discriminator — a scalar enum, not an object
    name, cost_class, vcpus, memory_mb,
    host_uri: str | None = None,      # required for remote-libvirt AND local-libvirt
    base_image: str | None = None,    # required for remote-libvirt only
    concurrent_allocation_cap: int = 1,
    secret_refs: tuple[str, ...] = (),
    owner_project: str | None = None,
)
```

This produces ten real `kdivectl` flags, including `--kind` with the three enum values as
argparse `choices`.

### 2. Branch required-ness is a handler contract, not JSON Schema — accepted cost

The flat schema cannot say "`base_image` is required when `kind == 'remote-libvirt'`". That
dependency lives in two places instead, and both are load-bearing:

- the `kind`, `host_uri`, and `base_image` `Field` descriptions, which are what an agent reads
  (epic requirement 7);
- `_validate_branch_fields`, driven by the `_BRANCHES` table, which returns
  `configuration_error` naming the offending field.

The contract is **symmetric**: a required field left blank or absent is a missing-field error, and
an inapplicable field that *is* supplied is a does-not-apply error. Silently ignoring, say, a
`base_image` passed with `kind='fault-inject'` would hide a real caller mistake — the old
per-provider request models rejected it as an unknown field, and that strictness is preserved
rather than dropped. Every rule is tested per kind, in both directions.

This is a genuine loss relative to a schema-expressed union: a client cannot pre-validate the
branch offline. It is accepted because the alternatives are worse — a union is uninvokable from
the CLI and violates ADR-0372, and three tools was the duplication the epic exists to remove.
A blank `host_uri` already returned `configuration_error` from the handler before this change,
so the *category* of error an agent sees is unchanged.

### 3. Branch validation runs after the authorization gate

`_validate_branch_fields` runs after `_authorize_registration`, so a caller without
`platform_admin` gets `authorization_denied` and learns nothing about which provider fields the
server wanted. The old `register_remote_libvirt_resource` checked its blank `host_uri` *before*
the role gate; that ordering is corrected here. A test pins the new precedence.

### 4. The reachability probe stays branch-dependent, and that is tested

`_BRANCHES[kind].probes` decides whether the bounded TCP connect runs. This is the one
branch-dependent *outbound side effect* of registration, and the one place a consolidation could
quietly change behavior — a fault-inject register that started probing would add latency and an
egress attempt where there was none. Tests use a recording probe so a skipped probe is visible:
fault-inject records zero probes even with an unreachable probe injected, and each libvirt kind
records exactly the host URI it was given.

### 5. Provider-schema projection, and the call-time guard it obliges

`"resources.register"` joins `NARROWED_TOOLS` in `kdive.mcp.schema.tool_projection`, so the
advertised `kind` enum is narrowed to the deployment's composed `ResourceKind` set
([ADR-0269](0269-derive-agent-schemas-from-composed-providers.md)). This preserves the
per-provider narrowing the three separate tools got implicitly.

ADR-0269 §4 makes narrowing and call-time rejection a **biconditional**, enforced in both
directions by `tests/mcp/lifecycle/test_call_time_kind_guard.py`: a narrowed tool must reject a
non-composed kind on the handler path, because `tools.invoke` passes raw arguments and bypasses
schema projection entirely. So the wrapper now calls
`assert_kind_composed(kind, resolver.registered_kinds())` before the handler, and
`kdive.mcp.tools.ops.resources.registrar.register` takes a `resolver` like the allocations and
systems registrars do.

This is the one deliberate behavior change in this refactor, and it is a narrow one: on a
deployment that composes every kind — the default — nothing changes. On a partially composed
deployment, registering an uncomposed kind now returns `configuration_error` instead of writing a
row for capacity that could never be provisioned. Accepting the schema narrowing without the guard
was not an option: it would have left the advertised enum a claim `tools.invoke` could ignore, and
would have reddened the existing ADR-0269 guard, which epic requirement 8 forbids waiving.

### 6. Retired names as `tools.search` vocabulary

Three rows are added to `RETIRED_TOOL_NAMES`:

```python
"resources.register_fault_inject": "resources.register",
"resources.register_local_libvirt": "resources.register",
"resources.register_remote_libvirt": "resources.register",
```

The mechanism is unchanged — the existing inversion into `_RETIRED_BY_REPLACEMENT` already groups
several retired names onto one replacement. This matters more here than for a rename: the provider
words (`remote libvirt`, `fault inject`) used to *be* the tool names, and after the consolidation
they survive only as `kind` enum values. `resources.register` also gains a `TOOL_KEYWORDS` entry
carrying registration and provider vocabulary, so an agent that describes the provider rather than
the verb still lands on the one tool.

## Consequences

- The `resources` namespace loses two tools; the live registry goes from 135 to 133.
- `kdivectl resources register-remote-libvirt` / `register-local-libvirt` /
  `register-fault-inject` become one `kdivectl resources register --kind <kind> …`.
- The audit `tool` column records `resources.register` for every kind; the kind survives in the
  recorded arguments, which already carried `kind`, `host_uri`, and `base_image`.
- The `denied()` breadcrumb emits `resources.register` as its `suggested_next_action`.
  `visible_next_actions` raises on an unregistered name, so the `exposure.py` key and the tool
  constant had to move together — they did.
- `resources.deregister`'s docstring no longer points at "the matching `resources.register_*`
  tool"; it names `resources.register` with the same kind and name. That text is agent-facing and
  is served at `docs/guide/reference/resources.md`.
- `resources.register` on a kind the deployment has not composed now returns
  `configuration_error` enumerating the available kinds, rather than registering unusable
  capacity. See decision 5.
- No schema migration: registration is a plain `INSERT` on the existing `resources` table.

## Rejected alternatives

- **The issue's `request=<discriminated union>`.** See decision 1 — it violates ADR-0372's
  repo-wide guard and generates a CLI verb with zero flags, a break no existing gate detects.
- **Keeping `host_uri` required and passing `fault-inject://local` for the synthetic kind.** That
  makes a server-owned constant a caller's responsibility and invites a caller to invent a
  different value that the insert would then persist.
- **Silently ignoring an inapplicable `base_image` / `host_uri`.** See decision 2 — it converts a
  caller mistake into a successful registration that does not do what was asked.
- **Narrowing the schema without the call-time guard.** See decision 5 — `tools.invoke` bypasses
  schema projection, so a narrowed-but-unguarded tool advertises a restriction it does not
  enforce, and ADR-0269's existing biconditional guard reddens.
- **Leaving `resources.register` out of `NARROWED_TOOLS` to avoid the guard.** That drops the
  per-provider narrowing the three separate tools had implicitly, so a local-only deployment would
  advertise `remote-libvirt` as a registerable kind.
- **Keeping the three names as aliases.** The project is pre-release and follows
  replace-don't-deprecate; an alias keeps three more names in the catalog, the RBAC matrix, the
  generated CLI, and the served docs for no capability.
