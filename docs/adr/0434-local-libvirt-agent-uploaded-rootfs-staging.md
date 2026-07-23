# ADR 0434 — Local-libvirt agent-uploaded rootfs: provision-time staging and lease-scoped teardown

- **Status:** Accepted
- **Date:** 2026-07-23
- **Depends on:** [ADR-0048](0048-external-build-artifact-ingestion.md) (the agent-upload
  transport, the System-owned `_UploadRootfs` kind, the pre-provision upload window, and the
  `_commit_uploaded_rootfs` write-once commit — §5/§6, whose §7 deferred the step this closes),
  [ADR-0228](0228-local-staged-path-catalog-source.md) (the connectionless, injected
  synchronous rootfs fetch this mirrors), [ADR-0060](0060-per-system-rootfs-overlay.md) (the
  per-System overlay backed by the staged base), [ADR-0017](0017-object-store-client-interface.md)
  (the object store).
- **Spec:** [`../specs/2026-07-23-agent-uploaded-rootfs-local-libvirt-743-design.md`](../specs/2026-07-23-agent-uploaded-rootfs-local-libvirt-743-design.md)

## Context

ADR-0048 §5/§6 built the agent→S3 upload side of a System-owned custom rootfs: the
`_UploadRootfs` profile kind (`{"kind": "upload"}`), the presigned PUT minted by
`artifacts.create_system_upload`, the pre-provision upload window opened by `systems.define`,
and the HEAD-only `_commit_uploaded_rootfs` that writes the write-once `artifacts` row at
`provisioning -> ready`. ADR-0048 §7 explicitly deferred the "install/boot" step: actually
**downloading** the uploaded object to the provisioning host and using it as the domain's
rootfs base.

Today that gap is a stub. `_materialize_uploaded_rootfs`
(`providers/local_libvirt/lifecycle/rootfs/materialize.py`) computes
`upload_rootfs_path(...)` and returns it **without any I/O** — no file is ever fetched to that
path. A `{"kind": "upload"}` local System therefore reaches `ready` with a committed
`artifacts` row while its per-System overlay is backed by a **nonexistent** base file: it
cannot boot. `tests/integration/test_systems_define_upload_provision.py` proves every step up
to, but not including, this staging step, and its docstring names the deferral.

