# Agent-selectable guest disk (#985) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing `disk_gb` knob actually size the local-libvirt guest disk, bound it with a host-advertised ceiling, seed a curated `debug` shape, and show real per-System size in the operator report.

**Architecture:** `disk_gb` already flows to the concrete stored profile (ADR-0067 reconcile). This plan (a) grows the per-System qcow2 overlay to `disk_gb` with `qemu-img resize` (grow-only, create-path only) and re-enables cloud-init `resize_rootfs` so the guest ext4 fills the device on first boot; (b) advertises a live-derived `disk_gb` host ceiling and enforces it at admission beside the vcpus/memory `≤ resource-caps` check; (c) reports the stamped `requested_*` size (COALESCE onto the shape catalog for legacy rows); (d) seeds a `debug` shape.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`; Postgres migrations (forward-only SQL); libvirt/qemu-img; cloud-init.

## Global Constraints

- ADR: [0312](../../adr/0312-agent-selectable-guest-disk.md). Spec: [agent-selectable-disk-985](../specs/2026-07-04-agent-selectable-disk-985-design.md).
- Guardrails run individually in CI: `just lint`, `just type` (whole tree), `just test`. Run `just docs` after any `@app.tool` wrapper docstring / `Field` change (generated-doc drift). ADR/doc guards: `just adr-status-check`, `just docs-links`, `just check-mermaid`.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict, whole tree (src+tests).
- Uniform `ToolResponse` envelope; `ErrorCategory` from `domain/errors.py` — pick the most specific existing value, never invent strings.
- Migrations are forward-only (ADR-0015); monotonic, never reused. Next free = **0061**.
- Doc-style: **Milestone** not "Sprint"; plain prose (no "critical"/"robust"/"comprehensive"/"elegant"/"significant").
- `disk_gb` stays OUT of the pricing `Selector` (disk is not a kcu input).
- Never shrink an overlay below its backing file; resize is grow-only, create-path only (never touch a live/reused overlay — ADR-0060).

---

### Task 0: Live spike — verify cc_resizefs grows a whole-disk ext4 (evidence gate)

This is a manual verification needing a KVM host, not a code task. It gates **Task 6** (the cloud-init flip) and the final merge — it verifies the guest-side assumption in the spec's "Load-bearing assumption". **Tasks 1-5, 7, and 8 are host-independent code and proceed in parallel without waiting on Task 0**; only Task 6's merge and the feature's done-declaration require Task 0's evidence. If Task 0 disproves the assumption, stop and rework the guest-side design before merging Task 6.

**Files:**
- Modify (evidence only): `docs/superpowers/specs/2026-07-04-agent-selectable-disk-985-design.md`

- [ ] **Step 1: Build/obtain a rootfs image with `resize_rootfs: true`.** On the KVM host, edit `src/kdive/images/families/_fedora_customize.py` `KDIVE_CLOUD_CFG_CONTENT` locally to `resize_rootfs: true` and run `kdive build-fs --image fedora-kdive-ready-44` (or reuse a scratch build). Keep `growpart: {mode: "off"}`.

- [ ] **Step 2: Create an overlay, grow it, boot it.**

```bash
BASE=/var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2
qemu-img create -q -f qcow2 -F qcow2 -b "$BASE" /tmp/spike-overlay.qcow2
qemu-img resize /tmp/spike-overlay.qcow2 60G
qemu-img info --output=json /tmp/spike-overlay.qcow2 | grep virtual-size
# boot it (virt-install / the live-stack), let cloud-init run first boot
```

- [ ] **Step 3: Assert the root filesystem grew.** In the booted guest: `df -h /` should show ≫ 6 GB (near 60 GB). Record the `df` output.

- [ ] **Step 4: Paste evidence into the spec.** Add a short "Verification (Task 0)" note under Part 2 with the `df` output and the image used. If cc_resizefs did NOT grow the fs, STOP and report — the design's guest-side assumption is wrong and needs rework before proceeding.

- [ ] **Step 5: Commit the evidence.**

```bash
git add docs/superpowers/specs/2026-07-04-agent-selectable-disk-985-design.md
git commit -m "docs(985): record cc_resizefs whole-disk-ext4 growth evidence"
```

---

### Task 1: Disk-ceiling capability readers

Add a typed `disk_gb` ceiling reader to Resource capabilities, mirroring `size_ceiling`/`require_size_ceiling`.

**Files:**
- Modify: `src/kdive/domain/catalog/resource_capabilities.py`
- Test: `tests/domain/catalog/test_resource_capabilities.py`

**Interfaces:**
- Produces: `DISK_GB_KEY = "disk_gb"`; `ResourceCapabilities.disk_ceiling() -> int | None`; `ResourceCapabilities.require_disk_ceiling(*, resource_id: UUID, resource_name: str | None) -> int`.

- [ ] **Step 1: Write failing tests.**

```python
# tests/domain/catalog/test_resource_capabilities.py
import uuid
import pytest
from kdive.domain.catalog.resource_capabilities import ResourceCapabilities, DISK_GB_KEY
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_disk_ceiling_reads_non_negative_int():
    caps = ResourceCapabilities.from_mapping({DISK_GB_KEY: 100})
    assert caps.disk_ceiling() == 100


