# Fault-inject runtime follows the resource row's capabilities (#3716)

Decision record: [ADR-0074](../../adr/0074-fault-inject-engine-port-wiring.md), amended by this
change. Builds on ADR-0071 (per-kind registry) and ADR-0823 (hot-plug). Follow-up: #3715.

## Problem

The registry builds one runtime per kind (ADR-0071), but `fault_rate`, `max_latency_s`, `seed`
and `hotplug_behaviour` live on each resource row. `build_runtime` is called without an engine
or behaviour, so setting those keys has no effect outside tests.

## Scope

In: an optional capabilities-aware rebind hook, the resolver reading `r.capabilities`, the
fault-inject composition using it, an ADR-0074 amendment, stale comments, tests.
Out: the per-call hot-plug fault draw (`FaultedHotplug` attempt stays 1, #3715); other providers;
mcp/jobs call sites (signatures unchanged).

Ownership: the resolver owns reading the row (it already holds the connection); the fault-inject
composition owns turning capabilities into ports. No other transition.

## Contract

- `ResourceBindingCapabilities` gains `rebind_with_capabilities: Callable[[str, Mapping[str,
  object]], ProviderRuntime] | None = None`.
- `ProviderRuntime.for_resource(name, capabilities=None)`: with a non-None `capabilities` and a
  hook set, calls the hook; otherwise behaves as before.
- The four resolver lookups select `r.capabilities` and pass it to `for_resource`.
- Fault-inject sets the hook. For a row it builds `FaultEngine.from_capabilities` and
  `HotplugBehaviour.from_capabilities`; the faulted wrappers are installed only when the row
  carries a non-empty `fault_rate` or `max_latency_s` (ADR-0074); the hot-plug port takes the
  row's behaviour.
- Stateful ports (inventory, hot-plug device ledger, external-boot state) are built once per
  kind and shared by every per-resource runtime, so a binding built per operation sees the same
  devices and domains. Only the stateless fault wrappers and the hot-plug behaviour vary.
- A row with only `seed` draws no fault: the engine is built when `fault_rate` or `max_latency_s`
  is non-empty (ADR-0074), so `seed` matters only alongside one of them. Values passed to
  `build_runtime` (`engine`, `hotplug_behaviour`) are defaults a row without the keys inherits.
- Invalid capabilities raise `CategorizedError(CONFIGURATION_ERROR)` at binding.

## Failure model

- Row edited while a System runs: the next operation's binding reads the new values; ledger state
  is unaffected because it is shared.
- Malformed row capabilities: binding fails closed with `configuration_error`; no port is built.
- A malformed row fails every binding for its Systems, teardown included. Accepted (operator
  decision: fail closed); validating the keys where a row is written is a follow-up candidate.
- Provider other than fault-inject: hook is `None`; `for_resource(name, capabilities)` falls back
  to the name-only rebind, so behaviour is unchanged (tested).
- `seed` differs between two fault-inject rows: each binding draws from its own engine.

## Tests

Registry-built acceptance tests (resolver + `binding_for_system`) for fail draw, latency,
hot-plug behaviour, absent keys, invalid keys; unit test that a hook-less runtime ignores
capabilities; resolver tests that each lookup passes `capabilities`; state-sharing test across
two bindings.
