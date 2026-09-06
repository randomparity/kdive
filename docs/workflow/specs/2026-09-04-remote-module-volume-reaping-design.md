# Remote module volume reaping — design

Issue: [#2168](https://github.com/randomparity/kdive/issues/2168). This design implements Task 5
of the accepted [ADR-0588](../../adr/0588-remote-module-volume-ownership-lives-in-the-volume-name.md)
ownership design against the current multi-host provider composition.

## Goal

The reconciler must reclaim remote-libvirt module volumes after their durable mutation or reap
obligation has discharged, while never deleting a foreign volume, a retained attempt's volume, or
storage referenced by an active or inactive domain definition.

## Invariants

- A volume is owned only when `parse_module_volume_name` full-matches its entire name. The sweep
  neither opens nor interprets volume contents.
- On each reachable host, the sweep refreshes and completely enumerates the configured storage
  pool before reading retained owners. A retained-set snapshot is therefore never older than a
  volume classified against it.
- Mutation retention protects `source.ext4` and `scratch.ext4`; reap retention independently
  protects `reaping.journal` and `reaped.journal`.
- The domain-reference set is resolved after retained owners and before deletion. The collector
  reuses #2167's landed complete disk-graph traversal: every `source` below a disk, including
  nested backing stores and data stores, plus active and legacy mirror path attributes. Every
  candidate and referenced path is resolved through ADR-0603's landed, Resource-bound
  `RemoteDeviceIdentityPort`; lexical equality is not an identity fallback. Missing identity,
  malformed identity, timeout, or operational lookup failure aborts the host preflight before the
  first delete. A volume-backed source contributes the identity of the canonical path returned by
  its named pool and volume. One bounded traversal and identity-call budget covers the complete
  host preflight.
- A referenced candidate is reported with bounded conflict telemetry and left intact while
  independent candidates continue. Foreign and retained volumes are silent skips. Any libvirt
  error that prevents a complete reference set is categorized as infrastructure failure with
  details limited to `pool` and, when known, `volume`.
- `VIR_ERR_NO_STORAGE_VOL` during delete is an achieved post-state and counts as removed. Repeating
  the sweep converges.

## Components and data flow

`providers/remote_libvirt/reaping/module_volumes.py` owns the synchronous, single-host libvirt
algorithm and the interfaces named by #2168, with one explicit
`RemoteDeviceIdentityPort` parameter added to the destructive reaper. It refreshes and enumerates
once, calls the retained-owner callback only after enumeration, resolves all domain references
through the shared `volume_references` and `path_references` traversal from
`remote_module_attachments.py`, rejects conflicts, and deletes the remaining candidates. #2168
promotes those two landed private helpers to provider-package interfaces without changing their
behavior, so the attempt-scoped inspector and whole-pool sweep cannot drift onto different disk
graphs. Neither the connection nor a helper constructs or captures identity authority implicitly.

`RemoteLibvirtModuleVolumeReaper` is the asynchronous fleet port. Its libvirt work runs through
#2170's landed completion-owned `RemoteModulePreparationExecutor` and the existing remote-reaper
connection bundle. Each fleet configuration supplies its immutable Resource-bound authority
binding; the adapter materializes the existing typed authority sender and ADR-0603 identity port
for that host with one bounded sweep deadline and passes that port explicitly to the single-host
reaper. A configured host without an authority route fails
closed rather than falling back to local or lexical identity. The low-level algorithm's
synchronous retained-owner callback bridges back to the reconciler event loop with
`asyncio.run_coroutine_threadsafe`; the event loop remains free while awaiting the worker thread,
so the callback can query Postgres at the exact point required by ADR-0588. This avoids a pre-read,
a second enumeration, and a new synchronous database connection. The landed executor retains
capacity through true worker completion, drains repeated cancellation without changing the
caller's cancellation count, and does not wait during process shutdown. Thus later cancellation
cannot orphan the destructive thread or its retained-owner future. A reachable-host operation or
authority error aborts the lane; connection-open failures retain the existing fleet behavior of
logging and skipping only that unreachable host.

The provider-neutral `ModuleVolumeReaper` port accepts an async callback returning immutable
`ModuleVolumeKey` values. The remote adapter converts those values to its provider-specific
`ModuleVolumeOwner` model. A null implementation makes the lane inert when remote-libvirt is not
configured.

`reconciler/cleanup/provider_resources/module_volume_reaping.py` supplies that async callback from
`RemoteModuleAttemptObligationRepository.retained_owners`. It expands each retained attempt by the
two kinds governed by each true retention flag, excluding `kind` from the durable ownership key
while preserving the two independent obligations. The lane returns the provider's removed count.

Provider composition registers the concrete port only for an enabled remote-libvirt deployment.
`ReconcileConfig` carries the port, the repair catalog runs it beside the other provider-volume
lanes, and `ReconcileReport` exposes its count and failure name.

## Failure handling and observability

The low-level provider operation is fail-closed. Pool lookup, refresh, enumeration, domain XML,
volume-path lookup, and deletion errors raise a categorized error, so the reconciler records
`reaped_module_volumes` in `failures` and continues later lanes. No partial count is reported after
an error, although deletions completed before a later delete failure remain effective and the next
pass re-derives state.

The reference set is built completely before any candidate is deleted. A referenced candidate is
then skipped with one warning carrying only pool and volume; an independent orphan remains
reclaimable in the same pass. An incomplete reference set still aborts before every delete.
Unreachable remote hosts use the established per-host warning and are retried next pass.

## Threat model

### Boundary inventory and actors

- Existing boundary widened: libvirt returns operator-controlled domain XML, pool names, volume
  names, and backing paths to the reconciler. The relevant untrusted actor is a remote-host
  operator or another workload permitted to define libvirt storage and domains.
- Existing boundary widened: Postgres returns server-written obligation rows to the reconciler.
  The database and server role are trusted; tenants cannot write these rows directly.
- Existing boundary widened: the reconciler can cause irreversible deletion on the configured
  remote host. Provider configuration and its referenced credentials are operator-controlled and
  trusted to select the intended fleet.

### Controls

- Whole-name anchored parsing is the ownership boundary; no prefix or malformed near-match can
  reach deletion.
- Domain XML is parsed with the landed bounded parser, and the shared traversal covers top-level
  and nested source, backing-store, data-store, and mirror forms. Volume references must resolve
  through libvirt or the entire deletion preflight fails closed.
  Every direct or managed path must then resolve through the Resource-bound identity port;
  unavailable, malformed, or operationally failed identity lookup fails closed. No local
  filesystem resolution participates.
- Durable retention is read after enumeration and expanded by the kind-specific obligation flags.
- Candidate and reference paths are sent only through the typed, bounded ADR-0603 identity request;
  they are never opened locally, executed, or interpolated into a shell command.
- Public errors expose only configured pool and volume identifiers. Connection credentials,
  domain XML, paths, and host identities do not enter error details.

### Out of scope

The design does not defend against a privileged remote-host operator falsifying both libvirt's
inventory and domain state; that operator already controls the storage being protected. It does
not add a lock against a new domain definition racing after reference preflight: ADR-0588 bounds
that window by ordering and requires the immediate pre-delete check, while libvirt offers no
cross-domain-and-volume transaction. Native ppc64le proof is explicitly excluded from this issue's
campaign run.

## Verification

The provider tests cover every named Task 5 behavior, including foreign-name exclusion,
both obligation classes, the enumeration/read interleaving, unresolved references, attachment
conflicts, mixed attached/orphan pools, active and inactive lexical aliases, nested backing/data/
mirror references, symlink, hard-link, bind, and block-device aliases, distinct-device
non-conflicts, and idempotent disappearance. A shared-traversal test pins the reaper to #2167's
exported helpers. Fleet-adapter tests prove the callback crosses from the worker thread at the
required point, two cancellation requests cannot finish the adapter before the worker and callback
finish, the cancellation count is preserved, and unreachable-host handling remains inherited.
Fake identity-port tests record both candidate and reference lookups and prove absent or malformed
identity prevents the first delete. A composition test proves the per-host port is passed through
unchanged.
Lane tests prove obligation expansion, catalog registration, reporting, failure isolation, and
disabled composition. Focused lint and whole-tree typing cover the protocol boundary; `just ci` is
the pre-push gate.
