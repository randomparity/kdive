# Implementation plan — local-libvirt discovery opt-out (#468)

Backs ADR-0131 / spec `2026-06-16-local-libvirt-discovery-opt-out.md`. TDD, one logical
change per commit.

## Files touched

- `src/kdive/providers/assembly/composition.py` — gate the `LOCAL_LIBVIRT` descriptor,
  thread `enable_local_libvirt` through `_runtime_descriptors`,
  `_enabled_runtime_descriptors`, `ProviderComposition.build_provider_resolver`, and the
  module-level `build_provider_resolver`.
- `src/kdive/providers/core/resolver.py` — drop the empty-map `ValueError`; add the one-line
  `INFO` in `register_all_discovery` naming the registered kinds.
- `src/kdive/config/core_settings.py` — broaden `LOCAL_LIBVIRT_ENABLED.help`.
- `docs/guide/reference/config.md` — regenerate (via the config-docs generator the
  `config-docs-check` recipe runs).
- Tests: `tests/providers/test_composition.py`, `tests/providers/test_resolver.py`,
  `tests/reconciler/test_main.py` (if it asserts resolver shape), and a bootstrap test for
  `register_discovered_resources` under the flag.

No DB migration. No deploy/helm change (the chart already sets
`KDIVE_LOCAL_LIBVIRT_ENABLED: "false"`).

## Steps

1. **Failing tests first.** Add to `tests/providers/test_composition.py`:
   - `build_provider_resolver(enable_local_libvirt=False)` (with remote configured) →
     `registered_kinds()` excludes `LOCAL_LIBVIRT`, includes `REMOTE_LIBVIRT`.
   - `KDIVE_LOCAL_LIBVIRT_ENABLED=false` env → default resolver excludes `LOCAL_LIBVIRT`.
   - A composition discovery test: compose a resolver with local disabled and assert the
     local-libvirt discovery registrar is **not composed** (probe the local `target_factory`
     / `LocalLibvirtDiscovery` is never constructed). Note: production remote-libvirt
     discovery is `creates=False` (bind-only — `_discovery_registrar` early-returns before any
     connect, ADR-0112), so it can never be the "failing sibling"; the composition layer only
     proves non-composition of the local registrar, not raise-isolation.
   - The "a failing sibling does not abort the others" property is asserted in
     `tests/providers/test_resolver.py` with synthetic `creates=True` runtimes (`_Runtime` /
     `_FailingRuntime`), where a sibling can actually raise — not at the composition layer.
   In `tests/providers/test_resolver.py`: replace `test_empty_resolver_is_rejected` with
   `test_empty_resolver_is_allowed_and_fails_closed_at_resolve` — `ProviderResolver({})`
   constructs, `register_all_discovery` is a no-op, `resolve(<any kind>)` raises
     `configuration_error`. Add a bootstrap test that `register_discovered_resources` on a
     local-disabled composition does not call the local-libvirt discovery.
   Confirm they fail for the right reason (the gate/guard not yet changed).

2. **Resolver change.** Drop the constructor `ValueError`; add the `INFO` line in
   `register_all_discovery`. Any test on the line asserts sorted kind-value membership, not
   the exact rendered string (mirrors the resolver's own `sorted(...)` in its
   `configuration_error` details). Green the resolver tests.

3. **Composition gate.** Thread `enable_local_libvirt` and change the `LOCAL_LIBVIRT`
   descriptor's `enabled`. Green the composition tests.

4. **Config help text + regen.** Broaden the help; run the config-docs generator so
   `docs/guide/reference/config.md` matches. Verify `just config-docs-check`/`config-guard`.

5. **Guardrails.** `just lint`, `just type`, `just test`, then full `just ci`. Regenerate any
   other generated doc the change invalidates (tool reference is untouched — no tool change).

## Risks / watch-items

- The empty-resolver relaxation must not regress the fail-closed `resolve` behavior — keep
  the existing `resolve` test.
- `register_all_discovery`'s `INFO` line must not log secrets (it logs only `kind.value`
  strings — no connection data).
- Other resolver call sites (`mcp/assembly/app.py`, `build_handler_registry`,
  `register_discovered_resources`) call `build_provider_resolver()` with no args, so they inherit
  the env-driven default — no signature break.
