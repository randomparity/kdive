# ADR 0076 — Independent remote-libvirt provider package + portability diff gate (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0071](0071-per-kind-provider-runtime-registry.md)
  (the per-kind `ProviderRuntime` registry this registers a third entry into),
  [ADR-0063](0063-typed-provider-runtime.md) (the typed port seam the package satisfies),
  [ADR-0004](0004-first-slice-local-libvirt.md) (the local-libvirt first slice this provider
  supersedes for production use).
- **Spec:** [`../specs/m2-remote-libvirt.md`](../specs/m2-remote-libvirt.md)

## Context

The top-level design's central bet is that adding a provider after M0 is "add a provider
package + its provisioning profiles, with the core and tool surface unchanged," and it states
this as a **falsifiable hypothesis** measured by diff scope (`top-level-design.md` §Roadmap).
M1.5 built the per-kind `ProviderRuntime` registry (ADR-0071) precisely so M2 could test that
hypothesis against a real second provider rather than the in-process mock.

`local_libvirt` was the M0 bootstrap. In production the MCP server and worker tier run
**separately** from the libvirt-enabled development hosts, so local-libvirt — which assumes a
shared filesystem and a local `qemu:///system` connection — is **not** the production provider
and is headed for removal once `remote_libvirt` is enabled. This reframes a tempting move: a
shared `libvirt_common` layer factored out of local-libvirt and reused by remote-libvirt would
couple the production provider to a module slated for deletion, producing exactly the
backward-compatible shim / migration path the project's "replace, don't deprecate" standard
forbids.

The remote host's libvirt API calls (define/start/destroy XML, capability parse) are nearly
identical to local-libvirt's — only the connection, file movement, and secret resolution
differ (ADR-0077, ADR-0078, ADR-0079). The question is how to draw the package boundary so the
falsifiability metric stays meaningful, DRY does not couple a production provider to a doomed
one, and the removal of local-libvirt later is a clean deletion rather than an untangling.

## Decision

We will build **`remote_libvirt` as an independent provider package** —
`src/kdive/providers/remote_libvirt/` with its own discovery, lifecycle, build, retrieve, and
debug modules satisfying the same typed `ProviderRuntime` ports (ADR-0063) — **without** a
shared `libvirt_common` layer with `local_libvirt`. We will register it behind the per-kind
`ProviderResolver` (ADR-0071) under a new `ResourceKind.REMOTE_LIBVIRT = "remote-libvirt"` and
migration `0020` (CHECK widen), **opt-in** by operator config (a remote host `qemu+tls://` URI
and a TLS-cert `secret_ref`). We will **keep `local_libvirt`** as the default and the
falsifiability baseline; its removal is a follow-up milestone, not M2. And we will **measure
the portability hypothesis with a diff gate**: M2 must touch zero net lines in core
(`domain`/`db`/`jobs`/`reconciler`/`services`/`store`/`security` and the `mcp` server skeleton)
and the MCP tool surface (`mcp/tools/*`), modulo an explicit allowlist (the `ResourceKind`
value, the `providers/composition.py` registration, the one migration, regenerated docs).

## Consequences

- **The falsifiability hypothesis becomes a checked gate, not a claim.** Issue 8's diff gate
  fails the milestone on any net change to core or `mcp/tools/*` outside the allowlist, so a
  core change surfaces as a smell to refactor away (the milestone's co-equal goal) rather than
  being silently absorbed. The gate runs against a stable baseline because local-libvirt stays.
- **Removal of local-libvirt later is a clean deletion.** Because remote-libvirt shares no code
  with local-libvirt, the follow-up that removes local-libvirt deletes a package and its
  composition entry — no shared layer to disentangle, no consumer to migrate.
- **Cost: some libvirt-API code is duplicated** between the two packages for the duration that
  both exist. This is accepted deliberately: the duplication is bounded (define/start/destroy
  and capability parse), it is short-lived (local-libvirt is going away), and "no premature
  abstraction" plus "replace, don't deprecate" both argue against extracting a shared layer to
  serve a module being removed.
- **No new resolver call sites.** Registration is a third entry in the composition map; the
  post-System resolution path (ADR-0071) already exists, so M2 threads no new resolver wiring —
  which is what keeps the core diff at zero.
- **Migration `0020`** widens `resources_kind_check`; the ADR-0071 CHECK↔registry parity test
  now covers three kinds. No other DDL.
- **Opt-in composition** means a deployment without a configured remote host registers no
  remote runtime and has no bookable remote resource — the same posture ADR-0071 set for
  fault-inject.

## Alternatives considered

- **Extract a shared `libvirt_common` layer** consumed by both providers. DRY, and refactoring
  within `providers/*` would not falsify the (core-and-tool-surface) diff gate. Rejected: it
  couples the production provider to local-libvirt, a module slated for removal, creating the
  migration-shim the "replace, don't deprecate" standard forbids and turning local-libvirt's
  later deletion into an untangling instead of a delete.
- **Parameterize `local_libvirt` with a `qemu+ssh://`/`qemu+tls://` URI** and an injected
  transport adapter — no new package. Least new code, but it makes the "second provider" story
  and the falsifiability metric **vacuous** (nothing distinct to diff), and it conflicts with
  the per-kind `ResourceKind` registry M1.5 built specifically to host a distinct second kind.
  Rejected.
- **Mark local-libvirt deprecated and defer removal indefinitely.** Rejected: it is the
  deprecation path the global standard prohibits, and it leaves two libvirt providers coexisting
  with no plan to converge.
