# Architecture Decision Records

This directory records the load-bearing architecture decisions for the KDIVE
production rewrite. The top-level design (`../specs/top-level-design.md`) lists
nine core decisions and states that each "should become an ADR before
implementation"; those ADRs live here.

## Process

- One decision per file, named `NNNN-kebab-title.md` with a zero-padded,
  monotonic number (`0001`, `0002`, …). Numbers are never reused.
- Copy `0000-template.md` to start a new ADR.
- Open it as **Proposed**, move it to **Accepted** once ratified, and to
  **Superseded by NNNN** when a later ADR replaces it (never edit an accepted
  decision in place — write a new ADR that supersedes it).

## Status lifecycle

```
Proposed → Accepted → Superseded by NNNN
                   ↘ Rejected
```

## Style

The project doc-style guard applies here too: use **Milestone**, not "Sprint",
and keep prose plain and factual (no "critical", "robust", "comprehensive").

## Index

Each row's **Decision** is a single concise sentence — two to three lines at
most — just enough to identify the decision and tell ADRs apart. The full
rationale, alternatives, and consequences belong in the ADR body, not here.
When you add an ADR, append a row in numeric order and keep the summary to one
sentence; do not paste the abstract.

| ADR | Decision | Status |
|-----|----------|--------|
| [0001](0001-greenfield-rewrite.md) | Greenfield rewrite, in Python. | Proposed |
| [0002](0002-multi-user-mcp-http.md) | Multi-user service; MCP over streamable HTTP. | Proposed |
| [0003](0003-six-durable-objects.md) | Six durable objects replace the run-centric model. | Proposed |
| [0004](0004-first-slice-local-libvirt.md) | First slice targets local libvirt/QEMU. | Proposed |
| [0005](0005-postgres-object-store-state.md) | Postgres + object store for state; advisory locks replace flock. | Proposed |
| [0006](0006-oidc-rbac-attribution.md) | OIDC/SSO + RBAC keyed on (principal, agent_session). | Proposed |
| [0007](0007-metering-budgets-admission.md) | Metering + budgets/quotas with an admission-control gate. | Proposed |
| [0008](0008-async-worker-tier-job-queue.md) | Async worker tier + durable job queue. | Proposed |
| [0009](0009-capability-provider-dispatch.md) | Capability-based provider dispatch. | Superseded for runtime assembly by 0063 |
| [0010](0010-fastmcp-framework-auth.md) | FastMCP server framework + streamable-HTTP auth. | Proposed |
| [0011](0011-provisioning-profile-schema.md) | Provisioning-profile schema. | Proposed |
| [0012](0012-secret-backend.md) | Secret backend (file-ref for M0). | Proposed |
| [0013](0013-object-store-layout-retention.md) | Object-store layout & retention. | Proposed |
| [0014](0014-structured-logging.md) | Structured logging via stdlib `logging` + `contextvars`. | Proposed |
| [0015](0015-sql-migration-runner.md) | Forward-only SQL migration runner. | Proposed |
| [0016](0016-repository-layer-locks-idempotency.md) | Repository layer, advisory locks, idempotency ledger. | Proposed |
| [0017](0017-object-store-client-interface.md) | Object-store client interface & failure contract. | Proposed |
| [0018](0018-job-queue-worker-execution.md) | Job-queue enqueue/dequeue + worker execution contract. | Proposed |
| [0019](0019-tool-response-envelope.md) | Uniform tool-response envelope. | Proposed |
| [0020](0020-rbac-audit-gate-implementation.md) | RBAC roles, audit record, destructive-op gate (M0 shapes). | Proposed |
| [0021](0021-reconciler-loop-drift-repair.md) | Reconciler loop: drift repair, leaked-domain reaping, lease-expiry compensation. | Proposed |
| [0022](0022-capability-registry-dispatch-impl.md) | Capability registry & dispatch implementation shapes (refines 0009). | Superseded for runtime assembly by 0063 |
| [0023](0023-discovery-allocation-admission.md) | Discovery registration & per-host allocation admission (M0). | Proposed |
| [0024](0024-provisioning-profile-model-shape.md) | Provisioning-profile model shape (M0, refines 0011). | Proposed |
| [0025](0025-provisioning-plane-libvirt.md) | Provisioning plane: System creation & teardown on local libvirt (M0). | Proposed |
| [0026](0026-investigation-run-lifecycle.md) | Investigation + Run lifecycle & tools (M0). | Proposed |
| [0027](0027-safety-modules-secret-backend-impl.md) | Safety modules & file-ref secret backend (impl, refines 0012). | Proposed |
| [0028](0028-control-plane-power-force-crash.md) | Control plane: power + force_crash on local libvirt (M0). | Proposed |
| [0029](0029-build-plane-local-make.md) | Build plane (local make): runs.build, BuildProfile, build handler (M0). | Proposed |
| [0030](0030-install-boot-plane.md) | Install + boot plane on local libvirt: runs.install, runs.boot (M0). | Proposed |
| [0031](0031-retrieve-plane-vmcore-postmortem.md) | Retrieve plane: vmcore capture/fetch + crash postmortem (M0). | Proposed |
| [0032](0032-connect-plane-gdbstub-debugsession.md) | Connect plane (gdbstub) + DebugSession lifecycle (M0). | Proposed |
| [0033](0033-drgn-introspection-from-vmcore.md) | Debug plane: offline drgn introspection from vmcore (M0). | Proposed |
| [0034](0034-debug-plane-gdbmi-tier.md) | Debug plane: gdb-MI tier — breakpoints, memory/register reads, continue/interrupt (M0). | Proposed |
| [0035](0035-walking-skeleton-e2e-harness.md) | Walking-skeleton end-to-end integration-test harness (M0). | Proposed |
| [0036](0036-reservation-lease-semantics.md) | Reservation / lease semantics (M1). | Proposed |
| [0037](0037-rbac-hardening-role-separation.md) | RBAC hardening: operator/admin separation (M1). | Proposed |
| [0038](0038-system-reprovision-in-place.md) | System reprovision-in-place (M1). | Proposed |
| [0039](0039-ssh-transport-live-introspection.md) | SSH transport + live drgn introspection (M1). | Proposed |
| [0040](0040-admission-lifecycle-concurrency.md) | M1 admission & lifecycle concurrency: lock hierarchy, idempotency, atomic reconciliation. | Proposed |
| [0041](0041-versioning-release-process.md) | Versioning policy (SemVer) & tag-driven release process. | Proposed |
| [0042](0042-live-stack-e2e-mcp-http.md) | Live-stack end-to-end functional test over MCP HTTP; supersedes the gated tier of 0035 (M1.2). | Proposed |
| [0043](0043-platform-scoped-rbac-tier.md) | Platform-scoped RBAC tier (`platform_roles`) + cross-project auditor surface (extends 0006). | Proposed |
| [0044](0044-mcp-wire-harness-oidc-token-issuance.md) | MCP-over-HTTP wire harness + OIDC token issuance (M1.2). | Proposed |
| [0045](0045-spine-driver-capability-grant-phase-naming.md) | Spine driver: out-of-band capability grant + phase-failure naming contract (M1.2). | Proposed |
| [0046](0046-spine-report-phase-accounting-assertions-artifact.md) | Spine `report` phase: accounting assertions + report artifact (M1.2). | Proposed |
| [0047](0047-agent-facing-tool-guide-generation.md) | Agent-facing tool guide generated from the registry. | Proposed |
| [0048](0048-external-build-artifact-ingestion.md) | External-build artifact ingestion: agent uploads, no server-side make. | Proposed |
| [0049](0049-crash-capture-tiers.md) | Crash-capture tiers: provider-agnostic method, local-libvirt realizations. | Proposed |
| [0050](0050-vmcore-method-aware-storage.md) | Method-aware vmcore storage: first-method-wins per System (refines 0049/0031). | Proposed |
| [0051](0051-install-method-conditional-crashkernel.md) | Install-time capture-method resolution + method-conditional crashkernel gate. | Proposed |
| [0052](0052-bootable-rootfs-image-builder.md) | Bootable rootfs builder: whole-disk-ext4 layout + managed SSH key. | Proposed |
| [0053](0053-build-checkout-seam.md) | Build checkout seam: warm-tree rsync + local config/patch refs. | Proposed |
| [0054](0054-object-store-unconditional-read.md) | Object-store unconditional read for system-produced keys. | Proposed |
| [0055](0055-install-readiness-kdump-seam.md) | Install-readiness console classifier + host initrd-presence kdump gate. | Proposed |
| [0059](0059-first-run-host-registration.md) | First-run local-libvirt host registration at reconciler startup. | Proposed |
| [0060](0060-per-system-rootfs-overlay.md) | Per-System rootfs overlay: a writable qcow2 layer over the shared base. | Proposed |
| [0061](0061-boot-cmdline-composition.md) | Boot cmdline composition: platform base + appended debug args (supersedes 0056). | Proposed |
| [0062](0062-platform-operations.md) | Platform operations (M1.3): operator infra/control-plane tools, break-glass, auditor reads. | Proposed |
| [0063](0063-typed-provider-runtime.md) | Typed ProviderRuntime is the active M0/M1 provider seam. | Accepted |
| [0064](0064-expected-boot-failures-artifact-search.md) | Expected boot failures + bounded redacted artifact search. | Proposed |
| [0065](0065-provider-component-references.md) | Provider component references and profile requirements. | Proposed |
| [0066](0066-remove-capability-registry-prototype-from-src.md) | Remove the capability-registry prototype from production source. | Accepted |
| [0067](0067-system-shapes-catalog.md) | System shapes catalog + selector unification (M1.4). | Proposed |
| [0068](0068-custom-config-pcie-modeling.md) | Custom config + PCIe capability modeling (M1.4). | Proposed |
| [0069](0069-reservation-pending-queue-scheduler.md) | Reservation / FIFO pending-queue scheduler (M1.4). | Proposed |
| [0070](0070-fleet-availability-system-reuse.md) | Fleet availability + system reuse (M1.4). | Proposed |
| [0071](0071-per-kind-provider-runtime-registry.md) | Per-kind ProviderRuntime registry — the provider selection seam (M1.5). | Proposed |
| [0072](0072-fault-injection-provider-seeded-engine.md) | Fault-injection provider + seeded decision-keyed fault engine (M1.5). | Proposed |
| [0073](0073-forced-secret-resolution-redaction.md) | Forced secret resolution + end-to-end redaction validation (M1.5). | Accepted |
| [0074](0074-fault-inject-engine-port-wiring.md) | Wire the seeded fault engine into the fault-inject ports (M1.5). | Proposed |
| [0075](0075-objectstore-quarantine-pre-registration-writes.md) | Object-store quarantine for pre-registration writes (M1.5). | Proposed |
| [0076](0076-remote-libvirt-provider-package.md) | Independent remote-libvirt provider package + portability diff gate (M2). | Proposed |
| [0077](0077-qemu-tls-control-transport.md) | qemu+tls:// control transport + x509 client-cert secret-by-reference (M2). | Proposed |
| [0078](0078-object-store-in-target-install-seam.md) | Object-store + presigned-URL in-target install/retrieve seam (M2). | Proposed |
| [0079](0079-remote-live-debug-transport.md) | Remote live-debug transport: direct-TCP gdbstub, in-guest drgn, worker-side postmortem (M2). | Proposed |
| [0080](0080-remote-provisioning-disk-image-profile.md) | Remote provisioning: disk-image base-OS profile, domain-XML gdbstub port registry, storage-pool overlay (M2). | Proposed |
| [0081](0081-remote-build-kernel-bundle.md) | Remote build publishes a single vmlinuz+modules bundle as `kernel_ref` (M2). | Proposed |
| [0082](0082-remote-install-in-guest-kernel.md) | Remote install: in-guest kernel install via one allowlisted helper + boot-id readiness (M2). | Proposed |
| [0083](0083-remote-connect-debug-plane.md) | Remote connect/debug plane: shared gdb-MI/drgn infra + ACL'd direct-TCP gdbstub (M2). | Proposed |
| [0084](0084-remote-control-two-phase-vmcore-retrieve.md) | Remote control over TLS + two-phase vmcore retrieve (M2). | Proposed |
| [0085](0085-drgn-live-transport-generalization.md) | Generalize the live-drgn transport off the ssh model (`drgn-live` capability token) (M2). | Proposed |
| [0086](0086-dead-worker-gdbstub-reconciler-reset.md) | Dead-worker gdbstub reconciler reset: free the single-client port on stale detach (M2). | Proposed |
| [0087](0087-config-registry.md) | Central typed configuration registry for the `KDIVE_*` contract (M2.1). | Proposed |
| [0088](0088-deployment-packaging.md) | Deployment & packaging: one multi-process image, compose + Helm reference, GHCR publish (M2.1). | Proposed |
| [0089](0089-operator-cli-mcp-client.md) | Operator CLI (`kdivectl`) as an authenticated MCP client (M2.2). | Proposed |
| [0090](0090-opentelemetry-adoption-service-health.md) | OpenTelemetry adoption: logs/metrics/traces spine, log-signal migration (amends 0014) (M2.3). | Proposed |
| [0091](0091-doctor-diagnostics-model.md) | `doctor` / diagnostics model: server-side authz-gated diagnostics tool (M2.3). | Proposed |
| [0092](0092-image-rootfs-lifecycle.md) | Image & rootfs lifecycle: Python build planes + `image_catalog` DB table as source of truth (M2.4). | Proposed |
| [0093](0093-private-image-uploads.md) | Private image uploads: owner-scoped, TTL'd, reconciler-pruned (M2.4). | Proposed |
| [0094](0094-remote-host-dump-via-coredump-volume.md) | Remote host_dump via core-dump-to-volume + presigned-PUT stream download (M2.5). | Proposed |
| [0095](0095-reconciler-remote-console-collector.md) | Reconciler-supervised remote console collector (M2.5). | Proposed |
| [0096](0096-kdump-config-fragment-build-input.md) | Kdump kernel-config fragment as a seeded build-config catalog input. | Proposed |
| [0097](0097-not-found-conflict-error-categories.md) | `not_found` / `conflict` error categories; ungranted rows stay indistinguishable from absent. | Proposed |
| [0098](0098-membership-denial-envelope.md) | Envelope project-membership denials as `authorization_denied` (supersedes 0020 §4 for named scopes). | Proposed |
| [0099](0099-remote-build-host-targets.md) | Remote build-host targets: `BuildTransport` seam (local/ssh) + `build_hosts` inventory & selection. | Proposed |
| [0100](0100-ephemeral-libvirt-build-vm.md) | Ephemeral remote-libvirt build VM (`kind='ephemeral_libvirt'`) over the guest-agent exec channel. | Proposed |
| [0101](0101-local-libvirt-remote-build-host.md) | Local-libvirt builds on a remote build host via a transport-capable local builder. | Proposed |
| [0102](0102-build-host-clone-dir-cleanup.md) | Clean up the per-run build workspace after a terminal build. | Proposed |
| [0103](0103-build-host-reachability-probe.md) | Reconciler reachability probe flips `build_hosts.state` for SSH hosts. | Proposed |
| [0104](0104-chunked-external-upload-reassembly.md) | Chunked external-build uploads >5 GiB with server-side reassembly + per-chunk SHA-256. | Proposed |
| [0105](0105-build-config-seed-actionable-error.md) | Actionable `remediation` in the error when the kdump build-config entry is unseeded. | Proposed |
| [0106](0106-build-rootfs-guest-image-wiring.md) | `build-rootfs` emits an eval-safe `export KDIVE_GUEST_IMAGE=…` line on stdout. | Proposed |
| [0107](0107-cli-mutating-tool-call-opt-in.md) | `kdivectl tool call` reaches mutating/destructive tools by explicit deny-by-default opt-in. | Proposed |
| [0108](0108-helm-demo-oidc-role-claims.md) | Helm demo OIDC role claims as a configurable value defaulting to a full RBAC grant. | Proposed |
| [0109](0109-reap-leaked-active-allocation.md) | Reap leaked `active` allocations whose System is terminal/absent past a grace window. | Proposed |
| [0110](0110-remote-s3-endpoint-guest-routable.md) | Remote install/capture preflight the S3 endpoint as guest-routable. | Proposed |
| [0111](0111-orphaned-domain-name-fallback-reaping.md) | Reap name-orphaned libvirt domains via the `kdive-<uuid>` naming convention. | Proposed |
| [0112](0112-systems-inventory-config.md) | Declarative systems inventory (`systems.toml`) reconciled into the DB under config/discovery/runtime ownership. | Proposed |
| [0113](0113-flat-tool-output-schema.md) | Advertise a flat tool `outputSchema` to stop the recursive-schema client error. | Proposed |
| [0114](0114-production-release-readiness.md) | Production-release readiness: docs structure, host preflight, packaging. | Proposed |
| [0115](0115-declarative-cost-class-coefficients.md) | Declarative cost-class coefficients in `systems.toml` (extends 0007). | Proposed |
| [0116](0116-granted-set-project-naming.md) | Name authorized projects in the granted-set accounting report. | Proposed |
| [0117](0117-projects-list-whoami.md) | Add a read-only `projects.list` (whoami) discovery tool. | Proposed |
| [0118](0118-wait-on-resource-mechanisms.md) | Wait-on-resource mechanisms: `allocations.wait`, queue-position hint, derived `retryable`. | Proposed |
| [0119](0119-operator-build-config-write-path.md) | Operator write-path for build-config fragments via the `buildconfig.set` tool. | Proposed |
| [0120](0120-operator-fixture-profile-write-path.md) | Operator override for local-libvirt fixture profiles via file/ConfigMap + `fixtures.validate`. | Proposed |
| [0121](0121-decouple-migrate-validate-systems.md) | Decouple `migrate()` to SQL-only + deploy-time `systems.toml` validation. | Proposed |
| [0122](0122-declarative-build-config-systems-toml.md) | Declarative `[[build_config]]` home in `systems.toml` (file-authoritative `source='config'`). | Proposed |
| [0123](0123-tool-error-detail-surfacing.md) | Tool-error `detail` surfacing on the response envelope (refines 0019). | Proposed |
| [0124](0124-provisioning-profile-discoverability.md) | Provisioning-profile discoverability: typed `profile` param + `systems.profile_examples` tool. | Proposed |
| [0125](0125-diagnostics-host-reachability.md) | Diagnostics host-reachability probe, incl. a remote-libvirt `qemu+tls://` check (refines 0091). | Proposed |
| [0126](0126-synchronous-tool-transport-bound.md) | Synchronous-tool transport bound: offload blocking calls + dispatch-boundary timeout envelope. | Proposed |
| [0127](0127-local-libvirt-reaper-opt-out.md) | Local-libvirt reconciler reaper opt-out when local-libvirt is not a registered provider. | Proposed |
| [0128](0128-remote-provision-vm-creation-gaps.md) | Remote-libvirt VM creation: discoverable base volume + non-masking provision failures. | Proposed |
| [0129](0129-systems-teardown-admin-authority.md) | `systems.teardown` requires project `admin` only (drops the dead `capability_scope` check). | Proposed |
| [0130](0130-destructive-gate-per-op-revision.md) | Destructive gate drops the un-grantable `capability_scope` check for reprovision/power/force_crash. | Proposed |
| [0131](0131-local-libvirt-discovery-opt-out.md) | Local-libvirt startup discovery opt-out (extends 0127 to provider-discovery registration). | Proposed |
| [0132](0132-allocation-denial-ergonomics.md) | Allocation-request denial ergonomics + sizing-source discoverability. | Proposed |
| [0133](0133-profile-examples-onboarding-chain.md) | Profile-examples onboarding: optional disk-image kernel source + full discovery chain. | Proposed |
| [0134](0134-chart-upgrade-config-drift.md) | Chart upgrade correctness: config-checksum rollout + config-default drift fix. | Proposed |
| [0135](0135-investigation-naming-reporting-fields.md) | Investigation naming + reporting fields: `description`/`title`, `investigations.set`/`list`. | Proposed |
| [0136](0136-runs-build-reachability.md) | `runs.build` reachability: sharpen the warm-tree error + name both build lanes. | Proposed |
| [0137](0137-build-profile-schema-discoverability.md) | Build-profile schema discoverability at the MCP boundary (mirrors 0124 for the build lane). | Proposed |
| [0138](0138-transport-reset-retry-contract.md) | Transport-reset retry contract for long-polls + explicit uvicorn keepalive. | Proposed |
| [0139](0139-diagnostics-worker-vantage-substitution-honesty.md) | Diagnostics worker-vantage substitution attributes its cause (not-enabled vs unavailable). | Proposed |
| [0140](0140-artifacts-get-content-retrieval.md) | End-to-end artifact retrieval through `artifacts.get`: inline content + presigned download. | Proposed |
| [0141](0141-failed-run-reason-surfacing.md) | Surface a failed Run's failure reason on `runs.get` via a linked job id. | Proposed |
| [0142](0142-diagnostic-precondition-ergonomics.md) | Diagnostic-tool precondition ergonomics: reason-keyed `not_found` + next actions. | Proposed |
| [0143](0143-investigation-enumerate-runs.md) | `investigations.get` enumerates its attached runs/systems. | Proposed |
| [0144](0144-ephemeral-build-network-readiness.md) | Ephemeral build-VM network-readiness gate + surface `git fetch`'s return code. | Proposed |
| [0145](0145-reconciler-tolerate-premigrate-schema.md) | Console-hosting tolerates a not-yet-migrated schema at startup. | Proposed |
