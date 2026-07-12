# Implementation plan — staged kernel-config offer (#1132)

Spec: `docs/design/2026-07-12-staged-kernel-config-offer.md` · ADR: `docs/adr/0336-staged-kernel-config-offer.md`

Branch: `feat/staged-kernel-config-offer-1132` · Base: `main` · No migration.

TDD throughout: write the failing test, then the code. Commit per task with a
conventional message. Keep `just lint type test` green at each commit.

## Task 1 — Shared `config_object_key` helper (single source of truth)

- **Code:** In `services/images/publish.py`, extract
  `config_object_key(provider, name, arch, visibility, owner) -> str`. It must be
  the **single source** of the key, not a parallel f-string: implement it by routing
  through the same `_write_request`/`ObjectWriteRequest` + `_object_owner_kind`
  machinery the current `kernel_config_object_key(request)` uses (so the produced key
  is byte-identical to today's write/fetch key), and reimplement
  `kernel_config_object_key(request)` to delegate to it. Do the same for
  `image_object_key` only if trivial; otherwise leave it.
- **Test:** assert `config_object_key(provider, name, arch, visibility, owner) ==
  kernel_config_object_key(request)` for the matching `PublishRequest`, for **both**
  a public and a private identity — guards against key drift that would make
  reconcile/stage-volume write configs the fetch path can't presign.

## Task 2 — `read_config_sibling` bounded reader + `build-fs` sibling write

- **Code:** In `images/rootfs/staged_provenance.py` (beside `read_sidecar`), add
  `config_sibling_path(qcow2) -> Path` (`<qcow2>.config`) and
  `read_config_sibling(qcow2) -> bytes | None` (bounded read, cap ~4 MiB; `None` on
  absent/oversize/unreadable, never raises). In `images/rootfs/command.py`,
  `run_build_fs` writes `output.kernel_config` to the sibling after `_publish_rootfs`
  when it is not `None`; advisory (log+swallow `OSError`), mirroring
  `_write_provenance_sidecar`.
- **Test:** `tests/images/test_staged_provenance.py` — round-trip present bytes;
  absent/oversize/unreadable → `None`. `tests/images/test_rootfs_command.py` —
  sibling written when the fake plane returns `kernel_config`; not written when
  `None`; a sibling-write failure does not fail the build.

## Task 3 — Reconcile uploads + sets `kernel_config_key` for staged-path

**CRITICAL (review finding #1): widen the SELECT or reconcile silently NULLs a
`publish_image`-owned key.** `_load_config_rows` (`images.py:172`) does not select
`kernel_config_key`. If the new `UPDATE ... SET kernel_config_key = %s` ships without
adding the column to that SELECT, every preserve branch reads `None` and wipes the
published config offer on the first reconcile — and the "key absent" gate misfires as
always-absent, re-uploading every tick. This is the exact asymmetry with `provenance`,
which survives only because it *is* in the SELECT.

- **Code:** In `inventory/reconcile/images.py`:
  - Add `kernel_config_key` to the `_load_config_rows` SELECT column list.
  - `_RealizedImage` gains `kernel_config_key: str | None`.
  - Every non-uploading `_realize` branch carries `_opt_str(row, "kernel_config_key")`
    (preserve). The staged-path branch: if the existing key is absent **and**
    `read_config_sibling(path)` returns bytes, upload to `config_object_key(...)` and
    set the key; else carry the existing key. Upload is advisory — catch
    `CategorizedError` (put failure) and any read miss, degrade to the existing key,
    log at warning.
  - The reconcile INSERT and UPDATE column lists include `kernel_config_key`.
  - **Reuse the store already passed in** (review finding #3): `reconcile_images`
    already receives `store` typed as the narrow `ImageHeadStore` (`head_present`
    only, `images.py:63`). Widen that protocol to add `put_artifact` and reuse it —
    do **not** thread a second `object_store_from_env` factory. Restate the no-S3
    story accurately: the CLI reconcile path already refuses to run without a store
    (`__main__.py:217`), so the degrade that matters is a *put failure* on the
    loop/reconciler path, not a missing store on the CLI path.
- **Test:** staged-path with sibling + absent key → uploads + sets key; with key
  already set → no upload, key preserved; sibling absent → key preserved/NULL; a
  store whose `put_artifact` raises → degrades to the existing key, reconcile still
  succeeds; **a published build/s3 row (row actually carries `kernel_config_key`)
  survives N reconciles with its key intact** (finding #1 regression guard).

## Task 4 — `kdive stage-volume` command (net-new remote volume upload)

**Scope note (review finding #2): the volume upload is net-new, not wiring.** kdive
has no `volUpload` prior art — only volume *download* (`retrieve/host_dump_capture.py`
`newStream`/download). The upload primitive must be written. Reuse the *connection*
seams (`providers/remote_libvirt/connection/transport.py` `remote_connection`, and
the pool lookup used by `diagnostics/base_image_staging.py` /
`connection/staged_volumes.py`); the create+upload+stream path is the new part.

- **Code:** New `providers/remote_libvirt/lifecycle/rootfs/stage_volume.py` plus a CLI
  command module with `run_stage_volume(args)`:
  1. Resolve the remote-libvirt config + target `[[image]]` catalog row; fail fast
     (`CONFIGURATION_ERROR`, actionable "declare and reconcile the [[image]] first")
     if the row is absent.
  2. Probe `/boot/config` locally on `--from` via the boot-facts probe (advisory).
  3. Upload the qcow2 into the pool over the shared `remote_connection` lifecycle
     — the net-new sequence, behind an injected `VolumeUpload` seam:
     `storagePoolLookupByName(pool)` → `storageVolCreateXML(vol_xml)` (qcow2 format,
     capacity = qcow2 file size) → `newStream(0)` → `vol.upload(stream, 0, length)`
     → `stream.sendAll(reader)` → `stream.finish()`. Fatal on libvirt fault
     (`INFRASTRUCTURE_FAILURE`); clean up a partially-created volume on failure.
  4. When a config was captured, upload it to `config_object_key(...)` and `UPDATE`
     `kernel_config_key` on the row (advisory — the volume already landed).
  - Register the subparser in `__main__` (`add_stage_volume_parser`), mirroring
    `add_build_fs_parser`. Operator CLI only — not an agent tool.
- **Test:** unit with a mocked libvirt connection + fake object store + fake DB:
  probe→upload→attach happy path sets the key and drives the create/stream/upload/
  finish calls in order; missing row → fail fast, no upload; upload libvirt fault →
  command fails, partial volume cleaned up, no key set; probe returns `None` →
  volume uploads, no key set; attach/put failure → volume staged, no key (advisory).
  The `VolumeUpload` seam keeps these host- and guestfs-free.
- **Live proof:** the local path is proven this session; the remote `stage-volume`
  round-trip is live-verified on the 2-host remote-libvirt HW tier when available,
  and noted in the PR as such if the tier is not reachable during this change.

## Task 5 — Docs + operator note

- Operator note (in the relevant provider walkthrough or a new short section):
  `stage-volume` usage, the declare-then-stage ordering, and the "clear
  `kernel_config_key` to refresh after a rebuild" caveat.
- Ensure `just docs-check config-docs-check env-docs-check` pass (new CLI command
  may need help-text/doc generation refresh).

## Task 6 — Guardrails + live functional proof

- `just ci` green (lint, type, docs, tests).
- Live: rebuild `fedora-kdive-ready-44` (`build-fs`) → reconcile → `images.kernel_config`
  returns a presigned URL to the real `/boot/config-<ver>`; `has_kernel_config: true`.
  Capture the evidence in the PR.
- Remote tier: a `stage-volume` round-trip sets the offer on a staged volume row
  (as available).

## Risk notes for review

- **Reconcile now touches S3.** Confirm the advisory degrade is airtight — a no-S3
  or flaky store must never fail or slow reconcile past the one gated put.
- **Idempotency / churn.** The "key absent" gate must prevent re-upload each tick.
- **Preserve rule.** Verify no source branch clobbers a `publish_image`- or
  `stage-volume`-owned key.
- **stage-volume auth/placement.** The command mutates a remote host and the
  catalog; confirm it belongs as an operator CLI (not an agent tool) and cannot
  target an arbitrary row.