def test_disk_ceiling_none_when_absent_or_invalid():
    assert ResourceCapabilities.from_mapping({}).disk_ceiling() is None
    assert ResourceCapabilities.from_mapping({DISK_GB_KEY: -1}).disk_ceiling() is None
    assert ResourceCapabilities.from_mapping({DISK_GB_KEY: True}).disk_ceiling() is None


def test_require_disk_ceiling_fails_closed_when_absent():
    rid = uuid.uuid4()
    with pytest.raises(CategorizedError) as exc:
        ResourceCapabilities.from_mapping({}).require_disk_ceiling(resource_id=rid, resource_name="h1")
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["key"] == DISK_GB_KEY
```

- [ ] **Step 2: Run to verify failure.** `uv run python -m pytest tests/domain/catalog/test_resource_capabilities.py -q` → FAIL (`DISK_GB_KEY` / `disk_ceiling` undefined).

- [ ] **Step 3: Implement.** In `resource_capabilities.py`, add the key next to `MEMORY_MB_KEY`, add it to `_KNOWN_KEYS`, and add the readers:

```python
DISK_GB_KEY = "disk_gb"
# ... add DISK_GB_KEY to _KNOWN_KEYS frozenset ...

    def disk_ceiling(self) -> int | None:
        return _non_negative_int(self._values.get(DISK_GB_KEY))

    def require_disk_ceiling(self, *, resource_id: UUID, resource_name: str | None) -> int:
        ceiling = self.disk_ceiling()
        if ceiling is None:
            label = resource_name or str(resource_id)
            raise CategorizedError(
                f"host {label} advertises no {DISK_GB_KEY} size ceiling; this is a "
                "host-registration gap, not a problem with your request. Re-register the "
                f"host with a {DISK_GB_KEY} value (remote-libvirt/fault-inject declare it in "
                "systems.toml or resources.register_*; local-libvirt derives it from host "
                "storage at discovery).",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "resource_id": str(resource_id),
                    "resource_name": resource_name,
                    "key": DISK_GB_KEY,
                },
            )
        return ceiling
```

- [ ] **Step 4: Run tests to verify pass.** `uv run python -m pytest tests/domain/catalog/test_resource_capabilities.py -q` → PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/domain/catalog/resource_capabilities.py tests/domain/catalog/test_resource_capabilities.py
git commit -m "feat(985): add disk_gb capability ceiling readers"
```

---

### Task 2: Enforce the disk ceiling at admission

Add `validate_disk_against_resource` and call it in the shared admission pricing/validation path.

**Files:**
- Modify: `src/kdive/domain/accounting/cost.py`
- Modify: `src/kdive/services/allocation/admission/core.py:231` (inside `price_window_and_estimate`)
- Test: `tests/domain/accounting/test_cost.py`; `tests/services/allocation/` (admission)

**Interfaces:**
- Consumes: `Resource.capability_view.require_disk_ceiling(...)` (Task 1), `_caps_error(field, requested, ceiling, resource)` (existing in cost.py).
- Produces: `validate_disk_against_resource(disk_gb: int | None, resource: Resource) -> None`.

- [ ] **Step 0 (PREREQUISITE — do before wiring): make every admission-path test resource advertise `disk_gb`.** Wiring the fail-closed ceiling into the shared `price_window_and_estimate` (Step 4) turns any Resource lacking a `disk_gb` capability into a `configuration_error` on a previously-passing path, so the fixtures MUST advertise it first or the whole admission/allocation/integration suite goes red. Find every Resource-capabilities construction reachable from admission: `rg -n "memory_mb" tests src/kdive/providers/fault_inject **/systems.toml` and any shared builder (`rg -n "def .*resource|capabilities=" tests/services/allocation tests/integration tests/conftest.py`). Add a `disk_gb` value beside each `vcpus`/`memory_mb` (a generous value, e.g. `disk_gb=500`, so existing sized requests stay under it). This includes: repo/test `systems.toml` inventories, fault-inject discovery fixtures, remote-libvirt fixtures, and any inline `ResourceCapabilities.from_mapping`/capabilities dict in admission tests. Run `just test` (or the allocation+integration subset) BEFORE Step 4 to confirm the sweep is complete and the suite is still green with the check un-wired; then run it again AFTER Step 4.

