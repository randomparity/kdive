# Agent-uploaded rootfs for local-libvirt (#743)

- **Issue:** [#743](https://github.com/randomparity/kdive/issues/743) — Allow Local-Libvirt
  Systems to Use Agent Uploaded Rootfs Image
- **ADR:** [ADR-0434](../adr/0434-local-libvirt-agent-uploaded-rootfs-staging.md)
- **Status:** Draft
- **Date:** 2026-07-23

## Problem

An agent can already upload a custom qcow2 rootfs to S3 for a DEFINED System (ADR-0048 §5/§6:
`systems.define` opens the window, `artifacts.create_system_upload` mints the presigned PUT,
`_commit_uploaded_rootfs` records the write-once `artifacts` row at `provisioning -> ready`).
But local-libvirt never **downloads** that object: `_materialize_uploaded_rootfs` returns a
staging path with no I/O (ADR-0048 §7 deferred the install/boot step). A `{"kind": "upload"}`
System reaches `ready` with its per-System overlay backed by a nonexistent base file — it
cannot boot. The custom-rootfs debug environment #743 asks for is therefore unreachable.

## Goal

Local-libvirt downloads the agent-uploaded rootfs object at provision time, uses it as the
qcow2 backing base of the per-System overlay, and removes the downloaded base at teardown so it
lives only for the duration of the System lease.

## Non-goals

- Remote-libvirt supplied rootfs (#1433 — parity-blocked on this local work; stays deferred).
- The `artifact`-kind rootfs (`ArtifactComponentRef`) — still `not wired yet`, unchanged.
- Any change to the agent-upload transport, the upload window, the admission state machine, or
  the `_commit_uploaded_rootfs` commit — all already built and unchanged.
- Re-hashing / content re-verification of the uploaded object (see ADR-0434 §2).
- A live-boot proof gated in CI (the download+teardown mechanics are unit/integration-tested; a
  local `live_vm` boot of an uploaded rootfs is an optional manual proof, noted below).

## Design (see ADR-0434 for rationale)

1. **Injected connectionless `upload_fetch`.** Add
   `type UploadFetch = Callable[[RootfsUploadContext], Path]` and an
   `upload_fetch: UploadFetch | None` field on `RootfsMaterializationContext`.
   `_materialize_uploaded_rootfs` delegates to it (unwired → `configuration_error`, mirroring
   the catalog branch). A new `rootfs_upload_fetch.py` provides
   `rootfs_upload_fetch_from_env() -> UploadFetch`, lazily building the object store per call
   (mirrors `rootfs_catalog_fetch.py`). **No DB connection** — unlike the catalog fetch, the
   uploaded object carries its own integrity anchor.
2. **The fetch — verify against the object's stored checksum.** Compute
   `key = artifact_key(upload.tenant, "systems", str(upload.system_id), "rootfs")` and
   `dest = upload_rootfs_path(...)`. If `dest` exists, return it (idempotent reuse of a
   previously-verified file). Else: `head(key)` → `None` ⇒ `configuration_error` "upload-kind
   rootfs was never uploaded"; `head.checksum_sha256 is None` ⇒ reject (no integrity anchor,
   like `runs.complete_build`). GET the bytes, recompute base64 SHA-256, compare to
   `head.checksum_sha256` — mismatch ⇒ `infrastructure_failure`. Write verified bytes to
   `dest.with_suffix(".qcow2.partial")`, `os.replace` into `dest`, return `dest`.
3. **Stage outside `allowed_roots`.** `upload_rootfs_path` stages under a dedicated
   `rootfs-uploads` directory (`Path(ROOTFS_DIR).parent / "rootfs-uploads"`), the sibling of the
   catalog `rootfs-cache`, so a staged SENSITIVE image is never reachable as a `local`
   staged-path candidate (ADR-0228 no-escape invariant).
4. **Wiring.** `LocalLibvirtProvisioning.__init__` gains `upload_fetch: UploadFetch | None`;
   `from_env` wires `rootfs_upload_fetch_from_env()`; `_materialize_rootfs_base` threads it and
   the `rootfs-uploads` dir into the context. Unit tests inject a fake fetch.
5. **Teardown — reclaim local file (fail-loud) and S3 object (mixed).**
   `ProvisioningFiles` gains `remove_uploaded_rootfs` (raises `infrastructure_failure` on
   `OSError`, `missing_ok` on absence — mirrors `_real_remove_overlay`) and
   `remove_uploaded_rootfs_for_domain(domain_name)`; `LocalLibvirtProvisioning.teardown` calls it
   after `remove_overlay_for_domain` / `remove_baseline_for_domain`. `teardown_handler` reclaims
   the fixed rootfs object key: the **object delete is best-effort** (store fault does not block
   teardown, in the existing reclaim block) but the **`artifacts`-row delete is fail-loud** in its
   own transaction (like `delete_system_bootstrap_key`), since the row is the download handle.
6. **Provision lanes + remove dead guard.** Remove `reject_rootfs_without_upload_window` and its
   `__all__` export + tests — it has zero production callers and its premise is now false (the
   committed S3 object persists after the first `ready`). The missing-object HEAD check is the
   sufficient enforcement: a one-step create provision with `upload` fails fast (no object);
   `reprovision` re-downloads the persistent object. Add an explicit `_UploadRootfs` short-circuit
   to `LocalLibvirtProvisioning.validate_rootfs_ref` (defer upload validation to provision, as
   `catalog` already is) so a `UUID(int=0)` admission call can never issue a bogus HEAD.

## Acceptance criteria

- **AC1 — download + verify.** With a fake store holding bytes and a matching
  `checksum_sha256` at `artifact_key("local","systems",<id>,"rootfs")`,
  `rootfs_upload_fetch_from_env` (store injected) writes those bytes to
  `upload_rootfs_path(<id>)` (under `rootfs-uploads`) and returns that path.
- **AC2 — missing object.** When `head(key)` is `None`, the fetch raises `CategorizedError`
  `CONFIGURATION_ERROR`, not a silent empty file.
- **AC3 — checksum mismatch / absent.** Bytes whose SHA-256 ≠ `head.checksum_sha256` raise
  `INFRASTRUCTURE_FAILURE`; a `head.checksum_sha256 is None` (object without integrity anchor)
  is rejected, not staged.
- **AC4 — idempotent reuse.** When the staged file already exists, the fetch returns it without
  HEAD/GET (assert the store is not read).
- **AC5 — atomic write.** Bytes reach `dest` only via temp-then-replace and only after the
  checksum passes; a GET failure or mismatch leaves no file at `dest`.
- **AC6 — outside allowed_roots.** `upload_rootfs_path(<id>)` resolves under
  `Path(ROOTFS_DIR).parent / "rootfs-uploads"`, not under any path in the provisioner's default
  `allowed_roots` (`[ROOTFS_DIR]`).
- **AC7 — materialize dispatch.** `materialize_rootfs_base(_UploadRootfs, context=...)` with a
  wired `upload_fetch` returns the fetch's path; with `upload_fetch=None` raises
  `CONFIGURATION_ERROR`.
- **AC8 — provisioning wiring.** `LocalLibvirtProvisioning.provision` for an upload profile
  invokes the injected `upload_fetch` with a `RootfsUploadContext` for `system_id` and passes
  the returned path as the overlay base.
- **AC9 — teardown local reclaim.** After `teardown(domain_name)`, the per-System uploaded
  rootfs file is unlinked; a non-upload teardown (file absent) is a no-op; a real `OSError`
  (not absence) raises `INFRASTRUCTURE_FAILURE`.
- **AC10 — teardown S3 reclaim.** `teardown_handler` deletes the rootfs object and its
  `artifacts` row; a non-upload System (no such object/row) is a no-op; a store fault does not
  block teardown.
- **AC11 — no admission download.** `LocalLibvirtProvisioning.validate_rootfs_ref(_UploadRootfs)`
  returns without a HEAD/GET (assert store untouched) — upload validation is deferred to
  provision.
- **AC12 — dead guard removed.** `reject_rootfs_without_upload_window` no longer exists (import
  fails); its tests are removed. Grep guard: no production reference remains.
- **AC13 — row delete fail-loud.** In `teardown_handler`, a store fault on the rootfs *object*
  delete does not block teardown, but a failure of the `artifacts`-**row** delete propagates
  (teardown job dead-letters), matching `delete_system_bootstrap_key`.
- **AC14 — staging integration.** Extend `test_systems_define_upload_provision.py` (or add a
  sibling) so the provision handler runs against a **real `LocalLibvirtProvisioning`** with fakes
  for every non-rootfs seam (libvirt `connect`/`defineXML`/`getCapabilities`, `ProvisioningFiles`
  `make_overlay`/baseline extract, `free_port`) and the **real** `upload_fetch` over an injected
  store, asserting the uploaded object is staged to the `rootfs-uploads` dir and passed to
  `make_overlay` as the base. The staged object **must carry a `checksum_sha256`** (staged via a
  checksum-storing `put_artifact`, as production PUTs do), or AC3's anchor check rejects it. This
  closes the staging half of the "does NOT boot" gap; actual guest boot is the manual live proof
  below, not this test.

## Failure modes

| Mode | Handling |
|---|---|
| Object never PUT (window opened, agent never uploaded) | `head` is `None` → `configuration_error` at provision → System `provisioning -> failed` (existing `_execute_system_lifecycle_call` path). |
| Object present but checksum mismatch / no stored checksum | `infrastructure_failure` / reject → provision fails, overlay reclaimed by `provision`'s `except`; corrupt/anchorless bytes never boot. |
| GET/HEAD raises (store down) | The store's `INFRASTRUCTURE_FAILURE` propagates → provision fails, overlay reclaimed. |
| Partial write / crash mid-download | Temp file only, replaced after checksum passes; `dest` never partial; retry re-downloads cleanly. |
| Provision retry after `ready` | Handler early-returns (state not `PROVISIONING`); no re-download. |
| Teardown local file: absent | `missing_ok` unlink → no-op. Real `OSError` → fail-loud (dead-letters the job, like overlay/baseline). |
| Teardown S3 object: absent or store fault | No-op / best-effort — does not block teardown (console/sysrq residual). |

## Guardrails

`just lint`, `just type`, `just test` (CI runs each individually). No migration; no generated-doc
regeneration expected (no MCP-surface/schema change) — `just ci` confirms.

## Optional live proof (manual, not gated)

On a KVM/libvirt host: define a System with an `upload` rootfs, `create_system_upload` a small
bootable qcow2, PUT it, `provision_defined`, and confirm the domain boots from the staged base;
then `teardown` and confirm the staged file is gone. Deferred from CI as a manual check.
