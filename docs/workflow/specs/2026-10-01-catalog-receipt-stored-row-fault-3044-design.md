# Image-catalog and module-attempt receipt rebuilds are server faults (#3044)

## Problem

PR #3030 sent repository-layer stored-row rebuilds through `validate_stored`. Two sites that
run inside MCP tool bodies kept the bare path:

- `src/kdive/images/cataloging/catalog.py` rebuilds `ImageCatalogEntry` with bare
  `model_validate` in `resolve_rootfs`, `resolve_public_rootfs_sync` and
  `resolve_system_catalog_rootfs`. `vmcore.fetch` reaches the last one through the kdump gate.
- `RemoteModuleAttemptObligationRepository.read_reap_preparation` builds
  `ModuleAttemptObligationReceiptV1(**rows[0])`. It is reached through
  `build_external_boot_payload` from `systems.teardown`, `ops.force_teardown`, the
  external-boot recovery-request tools, and run step enqueue.

A corrupt row at either site raises `ValidationError`, which FastMCP reports as the caller's
argument error.

## Design

**Rebuild sites.** All four rebuilds call `validate_stored(Model, row)`. The behavior on a
valid row is unchanged: `model_validate` of the `dict_row` mapping has the same strict
validation as keyword construction. On a corrupt row the rebuild raises `ServerFaultError`
chained from the pydantic error.

**Kdump gate decision: surface the fault.** `refusing_kdump_capability` adds no handler. A
corrupt catalog row propagates out of `vmcore.fetch`, which `ServerFaultMiddleware` turns into
an `infrastructure_failure` envelope. No job is enqueued. ADR-0361's fail-open rule covers
*resolution gaps*: the server lacks the information to judge, so it does not refuse. A corrupt
row is a different case. The server's own catalog is broken, and the operator needs to see
that. Admitting the capture would hide the corruption and run a capture against an image whose
record the server can no longer read. `runs.complete_build` fails open for a separate reason
(ADR-0678): its lookup runs after the build has committed and on every replay. That reason does
not apply to a pre-enqueue admission gate. The gate and resolver docstrings say that a corrupt
row is not a gap.

**Callers (survey at `95e7d2b07`).**

| Caller | Handler | Effect |
|---|---|---|
| `complete_build._target_os_id` | already catches `ServerFaultError` (#3009) | unchanged, fails open |
| `images/rootfs/fetch.py:156`, `:210` (provisioning) | none in `rootfs_catalog_fetch`, `materialize`, or `provision` | the worker's `_failure_category` already maps either exception to `infrastructure_failure` |
| `admission.py:119-123` | `ModuleAttemptObligationError` only | the fault propagates |
| `systems/admin.py`, `recovery_requests.py`, `steps.py` | `CategorizedError` or none | the fault reaches the middleware |

No handler on these paths catches `ValueError` or `ValidationError`.

**ADR.** ADR-0709 gets an append-only `### Amendment (2026-10-01)` that moves both sites onto
the allowlist and records the gate decision.

## Failure model

- **Actors and deployments:** MCP callers of `vmcore.fetch` and of the teardown, recovery and
  step tools; the local-libvirt provisioning worker; an operator reading server logs.
- **Invariants and assets at stake:** a genuine resolution gap still makes the gate admit;
  `complete_build` still fails open; a corrupt row at the four rebuild sites does not reach
  a tool caller as an argument error; the error envelope discloses nothing beyond `SERVER_FAULT_DETAIL`.
- **Accepted failure classes:**
  - A corrupt catalog row blocks `vmcore.fetch` kdump/fadump captures for Systems that boot
    that image until an operator repairs the row. The cost is bounded and visible, and it is
    the point of the decision.
  - The receipt columns are fully constrained in the database (`uuid` types and the nonce
    `CHECK`), so the receipt fault is reachable only after an out-of-band schema change.
    The change is defensive, and the test drops those constraints to reach it.
- **Covered elsewhere:** the provider-layer authority repository and caller-input rebuilds →
  operator (approved exclusions); envelope translation itself → `ServerFaultMiddleware` tests
  (#2981).

## Testing

Each site gets a corrupt-row test. The test asserts `ServerFaultError` with a `ValidationError`
cause, and asserts the `infrastructure_failure` envelope through a FastMCP app that runs
`ServerFaultMiddleware`. For the catalog, the test goes through the real `vmcore.fetch` handler
and also asserts that no job is enqueued. The plan's verification entries name each test.
