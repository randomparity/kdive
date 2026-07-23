# ADR 0440 — Remote-libvirt supplied rootfs: provision-time volume staging

- **Status:** Accepted
- **Date:** 2026-07-23
- **Depends on:** [ADR-0080](0080-remote-provisioning-disk-image-profile.md) (the disk-image
  provisioning model, the operator-staged `base_image_volume`, and the per-System overlay backed
  by it), [ADR-0336](0336-staged-kernel-config-offer.md) (the dormant `upload_qcow2_volume`
  primitive this consumes), [ADR-0430](0430-remote-libvirt-kernel-vmlinux-component-source.md) (remote's
  `local` worker-host component-source precedent, #1432),
  [ADR-0434](0434-local-libvirt-agent-uploaded-rootfs-staging.md) (local's supplied-rootfs
  precedent and the qcow2-magic staging gate) / [ADR-0438](0438-rootfs-transport-strip-streaming-fetch.md)
  (the `QFI\xfb` format gate), [ADR-0435](0435-reclaim-failed-provision-artifacts.md) (the
  reclaim-only-what-this-call-created contract), [ADR-0065](0065-provider-component-references.md)
  (the component-reference model).
- **Spec:** [`../specs/2026-07-23-remote-libvirt-supplied-rootfs-1433-design.md`](../specs/2026-07-23-remote-libvirt-supplied-rootfs-1433-design.md)

## Context

A remote-libvirt System boots a `disk-image` base OS (ADR-0080). Today the base is named by
`RemoteLibvirtProfile.base_image_volume` — a qcow2 volume an **operator** has already staged on
the remote host's storage pool out of band. `RemoteLibvirtProfilePolicy.rootfs_source()` returns
`None` unconditionally, so `reject_unsupported_component_source` is dead for remote and no
component-source path can turn a *supplied* qcow2 into a System's base image, even though the
`upload_qcow2_volume` primitive that would place it there already exists (ADR-0336) — wired only
into the `stage-volume` CLI, never into provisioning.

Issue #1433 closes that gap. It is the remote counterpart to #743/ADR-0434 (local supplied
rootfs), which has since merged — so this reaches **parity** with local rather than getting ahead
of it (the concern the issue raised was conditioned on #743 being open).

## Decision

### 1. A `base_image_source` profile field; exactly one of source-or-volume

`RemoteLibvirtProfile` gains `base_image_source: LocalComponentRef | None = None` and
`base_image_volume` becomes `NonEmptyStr | None = None`. A model validator requires **exactly
one** of the two: the operator-staged path (`base_image_volume`, unchanged) or the supplied path
(`base_image_source`). Neither-or-both is a `configuration_error` at parse. `rootfs_source()`
returns `base_image_source` (a `LocalComponentRef` is a valid `RootfsSource` variant), so the
previously-dead `reject_unsupported_component_source` gate is now live for remote.

### 2. Only the `local` source kind — deliberately narrower than local's `{catalog, local}`

Remote advertises `ROOTFS_COMPONENT: {local}` — a worker-host absolute qcow2 path
(`LocalComponentRef`), the same `local` kind remote already accepts for `KERNEL`/`VMLINUX`
(ADR-0430). It does **not** add `catalog`, and it does **not** add the System-owned `upload` kind:

- **`catalog` is already served by `base_image_volume`.** A remote catalog/`staged` `[[image]]`
  is reconciled into `image_catalog` with a `volume` name and mapped straight to
  `base_image_volume` (`images/cataloging/read_model.py`) — the image lives *on the remote host*,
  not in S3. A `catalog` component source would mean "download the object to the worker host, then
  re-upload it to the host it already lives on": a semantically-wrong duplicate. So the catalog
  role stays the operator-staged volume-name reference (AC5, unchanged).
- **`upload` (agent S3, `_UploadRootfs`) is out of the component-source framing.** It is a
  System-owned kind with its own admission window, kept outside `accepted_component_sources` on
  every provider (ADR-0434 §5). Adding it to remote is #743-style new scope beyond #1433's ACs;
  because remote adopts the `local` kind, the ADR-0439 transport-`encoding` parity (epic #1508
  Req 9) remains deferred — there is still no remote S3-upload consumer to gzip-strip into.

### 3. Stage at provision time onto a per-System base volume

When `base_image_source` is set, `RemoteLibvirtProvisioning.provision` — after `lookup_pool`,
before `ensure_overlay` — resolves the `LocalComponentRef` to a worker-host path via the shared
`validate_local_component_path` (absolute, inside the provider allowed roots
`[/var/lib/kdive/rootfs]`, a regular readable file, optional `sha256` match), then
`upload_qcow2_volume`s it over the already-open mutual-TLS connection into a per-System base
volume `kdive-<system_id>-base.qcow2`, and feeds **that** name to `ensure_overlay`. The
operator-staged path is unchanged: `base_image_volume` is fed to `ensure_overlay` directly, no
upload. The upload seam is injected (default `upload_qcow2_volume`) so the orchestration is
unit-tested without a real libvirt stream, mirroring the injected `bootstrap_injector`.

Idempotency (ADR-0080 §4): a `lookup_volume_staged` probe before the upload records whether **this
call** creates the base volume; a base already present is a prior successful stage and is reused
(`upload_qcow2_volume` is itself idempotent — it skips a present volume), so a provision retry
never re-streams a multi-GiB object.

### 4. Format-gate at staging; existence-only verification stands (the load-bearing decision)

`upload_qcow2_volume` gains a `QFI\xfb` qcow2-magic gate on the local file before it creates and
streams the volume — matching local's ADR-0434/0438 gate and closing the same
no-format-validation gap (it also hardens the pre-existing `stage-volume` CLI path). A non-qcow2
file is rejected with `configuration_error` before any volume is created.

Beyond format, **existence-only verification stands** (ADR-0080). The deeper image-content
obligations — qemu-guest-agent enabled, drgn present, matching vmlinux/debuginfo — are **not
introspectable from a volume lookup** and **transfer to whoever supplies the image**. They are
documented on the `base_image_source` field and surface, unchanged, at their natural boundaries: a
broken image provisions successfully and then fails at guest-agent contact (provision agent-gate,
`provisioning_failure`), install, or debug. No guest-agent/drgn/vmlinux probe is added at staging
time — the same decision local made — because staging streams bytes over TLS with no ability to
boot or introspect the image, and a partial content probe would give false assurance.

### 5. Reclaim the base volume this attempt staged, on failed provision

The volume upload primitive already cleans up its **own** partial volume on a stream fault
(ADR-0336: abort + best-effort delete → `infrastructure_failure`), satisfying AC3 for a mid-stream
fault. Extending ADR-0435's reclaim-only-what-this-call-created contract to the later
define/start-failure path: when define/start raises after a *successful* stage, `provision`
reclaims the created base volume (best-effort delete, swallowing a secondary error so it never
masks the original) alongside the existing `cleanup_overlay_if_created`. A base volume that
pre-existed (operator-staged, or a prior attempt's reused stage) is left in place. Agent-gate
failures deliberately leave the domain + overlay + base in place as the diagnosable artifact and a
convergent retry, exactly as today.

## Consequences

- A remote System can boot a supplied qcow2 without an operator pre-staging it out of band; the
  supplied image is staged per-System and its overlay backs onto it. Remote reaches parity with
  local #743.
- Operator-staged `base_image_volume` Systems are unchanged (AC5): the field is still accepted, and
  its provisioning path skips the stage entirely.
- No new DB column, no migration (provenance rides the profile JSON / `provider_components.source`,
  as sibling #1432 did), no new MCP tool or RBAC surface. Reserved migration 0076 is **unused**.
- **Residual — content obligations are unverified at staging.** A format-valid qcow2 lacking the
  guest agent / drgn / a matching vmlinux provisions and then fails later at guest-agent, install,
  or debug. Accepted and documented: the obligations transfer to the supplier and the format gate
  is the only cheap check available without booting the image (identical to local ADR-0434).
- **Residual — a failed-provision base-volume reclaim is best-effort.** A libvirt fault during the
  reclaim leaves the per-System base volume; the teardown path and reconciler remain the backstops,
  matching `cleanup_overlay_if_created`.

## Considered & rejected

- **Add `catalog` to the remote ROOTFS accepted set for literal `{catalog, local}` parity with
  local.** Rejected: a remote catalog image is the host-staged volume named by `base_image_volume`
  (read_model), so a catalog component source would download-then-re-upload an image to the host it
  already lives on. The narrower `{local}` set is the honest capability; catalog stays the
  operator-staged volume reference.
- **Add the `upload` (agent S3) kind to remote too.** Rejected as new scope: `upload` is a
  System-owned kind outside `accepted_component_sources` (#1433's ACs frame this as a *component
  source*), and it is #743's local-specific story. Deferred with the ADR-0439 encoding parity.
- **Validate the supplied path at admission (a remote rootfs validator), like local's `local`
  kind.** Rejected for scope: staging is a provision-time upload, like local's `upload` kind which
  ADR-0434 also defers to provision. Admission validates the source *kind*
  (`reject_unsupported_component_source`); the path/format is validated at the stage. `runtime.rootfs.validator`
  stays `None`.
- **Reuse the operator `base_image_volume` name for the supplied volume.** Rejected: a per-System
  `kdive-<id>-base.qcow2` name keeps the supplied base System-scoped (reclaimable with the lease,
  never colliding with an operator volume or another System's).
- **Do nothing (leave `rootfs_source()` returning `None`).** Rejected: that is the phantom-feature
  status quo #1433 exists to close — the dead gate and the unreachable upload primitive.