Issue #743 closes this for local-libvirt: an agent uploads a custom qcow2, and local-libvirt
downloads and installs it **temporarily for the duration of the System lease** as a custom
debug environment's root filesystem. This is also the local-side precedent the deferred remote
counterpart (#1433) is parity-blocked on.

## Decision

### 1. Download at provision via an injected, connectionless `upload_fetch`

The provider provision seam is synchronous and owns no Postgres pool or object store (it runs
off the event loop via `asyncio.to_thread`). Mirroring the ADR-0228 catalog-fetch injection,
`LocalLibvirtProvisioning.from_env` wires a lazily-constructed
`upload_fetch: Callable[[RootfsUploadContext], Path]` that builds its object store per call.
`_materialize_uploaded_rootfs` delegates to it; an unwired lane raises `configuration_error`
("upload rootfs materialization is not wired for this lane"), exactly as the catalog branch
does. The fetch downloads the deterministic object key
`artifact_key(tenant, "systems", str(system_id), "rootfs")` and returns the staged local path.

### 2. Verify against the object's stored checksum; no DB read

ADR-0048 §2 settled the write-side model: the presigned PUT signs the agent-declared
`x-amz-checksum-sha256`, and the store **rejects at PUT time** a body whose checksum does not
match — so the stored object's content is pinned to the agent's declared sha256. The read side
is not free, though: `get_artifact` is a plain GET with no checksum comparison. So the download
**HEADs** the object (`ChecksumMode=ENABLED`, which returns the base64 SHA-256 stored at PUT —
`HeadResult.checksum_sha256`), GETs the bytes, recomputes their SHA-256, and rejects a mismatch
with `infrastructure_failure`. This is the same post-PUT check the `head().checksum_sha256`
contract already exists for (`artifacts/storage.py`), and it catches transport corruption and
post-PUT bit-rot that the PUT-time signature alone does not. An object present but carrying **no**
stored checksum (PUT outside the presigned path) is rejected too, mirroring
`runs.complete_build`'s no-checksum rejection.

The verification needs **only the object store, no DB connection** — unlike the catalog path,
whose trust anchor is a DB `image_catalog` row it must resolve first; the uploaded object carries
its own checksum. A missing object fails fast with `configuration_error` ("upload-kind rootfs was
never uploaded"), mirroring `_commit_uploaded_rootfs`'s guard. The download runs strictly
**before** the `artifacts` row is committed (commit is at `provisioning -> ready`, after
`provision()` returns), so there is no `artifacts`-row digest to check against by construction —
the object's own stored checksum is the anchor.

### 3. Atomic, idempotent staging OUTSIDE the provider roots

The fetch writes to `upload_rootfs_path(system_id)` under a dedicated `rootfs-uploads` directory
that lives **outside** `allowed_roots` — the sibling of the catalog `rootfs-cache`, for the same
no-escape reason (ADR-0228): a staged image inside `allowed_roots` would be reachable as a `local`
staged-path candidate, so System B's `local` rootfs ref could name System A's uploaded SENSITIVE
image. Bytes are written to a `.partial` temp file and `os.replace`d into `dest` **only after the
checksum passes**, so `dest` is only ever a verified base and a crash mid-download never leaves a
truncated file a retry would reuse. A present `dest` is therefore a previously-verified download
and is **reused** (skip re-download) — matching the overlay create-only-when-absent contract
(ADR-0060) and the catalog cache-hit — so a provision retry under at-least-once delivery never
re-pulls a multi-GB object.

### 4. Lease-scoped teardown reclaim — local file **and** S3 object

The SENSITIVE image must live only for the lease, so teardown reclaims it in **both** places:

- **Local staged base** — `LocalLibvirtProvisioning.teardown` removes the per-System staged file
  **after** the domain is destroyed and the overlay/baseline are removed. This unlink is
  **fail-loud** (raises `infrastructure_failure` on a real `OSError`, `missing_ok` on absence),
  exactly like the overlay/baseline removal it sits beside — a persistent failure dead-letters the
  teardown job rather than silently leaking the image on disk. It is **not** best-effort.
- **Committed S3 object** — the worker reclaims the `artifact_key(tenant,"systems",<id>,"rootfs")`
  object and its `artifacts` row at teardown, because `owner_kind='systems'` objects are exempt
  from the #768 expiry reaper and would otherwise linger forever. The **object delete is
  best-effort** (a store fault must not block teardown, like the console/sysrq reclaim), but the
  **`artifacts`-row delete is fail-loud** in its own transaction — like the bootstrap-key delete
  in the same handler — because the row *is* the download handle (`artifacts.fetch_raw` presigns
  through it), so dropping it revokes agent access to the SENSITIVE image even if the object byte
  delete failed and left an unreferenced orphan.

Both reclaims are unconditional and a no-op for a non-upload System (absent file / absent
object+row), like the existing per-System reclaims.

### 5. `upload` stays outside `accepted_component_sources`

`_UploadRootfs` is a distinct discriminated `RootfsSource` kind with its own admission window;
`validate_profile_for_provider`/`validate_rootfs_for_provider` already short-circuit it before
the generic `reject_unsupported_component_source` gate. The local ROOTFS accepted set stays
`{catalog, local}`; upload is not added to it. Nothing on remote-libvirt changes (#1433 stays
deferred), so the ADR-0428/#1428 parity waivers remain accurate.

### 6. The provision lanes, and removing a now-dead guard

The missing-object HEAD check (decision 2) is the single, sufficient enforcement across every
lane, so the pre-existing `reject_rootfs_without_upload_window` guard is **removed as dead code**
(it has zero production callers — it was written for ADR-0048 §5 but never wired — and its
premise "reprovision can never have a staged object" is now false):

- **One-step create provision** (`systems.provision` create) with an `upload` rootfs: the System
  never opened a define+upload window, so no object exists → HEAD returns `None` →
  `configuration_error` ("upload-kind rootfs was never uploaded"). The guard's job is done by the
  materialize check, with a clearer, later-but-still-pre-boot failure.
- **`reprovision`** with an `upload` rootfs: the committed S3 object **persists** after the first
  `provisioning -> ready` (commit only HEADs it), so `reprovision` (teardown-then-provision
  in-place) re-downloads and re-verifies the *same* object and reprovisions from it. Correct — a
  reprovision reapplies the profile, and the uploaded image is the profile's rootfs. The
  provider-level teardown inside `reprovision` removes the local staged file (re-fetched on the
  following provision) but not the S3 object (that reclaim is teardown-*handler*-only, so a live
  System keeps its uploaded image).

To keep an admission-time `validate_rootfs_ref` from ever issuing a bogus HEAD for the
placeholder `UUID(int=0)`, it gains an explicit `_UploadRootfs` short-circuit (upload validation
is deferred to provision, exactly as `catalog` already is) — belt-and-suspenders behind the
`validate_*_for_provider` short-circuits that already never route an upload ref to it.

## Consequences

- A `{"kind": "upload"}` local System now boots the agent's custom rootfs. The last mile of
  ADR-0048 is closed for local-libvirt.
- The staged base is per-System, verified before use, and reclaimed at teardown from **both**
  local disk and the object store → no cross-lease leakage of the SENSITIVE image, and host disk
  is reclaimed with the lease.
- No new DB column, no migration, no MCP-surface change, no AI surface. The change is a stubbed
  provider function made real, two teardown reclaim steps, and their wiring.
- **Residual — verify anchors on the object's own stored checksum, not a fresh re-anchor to the
  agent's declaration.** The stored checksum *is* the declared value (bound at PUT, ADR-0048 §2),
  so this catches transport/storage corruption but not a hypothetical store that lets the checksum
  and body drift together. Accepted: the PUT-time signature is the end-to-end anchor and no
  cheaper stronger check exists without the DB the catalog path needs.
- **Residual — the S3 reclaim is best-effort.** A store fault during teardown leaves the rootfs
  `artifacts` row (and possibly the object), and no `owner_kind='systems'` expiry reaper collects
  it — identical to the console/sysrq reclaim residual. The local-disk removal, by contrast, is
  fail-loud. The asymmetry is intentional: a leaked host file is a local-disk and boot-safety
  concern (fail the job), a leaked store object is a hygiene concern already tolerated for
  same-owner artifacts (do not block teardown on it).
- **Residual — not a shared digest-keyed cache.** Each System pulls its own object even if two
  Systems upload identical bytes. Correct for the SENSITIVE, System-owned, lease-scoped model;
  a shared cache would break teardown isolation.

## Considered & rejected

- **Trust the PUT-time checksum with no read-side verification.** Rejected: `get_artifact` does a
  plain GET, so nothing re-checks the bytes the guest is about to boot; the catalog path it mirrors
  deliberately hash-verifies. A HEAD-checksum comparison adds a read-side integrity gate at the
  cost of one hash of bytes already in memory — cheap relative to booting a corrupt rootfs. (It is
  not the catalog's *independent* anchor — the object's stored checksum is the same value bound at
  PUT — but it is the only read-side check available without the DB the catalog path needs; see
  the Residual.)
- **Verify at download against the upload *manifest* sha256 (DB-connected).** Read the `rootfs`
  manifest entry's declared sha256 over a Postgres connection, like the catalog fetch resolves its
  row. Rejected in favor of the object's own stored checksum (decision 2): the two values are equal
  by construction (both are the PUT-signed declaration), so the manifest read buys no extra safety
  while forcing a DB connection into an otherwise connectionless fetch — and the manifest is
  deleted at commit, so it is not even a durable anchor for a post-`ready` re-materialization.
- **Download into a digest-keyed shared cache, or at commit time (like catalog).** Rejected:
  the object is SENSITIVE and System-owned, so a shared cache breaks per-lease isolation and the
  teardown-reclaim contract; and commit runs *after* provision, too late to back the overlay.
- **Wire `upload` into `accepted_component_sources`.** Rejected: `_UploadRootfs` is a distinct
  discriminated kind with its own admission window and validation short-circuit; adding it to
  the generic component-source gate would double-model it (the "consumed vs. declared"
  distinction, #1428).
- **Do nothing (leave the stub).** Rejected: the feature is unusable — #743's entire point —
  and a System silently reaching `ready` with an unbootable overlay is a latent phantom-feature.
