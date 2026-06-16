# ADR 0132 — Allocation-request denial ergonomics and sizing-source discoverability

- **Status:** Proposed
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

ADR-0123 added a `detail: str | None` field to the response envelope so a
`CategorizedError` message reaches the caller, and ADR-0124–0126 continued the
onboarding-ergonomics work (epic #449/#450). Validating the live VM lifecycle on
`sha-6898353` surfaced two gaps where that work does not reach the one place a
black-box (MCP-only, no source) agent gets stuck: the first `allocations.request`.

**#471 — `configuration_error` denials are a black-box dead-end.** The default
resource selector (`ResourceByKind`) defaults `kind` to `local-libvirt`
(`tool_payloads.py`), which is absent on a remote-only deploy. When no schedulable
host exists for the selected kind (or an explicit-id selector names an
absent/cordoned/unavailable host), `request_admission`
(`services/allocation/admission/request.py`) returns
`RequestAdmissionResult(category=CONFIGURATION_ERROR, resource=None)`, and the MCP
mapper `_request_response` (`mcp/tools/lifecycle/allocations.py`) builds a bare
`ToolResponse.failure(..., data={})`: `detail` is null and `suggested_next_actions`
is empty. The agent cannot learn that the selected kind has no host, what kinds *do*
exist, or which discovery tool to call. The no-capacity denial path
(`_denial_response`) does set `suggested_next_actions=["allocations.list"]` but its
`detail` is also null. The `detail` field from ADR-0123 was never populated on any of
these admission denial paths.

**#473 — `shape` is undocumented and the shape-XOR-custom rule is invisible.** The
`shape` field on `AllocationRequestPayload` carries no description, so the published
input schema is `"shape": {"anyOf":[{"type":"string"},{"type":"null"}],"default":null}`
— no pointer to `shapes.list`, no statement of the constraint. The
shape-XOR-custom-triple rule (supply `shape` *or* the full `{vcpus, memory_gb,
disk_gb}` triple, never both/neither) lives in a Pydantic `model_validator`
(`_shape_xor_custom_triple`). JSON Schema cannot express it, and a violation raises
`ValidationError` at the FastMCP transport boundary — so it surfaces as a protocol
validation error, never as a `configuration_error` envelope with a populated `detail`.

## Decision

We surface a populated `detail` and discovery `suggested_next_actions` on every
`allocations.request` configuration-error denial, and we make the `shape` field and its
XOR constraint discoverable. This refines ADR-0019/0023/0067 and continues
ADR-0123–0126; it does not reopen those choices.

### #471 — denial ergonomics

1. **Thread available kinds to the no-resource result, with a by-kind/by-id
   discriminant.** When `_select_target` resolves no schedulable host for a **by-kind**
   selector, `request_admission` queries the **distinct registered resource kinds** (a
   fleet-topology fact, not project-scoped data — the same aggregate `resources.list`
   would surface) and returns them as `available_kinds: tuple[str, ...]` on
   `RequestAdmissionResult`; for a **by-id** selector it leaves `available_kinds = None`
   (the kind list is irrelevant to a caller who named a host). The MCP mapper branches on
   `available_kinds is not None` (never on "is `object_id` a UUID"): by-kind →
   `"no schedulable 'local-libvirt' resource is registered; available kinds:
   remote-libvirt"` (or the empty-fleet `"...; no resource kinds are registered"`); by-id
   → `"no schedulable resource '<id>' is registered"` (the id is caller-supplied, not a
   leaked identifier).

2. **Set discovery next-actions; capacity detail is prose, not the raw token.**
   Configuration-error denials carry
   `suggested_next_actions=["resources.list", "shapes.list"]` (literal valid tool
   identifiers). The existing capacity-denial path keeps `["allocations.list"]` (its
   recourse is queue/wait, not discovery) and gains a `detail` mapped from the internal
   `outcome.reason` token to an **author-controlled prose string** via one small helper
   (`at_capacity` → host-cap prose with `cap`/`in_use`; `budget_exceeded` → budget prose;
   `quota_exceeded` → quota prose; unknown/unset → generic `"allocation denied"`), so a
   new reason token can never leak verbatim as `detail`.

3. **No-leak boundary.** Available *kinds* are deployment topology, not
   project-affinity-scoped resource existence, so naming them does not leak per-project
   data; the suppressed-category seam (ADR-0123) is untouched (`configuration_error` is
   not suppressed). The detail strings are author-controlled and interpolate only the
   caller-supplied selector and the fleet kind set — no secrets, hostnames, or object
   keys.

### #473 — sizing-source discoverability

4. **Describe the `shape` field.** Add a description:
   *"Named size from `shapes.list`; mutually exclusive with vcpus/memory_gb/disk_gb
   (supply exactly one sizing source)."* It renders into the published input schema and
   the generated tool reference.

5. **Surface the XOR violation as an envelope — and ONLY the XOR violation — at the
   existing binding middleware.** The `model_validator` stays as defence-in-depth but
   raises a `PydanticCustomError` with a **stable type** (`"shape_xor_custom"`, carrying a
   `both` context flag) instead of a bare `ValueError`, so its errors are
   machine-distinguishable. The shape-XOR violation is a binding-time `ValidationError` on
   the typed `request` param — the same class ADR-0124's `ProfileBindingMiddleware`
   already re-envelopes for the `profile` param. We reuse that seam: the middleware
   generalizes to a small per-tool registry and gains an `allocations.request` case that
   converts a binding `ValidationError` to a `configuration_error` envelope **only when
   every error entry is `shape_xor_custom`**, **re-raising** any field-level error
   (`extra='forbid'` typo, bad `resource.mode` discriminator, non-int `vcpus`) so
   FastMCP's per-field detail is never collapsed into the generic XOR message. The tool
   keeps its typed-param binding (no in-handler raw parse).

## Alternatives considered

- **Drop the XOR `model_validator`, enforce in the handler only.** Rejected:
  replace-don't-deprecate cuts the wrong way here — the validator is the
  defence-in-depth guard that stops a partial triple reaching a NULL `requested_disk_gb`
  snapshot (ADR-0067), and it still protects the service-layer callers
  (`resolve_request_sizing`). We keep it and convert its error at the boundary.

- **Name available *resources* (ids), not kinds, in the detail.** Rejected as a
  no-leak risk: resource rows are project-affinity-scoped; enumerating them in a denial
  detail would leak existence. Kinds are fleet topology and safe.

- **Resolve the available-kinds query in the MCP layer.** Rejected: the MCP layer holds
  no `conn` at the mapper seam, and the service layer already owns the DB reads for this
  result. The kind set rides on `RequestAdmissionResult`.

## Consequences

- A black-box agent that issues the default `allocations.request` on a remote-only
  deploy now reads, from the envelope alone, that `local-libvirt` is absent, that
  `remote-libvirt` is available, and to call `resources.list` / `shapes.list` — and can
  reach a granted allocation following only the envelope.
- The published `allocations.request` schema and the generated tool reference document
  the `shape` field and its XOR rule; the generated reference is regenerated and
  committed.
- One additional `SELECT DISTINCT kind` runs only on a no-resource denial (the cold
  path), not on the grant path. No schema change or migration.
