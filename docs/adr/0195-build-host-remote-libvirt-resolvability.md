# ADR 0195 — Surface build-host remote-libvirt resolvability

- **Status:** Accepted
- **Date:** 2026-06-20
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0187](0187-remote-libvirt-per-op-resource-selection.md)
  (the build worker resolves a `[[remote_libvirt]]` instance by build-host name),
  [ADR-0112](0112-systems-inventory-config.md) (the `systems.toml` inventory + fault-isolation
  posture), [ADR-0160](0160-buildhost-source-kind-discovery.md) (the `runs.profile_examples` /
  `build_hosts.list` discovery surfaces), [ADR-0103](0103-build-host-reachability-probe.md)
  (the distinct SSH-reachability `state`).
- **Spec:** [`../superpowers/specs/2026-06-20-build-host-resolvability-surfacing.md`](../archive/superpowers/specs/2026-06-20-build-host-resolvability-surfacing.md)
- **Related:** #626 (this), #618 (RUN_REVIEW.md follow-ups).

## Context

An `ephemeral_libvirt` build host provisions its throwaway build VM on a remote
libvirt host. The build worker resolves that host's connection config **by the build
host's name**: `ephemeral_build_session(resource_name=host.name)` calls
`remote_config_for_resource(host.name)`, which looks up the `[[remote_libvirt]]`
instance whose `name == host.name` in `systems.toml` (ADR-0187). If no instance with
that name is declared, the build fails at execution time with:

> no [[remote_libvirt]] instance named 'ub24-big-build' is declared in systems.toml

Two read surfaces advertise build hosts without checking this resolution
(`RUN_REVIEW.md` D2, #626):

- `runs.profile_examples` emits one ready-to-edit build profile per registered host,
  presenting it as usable. An `ephemeral_libvirt` host whose name has no backing
  `[[remote_libvirt]]` instance is advertised as ready but fails at build time.
- `build_hosts.list` returns each host's `state` (the ADR-0103 SSH reachability
  probe, `ready`/`unreachable`) but nothing that reflects whether an
  `ephemeral_libvirt` host's name resolves to a declared instance.

`state` does not cover this: the reachability probe only checks `kind='ssh'` hosts
(`list_probeable_ssh_hosts`), so an `ephemeral_libvirt` host is always `ready`
regardless of whether its backing instance exists. Resolvability and reachability are
distinct facts and need distinct surfacing.

Which kinds need a backing instance:

- `local` (`worker-local`) builds on the worker itself — no remote host, always
  resolves.
- `ssh` connects to its own `address`/`ssh_credential_ref` and never reads
  `[[remote_libvirt]]` — always resolves (its address reachability is the `state`
  probe's job, ADR-0103).
- `ephemeral_libvirt` is the only kind that resolves a `[[remote_libvirt]]` instance
  by name, so it is the only kind that can fail to resolve.

## Decision

Add a single shared predicate and surface it on both read tools; do not change the
build admission or execution path.

### One source of truth for "resolves"

Add `build_host_resolves(host_kind, host_name, declared_instances)` to
`services/runs/build_host_selection.py` (alongside `accepted_source_kinds`, the
existing host-kind matrix single-source-of-truth):

- `LOCAL` / `SSH` → always `True` (no `[[remote_libvirt]]` dependency).
- `EPHEMERAL_LIBVIRT` → `True` iff `host_name in declared_instances`.

`declared_instances` is the set of `[[remote_libvirt]]` instance names, passed in by
the caller (the predicate stays inventory-free and trivially testable). Callers obtain
it from `remote_instance_names()` (ADR-0187), which parses but does not
connection-validate.

### `runs.profile_examples`: filter out unresolvable hosts

The registrar resolves the declared instance-name set once per call and passes it to
`build_host_profile_examples(hosts, declared_instances)`, which omits any host that
does not resolve. The tool's contract — "every emitted example is ready to edit and
build" — then holds: a non-resolving `ephemeral_libvirt` host produces no example
rather than a broken one. (`local`/`ssh` examples are unaffected.)

### `build_hosts.list`: add a `resolves` field

Each item's `data` gains `resolves` (`"true"`/`"false"`, matching the tool's existing
string-scalar convention) computed from the same predicate over the same declared set.
An operator listing build hosts sees a non-resolving `ephemeral_libvirt` host as
visibly not-ready. `state` is unchanged (it remains the SSH reachability probe).

### Fault isolation

Resolving the declared instance set **degrades** rather than raises: a missing
`systems.toml` is the normal pre-config state, and a present-but-malformed file is
treated as "no instances declared" for these two read tools (the same fault-isolation
posture as `is_remote_libvirt_configured`, ADR-0112). The consequence is conservative:
when the inventory cannot be read, every `ephemeral_libvirt` host shows
`resolves=false` / is omitted from examples — which is exactly the not-ready signal an
operator needs, and it never crashes a read tool on a bad operator edit. The precise
parse error still surfaces fail-closed at build time via
`remote_config_for_resource`.

## Consequences

- The two surfaces no longer advertise an `ephemeral_libvirt` build host that cannot
  build. The fix is read-only: no schema, no migration, no change to
  `resolve_and_admit` or the build worker.
- A new public field (`resolves`) appears on `build_hosts.list` items. It is additive
  under ADR-0113's flat outputSchema, so no outputSchema change is required.
- The predicate and both surfaces derive from `remote_instance_names()`, so the
  advertised resolvability tracks the same inventory the build path resolves against —
  no second copy of the rule.
- A non-resolving host is still selectable by `runs.build` (admission is unchanged);
  it simply fails at build time as before. Surfacing resolvability is a discovery
  improvement, not a new admission gate — closing the admission gap is out of scope for
  this read-only change (see Considered & rejected).

## Considered & rejected

- **Reject the host at `runs.build` admission (`resolve_and_admit`).** Tempting, but
  it changes the build admission contract and the failure category/timing for an
  existing path, widening scope well beyond the two read surfaces #626 names. The
  build-time error is already specific (`CONFIGURATION_ERROR` naming the missing
  instance). Surfacing resolvability on the discovery tools is the minimal fix; an
  admission gate can be its own ADR if wanted.
- **Reuse `BuildHostState` (`ready`/`unreachable`) for resolvability.** `state` is the
  ADR-0103 SSH-reachability probe with its own compare-and-swap write path; overloading
  it would conflate two independent facts (an `ephemeral_libvirt` host is never probed)
  and require a DB write to reflect a `systems.toml` edit. `resolves` is a derived,
  read-time projection — no column, no probe.
- **Mark the example unusable instead of omitting it.** The acceptance allows either.
  Omitting keeps the existing tool invariant ("every emitted example parses and is
  buildable for its host") intact and avoids inventing a per-item "unusable" flag the
  schema and tests would have to carry. `build_hosts.list` is where an operator sees
  the full roster including non-resolving hosts.
- **Resolve the inventory inside the pure `build_host_profile_examples` helper.** Keeps
  the helper pure and unit-testable by passing the declared set in; the registrar (which
  already opens a connection) does the one inventory read.