- [ ] **Step 1: Write failing tests.**

```python
# tests/domain/accounting/test_cost.py (add)
import pytest
from kdive.domain.accounting.cost import validate_disk_against_resource
from kdive.domain.errors import CategorizedError, ErrorCategory
# build a Resource whose capabilities advertise disk_gb=50 (reuse the module's Resource factory/fixture)

def test_disk_at_ceiling_is_admitted(resource_with_disk_ceiling_50):
    validate_disk_against_resource(50, resource_with_disk_ceiling_50)  # no raise

def test_disk_over_ceiling_is_configuration_error(resource_with_disk_ceiling_50):
    with pytest.raises(CategorizedError) as exc:
        validate_disk_against_resource(51, resource_with_disk_ceiling_50)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["field"] == "disk_gb"

def test_none_disk_skips_check(resource_with_disk_ceiling_50):
    validate_disk_against_resource(None, resource_with_disk_ceiling_50)  # no raise
```

Use the existing Resource construction pattern in `tests/domain/accounting/test_cost.py` (the same one used for `validate_against_resource` tests); advertise `{"vcpus":..,"memory_mb":..,"disk_gb":50}` in capabilities. If a shared fixture exists, add `disk_gb`; otherwise build the Resource inline mirroring the existing over-caps test.

- [ ] **Step 2: Run to verify failure.** `uv run python -m pytest tests/domain/accounting/test_cost.py -q` → FAIL (`validate_disk_against_resource` undefined).

- [ ] **Step 3: Implement in `cost.py`** (next to `validate_against_resource`):

```python
def validate_disk_against_resource(disk_gb: int | None, resource: Resource) -> None:
    """Reject a disk request exceeding the chosen Resource's advertised disk ceiling.

    The admission-only ≤ resource-caps check for disk (ADR-0007 §2, ADR-0312). disk is
    not a kcu input, so it is validated here beside the priced selector rather than on it.
    ``disk_gb`` is ``None`` only for a request that carries no disk (defended; the ADR-0067
    XOR rule makes a sized request always carry one), in which case there is nothing to bound.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if the resource advertises no disk ceiling,
            or ``disk_gb`` exceeds it.
    """
    if disk_gb is None:
        return
    ceiling = resource.capability_view.require_disk_ceiling(
        resource_id=resource.id, resource_name=resource.name
    )
    if disk_gb > ceiling:
        raise _caps_error("disk_gb", disk_gb, ceiling, resource)
```

- [ ] **Step 4: Wire into admission.** In `core.py` `price_window_and_estimate`, right after `validate_against_resource(request.selector, request.resource)`:

```python
    validate_disk_against_resource(request.disk_gb, request.resource)
```

Add `validate_disk_against_resource` to the existing import from `kdive.domain.accounting.cost` (the block importing `validate_against_resource`, `validate_size`).

- [ ] **Step 5: Add an admission-level test** that an `allocations.request` with `disk_gb` over the host ceiling is denied `configuration_error`, and at-ceiling admits. Extend the nearest existing admission test module (find with `rg -l "validate_against_resource|over.*cap|allocation_denied" tests/services`). Advertise `disk_gb` in the test host's capabilities fixture (see Task 3 for the live source; tests set it directly).

- [ ] **Step 6: Run tests.** `uv run python -m pytest tests/domain/accounting/test_cost.py tests/services/allocation -q` → PASS.

- [ ] **Step 7: Commit.**

```bash
git add src/kdive/domain/accounting/cost.py src/kdive/services/allocation/admission/core.py tests/ **/systems.toml src/kdive/providers/fault_inject
git commit -m "feat(985): enforce disk_gb ceiling at admission"
```

The commit includes the Step 0 fixture sweep so the wiring and the fixtures that keep it green land together (bisect-safe — no commit leaves the suite red).

---

### Task 3: local-libvirt discovery advertises a live-derived disk ceiling

**Files:**
- Modify: `src/kdive/providers/local_libvirt/discovery.py:155-162` (`list_resources` capabilities dict)
- Test: `tests/providers/local_libvirt/test_discovery.py`

