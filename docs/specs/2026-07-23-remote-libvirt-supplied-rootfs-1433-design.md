# Remote-libvirt supplied rootfs — provision-time volume staging (#1433)

**ADR:** [ADR-0440](../adr/0440-remote-libvirt-supplied-rootfs-staging.md)
**Issue:** #1433 (epic #1423 remote-libvirt parity)
**Status:** design

## Problem

A remote System's base image must be an operator-staged `base_image_volume` (a volume name
already on the remote pool). There is no path from a *supplied* qcow2 to that base image, even
though `upload_qcow2_volume` (ADR-0336) exists — it is wired only into the `stage-volume` CLI, and
`RemoteLibvirtProfilePolicy.rootfs_source()` returns `None` so the component-source gate is dead.

## Acceptance criteria (from #1433)

1. Remote accepts a ROOTFS component source from the source kinds local accepts.
2. A supplied qcow2 is staged onto the remote pool and becomes the System's `base_image_volume`.
3. A partially-uploaded volume is cleaned up on fault, per the existing primitive's contract.
4. Image-content obligations documented for the supplier; staging-time-check decision recorded.
5. Operator-staged images continue to work unchanged.

## Design (see ADR-0440 for rationale)

- **Profile** (`profiles/provisioning.py`): `RemoteLibvirtProfile` gains
  `base_image_source: LocalComponentRef | None = None`; `base_image_volume` becomes
  `NonEmptyStr | None = None`; a `model_validator` requires exactly one. Docstring records the
  supplier's content obligations.
- **Policy** (`remote_libvirt/profile_policy.py`): `rootfs_source()` returns `base_image_source`.
- **Component map** (`remote_libvirt/composition.py`): add `ROOTFS_COMPONENT: {local}`.
- **Provisioning** (`remote_libvirt/lifecycle/provisioning.py`): before `ensure_overlay`, resolve
  the base volume. Operator path → `(base_image_volume, created=False)`. Supplied path →
  `validate_local_component_path` (roots `[/var/lib/kdive/rootfs]`, sha256), probe
  `lookup_volume_staged` for created-vs-reuse, `upload_qcow2_volume` (injected seam) into
  `kdive-<id>-base.qcow2`, `(name, created)`. On define/start failure reclaim the created base
  volume best-effort beside `cleanup_overlay_if_created`.
- **Primitive** (`remote_libvirt/lifecycle/rootfs/volume_upload.py`): add a `QFI\xfb` qcow2-magic
  gate before create+stream.
- **XML** (`remote_libvirt/lifecycle/xml.py`): add `supplied_base_volume_name(system_id)`.
- **Storage** (`remote_libvirt/lifecycle/storage.py`): add best-effort `cleanup_created_volume`.

## Not in scope / decisions

- `catalog` and `upload` ROOTFS kinds are excluded (ADR-0440 §2). No migration (0076 unused). No
  admission-time rootfs validator (`runtime.rootfs.validator` stays `None`; kind-checked at
  admission, path/format at the stage). No MCP/RBAC change.

## Test surface

- Profile: exactly-one validator (neither/both reject), supplied+operator parse.
- Policy: `rootfs_source()` returns source / None.
- Composition: `_component_sources()` map gains `ROOTFS: {local}`; local accepted, catalog/upload/
  artifact rejected.
- volume_upload: qcow2-magic accept + reject; existing fakes use magic bytes.
- Provisioning: supplied-rootfs provision stages base + backs overlay; idempotent reuse of a staged
  base; define/start failure reclaims the created base volume; operator-staged path stages nothing.

## Live proof

A remote-libvirt live proof needs a *second* remote host (remote `live_vm` tier, #1424). This dev
host runs local KVM directly and may have no remote target; unit + integration coverage mocks the
libvirt connection/stream at the boundary. Live-proof status is reported to the orchestrator.
