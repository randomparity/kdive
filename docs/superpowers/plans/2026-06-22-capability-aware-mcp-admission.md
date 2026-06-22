# Implementation plan — A2: capability-aware MCP admission + profile-resolved vmcore.fetch default

- **Issue:** #674 (M2.8 Epic A, sub A2)
- **Spec:** [`../specs/2026-06-22-local-libvirt-service-parity-honesty.md`](../specs/2026-06-22-local-libvirt-service-parity-honesty.md)
- **ADR:** [ADR-0209](../../adr/0209-capability-aware-mcp-admission.md) (Accepted, on main) — no new ADR, no migration.
- **Depends on:** #672 (A1 descriptor) — MERGED on main (`supported_debug_transports`,
  `supported_introspection`, fail-closed `supported_capture_methods`).

The design is settled in ADR-0209 / the spec. This plan only sequences the code.

## What changes (and what does not)

Four tool handlers consult the bound `ProviderRuntime` ADR-0208 descriptor and reject an
unsupported plane/method **before** enqueue/execution, with a `CONFIGURATION_ERROR` carrying
ADR-0174 detail `{reason: capability_unsupported, capability, provider, supported}`. `vmcore.fetch`
loses its static `HOST_DUMP` default and resolves an omitted method through the existing
`ProfilePolicy.capture_method(profile)` seam, clamped to the core-producing set `_VMCORE_METHODS`.

Not in scope: any provider seam wiring (Epic B), maturity metadata (A3 #673, already on main as
`f649775b`), the descriptor itself / `resources.describe` projection (A1 #672, already merged),
schema/migration (none), new tools/ports/error category.

## Shared helper (Task 1)

Add to `src/kdive/mcp/tools/_common.py`:

```python
def capability_unsupported(
    object_id: str, *, capability: str, provider: str, supported: list[str]
) -> ToolResponse:
    """A CONFIGURATION_ERROR for a plane/method the bound provider does not support (ADR-0209)."""
```

- `data = {"reason": "capability_unsupported", "capability": capability, "provider": provider,
  "supported": sorted(supported)}`.
- `detail` is a fixed template naming the provider, capability, and supported set — no secret,
  hostname, object-store key, or un-supplied resource name (ADR-0123). `provider` and the
  capability tokens are provider-derived enum-like values, safe to echo.
- `capability` token format (ADR-0209 §1): `"capture_method:<method>"`, `"debug_transport:<kind>"`,
  `"introspection:<mode>"`.
- Files: `src/kdive/mcp/tools/_common.py` (+ `__all__`).
- Acceptance: unit test builds it and asserts the `data` shape + sorted `supported`.

## vmcore.fetch — profile-resolved default + ADR-0209 detail (Task 2)

`src/kdive/mcp/tools/lifecycle/vmcore.py`:

- Tool signature + `fetch_vmcore` + `_fetch_vmcore`: `method` becomes `CaptureMethod | str | None`
  defaulting to `None` (drop the static `HOST_DUMP`). Remove `HOST_DUMP` from the tool `Field`
  default; the FastMCP param becomes optional.
- The runtime callback passes the **whole `runtime`** (not just `supported_capture_methods`) so the
  handler can read `profile_policy` and `component_sources.provider`.
- Resolution order inside `_fetch_vmcore`, after the System is loaded (the profile lives on the row):
  1. If `method` supplied: parse → must be a known `CaptureMethod` (else config_error
     `invalid capture method`); must be core-producing (`_VMCORE_METHODS`, else config_error
     `method does not produce a vmcore`); must be in `runtime.supported_capture_methods` (else the
     new `capability_unsupported` with `capability=f"capture_method:{m.value}"`).
  2. If omitted: `resolved = runtime.profile_policy.capture_method(profile)` clamped to
     `_VMCORE_METHODS`. If `resolved` is core-producing AND in `supported_capture_methods`, use it.
     Otherwise (non-core profile method, e.g. console/gdbstub System) there is no implicit core
     default → `capability_unsupported` (capability = the resolved method, or a
     `missing_required_field`-style config_error naming that an explicit core `method` is required).
- The profile parse uses `ProvisioningProfile.parse(system.provisioning_profile)`; a parse failure
  is the existing typed failure path.
- Keep the existing dedup key `f"{system_id}:capture_vmcore:{method.value}"` keyed on the *resolved*
  method so a no-method and an explicit-same-method call dedupe together.
- Files: `src/kdive/mcp/tools/lifecycle/vmcore.py`.
- Acceptance:
  - no-method on the seeded crashkernel local System (`capture_method → KDUMP`, local supports
    `{KDUMP}`) → `queued`, one `capture_vmcore` job, dedup key `...:kdump`.
  - no-method on a `preserve_on_crash` local System (`→ HOST_DUMP`) with a descriptor that supports
    `{HOST_DUMP}` → `queued`.
  - no-method on a console-only System (`→ CONSOLE`, non-core) → `CONFIGURATION_ERROR`, no job.
  - explicit unsupported (`host_dump` on local `{KDUMP}`) → `capability_unsupported`, no job.
  - explicit non-core (`console`) → config_error, no job.

## debug.start_session — supported_debug_transports gate (Task 3)

`src/kdive/mcp/tools/debug/sessions_lifecycle.py`:

- After the transport-kind validation (`DEBUG_TRANSPORT_KINDS`) and after the connector/runtime is
  resolved for the run (the `_AttachResources` already comes from the resolved runtime), add a
  descriptor check: `transport_kind in runtime.supported_debug_transports` else
  `capability_unsupported(run_id, capability=f"debug_transport:{transport_kind}", provider=...,
  supported=sorted(runtime.supported_debug_transports))` — **before** `_open_transport` and the
  insert. No DebugSession row, no transport opened.
- `_AttachResources` must carry the descriptor's `supported_debug_transports` + `provider` (extend
  the dataclass; `_resolved_connector_for_run` already has the full `runtime`).