**Interfaces:**
- Consumes: `DISK_GB_KEY` (Task 1), `ROOTFS_DIR` from `kdive.providers.local_libvirt.lifecycle.storage`.
- Produces: capabilities dict gains `DISK_GB_KEY: <disk_usage(ROOTFS_DIR).total // 1024**3>`.

- [ ] **Step 1: Write failing test.**

```python
# tests/providers/local_libvirt/test_discovery.py (add)
def test_discovery_advertises_disk_ceiling_from_host_storage(monkeypatch):
    import shutil
    import types
    from kdive.domain.catalog.resource_capabilities import DISK_GB_KEY
    # discovery reads only `.total`, so a SimpleNamespace suffices (avoid the private
    # shutil._ntuple_diskusage).
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda p: types.SimpleNamespace(total=200 * 1024**3, used=0, free=200 * 1024**3),
    )
    disco = _discovery_with_fake_conn(vcpus=8, memory_mb=16384)  # existing test helper
    (record,) = disco.list_resources()
    assert record.capabilities[DISK_GB_KEY] == 200
```

Reuse the module's existing fake-libvirt-connection helper (see how `test_discovery.py` builds `LocalLibvirtDiscovery` with a stub `connect`). If none exists, construct `LocalLibvirtDiscovery(host_uri="test:///x", connect=lambda: FakeConn(), concurrent_allocation_cap=1)` with a `FakeConn` returning `getInfo()`/`getCapabilities()`/`listAllDevices()` stubs mirroring existing tests.

- [ ] **Step 2: Run to verify failure.** `uv run python -m pytest tests/providers/local_libvirt/test_discovery.py -q` → FAIL (`disk_gb` not in capabilities).

- [ ] **Step 3: Implement.** In `discovery.py`, import at top:

```python
import shutil
from kdive.domain.catalog.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY, DISK_GB_KEY
from kdive.providers.local_libvirt.lifecycle.storage import ROOTFS_DIR
```

Add a private helper and a capabilities entry:

```python
_BYTES_PER_GB = 1024**3

def _host_disk_ceiling_gb() -> int:
    """Total capacity (GB) of the storage backing per-System overlays (ADR-0312).

    Raises:
        CategorizedError: ``INFRASTRUCTURE_FAILURE`` if ``ROOTFS_DIR`` cannot be stat-ed.
    """
    try:
        usage = shutil.disk_usage(ROOTFS_DIR)
    except OSError as exc:
        raise CategorizedError(
            f"cannot stat {ROOTFS_DIR} to advertise the host disk ceiling",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"path": ROOTFS_DIR, "error": type(exc).__name__},
        ) from exc
    return usage.total // _BYTES_PER_GB
```

Then in `list_resources` capabilities dict add: `DISK_GB_KEY: _host_disk_ceiling_gb(),`.

- [ ] **Step 4: Run tests.** `uv run python -m pytest tests/providers/local_libvirt/test_discovery.py -q` → PASS. Add a test that an un-stat-able `ROOTFS_DIR` (monkeypatch `shutil.disk_usage` to raise `OSError`) raises `INFRASTRUCTURE_FAILURE`.

- [ ] **Step 5: Commit.** (The repo/test `systems.toml` and fault-inject/remote fixtures already advertise `disk_gb` from Task 2 Step 0; this task only adds the local-libvirt live-derived source.)

```bash
git add src/kdive/providers/local_libvirt/discovery.py tests/
git commit -m "feat(985): advertise live-derived disk_gb host ceiling"
```

---

### Task 4: Grow the per-System overlay to `disk_gb`

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/storage.py`
- Modify: `src/kdive/providers/local_libvirt/lifecycle/provisioning.py:228`
- Test: `tests/providers/local_libvirt/test_storage.py` (or the existing storage/overlay test module)

**Interfaces:**
- Produces: `ProvisioningFiles.prepare_overlay(system_id, *, base, disk_gb: int | None)`; seams `resize_overlay: ResizeOverlay` and `overlay_virtual_size: OverlayVirtualSize` on `ProvisioningFiles`.
- Consumes: `profile.disk_gb` at the provisioning call site (always concrete post-reconcile, ADR-0067).

- [ ] **Step 1: Write failing tests** (mock the qemu-img seams):

```python
# tests/providers/local_libvirt/test_storage.py (add)
from kdive.providers.local_libvirt.lifecycle.storage import ProvisioningFiles, PreparedOverlay

_BASE_BYTES = 6 * 1024**3

