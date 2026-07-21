# Errors

When a KDIVE tool reports a failure, the `ToolResponse`
(resource://kdive/docs/guide/response-envelope.md)
`error_category` field carries a value from a closed taxonomy defined in
`src/kdive/domain/errors.py` and referenced in
[ADR-0019](../adr/0019-tool-response-envelope.md). The rule is: pick the most
specific category; never invent strings. The taxonomy is stable across the rewrite —
the same strings are comparable with PoC failure categories where the names overlap.

## Reading a failure envelope

A failed response has `status` equal to `failed` or `error`, and `error_category`
set to one of the values below. The `suggested_next_actions` list in that envelope
tells the agent what to call next (e.g. `["jobs.get"]` after a failed job). The
`data` field may carry structured context such as `current_status` for sequencing
errors.

## Category reference

| Category | When it applies |
|---|---|
| `configuration_error` | Sequencing error or invalid input — e.g. calling `runs.create` on a System that is not yet `ready`. Recoverable by waiting or correcting the request. |
| `missing_dependency` | A required upstream object or resource is absent. |
| `build_failure` | The kernel build step failed. |
| `boot_timeout` | The system did not reach a ready state within the allowed boot window. |
| `readiness_failure` | A readiness preflight check failed after boot. |
| `debug_attach_failure` | The debug transport could not be attached. |
| `symbol_not_found` | `debug.resolve_symbol` could not resolve the name to an address — the symbol is inlined / optimized away, or is an addressless enum/macro constant. The attach is fine and retrying will not help; `data.hint` suggests disassembling the caller. |
| `infrastructure_failure` | An unclassified failure in the underlying infrastructure layer. The fallback when no more specific category applies. |
| `stale_handle` | The referenced object (System, DebugSession) no longer exists or has been torn down. The handle is invalid; create a new object. |
| `transport_conflict` | Two attaches contended for the same debug transport simultaneously. |
| `not_implemented` | The requested operation has no registered handler for this provider or milestone. |
| `allocation_denied` | Admission control denied the allocation (capability mismatch, capacity, or policy). |
| `quota_exceeded` | The principal or project has exhausted their quota or budget. |
| `lease_expired` | The allocation's lease expired while a job was in flight; the run was terminated. Distinct from `canceled` (an explicit abort). |
| `queue_timeout` | A queued (`requested`) allocation was reaped after exceeding the max-wait window without ever being placeable. Distinct from `lease_expired` (a *granted* lease window elapsing) — a queued request never held a lease. |
| `provisioning_failure` | The provisioning step failed to produce a ready System. |
| `install_failure` | The kernel install step failed. |
| `transport_failure` | A console or debug transport failed during an active session. |
| `control_failure` | A power or crash control operation failed. |
| `authorization_denied` | The caller's role or required provisioning-profile opt-in does not permit the requested operation. |

## Recovery patterns

- **`configuration_error` with `data.current_status`** — the object is not yet in
  the required state; call `jobs.wait` or `systems.get` / `runs.get` and retry when
  the state advances.
- **`stale_handle`** — the target object is gone; create a new Run or provision a
  new System.
- **`transport_conflict`** — wait for the existing session to detach, then retry
  `debug.start_session`.
- **`transport_failure`** — a console/debug transport or held long-poll stream failed; it is
  `retryable=true`. Retry the same call. A *raw* `socket connection was closed unexpectedly`
  (no envelope, so no `transport_failure` category) from a held `jobs.wait` is the same
  transient class — see the transport-reset retry contract in the async-jobs guide
  (resource://kdive/docs/guide/async-jobs.md).
- **`lease_expired`** — the allocation has expired; request a new allocation and
  provision a new System.
- **`queue_timeout`** — the queued request never became placeable within the max-wait
  window; re-request once capacity frees, or relax the target (kind/PCIe) to widen the
  candidate hosts.
- **`authorization_denied`** — the caller needs a higher role or the provisioning
  profile needs an opt-in. See the safety-and-RBAC guide
  (resource://kdive/docs/guide/safety-and-rbac.md).
- **`infrastructure_failure`** or **`provisioning_failure`** — retry if the job has
  remaining attempts; otherwise triage via `jobs.list` and the audit log.
