# ADR 0131 — Local-libvirt startup discovery opt-out

- **Status:** Accepted
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

ADR-0127 (#459) introduced `KDIVE_LOCAL_LIBVIRT_ENABLED` (default `true`) and gated the
reconciler's periodic local-libvirt **leaked-domain reaper** on it, so a remote-libvirt-only
Kubernetes deployment stops flooding the log with per-pass `libvirtError: Failed to connect
socket` tracebacks.

That gate does **not** cover the one-time **startup provider-discovery registration**. The
local-libvirt runtime descriptor in `providers/assembly/composition.py`
(`_runtime_descriptors`) carries `enabled=lambda: True` — hardcoded on, ignoring the flag.
So `build_provider_resolver` always composes the local-libvirt runtime (and its discovery
registrar) into the `ProviderResolver`, and the reconciler's startup
`register_all_discovery` then calls the local-libvirt discovery registrar, which opens a
connection to `/var/run/libvirt/libvirt-sock`. On a k8s pod that socket does not exist, so a
single non-fatal `WARN` is emitted at startup even with `KDIVE_LOCAL_LIBVIRT_ENABLED=false`
(observed on `kdive-demo`, `sha-6898353`).

Two facts shape the fix:

- `register_all_discovery` already **isolates per-runtime failures**: it iterates every
  composed runtime, logs each registration fault, and re-raises only the first after every
  runtime has been attempted (`providers/core/resolver.py`). So remote-libvirt discovery
  already registers regardless of iteration order — the issue's "raise first_failure can
  abort siblings" describes a behavior that was already fixed. The re-raise's only remaining
  effect on the reconciler is to trigger the outer best-effort `WARN` in
  `__main__._register_provider_resources` (which catches and logs, never crashes).
- The local-libvirt runtime descriptor is the single composition point that feeds **both**
  the resolver (used by server/worker post-System ops) and the discovery set (used by the
  reconciler). Gating the descriptor itself fixes discovery at the root instead of papering
  over it at the discovery layer.

## Decision

Gate the local-libvirt runtime descriptor on the existing `_local_libvirt_enabled` helper,
the same flag that already gates the local reaper (ADR-0127):

- `_runtime_descriptors` / `_enabled_runtime_descriptors` take an `enable_local_libvirt`
  parameter (explicit flag wins, else env, default on — the established convention), and the
  `LOCAL_LIBVIRT` descriptor's `enabled` becomes
  `lambda: _local_libvirt_enabled(enable_local_libvirt)` instead of `lambda: True`.
- `build_provider_resolver` (both the `ProviderComposition` method and the module function)
  threads `enable_local_libvirt` through. When local is disabled, the local-libvirt runtime
  is not composed into the resolver, so `register_all_discovery` has no local-libvirt
  registrar to run and never touches the missing socket.

Relax the `ProviderResolver` non-empty invariant. With local gateable, a deployment that
disables local-libvirt **and** has no remote config **and** leaves fault-inject off would
produce an empty runtime set. Today `ProviderResolver.__init__` raises `ValueError` on an
empty map (it could never happen while local was always-on). An empty resolver is a valid
"no providers configured" state for a misconfigured deployment; it must not crash startup
with an opaque `ValueError`. The resolver already **fails closed at resolution** — `resolve`
raises `configuration_error` for any unregistered kind — so an empty resolver simply fails
every resolution with that same actionable error, and `register_all_discovery` over an empty
set is a no-op. Drop the constructor guard. Because dropping the guard makes the
zero-provider case silent at startup (no eager error), `register_all_discovery` logs a single
`INFO` line naming the registered kinds — or stating that none were composed — so a
fully-disabled deployment is visible in the reconciler log instead of only surfacing when the
first `allocations.request` finds no Resource.

Broaden the `KDIVE_LOCAL_LIBVIRT_ENABLED` help text and regenerate the config reference. The
flag previously documented itself as gating only "the local-libvirt reconciler reaper"; it
now also gates discovery registration and the resolver's local-libvirt runtime in every
process. The generated config reference (`docs/guide/reference/config.md`) is CI-checked
(`config-docs-check`/`config-guard`), so the help text and the doc move together.

Keep `register_all_discovery`'s attempt-all-then-raise-first behavior as is. It already
satisfies "remote-libvirt discovery registers regardless of compose/iteration order," and
the reconciler's caller is already best-effort. Changing it to never raise would silently
weaken `admin/bootstrap.register_local_resource`, whose contract is to surface a local
registration failure to the operator running `migrate`. The gate, not a re-raise change, is
the correct fix for #468.

## Consequences

- With `KDIVE_LOCAL_LIBVIRT_ENABLED=false`, the reconciler composes **no** local-libvirt
  discovery registrar; startup logs zero local-libvirt socket errors. Remote-libvirt
  discovery still registers (it is a separately-enabled descriptor, isolated in
  `register_all_discovery`).
- The gate applies uniformly to the resolver used by the **server and worker** too, not just
  the reconciler. This is intentional and consistent: an operator who disables local-libvirt
  is declaring the deployment has no local libvirt, so no plane should serve a local-libvirt
  System. A local-libvirt System on such a deployment now fails its post-System ops with the
  resolver's `configuration_error` ("no provider runtime is registered for resource kind
  'local-libvirt'") rather than a deep libvirt socket error.
- Default behavior is unchanged: with the flag absent or `true`, the local-libvirt runtime is
  composed into every resolver exactly as before.
- The `migrate`-time `admin/bootstrap.register_local_resource` step is a
  `build_provider_resolver().register_all_discovery(pool)` call, so with the flag false it
  registers no local-libvirt resource (only other enabled providers). This is correct for k8s
  (the migrate Job has no local libvirt socket); the function name now means "register this
  deployment's discoverable resources." Renaming is out of scope; the behavior is tested.
- A zero-provider deployment (every provider disabled) is no longer a startup crash; it is a
  `WARN` from the `ProviderResolver` constructor (so the server and worker tiers, whose
  readiness probes do not inspect provider composition, surface it at startup), an `INFO` line
  from `register_all_discovery` on the discovery path, and a resolver that fails every
  resolution with `configuration_error`.
- `ProviderResolver` accepts an empty runtime map. The single existing constructor-guard test
  is removed; the resolve-time fail-closed behavior (the real safety property) is retained
  and tested.

## Alternatives considered

- **Gate only the discovery registrar (skip the local registrar inside
  `register_all_discovery`) and leave the local runtime in the resolver**: fixes the WARN but
  leaves the resolver inconsistent — a deployment with "no local libvirt" would still serve
  local-libvirt Systems through server/worker ops that then fail deep in the libvirt layer.
  Gating the descriptor is the single-source fix; gating only discovery treats the symptom.
- **Make `register_all_discovery` swallow all failures (never re-raise)**: would also silence
  the reconciler WARN, but silently weakens the bootstrap/migrate path's fail-fast contract
  and does nothing for the resolver inconsistency above. The re-raise is already isolated
  (attempt-all-first), so it is not the defect. Rejected.
- **Keep the `ProviderResolver` non-empty guard and synthesize a guaranteed provider**:
  forcing a phantom provider to keep the map non-empty adds a fictional capability. Allowing
  empty + fail-closed-at-resolve is simpler and already the resolver's error model. Rejected.
- **Probe the libvirt socket at composition time**: couples assembly to host state and races
  a `libvirtd` that starts later (rejected identically in ADR-0127). The declarative flag is
  the right seam. Rejected.