def _files(created, resize_calls, exists=False):
    return ProvisioningFiles(
        make_overlay=lambda base, overlay: None,
        overlay_exists=lambda overlay: exists,
        overlay_virtual_size=lambda overlay: _BASE_BYTES,
        resize_overlay=lambda overlay, gb: resize_calls.append((overlay, gb)),
    )

def test_grows_overlay_when_disk_exceeds_base():
    calls = []
    files = _files(created=True, resize_calls=calls)
    files.prepare_overlay(_SYS_ID, base="/b.qcow2", disk_gb=60)
    assert calls == [(f"/var/lib/kdive/rootfs/{_SYS_ID}-overlay.qcow2", 60)]

def test_no_resize_at_or_below_base():
    calls = []
    files = _files(created=True, resize_calls=calls)
    files.prepare_overlay(_SYS_ID, base="/b.qcow2", disk_gb=6)  # == base
    assert calls == []
    files.prepare_overlay(_SYS_ID, base="/b.qcow2", disk_gb=3)  # < base
    assert calls == []

def test_no_resize_on_reuse_path():
    calls = []
    files = _files(created=True, resize_calls=calls, exists=True)  # overlay already present
    files.prepare_overlay(_SYS_ID, base="/b.qcow2", disk_gb=60)
    assert calls == []  # never touch a reused/live overlay

def test_none_disk_never_resizes():
    calls = []
    files = _files(created=True, resize_calls=calls)
    files.prepare_overlay(_SYS_ID, base="/b.qcow2", disk_gb=None)
    assert calls == []
```

Use a real `UUID` for `_SYS_ID` matching the existing test module's convention.

- [ ] **Step 2: Run to verify failure.** `uv run python -m pytest tests/providers/local_libvirt/test_storage.py -q` → FAIL (`prepare_overlay` has no `disk_gb`; seams undefined).

- [ ] **Step 3: Implement in `storage.py`.** Add the real helpers, seam types, dataclass fields, and grow-only logic:

```python
_BYTES_PER_GB = 1024**3

def _real_overlay_virtual_size(overlay: str) -> int:
    """Return the overlay's qcow2 virtual size in bytes via `qemu-img info`."""
    qemu_img = shutil.which(_QEMU_IMG)
    if qemu_img is None:
        raise CategorizedError(
            "qemu-img is not installed; cannot read the overlay virtual size",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details=_overlay_error_details("overlay_info", overlay, tool=_QEMU_IMG),
        )
    result = subprocess.run(  # noqa: S603 - resolved qemu-img; overlay is argv data
        [qemu_img, "info", "--output=json", overlay],
        capture_output=True, text=True, timeout=_QEMU_IMG_TIMEOUT_S, check=False,
    )
    if result.returncode != 0:
        raise CategorizedError(
            "qemu-img failed to read the overlay virtual size",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={**_overlay_error_details("overlay_info", overlay, tool=_QEMU_IMG),
                     "stderr": result.stderr[-_QEMU_IMG_ERROR_TAIL_CHARS:]},
        )
    import json
    return int(json.loads(result.stdout)["virtual-size"])


def _real_resize_overlay(overlay: str, disk_gb: int) -> None:
    """Grow the overlay's qcow2 virtual size to `disk_gb` GB via `qemu-img resize`."""
    qemu_img = shutil.which(_QEMU_IMG)
    if qemu_img is None:
        raise CategorizedError(
            "qemu-img is not installed; cannot resize the per-System rootfs overlay",
            category=ErrorCategory.MISSING_DEPENDENCY,
            details=_overlay_error_details("resize_overlay", overlay, tool=_QEMU_IMG),
        )
    result = subprocess.run(  # noqa: S603 - resolved qemu-img; overlay is argv data
        [qemu_img, "resize", overlay, f"{disk_gb}G"],
        capture_output=True, text=True, timeout=_QEMU_IMG_TIMEOUT_S, check=False,
    )
    if result.returncode != 0:
        raise CategorizedError(
            "qemu-img failed to resize the per-System rootfs overlay",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={**_overlay_error_details("resize_overlay", overlay, tool=_QEMU_IMG),
                     "disk_gb": disk_gb,
                     "stderr": result.stderr[-_QEMU_IMG_ERROR_TAIL_CHARS:]},
        )
```

Add seam type aliases and dataclass fields:

```python
type ResizeOverlay = Callable[[str, int], None]
type OverlayVirtualSize = Callable[[str], int]
```

On `ProvisioningFiles`:

```python
    resize_overlay: ResizeOverlay = _real_resize_overlay
    overlay_virtual_size: OverlayVirtualSize = _real_overlay_virtual_size
