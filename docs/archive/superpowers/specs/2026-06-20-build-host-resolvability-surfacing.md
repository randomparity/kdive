# Spec: Surface build-host remote-libvirt resolvability (#626)

ADR: [0195](../../adr/0195-build-host-remote-libvirt-resolvability.md)

## Problem

`runs.profile_examples` and `build_hosts.list` advertise registered build hosts
without checking whether an `ephemeral_libvirt` host's name resolves to a declared
`[[remote_libvirt]]` instance in `systems.toml`. The build worker resolves that
instance by the build host's name at execution time
(`ephemeral_build_session(resource_name=host.name)` →
`remote_config_for_resource(host.name)`), so a host with no backing instance is
advertised as ready but fails at build time:

> no [[remote_libvirt]] instance named 'ub24-big-build' is declared in systems.toml

## Goal

The two read surfaces stop advertising an unbuildable `ephemeral_libvirt` host:

- `runs.profile_examples` does not emit an example for a build host that does not
  resolve.
- `build_hosts.list` exposes, per host, whether it resolves.

Read-only: no schema, no migration, no change to build admission or execution.

## Resolution rule (single source of truth)

`build_host_resolves(host_kind, host_name, declared_instances) -> bool` in
`src/kdive/services/runs/build_host_selection.py`:

| host kind           | resolves                                  |
|---------------------|-------------------------------------------|
| `local`             | always `True`                             |
| `ssh`               | always `True`                             |
| `ephemeral_libvirt` | `True` iff `host_name in declared_instances` |

`declared_instances` is a collection of `[[remote_libvirt]]` instance names. The
predicate is inventory-free; callers pass the set in. Production callers obtain it from
`remote_instance_names()` (ADR-0187).

Rationale for the matrix: `local` builds on the worker; `ssh` connects to its own
`address`/`ssh_credential_ref`; only `ephemeral_libvirt` resolves a
`[[remote_libvirt]]` instance by name (ADR-0187).

## Surface changes

### `runs.profile_examples`

- `build_host_profile_examples(hosts, declared_instances)` gains a second parameter
  and omits any host where `build_host_resolves(...) is False`.
- The registrar (`mcp/tools/lifecycle/runs/registrar.py`) resolves the declared set
  once per call via `_declared_remote_instances()` and passes it in.

### `build_hosts.list`

- Each item's `data` gains `"resolves"` = `"true"`/`"false"` (string scalar, matching
  the existing `enabled`/`max_concurrent` convention on this tool).
- The handler resolves the declared set once before building items.
- `state` is unchanged.

## Fault isolation

Resolving the declared instance set must not raise out of either read tool. A missing
`systems.toml` yields an empty set; a present-but-malformed file is caught and treated
as an empty set (the `is_remote_libvirt_configured` posture, ADR-0112). Consequence:
when the inventory is unreadable, every `ephemeral_libvirt` host shows
`resolves=false` / is omitted — the correct not-ready signal — and no read tool crashes
on a bad operator edit. The precise parse error still surfaces fail-closed at build
time.

A shared helper `declared_remote_instance_names()` (degrade-to-empty wrapper over
`remote_instance_names()`) lives next to the predicate so both surfaces share the same
degrade behavior.

## Success criteria (falsifiable)

1. `build_host_resolves` returns `True` for `local` and `ssh` regardless of
   `declared_instances`; for `ephemeral_libvirt` returns `True` only when the name is
   in the set.
2. `build_host_profile_examples` with an `ephemeral_libvirt` host **not** in
   `declared_instances` emits no item for that host; the same host **in** the set emits
   one item. `local`/`ssh` items are present regardless.
3. `build_hosts.list` item `data["resolves"]` is `"false"` for an `ephemeral_libvirt`
   host with no backing instance and `"true"` when one is declared; `"true"` for
   `local`/`ssh` hosts.
4. `declared_remote_instance_names()` returns an empty list (does not raise) when
   `systems.toml` is absent and when it is present-but-malformed.
5. Existing `runs.profile_examples` and `build_hosts.list` contracts (validity,
   source-kind agreement, auth/read-only, redaction of credential refs) still hold.

## Edge cases

- Empty host list → empty examples collection (unchanged) and empty `build_hosts.list`
  collection.
- No `systems.toml` → empty declared set → `ephemeral_libvirt` hosts omitted /
  `resolves=false`; `local`/`ssh` unaffected.
- Malformed `systems.toml` → same as absent (degrade), no crash.
- An `ephemeral_libvirt` host whose name matches a declared instance → emitted /
  `resolves=true`.

## Out of scope

- Gating `runs.build` admission on resolvability (ADR-0195 rejected; build-time error
  is already specific).
- Changing `BuildHostState`/the reachability probe.
- SSH-host or `[[remote_libvirt]]` connection validation.