- The check sits in `_prepare_attach_request` right after `_connector_for_run` returns, so a
  miss returns before `_open_transport`.
- Files: `src/kdive/mcp/tools/debug/sessions_lifecycle.py`.
- Acceptance: `start_session` with `gdbstub` against a runtime whose `supported_debug_transports`
  is empty → `capability_unsupported`, no `debug_sessions` row, connector `open_transport` never
  called (fake connector asserts).

## introspect.from_vmcore / introspect.run — supported_introspection gate (Task 4)

`src/kdive/mcp/tools/debug/introspect.py`:

- `introspect.from_vmcore` is registered through `with_runtime_for_run`; add the check in the
  wrapper lambda (it has `runtime`) before delegating to `introspect_from_vmcore`: require
  `"offline-vmcore" in runtime.supported_introspection`.
- `introspect.run` resolves `runtime_for_session`; add the check before
  `_introspect_live_session`: require `"live" in runtime.supported_introspection`.
- capability tokens: `"introspection:offline-vmcore"`, `"introspection:live"`.
- The direct-handler signatures (`introspect_from_vmcore(..., introspector=...)`) are unchanged —
  the gate lives in the registered wrappers, matching how runtime resolution already lives there.
- Files: `src/kdive/mcp/tools/debug/introspect.py`.
- Acceptance: a registered-tool test (FastMCP `Client`) against a runtime with empty
  `supported_introspection` → `capability_unsupported`, port never called.

## Provider-neutrality

Every check reads `runtime.supported_*` / `runtime.component_sources.provider`; none branches on
`ResourceKind`. The same handler admits a remote System (full sets) and rejects a local System
(empty debug/introspection until Epic B) by reading data.

## Test-fixture fallout to expect

- `tests/mcp/systems_support.py::provider_resolver` does not set `supported_debug_transports` /
  `supported_introspection` (default empty) — debug/introspect admission tests that must *pass* need
  a resolver that sets them. Add optional params there (additive), do not change existing defaults.
- `tests/mcp/lifecycle/test_vmcore_tools.py`: `_real_local_handlers` asserts the old reason string
  `"method not supported by provider"`; update it to the ADR-0209 `capability_unsupported` shape.
  `_fetch_vmcore` test helper defaults `method="host_dump"`; the no-method default-resolution tests
  call with `method=None`.

## Guardrails (run before every commit; CI gates these individually)

`just lint`, `just type`, `just test`. Full `just ci` (adds lint-shell, lint-workflows,
check-mermaid) once before pushing — `test_tool_docs` and generated `docs/guide/reference/*` live
outside the touched dirs. No `vmcore.fetch` signature change should alter the generated reference
beyond the param default (regenerate if the docs guard flags it).

## Rollback

Pure handler-logic change, no schema. Revert the branch.