```

Change `prepare_overlay`:

```python
    def prepare_overlay(self, system_id: UUID, *, base: str, disk_gb: int | None) -> PreparedOverlay:
        overlay = overlay_path(system_id)
        created = not self.overlay_exists(overlay)
        if created:
            self.make_overlay(base, overlay)
            self._grow_if_requested(overlay, disk_gb)
        return PreparedOverlay(path=overlay, created=created)

    def _grow_if_requested(self, overlay: str, disk_gb: int | None) -> None:
        if disk_gb is None:
            return
        target = disk_gb * _BYTES_PER_GB
        if target > self.overlay_virtual_size(overlay):
            self.resize_overlay(overlay, disk_gb)
```

Note: a resize failure raises before the domain is defined, so the existing `cleanup_overlay_if_created` in the provisioner's failure path reclaims the just-created overlay.

- [ ] **Step 4: Update the provisioning call site** (`provisioning.py:228`):

```python
        overlay = self._files.prepare_overlay(system_id, base=base, disk_gb=profile.disk_gb)
```

Confirm `profile.disk_gb` is the field name (`src/kdive/profiles/provisioning.py`); it is always concrete post-reconcile (ADR-0067). If a size-less profile ever reaches here, `disk_gb` is `None` and the overlay keeps the base size.

- [ ] **Step 5: Run tests.** `uv run python -m pytest tests/providers/local_libvirt/test_storage.py -q` → PASS. Grep other `prepare_overlay(` call sites (`rg -n "prepare_overlay\("`) and update any test constructions to pass `disk_gb=`.

- [ ] **Step 6: Commit.**

```bash
git add src/kdive/providers/local_libvirt/lifecycle/storage.py src/kdive/providers/local_libvirt/lifecycle/provisioning.py tests/
git commit -m "feat(985): grow per-System overlay to requested disk_gb"
```

---

### Task 5: Default-gate plumbing-seam test (allocation → provisioner)

Prove the stamped `disk_gb` reaches `prepare_overlay` through the real profile-reconcile + provisioner seam, so a wiring regression fails in the default gate (spec finding 3).

**Files:**
- Test: `tests/providers/local_libvirt/test_provisioning.py` (or the existing provisioning test module)

- [ ] **Step 1: Write the test.** Drive `LocalLibvirtProvisioner.define_and_start` (or the provisioner entrypoint the existing provisioning tests use) with a `ProvisioningProfile` whose reconciled `disk_gb=60`, injecting a `ProvisioningFiles` whose `resize_overlay` records calls and whose other seams are no-ops. Assert `resize_overlay` was called with `60`. Follow the existing provisioning test's construction of profile + fakes; mock libvirt/domain define as those tests already do.

```python
def test_provision_grows_overlay_to_profile_disk_gb(...):
    resize_calls = []
    files = ProvisioningFiles(
        make_overlay=lambda b, o: None,
        overlay_exists=lambda o: False,
        overlay_virtual_size=lambda o: 6 * 1024**3,
        resize_overlay=lambda o, gb: resize_calls.append(gb),
        # remaining seams: reuse the module's existing no-op fakes
    )
    provisioner = _provisioner_with(files)  # existing helper
    provisioner.define_and_start(system_id=_SYS_ID, profile=_profile_with_disk(60), ...)
    assert resize_calls == [60]
```

- [ ] **Step 2: Run.** `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -q` → PASS (Task 4 already implements the behavior; this test locks the seam).

- [ ] **Step 3: Commit.**

```bash
git add tests/providers/local_libvirt/test_provisioning.py
git commit -m "test(985): assert stamped disk_gb reaches overlay resize"
```

---

### Task 6: Re-enable cloud-init `resize_rootfs` + guard it in the build self-check

**Files:**
- Modify: `src/kdive/images/families/_fedora_customize.py` (`KDIVE_CLOUD_CFG_CONTENT`)
- Modify: `src/kdive/providers/local_libvirt/rootfs_build.py` (`_real_verify_cloud_init`)
- Test: `tests/images/families/` (the test asserting `resize_rootfs: false`); build self-check test

- [ ] **Step 1: Update the failing config test.** Find it: `rg -n "resize_rootfs" tests`. Change the assertion from `"resize_rootfs: false" in cfg` to `"resize_rootfs: true" in cfg`, and keep/assert `'growpart: { mode: "off" }'` present. Run it → FAIL (config still says false).

- [ ] **Step 2: Flip the config.** In `_fedora_customize.py` `KDIVE_CLOUD_CFG_CONTENT`, change `resize_rootfs: false` → `resize_rootfs: true`. Leave `growpart: { mode: "off" }` unchanged, and update the nearby comment to say cloud-init grows the whole-disk ext4 to fill the disk sized at provision (ADR-0312), growpart stays off (no partition table).

- [ ] **Step 3: Extend the build self-check.** In `_real_verify_cloud_init` (`rootfs_build.py`, `# pragma: no cover - live_vm`), add an assertion that the baked drop-in contains `resize_rootfs: true` (mirror the existing `test -e`/`grep` guestfish checks in that function) and update the docstring to state the resize guarantee. This is a **live-build** defense; the **default-gate** guard for the flip is the config-content test in Step 1 (do not assume a self-check unit test exists — the function is not unit-covered).

- [ ] **Step 4: Run tests.** `uv run python -m pytest tests/images tests/providers/local_libvirt/test_rootfs_build.py -q` → PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/images/families/_fedora_customize.py src/kdive/providers/local_libvirt/rootfs_build.py tests/
git commit -m "feat(985): enable cloud-init resize_rootfs + guard in build self-check"
```

---

### Task 7: Seed the `debug` system shape (migration 0061)

**Files:**
- Create: `src/kdive/db/schema/0061_debug_system_shape.sql`
- Modify: `tests/db/test_migrate.py:166,599,938` (golden version lists)
- Test: `tests/db/test_migration_0061_debug_shape.py`

- [ ] **Step 1: Write the migration test.**

```python
# tests/db/test_migration_0061_debug_shape.py
"""Migration 0061 seeds the curated 'debug' system shape (#985, ADR-0312)."""
import psycopg


def test_migration_0061_seeds_debug_shape(pg_conn: psycopg.Connection) -> None:
    row = pg_conn.execute(
        "SELECT vcpus, memory_mb, disk_gb FROM system_shapes WHERE name = 'debug'"
    ).fetchone()
    assert row == (4, 8192, 60)
```

Mirror the fixture/harness of `tests/db/test_migration_0060_image_description.py` (how it applies migrations up to N).

- [ ] **Step 2: Run to verify failure.** `uv run python -m pytest tests/db/test_migration_0061_debug_shape.py -q` → FAIL (no `debug` row).

- [ ] **Step 3: Write the migration.**

```sql
-- 0061_debug_system_shape.sql — seed the curated 'debug' system shape (#985, ADR-0312).
-- Forward-only (ADR-0015). Generous disk for runtime tracer installs + build artifacts +
-- a captured vmcore. memory_mb is a whole-GB multiple (the 0013 memory_whole_gb check).
-- Idempotent: ON CONFLICT keeps a re-run a no-op and never re-sizes a re-set preset.
INSERT INTO system_shapes (name, vcpus, memory_mb, disk_gb) VALUES
    ('debug', 4, 8192, 60)
ON CONFLICT (name) DO NOTHING;
```

- [ ] **Step 4: Update golden version lists.** In `tests/db/test_migrate.py`, add `"0061",` immediately after each `"0060",` at lines ~166, ~599, ~938.

- [ ] **Step 5: Run tests.** `uv run python -m pytest tests/db/test_migrate.py tests/db/test_migration_0061_debug_shape.py -q` → PASS.

- [ ] **Step 6: Commit.**

```bash
git add src/kdive/db/schema/0061_debug_system_shape.sql tests/db/test_migrate.py tests/db/test_migration_0061_debug_shape.py
git commit -m "feat(985): seed the debug system shape (migration 0061)"
```

---

### Task 8: Report real per-System size (stamped `requested_*` + COALESCE fallback)

**Files:**
- Modify: `src/kdive/services/reports/sections.py` (`InventorySection.gather` SQL)
- Test: `tests/services/reports/` (the inventory-section test module)

- [ ] **Step 1: Write failing tests.** Add: (a) a custom-triple System (allocation with `requested_vcpus/memory_gb/disk_gb`, `shape = NULL`) reports those values (was `NULL`); (b) a shaped System (allocation with NULL `requested_*`, `shape='medium'`) still reports the catalog size via fallback; (c) a legacy System with NULL `requested_*` and NULL shape reports `NULL`. Follow the existing inventory-section test's row-insertion helpers.

- [ ] **Step 2: Run to verify failure.** `uv run python -m pytest tests/services/reports -q -k inventory` → FAIL (custom system shows NULL).

- [ ] **Step 3: Implement.** Replace the `SELECT` columns and drop the shape-only dependency (keep the LEFT JOIN as fallback):

```python
        sql: LiteralString = (
            "SELECT s.id AS system_id, s.domain_name AS name, s.project, s.state, "
            "r.kind AS resource_kind, "
            "COALESCE(a.requested_vcpus, sh.vcpus) AS vcpus, "
            "COALESCE(a.requested_memory_gb * 1024, sh.memory_mb) AS memory_mb, "
            "COALESCE(a.requested_disk_gb, sh.disk_gb) AS disk_gb "
            "FROM systems s "
            "JOIN allocations a ON a.id = s.allocation_id "
            "JOIN resources r ON r.id = a.resource_id "
            "LEFT JOIN system_shapes sh ON sh.name = s.shape"
            + scope_clause
            + " ORDER BY s.created_at DESC, s.id DESC LIMIT %s"
        )
```

Column names/units unchanged (`vcpus`/`memory_mb`/`disk_gb`).

- [ ] **Step 4: Run tests.** `uv run python -m pytest tests/services/reports -q` → PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/kdive/services/reports/sections.py tests/services/reports/
git commit -m "feat(985): report stamped per-System size with catalog fallback"
```

---

### Task 9: Agent-facing + operator docs

**Files:**
- Modify: `allocations.request` wrapper (`rg -l "def request" src/kdive/mcp/tools` — the allocations tool wrapper docstring / `Field` text)
- Modify: `shapes.list` docs (wrapper or the docs page listing presets)
- Modify: local-libvirt operator doc (rebuild-to-enable-resize; the live-derived ceiling) — find with `rg -l "kdive build-fs" docs`
- Regenerate: `just docs` (agent-facing tool reference)

- [ ] **Step 1: Update the `allocations.request` wrapper contract.** In the wrapper docstring / `Field` text, state that `disk_gb` (custom triple or via a shape) sizes the guest disk, bounded by the host disk ceiling (`configuration_error` if exceeded), and name the `debug` shape as the ready-sized debug preset. Keep it in the wrapper (agent-visible), not only the handler (AGENTS.md wrapper-contract rule).

- [ ] **Step 2: Document the `debug` shape** where presets are described (shapes docs / `shapes.list`): `4 vcpu / 8 GB / 60 GB`, for runtime tracer installs + vmcore capture.

- [ ] **Step 3: Operator doc.** Add a short note to the local-libvirt image/rebuild doc: images must be rebuilt (`kdive build-fs`) to gain `resize_rootfs`; the disk ceiling is derived from `/var/lib/kdive/rootfs` free capacity (no new env); remote/fault-inject declare `disk_gb` in `systems.toml`.

- [ ] **Step 4: Regenerate + verify docs.**

```bash
just docs
just docs-check
just docs-links
```

Expected: no drift; links resolve.

- [ ] **Step 5: Commit.**

```bash
git add -A
git commit -m "docs(985): document agent-selectable disk + debug shape + rebuild"
```

---

### Task 10: Full guardrail sweep

- [ ] **Step 1:** `just lint` → clean.
- [ ] **Step 2:** `just type` → clean (whole tree).
- [ ] **Step 3:** `just test` → green.
- [ ] **Step 4:** `just adr-status-check && just docs-check && just docs-links && just check-mermaid` → clean.
- [ ] **Step 5:** If anything is red, fix and fold the fixup into the owning task's commit; do not leave a red guardrail.

---

### Live verification (manual, `live_vm` marker — not the default gate)

Provision a System with `disk_gb=60` on a rebuilt image; assert `df -h /` in the guest ≫ 6 GB, a tracer package install (`dnf/apt install trace-cmd bpftrace`) succeeds, and a `force_crash` captures a vmcore that fits. This is the standing regression guard for Task 0's assumption. Add or extend a `@pytest.mark.live_vm` test under `tests/` following the existing live provisioning tests.

## Notes for the implementer

- The stored profile is always concrete after `resolve_profile_for_allocation` (ADR-0067), so `profile.disk_gb` is a real int at provision except for a genuinely size-less profile lane (then `None`, base size kept).
- The disk ceiling check lives in `price_window_and_estimate`, which is shared by synchronous admission and the promotion sweep — so a queued allocation is re-checked identically on promotion.
- `disk_gb` is never added to the pricing `Selector`; the ceiling check takes it as a separate argument.
- Grow-only: never emit `qemu-img resize` for a shrink; the `target > current` guard covers sub-base requests (agent gets the base size, which is larger than asked — acceptable).
