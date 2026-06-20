# Spec — Make the staged remote-libvirt host discoverable (#625)

- **Issue:** #625 (RUN_REVIEW.md D1(a), D1(b)), part of epic #618
- **ADR:** [0194](../adr/0194-staged-host-discoverability.md)
- **Status:** Implementation-ready

## Problem

A live four-host remote-libvirt fleet had its operator-staged base image present on only one host.
Two diagnostics surfaces fail to let an operator find that host:

1. `ops.diagnostics` fans the base-image-staging check over each `[[remote_libvirt]]` instance
   (ADR-0187), but every resulting row is labeled only `provider: "remote-libvirt"`. With N hosts
   the verdict carries N indistinguishable staging rows.
2. `resources.describe` reports `staged_base_images[].staged: "unknown"` for a host whose volume is
   demonstrably staged, because the probe is resolved from the *unbound* provider runtime
   (`resolver.resolve(kind)`), whose `config_factory` is `unbound_remote_config` and always raises
   `CONFIGURATION_ERROR`, degrading every volume to `"unknown"`.

## Goals

- `ops.diagnostics` names the specific host that passes (or fails/errors) base-image staging.
- `resources.describe` returns a real per-volume status for a reachable host; `"unknown"` only when
  the probe genuinely could not run.

## Non-goals

- No change to the three-state `pass`/`fail`/`error` semantics or the `fix`-only-on-`fail` /
  `failure_category`-not-on-`pass` invariants (ADR-0091).
- No new entity, schema column, or migration (`resource_id` is an additive `data` key under the
  ADR-0113 flat outputSchema; the host name already lives in inventory).
- No change to worker-vantage checks' behavior (TLS, gdbstub-ACL); they round-trip the new field as
  `null` only.
- No change to `probe_staged_volumes`' control flow; the (b) fix is at the caller.

## Design

### (a) `resource_id` on `CheckResult`, projected through `ops.diagnostics`

`src/kdive/diagnostics/checks.py`

- Add `resource_id: str | None = None` to `CheckResult` (last field, after `failure_category`).
  Docstring: "The registered resource this result pertains to — for remote-libvirt the
  `[[remote_libvirt]]` instance name; `None` for a fleet-aggregate or resource-independent check."
- No new `__post_init__` invariant (it is legal on any status, including `pass`/`error`, because an
  operator most needs the host name on a failing/erroring host).
- `BaseImageStagingCheck.__init__` and `RemoteLibvirtReachabilityCheck.__init__` gain a
  `resource_id: str | None = None` parameter, stored and stamped onto **every** `CheckResult` they
  emit (all branches).

`src/kdive/providers/remote_libvirt/diagnostics/contribution.py`

- In `_checks()`, the loop already binds `name`; pass `resource_id=name` to both
  `RemoteLibvirtReachabilityCheck(...)` and `BaseImageStagingCheck(...)`.

`src/kdive/mcp/tools/ops/diagnostics.py`

- In `_item`, add `"resource_id": result.resource_id` to the `data` dict.

`src/kdive/diagnostics/result_codec.py`

- `serialize_results`: add `"resource_id": r.resource_id` to each serialized item.
- `_reconstruct`: pass `resource_id=item.get("resource_id")` to the `CheckResult` constructor.

### (b) Bind `resources.describe`'s staged probe to the described host

`src/kdive/mcp/tools/catalog/resources.py`

- `_runtime_staged_probe(resolver, kind)` becomes `_runtime_staged_probe(resolver, kind, name)`. It
  resolves `runtime = resolver.resolve(kind)`, then binds to the host with
  `runtime.for_resource(name)` **when `name` is not `None`** (else uses the unbound `runtime`), and
  returns its `staged_volume_probe`. For remote-libvirt a present name rebinds `config_factory` to
  `remote_config_for_resource(name)` so the probe connects to the described host; for single-host
  providers `for_resource` is identity. A `None` name (a resource row without an instance name —
  only the reconcile-less synthetic case) keeps the prior unbound behavior, which degrades to
  `"unknown"` exactly as before, so the change is strictly additive for that path.
- `describe_resource` passes `resource.name` to `_runtime_staged_probe`.
- Update the `staged_base_images` docstring note: the live probe is bound to the described host, so
  a reachable host returns `staged`/`absent`/`pool_absent`; `"unknown"` means the probe could not
  run.

`src/kdive/providers/remote_libvirt/staged_volumes.py`

- Docstring only: tighten the `Returns:` note so `"unknown"` is documented as "remote config could
  not be resolved (the probe could not run)", distinct from the reachable verdicts. No control-flow
  change.

## Test plan (TDD — behavior, not implementation)

`tests/diagnostics/test_base_image_staging.py`

- A new test: a `BaseImageStagingCheck(resource_id="ub26", ...)` stamps `resource_id == "ub26"` on a
  `STAGED` pass result, a `NOT_STAGED` fail result, and an `UNREACHABLE`/`INDETERMINATE` error
  result. Default (`resource_id` omitted) is `None` (covers the back-compat path).

`tests/diagnostics/test_reachability.py`

- Same stamping assertion for `RemoteLibvirtReachabilityCheck` across pass/fail/error.

`tests/diagnostics/test_result_codec.py`

- A `CheckResult` with `resource_id` set survives `serialize_results` → `deserialize_results`
  unchanged; a payload without the key reconstructs `resource_id=None`.

`tests/diagnostics/test_provider_checks.py` (or contribution test)

- Assembling the remote-libvirt contribution over a two-instance inventory yields staging +
  reachability checks whose `resource_id` matches each declared instance name (drives the
  `contribution.py` wiring).

`tests/mcp/test_diagnostics_tool.py` (the `ops.diagnostics` handler test)

- A served verdict's per-check item carries `data["resource_id"]` equal to the producing check's
  `resource_id` (and `None` for a resource-independent check).

`tests/mcp/catalog/test_resources_tools.py`

- A new test driving `describe_resource` for a **named** remote resource with a resolver whose
  runtime sets `rebind_for_resource(name)` asserts the bound probe (selected by the described host's
  name) is the one called, not the unbound runtime's probe — verifies the call goes through
  `for_resource(resource.name)`.
- The existing `test_describe_remote_uses_provider_runtime_staged_probe` keeps passing (its resource
  has no name → unbound runtime, and its resolver sets no rebind hook, so `for_resource` is
  identity anyway).

## Edge cases

- **`resource.name is None`** — probe skipped, volumes degrade to `"unknown"` (unchanged contract).
- **Single declared host** — behavior unchanged except rows now carry the (single) `resource_id`.
- **Worker-vantage checks** — `resource_id` serializes as `null`; the codec's `_ALLOWED_IDS` gate is
  untouched.
- **Resource-independent / aggregate checks** (`secret_ref`, `local_kernel_src`,
  `ephemeral_libvirt_buildhost_agent`) — `resource_id` stays `None`; `ops.diagnostics` emits `null`.

## Guardrails

`just lint`, `just type` (whole-tree), `just test`; full `just ci` before push.
