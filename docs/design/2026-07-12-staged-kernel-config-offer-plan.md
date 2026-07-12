# Implementation plan — staged kernel-config offer (#1132)

Spec: `docs/design/2026-07-12-staged-kernel-config-offer.md` · ADR: `docs/adr/0336-staged-kernel-config-offer.md`

Branch: `feat/staged-kernel-config-offer-1132` · Base: `main` · No migration.

TDD throughout: write the failing test, then the code. Commit per task with a
conventional message. Keep `just lint type test` green at each commit.

## Task 1 — Shared `config_object_key` helper

- **Code:** In `services/images/publish.py`, extract
  `config_object_key(provider, name, arch, visibility, owner) -> str` (pure; the
  `images/{provider}[__{owner}]/{name}/{arch}.config` scheme). Reimplement
  `kernel_config_object_key(request)` as a wrapper over it. Also extract the qcow2
  key sibling `image_object_key` onto the same helper if it shares the scoping (keep
  the change minimal — only what both callers need).
- **Test:** `tests/services/images/test_publish.py` (or a focused new test) — public
  vs private scoping; wrapper equals helper for a `PublishRequest`.

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

- **Code:** In `inventory/reconcile/images.py`:
  - `_RealizedImage` gains `kernel_config_key: str | None`.
  - The staged-path branch of `_realize` computes the key: if the existing row's
    key is absent and `read_config_sibling(path)` returns bytes, upload to
    `config_object_key(...)` via a lazily-built object store and set the key; else
    carry the existing row's key. All other source branches carry the existing
    row's key (preserve). The upload is advisory — catch `CategorizedError` (no
    store / put failure) and any read miss, degrade to the existing key, log at
    debug/warning.
  - The reconcile INSERT and UPDATE column lists include `kernel_config_key`.
  - Thread an object-store factory (default `object_store_from_env`) into the
    reconcile entrypoint as a seam so tests inject a fake and the no-S3 path is
    exercised.
- **Test:** `tests/inventory/reconcile/test_images.py` (or the reconcile test
  module) — staged-path with sibling + absent key → uploads + sets key; with key
  already set → no upload, key preserved; sibling absent → key preserved/NULL; a
  store that raises (no-S3) → degrades to NULL, reconcile still succeeds; a build/s3
  row's key is never clobbered.

## Task 4 — `kdive stage-volume` command

- **Code:** New `images/rootfs/stage_volume.py` (or `providers/remote_libvirt`
  command module) with `run_stage_volume(args)`:
  1. Resolve the remote-libvirt config + target `[[image]]` row; fail fast
     (`CONFIGURATION_ERROR`, actionable) if the row is absent.
  2. Probe `/boot/config` locally on `--from` via the boot-facts probe (advisory).
  3. `volUpload` the qcow2 into the pool over the shared `remote_connection`
     lifecycle; fatal on libvirt fault (`INFRASTRUCTURE_FAILURE`).
  4. When a config was captured, upload it to `config_object_key(...)` and `UPDATE`
     `kernel_config_key` on the row (advisory).
  - Register the subparser in `__main__`/`cli` (`add_stage_volume_parser`), mirroring
    `add_build_fs_parser`.
- **Test:** unit with a mocked libvirt connection + fake object store + fake DB:
  probe→upload→attach happy path sets the key; missing row → fail fast, no upload;
  `volUpload` fault → command fails, no key set; probe returns `None` → volume
  uploads, no key set; attach/put failure → volume staged, no key (advisory).
- **Note:** keep libvirt/guestfs calls behind injected seams so the unit tests need
  neither a host nor guestfish. The live proof covers the real path.

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
