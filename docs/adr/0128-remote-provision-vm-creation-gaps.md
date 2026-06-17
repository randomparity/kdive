# ADR 0128 — Remote-libvirt VM creation: discoverable base volume + non-masking provision failures

- **Status:** Accepted
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

Driving the deployed MCP surface end-to-end (`allocations.request` → `systems.provision` →
`jobs.wait`) to create a remote-libvirt VM surfaced two defects that together made tools-only VM
creation impossible to complete *and* impossible to diagnose:

1. **The advertised `base_image_volume` does not resolve.** `systems.profile_examples` (#451)
   emitted the catalog *image name* (`fedora-kdive-remote-base-43`) for `base_image_volume`, but
   the field is the operator-staged libvirt *volume* the provider looks up by name on the host's
   storage pool (`providers/remote_libvirt/rootfs_build.py`, ADR-0080), and the staged volume is
   `fedora-kdive-remote-base-43.qcow2`. The provider does a literal `storageVolLookupByName`, so an
   agent that copies the example verbatim provisions against a name that is not staged and the
   provision fails (`CategorizedError: base image volume … is not staged`).

2. **A provision failure is masked as job success.** When `provisioner.provision` raises, the
   handler drives the System `provisioning → failed` (`_record_system_failure`) and re-raises. The
   worker requeues the job (it never sets `terminal`), and on the retry the handler re-enters with
   the System already terminal and returns `str(system_id)` as success. Net: the job ends
   `succeeded` while the System is `failed`, so an agent following `jobs.wait` believes the VM
   exists. `from_job` already surfaces a failed job's `failure_context`, so the real reason is
   available *iff* the job actually ends `failed`.

The job-layer retry policy is deliberately distinct from the MCP `_RETRYABLE_BY_CATEGORY` flag
(e.g. `BUILD_FAILURE` is non-retryable for the client but the worker still retries it to
`max_attempts`), so the fix cannot key the worker's terminal decision on category.

## Decision

1. `systems.profile_examples` emits the staged source's `volume` (the value the provider looks up),
   not the catalog image name — and only when the referenced public image has a `staged` source (a
   non-staged image has no host volume to provision from, so it falls back to the placeholder).
2. Add a `terminal: bool = False` flag to `CategorizedError`. The worker dead-letters a job whose
   handler raises a `terminal` error at once, irrespective of category (the existing
   category-driven requeue is unchanged for every non-terminal error). `provision_handler` and
   `reprovision_handler` mark the error `terminal` on the failure path, because the failure has
   already driven the System to a terminal `failed` state — a retry cannot succeed and would only
   mask the failure as success. The job then ends `failed` carrying the original category and
   `failure_context`, which `ToolResponse.from_job` already surfaces to the agent.

## Consequences

- An MCP-only agent can complete remote-libvirt VM creation: the example profile carries a
  `base_image_volume` that resolves on the host.
- A provision/reprovision failure ends the job `failed` (not `succeeded`) on the first attempt,
  carrying the real reason (e.g. "base image volume not staged") through `failure_context` —
  `jobs.wait`/`jobs.get` now tell the agent *why*.
- `CategorizedError.terminal` is a general, opt-in mechanism: only errors explicitly marked
  terminal change retry behavior, so build/install/other retryable failures are unaffected.
- The masking previously also affected retryable-category provision failures (infrastructure
  errors), not only the configuration error reproduced here; marking the failure path terminal
  closes both.

## Alternatives considered

- **Key the worker's terminal decision on `_RETRYABLE_BY_CATEGORY`**: conflates the client-retry
  flag with job-retry policy and would change requeue behavior for `BUILD_FAILURE`/`INSTALL_FAILURE`
  (today retried to `max_attempts`). Rejected.
- **Make the handler's terminal-state re-entry return failure instead of success**: closes the
  masking only after burning every retry, and the final `failure_context` is the re-entry message,
  not the original reason. The `terminal` flag fails on attempt 1 with the real reason. Rejected as
  strictly worse.
- **Resolve `base_image_volume` through the image catalog at provision time**: also viable, but the
  field is contractually the staged volume name (ADR-0080), and the discovery tool is where the
  honest, host-resolvable value should be advertised; resolving in the provider would couple the
  provider to the catalog for no added correctness. Rejected for the discovery-side fix.
- **Validate `base_image_volume` is staged at admission**: a pre-mutation existence probe is a
  separate hardening (it needs a host connection in the request path); out of scope here, where the
  failure already returns a typed, now-visible error.
