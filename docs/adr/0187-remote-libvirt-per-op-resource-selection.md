# ADR 0187 — Remote-libvirt per-op resource selection (de-singletoning, #395)

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** platform maintainers
- **Builds on (does not supersede):** [ADR-0076](0076-remote-libvirt-provider-package.md) (the
  opt-in provider package and deferred config read), [ADR-0112](0112-systems-inventory-config.md)
  (the `systems.toml` inventory and `(kind, name)` resource identity the per-op resolution keys
  off), [ADR-0095](0095-reconciler-remote-console-collector.md) (the single-leader console
  hosting loop that must now resolve config per system), [ADR-0186](0186-pool-selection-axis.md)
  (the pool selection axis that makes multiple interchangeable remote hosts useful — the consumer
  of this primitive).
- **Spec:** [`../superpowers/specs/2026-06-19-system-pools-design.md`](../superpowers/specs/2026-06-19-system-pools-design.md)

## Context

The remote-libvirt provider is hard-singleton in two independent places:

1. **Inventory parse** — `inventory/model.py` `_check_remote_libvirt_singleton` rejects more than
   one `[[remote_libvirt]]` block.
2. **Per-op connection resolution** — `providers/remote_libvirt/config.py` `_resolve_instance` /
   `_require_single_instance` fail closed on more than one declared instance, because **the per-op
   call path carries no resource identity**, so it cannot pick which host to talk to. Failing
   closed there is safer than dispatching an op to the wrong host.

The connection identity (URI, TLS cert refs, `gdb_addr`, gdbstub port range, per-host cap) is
resolved per op from *the one* declared instance, not from the resource the allocation actually
granted. So even though `resources.register_remote_libvirt` will write multiple catalog rows,
provisioning/build/install/boot still resolve the lone instance — a second registered remote
resource has a catalog row with no usable connection config, and ops could be dispatched to the
wrong host. This is the gap #395 names "allocation → resource → instance threading is future
work," and it blocks pooling interchangeable remote hosts (ADR-0186) for the remote-libvirt
provider specifically.

Two facts make the fix tractable. First, the reconcile writes `resource.name =
[[remote_libvirt]].name` (config-owned rows are keyed on `(kind, name)`, ADR-0112), so a remote
resource row's `name` *is* its inventory instance name — the natural resolution key. Second,
~16 per-op modules already take a `config_factory: Callable[[], RemoteLibvirtConfig] =
remote_config_from_inventory` dependency-injection seam; the threading reuses it rather than
rewiring call paths.

## Decision

Thread the **granted resource identity through the per-op call path** so the remote-libvirt
connection config is resolved for the *allocated* host, and remove the singleton guards that
existed only because that threading was unwired.

**Resolve config by resource name.** Add `remote_config_for_resource(resource_name)` to
`providers/remote_libvirt/config.py`: it loads the declared instances and selects the one whose
`name == resource_name`, applying the same URI and gdbstub-range validation to the selected
instance. Zero matches → `CONFIGURATION_ERROR` naming the missing instance. The per-op DI seam
becomes `config_factory: Callable[[str], RemoteLibvirtConfig] = remote_config_for_resource`
(keyed by the resource name), so every module stays unit-testable with an injected fake.

**Carry the identity from the System.** A per-op job operates on a `System`, and `System →
Allocation → Resource` is a persisted chain. The worker job handler resolves the bound resource's
`name` and passes it to the provider entry point, which builds the config for that host. Build-VM
/ ephemeral-build paths key off the `BuildHost` resource name the same way.

**Remove the singleton guards.** `_check_remote_libvirt_singleton` is removed (instance-name
uniqueness is already required by the `(kind, name)` reconcile identity); `_resolve_instance` /
`_require_single_instance` are replaced by the by-name selection. Discovery enumerates all
declared instances; the reconcile (already the sole creator, already iterating
`doc.remote_libvirt`) registers one resource per instance once the parser admits multiple.

**Console hosting resolves per system.** `build_console_hosting` runs one leader loop hosting all
running systems and today opens every console with one `remote_config`. The console collector
factory changes to resolve config **per system** — inside `factory(system_id)` it looks up the
system's bound resource name and calls `remote_config_for_resource(name)` to open that system's
console on its own host. This is the one caller that moves from resolve-once-at-bootstrap to
resolve-per-system.

**No caller resolves "an arbitrary single instance."** Several remote callers have no System yet
today depend on the singleton: the reconciler sweeps (`transport_reset.py` dead-worker gdbstub
re-arm, `reaping/connections.py` port reaping) and the doctor diagnostics
(`reachability`/`base_image_staging`/`contribution`/`gdbstub_acl`). The sweeps resolve **per
domain → System → Resource** (a domain-less reap enumerates all hosts); the diagnostics **fan out
per declared instance**. To serve the genuinely fleet-wide callers, add `all_remote_configs()`
(validates and returns every instance). `remote_config_from_inventory()` is **deleted** — every
site moves to `remote_config_for_resource(name)` or `all_remote_configs()`, so a dispatch with N
hosts can never silently hit the wrong host. Discovery and the console-hosting bootstrap keep a
no-identity entry point, but via `all_remote_configs()` / per-instance resolution.

## Consequences

- Multiple `[[remote_libvirt]]` instances can be declared, registered, and operated; an op
  dispatches to the host its allocation granted, resolved from the persisted resource identity.
  This unblocks pooling remote hosts (ADR-0186) for the three interchangeable hosts that motivated
  #561.
- The failure mode flips from "fails closed on >1 instance" to "fails closed on an *unknown*
  resource name" — a dispatch can never silently hit the wrong host, preserving the original
  safety property while removing the single-host ceiling.
- The change is mostly a seam-signature change (`() -> config` becomes `(name) -> config`) plus
  resolving the resource name at the worker dispatch boundary; no schema or migration change (the
  `resources` row and the `System → Allocation → Resource` chain already exist).
- The console factory parses `systems.toml` per console open. Accepted — console opens are
  infrequent and the document is small; if it shows up in practice, cache the parsed doc per sweep
  (noted, not pre-optimized).

## Alternatives considered

- **Add an explicit `remote_instance_name` column to `resources`.** Redundant — the reconcile
  already sets `resource.name` to the instance name as the `(kind, name)` identity; matching on
  `name` needs no new column.
- **Keep the singleton and require one remote host per deployment.** Rejected — it is exactly the
  ceiling #561/#395 remove, and it makes interchangeable remote capacity unrepresentable.
- **Resolve config from a process-wide registry built at startup.** Would re-introduce a
  resolve-once cache that goes stale when the reconcile re-assigns instances; rejected in favor of
  resolving per op from the current inventory keyed by the resource name (the config read is
  already deferred per ADR-0076).
- **Retain `remote_config_from_inventory()` for the no-System callers (reaper/diagnostics).**
  With N instances it would resolve an arbitrary single host, silently resetting the wrong
  gdbstub / reaping the wrong host / reporting one host's health as the fleet's. Rejected —
  delete it and force every no-System caller onto `all_remote_configs()` (fan-out) or a
  per-domain→resource resolution.
- **Thread a full `RemoteLibvirtConfig` object through the job payload.** Serializes connection
  identity (including secret refs) into the queue row; rejected — pass the resource *name* and
  resolve at the worker boundary where secrets already resolve.
