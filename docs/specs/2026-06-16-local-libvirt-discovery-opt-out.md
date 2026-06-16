# Local-libvirt startup discovery opt-out (#468)

Design note for ADR-0131. Follow-on to ADR-0127 / #459.

## Problem

`KDIVE_LOCAL_LIBVIRT_ENABLED=false` gates the reconciler's periodic local-libvirt
leaked-domain reaper (ADR-0127) but not the one-time startup provider-discovery
registration. On a remote-libvirt-only k8s deploy with the flag false, the reconciler
still composes the local-libvirt runtime into its `ProviderResolver`, and
`register_all_discovery` runs the local-libvirt discovery registrar, which connects to a
non-existent `/var/run/libvirt/libvirt-sock` and emits a single startup `WARN`.

Root cause: the `LOCAL_LIBVIRT` entry in
`providers/assembly/composition.py::_runtime_descriptors` is `enabled=lambda: True` —
hardcoded on, ignoring the flag that the reaper factory (`_reconciler_reaper_factories`,
gated on `_local_libvirt_enabled`) already honors.

## Change

1. Thread `enable_local_libvirt: bool | None` through `_runtime_descriptors`,
   `_enabled_runtime_descriptors`, and both `build_provider_resolver` entrypoints (the
   `ProviderComposition` method and the module-level function). The `LOCAL_LIBVIRT`
   descriptor's `enabled` becomes `lambda: _local_libvirt_enabled(enable_local_libvirt)`.
   Convention matches the existing `enable_fault_inject` / `enable_remote_libvirt` knobs:
   explicit flag wins, else env, default on.

2. Relax `ProviderResolver.__init__` to accept an empty runtime map (drop the `ValueError`
   guard). With local now gateable, `local off + remote unconfigured + fault off` yields an
   empty set. The resolver already fails closed at resolution time
   (`resolve` → `configuration_error` on any unregistered kind), so an empty resolver fails
   every resolution with that actionable error instead of crashing startup with an opaque
   `ValueError`; `register_all_discovery` over an empty set is a no-op. Because that case is
   now silent-until-first-request (no eager startup error), `register_all_discovery` emits a
   single `INFO` line naming the registered kinds (or stating none were composed), so an
   operator who disabled every provider can see it in the reconciler log rather than only
   discovering it when `allocations.request` finds no Resource.

3. Broaden the `LOCAL_LIBVIRT_ENABLED` setting help text — it now gates discovery
   registration and the resolver's local-libvirt runtime across every process (migrate,
   reconciler, server, worker), not only the reconciler reaper — and regenerate the config
   reference (`docs/guide/reference/config.md`). `just ci`'s `config-docs-check` /
   `config-guard` recipes fail if the help text and the generated doc drift apart, so both
   move together.

No change to `register_all_discovery`'s attempt-all-then-raise-first behavior: it already
isolates per-runtime faults and registers remote-libvirt regardless of iteration order. The
fix is the gate, not a re-raise change (which would weaken
`admin/bootstrap.register_local_resource`'s fail-fast contract).

## Scope of the gate

The descriptor gate feeds every `build_provider_resolver` caller: reconciler
(`__main__`), server (`mcp/app.py`), worker (`build_handler_registry`), and bootstrap
(`admin/bootstrap.register_local_resource`). This is intentional — disabling local-libvirt
declares the deployment has no local libvirt, so no plane should serve a local-libvirt
System. A local-libvirt System on a local-disabled deploy now fails its post-System ops
with `configuration_error` instead of a deep libvirt socket error. The default (flag absent
or `true`) composes local-libvirt into every resolver exactly as today.

`register_local_resource` (the `migrate`-time bootstrap step) is itself a
`build_provider_resolver().register_all_discovery(pool)` call. With the flag false it
registers **no** local-libvirt resource — only whatever other providers are enabled
(remote-libvirt when configured). That is the correct k8s behavior (the migrate Job has no
local libvirt socket), but the name now means "register this deployment's discoverable
resources," not "always create a local-libvirt row." Renaming it is out of scope for #468;
the behavior is captured here and asserted by a test.

## Tests

- `build_provider_resolver(enable_local_libvirt=False)` → `registered_kinds()` excludes
  `LOCAL_LIBVIRT`.
- `register_all_discovery` on a local-disabled resolver composes/attempts no local-libvirt
  registrar (assert via a probe that local discovery is never invoked) while a sibling
  remote-libvirt registrar still runs and a failing sibling does not abort the others.
- `ProviderResolver({})` constructs and `register_all_discovery` is a no-op; `resolve`
  still raises `configuration_error`.
- Env-driven gate: `KDIVE_LOCAL_LIBVIRT_ENABLED=false` excludes local from the default
  resolver.
- `register_local_resource` on a local-disabled composition invokes no local-libvirt
  discovery registrar.

## Acceptance (from #468)

- `KDIVE_LOCAL_LIBVIRT_ENABLED=false` → reconciler startup logs zero local-libvirt socket
  errors (no local-libvirt discovery registrar composed).
- Remote-libvirt discovery still registers regardless of compose/iteration order.
