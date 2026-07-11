# Allocation-request denial ergonomics & sizing-source discoverability (#471/#473)

- **ADR:** [0132](../adr/0132-allocation-denial-ergonomics.md)
- **Issues:** #471 (denial dead-end), #473 (undocumented `shape` + invisible XOR rule)
- **Continues:** ADR-0123–0126 / epic #449 (MCP onboarding error ergonomics)

## Problem

A black-box (MCP-only, no source) agent cannot recover from the first
`allocations.request` it issues:

- The default selector `ResourceByKind` defaults `kind=local-libvirt`, absent on a
  remote-only deploy → `configuration_error` with `detail: null` and
  `suggested_next_actions: []` (#471).
- The `shape` param has no schema description and its shape-XOR-custom-triple rule lives
  in a Pydantic `model_validator` that JSON Schema can't express; a violation raises a
  transport-level `ValidationError`, not a `configuration_error` envelope with a usable
  `detail` (#473).

## Goals / non-goals

**Goals.** Every `allocations.request` configuration-error denial returns a populated
`detail` (cause + fix) and discovery `suggested_next_actions`. The `shape` field is
documented and its XOR constraint is discoverable from the published schema and from the
error envelope on violation.

**Non-goals.** No change to admission semantics, the grant path, the no-leak seam
(ADR-0123), or the shape catalog. No new tool, column, env knob, or migration.

## Design

### #471 — denial detail + discovery actions

The seam where the dead-end is built is the MCP mapper `_request_response` /
`_denial_response` in `src/kdive/mcp/tools/lifecycle/allocations.py`, fed by
`RequestAdmissionResult` from `src/kdive/services/allocation/admission/request.py`.

1. **Service layer (only place with `conn`).** When `_select_target` resolves no
   schedulable host, `request_admission` returns the existing
   `category=CONFIGURATION_ERROR, resource=None` plus a **discriminant the mapper needs**:
   for a **by-kind** selector (`spec.resource_id is None`) it runs `SELECT DISTINCT kind
   FROM resources` (sorted) and sets `available_kinds: tuple[str, ...]` on
   `RequestAdmissionResult`; for a **by-id** selector it leaves `available_kinds = None`
   (the kind list is irrelevant — the caller named a specific host). The mapper branches
   on `available_kinds is not None`, never on "is `object_id` a UUID". The query is a
   cold-path read (no-resource denial only), never on grant.

2. **MCP mapper.** `_request_response`'s `resource is None` branch builds the `detail`
   off the `available_kinds is not None` discriminant:
   - by-kind (`available_kinds is not None`): `"no schedulable '<kind>' resource is
     registered; available kinds: <k1, k2>"`, or `"...; no resource kinds are
     registered"` when the tuple is empty.
   - by-id (`available_kinds is None`): `"no schedulable resource '<id>' is
     registered"` (the id is the caller-supplied selector, not a leaked identifier).
   It sets `suggested_next_actions=["resources.list", "shapes.list"]` (literal valid
   tool identifiers).

3. **Capacity denial.** `_denial_response` keeps `["allocations.list"]` (its recourse is
   `on_capacity=queue` + `allocations.wait`, not discovery) and gains a `detail` that is
   an **author-controlled prose string keyed off the internal `outcome.reason` token**,
   never the raw token: `at_capacity` → `"host capacity exhausted (cap <cap>, in use
   <in_use>)"`; `budget_exceeded` → `"project budget exhausted for the requested
   window"`; a `quota_exceeded` denial (no `reason`) → `"project concurrency quota
   exhausted"`; any other/unset reason → a generic `"allocation denied"`. The mapping
   lives in one small helper so a new reason token cannot silently leak as `detail`.
   No-resource and capacity details are distinct strings; both categories
   (`configuration_error`, `allocation_denied`, `quota_exceeded`) are unsuppressed, so
   the prose passes through.

4. **No-leak.** `configuration_error` is *not* in the ADR-0123 suppressed set, so its
   `detail` passes through. Available *kinds* are deployment topology (the aggregate
   `resources.list` already surfaces), not project-affinity-scoped existence, so naming
   them leaks nothing. Detail strings interpolate only the caller-supplied selector and
   the fleet kind set — never secrets, hostnames, or object keys.

### #473 — `shape` description + XOR-as-envelope

The XOR rule is enforced in `AllocationRequestPayload._shape_xor_custom_triple`
(`src/kdive/mcp/tool_payloads.py`). It raises `ValidationError` at the FastMCP boundary
before the handler runs.

5. **Describe `shape`.** Add to the field:
   *"Named size from `shapes.list`; mutually exclusive with vcpus/memory_gb/disk_gb
   (supply exactly one sizing source)."* The two `ValueError` messages in the validator
   are sharpened to name the violation precisely (both/neither) so they are reusable as
   `detail`. The validator is tagged so its errors are machine-distinguishable from
   field-level errors — it raises a `PydanticCustomError` with a **stable error type**
   (`"shape_xor_custom"`) carrying a `both: bool` context flag, instead of a bare
   `ValueError`. (A bare `ValueError` from a `model_validator` becomes Pydantic error
   `type="value_error"` with no stable discriminant, which step 6's narrowing needs.)

6. **Convert ONLY the XOR error to an envelope, at the existing binding middleware.**
   The shape-XOR violation is a **binding-time** `ValidationError` on the
   `request: AllocationRequestPayload` param — the same class of error ADR-0124's
   `ProfileBindingMiddleware` already re-envelopes for the typed `profile` param
   (`src/kdive/mcp/middleware.py`). We reuse that seam instead of a new in-handler parse:
   the tool keeps FastMCP's typed-param binding (so every **field-level** failure — a
   typo'd field under `extra='forbid'`, a bad `resource.mode` discriminator, a non-int
   `vcpus` — keeps FastMCP's per-field detail and is NOT converted), and the middleware
   gains an `allocations.request` case that converts a binding `ValidationError`
   **only when every error entry is the `shape_xor_custom` validator error**, re-raising
   otherwise. The `detail` is derived from the entry's `both` context flag:
   - both: `"supplied both a shape and a custom size; supply exactly one sizing source
     (a shape, or the full {vcpus, memory_gb, disk_gb} triple)"`
   - neither/partial: `"supplied neither a shape nor a full {vcpus, memory_gb, disk_gb}
     triple; supply exactly one sizing source"`
   The middleware generalizes from a profile-only seam to a small per-tool registry
   (tool → object-id arg + binding-error predicate + envelope builder), keeping the
   profile behavior byte-identical. The `model_validator` itself stays (defence-in-depth
   for the service-layer callers and the NULL-`requested_disk_gb` guard of ADR-0067,
   which the service layer's `resolve_request_sizing` also enforces).

## Boundary & test plan (TDD)

Tested at the handler boundary (`alloc_tools.request_allocation` / the registered tool
wrapper), matching the existing `tests/mcp/lifecycle/test_allocations_tools.py` pattern
(direct call with an injected pool), plus the published-schema assertion and the
generated tool-reference regeneration.

- **#471 no-resource (by-kind):** unregistered kind → `detail` names the selected kind
  and the available kinds (and the empty-fleet variant); `suggested_next_actions`
  includes `resources.list` and `shapes.list`.
- **#471 no-resource empty fleet (by-kind):** zero registered resources → `detail`'s
  "no resource kinds are registered" variant; same actions.
- **#471 unknown/cordoned/unavailable id (by-id):** `detail` names the id and does NOT
  list kinds (the `available_kinds is None` branch); same actions.
- **#471 capacity denial (host cap):** `detail` is the `at_capacity` prose ("host
  capacity exhausted (cap …, in use …)"), NOT the raw token; actions stay
  `["allocations.list"]`.
- **#471 budget / quota denial:** `detail` is the budget/quota prose; actions
  `["allocations.list"]`.
- **#471 acceptance walk:** following only the envelope (`resources.list` →
  `allocations.request` with a registered kind) reaches `granted`.
- **#473 schema:** the published `allocations.request` input schema's `shape` carries
  the description pointing to `shapes.list` and stating the XOR rule.
- **#473 XOR-as-envelope:** both `shape`+triple, and neither/partial, return a
  `configuration_error` envelope whose `detail` names the violation (not a transport
  `ValidationError`).
- **#473 narrowing (no over-collapse):** a non-XOR payload error (e.g. an unknown extra
  field under `extra='forbid'`, or a bad `resource.mode`) still surfaces FastMCP's
  field-level error — it is NOT collapsed into the XOR `detail`.
- **Validator unit tests:** the existing `test_allocation_request_payload.py` cases still
  reject both/neither/partial via `model_validate` (now raising the `shape_xor_custom`
  typed error); assert the error `type` is stable.
- **Regression:** the existing `unknown_shape` / `over_host_shape` fail-closed tests
  (which already populate `detail` via `failure_from_error`) stay green.

Confirm each new test red for the right reason before implementing.

## Files

- `src/kdive/services/allocation/admission/request.py` — available-kinds query + field
  on `RequestAdmissionResult`.
- `src/kdive/mcp/tools/lifecycle/allocations.py` — `_request_response` /
  `_denial_response` detail + actions; capacity reason→prose helper.
- `src/kdive/mcp/tool_payloads.py` — `shape` description + `shape_xor_custom` typed
  validator error (with `both` context flag).
- `src/kdive/mcp/middleware.py` — generalize the profile-binding seam to a per-tool
  registry; add the `allocations.request` shape-XOR binding-error → envelope case.
- `tests/mcp/lifecycle/test_allocations_tools.py`,
  `tests/mcp/test_allocation_request_payload.py`, the middleware test module — new
  assertions.
- `docs/guide/reference/allocations.md` — regenerated (`just docs`); the generated
  reference renders only top-level tool params, so the nested `shape` description rides in
  the published MCP input schema, not this file.
