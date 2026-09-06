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
  its named pool and volume. One host preflight admits at most 4,096 distinct normalized paths
  across candidate paths, direct disk-graph references, and named-volume canonical paths. It
  deduplicates normalized paths before identity lookup, so lexical aliases consume one lookup only
  when normalization makes them identical. Discovery of a 4,097th distinct path aborts with a
  redacted `INFRASTRUCTURE_FAILURE` before the first delete. This whole-host ceiling bounds both
  authenticated identity traffic and preflight latency.
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

The reconciler does not execute this authority-bearing provider operation. Its cleanup lane
idempotently enqueues one platform-internal `remote_module_volume_reap` job with the closed payload
`{"schema":"remote-module-volume-reap-v1"}` and the stable deduplication key
`remote-module-volume-reap:v1`. The payload carries no host, endpoint, credential, path, owner, or
caller-selected destination. Migration `0131` adds the persisted job-kind enum value. The
synthetic authorizing principal and project are both `remote-libvirt`, matching the existing
platform-internal worker-check convention. Project naming is not an isolation boundary: a tenant
may legitimately have the same project name. `REMOTE_MODULE_VOLUME_REAP` is therefore classified
by kind as platform-internal. Tenant `jobs.list`, point read/wait, and cancel surfaces exclude it
before project-role evaluation even when the caller is a member of a colliding `remote-libvirt`
project. The existing platform-operator `ops.jobs_list` surface remains able to observe the row.

Queue uniqueness admits at most one row for the stable key. Each reconciler pass uses terminal
recycling: an existing queued or running job remains unchanged, while a succeeded, failed, or
canceled row returns to queued with its attempt and lease state cleared. Concurrent passes
serialize on the unique row. The ordinary worker lease and retry contract handles worker-process
failure; a reclaimed or retried job safely repeats the full inventory because ownership and
retention are re-read and deletion treats absence as achieved.

The worker handler owns the provider call. It receives the claimed job's database connection,
validates the closed payload, and supplies the retained-owner callback from
`RemoteModuleAttemptObligationRepository.retained_owners`. The callback expands each retained
attempt by the two kinds governed by each true retention flag, excluding `kind` from the durable
ownership key while preserving the independent obligations. Worker assembly constructs the
concrete fleet adapter with the active worker incarnation's credential and each Resource's fixed
authority binding; the handler cannot accept an endpoint or credential from the payload. Disabled
remote-libvirt composition uses the null port and succeeds without provider work. The handler logs
only the aggregate removed count and returns no tenant-visible result.

`ReconcileReport` exposes `module_volume_reap_jobs_enqueued`, not a deletion count: successful
enqueue/recycle returns one and an already in-flight job returns zero. Queue or validation failure
uses the repair catalog's existing failure isolation and later lanes continue.

The ownership interfaces are explicit:

```python
async def enqueue_remote_module_volume_reap(conn: AsyncConnection) -> bool: ...

async def remote_module_volume_reap_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    reaper: ModuleVolumeReaper,
) -> None: ...
```

`WorkerHandlerAssembly` carries `module_volume_reaper: ModuleVolumeReaper` and registers that exact
handler for `JobKind.REMOTE_MODULE_VOLUME_REAP`. `build_worker_handler_assembly` passes its
active-incarnation sender factory to `ProviderComposition.build_worker_module_volume_reaper`.
That method replaces and removes `build_reconciler_module_volume_reaper`. No reconciler
configuration, process assembly, or repair-catalog entry receives a reaper or authority sender.

## Failure handling and observability

The low-level provider operation is fail-closed. Pool lookup, refresh, enumeration, domain XML,
volume-path lookup, and deletion errors raise a categorized error. They remain worker-job failures:
the ordinary worker contract retries or terminalizes the durable row and records its redacted job
telemetry. They cannot retroactively enter the completed reconciler tick's report. The reconciler
records only enqueue/recycle failure under `module_volume_reap_jobs_enqueued` and continues later
lanes. No partial removed count is returned after an error, although completed deletions remain
effective and a retry re-derives state. Success emits the aggregate removed count only through
bounded worker-job telemetry, not `ReconcileReport` or a tenant-visible result.

The reference set is built completely before any candidate is deleted. A referenced candidate is
then skipped with one warning carrying only pool and volume; an independent orphan remains
reclaimable in the same pass. An incomplete reference set still aborts before every delete.
Unreachable remote hosts use the established per-host warning and do not starve later hosts. A
handler failure follows the queue's bounded attempt/lease behavior; after terminal failure, a later
reconciler pass recycles the stable row and re-derives all state.

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
- New boundary: a platform-internal durable queue row crosses from reconciler to worker. Its closed,
  constant-size payload selects neither a Resource nor an authority destination. Only worker
  assembly may borrow the active incarnation credential and bind it to configured Resources.
- Existing boundary narrowed: tenant job APIs authorize ordinary rows by project name. The reap
  kind is denied independently of that name, preventing a tenant-created project collision from
  exposing or canceling platform maintenance. Platform-admin queue visibility remains unchanged.

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
- The host preflight performs no more than 4,096 identity lookups. It normalizes and deduplicates
  all candidate, direct-reference, and named-volume canonical paths before lookup; exceeding the
  distinct-path ceiling fails closed before deletion and reports no path.
- Public errors expose only configured pool and volume identifiers. Connection credentials,
  domain XML, paths, and host identities do not enter error details.
- `REMOTE_MODULE_VOLUME_REAP` belongs to a shared platform-internal job-kind set consumed by every
  tenant list/read/wait/cancel path. Denials retain the ordinary not-found-shaped response, while
  the existing platform-operator queue view continues to include the kind.
- The stable queue key admits one in-flight sweep; terminal recycling, worker leases, and
  idempotent missing-volume deletion make retries and process restart convergent. Queue payload
  validation rejects extra or alternate fields before provider work.

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
identity prevents the first delete. Boundary tests prove exactly 4,096 distinct normalized paths
are admitted, a 4,097th produces redacted `INFRASTRUCTURE_FAILURE` before deletion, and normalized
aliases consume one lookup without weakening identity-based alias protection. A composition test
proves the per-host port is passed through unchanged.
Lane tests prove obligation expansion, catalog registration, reporting, failure isolation, and
disabled composition. Queue tests prove the closed bounded payload, stable-key in-flight
deduplication, terminal recycling, concurrent admission, retry after failure, and restart reclaim.
Handler tests prove retention reads occur only after provider enumeration, the active
worker-incarnation sender is used with fixed Resource bindings, and payload values cannot select a
destination. Composition assertions prove worker assembly owns the reaper, reconciler configuration
owns no reaper or sender, and the removed `build_reconciler_module_volume_reaper` name is absent.
Failure-path assertions distinguish reconciler enqueue failure from later worker retry, terminal
failure, and removed-count telemetry. Focused lint and whole-tree typing cover the protocol
boundary; `just ci` is the pre-push gate.
