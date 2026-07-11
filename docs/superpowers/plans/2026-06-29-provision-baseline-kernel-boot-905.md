# Provision-time baseline-kernel boot (#905) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a local-libvirt `direct-kernel` provision render a direct-kernel `<os>` that boots the rootfs's own baseline kernel, so a freshly-provisioned System is SSH/drgn-reachable without a build → install.

**Architecture:** A new injected extraction seam reads the rootfs base read-only via libguestfs and stages the baseline `vmlinuz`+initramfs into a per-System directory; the provisioning plane passes those paths to `render_domain_xml`, which now always emits a fail-closed direct-kernel `<os>`. Idempotent (extract only when the baseline dir is absent), atomic (temp-dir renamed in), and teardown-symmetric.

**Tech Stack:** Python 3.14, `uv`, `xml.etree.ElementTree`, libguestfs `guestfs` binding (live only), pytest. Tools: `just lint`, `just type`, `just test`.

## Global Constraints

- Spec: [docs/specs/2026-06-29-provision-baseline-kernel-boot-905.md](../../specs/2026-06-29-provision-baseline-kernel-boot-905.md); ADR-0272.
- Scope: local-libvirt only. No schema, migration, RBAC, tool-surface, or config-setting change.
- Baseline cmdline is exactly `root=/dev/vda console=ttyS0 rw` — no `crashkernel`.
- `select_kernel_and_initrd` fails closed: exclude `*rescue*`, raise `CONFIGURATION_ERROR` on zero or >1 non-rescue kernel.
- Error types: `CategorizedError` with the most specific `ErrorCategory`; never invent strings.
- Per-site libguestfs import inside the function (never module-load), `# pragma: no cover - live_vm` on the real seam, `# ty: ignore[unresolved-import]` on the `guestfs` import (ADR convention).
- Ruff line length 100, lint set `E,F,I,UP,B,SIM`. Whole-tree `ty`. Run `just lint && just type && uv run python -m pytest <touched tests> -q` before each commit; full `just test` before the first push.
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: `baseline_kernel.py` — `BaselineKernel`, `select_kernel_and_initrd`, seam type, real extractor

**Files:**
- Create: `src/kdive/providers/local_libvirt/lifecycle/baseline_kernel.py`
- Test: `tests/providers/local_libvirt/lifecycle/test_baseline_kernel.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True, slots=True) class BaselineKernel: kernel: Path; initrd: Path | None`
  - `select_kernel_and_initrd(boot_entries: list[str]) -> tuple[str, str | None]` (returns basenames)
  - `type ExtractBaselineKernel = Callable[[Path, Path], BaselineKernel]`
  - `_real_extract_baseline_kernel(base: Path, dest_dir: Path) -> BaselineKernel` (live_vm/no-cover)

- [ ] **Step 1: Write the failing tests** (`tests/providers/local_libvirt/lifecycle/test_baseline_kernel.py`)

```python
from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.rootfs.baseline_kernel import select_kernel_and_initrd

_V = "6.19.10-300.fc44.x86_64"


def test_fedora_kernel_pairs_with_initramfs() -> None:
    entries = [f"/boot/vmlinuz-{_V}", f"/boot/initramfs-{_V}.img", "/boot/config-x", "/boot/grub2"]
    assert select_kernel_and_initrd(entries) == (f"vmlinuz-{_V}", f"initramfs-{_V}.img")


def test_debian_kernel_pairs_with_initrd_img() -> None:
    v = "6.1.0-13-amd64"
    entries = [f"/boot/vmlinuz-{v}", f"/boot/initrd.img-{v}"]
    assert select_kernel_and_initrd(entries) == (f"vmlinuz-{v}", f"initrd.img-{v}")


def test_kernel_without_initramfs_returns_none() -> None:
    assert select_kernel_and_initrd([f"/boot/vmlinuz-{_V}"]) == (f"vmlinuz-{_V}", None)


def test_rescue_pair_is_excluded_when_a_real_kernel_exists() -> None:
    entries = [
        "/boot/vmlinuz-0-rescue-abc",
        "/boot/initramfs-0-rescue-abc.img",
        f"/boot/vmlinuz-{_V}",
        f"/boot/initramfs-{_V}.img",
    ]
    assert select_kernel_and_initrd(entries) == (f"vmlinuz-{_V}", f"initramfs-{_V}.img")


def test_only_rescue_kernel_raises() -> None:
    with pytest.raises(CategorizedError) as exc:
        select_kernel_and_initrd(["/boot/vmlinuz-0-rescue-abc", "/boot/initramfs-0-rescue-abc.img"])
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_empty_boot_raises() -> None:
    with pytest.raises(CategorizedError) as exc:
        select_kernel_and_initrd([])
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_multiple_kernels_fails_closed_and_names_candidates() -> None:
    a, b = "vmlinuz-6.19.10-300.fc44.x86_64", "vmlinuz-6.18.0-100.fc44.x86_64"
    with pytest.raises(CategorizedError) as exc:
        select_kernel_and_initrd([f"/boot/{a}", f"/boot/{b}"])
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert set(exc.value.details["candidates"]) == {a, b}


def test_accepts_bare_basenames_too() -> None:
    assert select_kernel_and_initrd([f"vmlinuz-{_V}", f"initramfs-{_V}.img"]) == (
        f"vmlinuz-{_V}",
        f"initramfs-{_V}.img",
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/local_libvirt/lifecycle/test_baseline_kernel.py -q`
Expected: FAIL (`ModuleNotFoundError`/import error — module not created).

- [ ] **Step 3: Write minimal implementation** (`src/kdive/providers/local_libvirt/lifecycle/baseline_kernel.py`)

```python
"""Baseline-kernel extraction from a local-libvirt rootfs base (ADR-0272).

A `direct-kernel` provision boots the rootfs's own kernel: the bootloader-less whole-disk ext4
rootfs (ADR-0030/0052) has a kernel under `/boot` but no in-image bootloader, so the kernel must be
extracted host-side and rendered as a libvirt `<kernel>`. `select_kernel_and_initrd` is the pure,
fail-closed selection; `_real_extract_baseline_kernel` is the `live_vm` libguestfs read.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory

_VMLINUZ_PREFIX = "vmlinuz-"


@dataclass(frozen=True, slots=True)
class BaselineKernel:
    """The baseline kernel image and its optional initramfs, as host paths."""

    kernel: Path
    initrd: Path | None


type ExtractBaselineKernel = Callable[[Path, Path], BaselineKernel]
"""Seam: extract the baseline kernel+initramfs from ``base`` into ``dest_dir`` (atomic)."""


def select_kernel_and_initrd(boot_entries: list[str]) -> tuple[str, str | None]:
    """Pick the System's `vmlinuz-<ver>` and matching initramfs from a `/boot` listing.

    Fails closed (a silent wrong pick boots a dead guest that still reports ``ready``, #905):
    rescue images are excluded, and zero or more-than-one non-rescue kernel raises rather than
    guessing a version order. Returns basenames; the initramfs is ``None`` for an
    embedded-initramfs kernel.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when there is no non-rescue kernel, or more than
            one (the kdive-ready build emits exactly one).
    """
    names = [os.path.basename(entry) for entry in boot_entries]
    kernels = [n for n in names if n.startswith(_VMLINUZ_PREFIX) and "rescue" not in n]
    if not kernels:
        raise CategorizedError(
            "rootfs /boot has no bootable kernel; image cannot direct-kernel boot",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"boot_entries": sorted(names)},
        )
    if len(kernels) > 1:
        raise CategorizedError(
            "rootfs /boot has multiple kernels; cannot select a baseline kernel unambiguously",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"candidates": sorted(kernels)},
        )
    kernel = kernels[0]
    version = kernel[len(_VMLINUZ_PREFIX) :]
    initrd = next(
        (n for n in (f"initramfs-{version}.img", f"initrd.img-{version}") if n in names), None
    )
    return kernel, initrd


def _real_extract_baseline_kernel(  # pragma: no cover - live_vm (libguestfs)
    base: Path, dest_dir: Path
) -> BaselineKernel:
    """Mount ``base`` read-only via libguestfs and stage its baseline kernel+initramfs.

    Downloads into a sibling ``.part`` directory and renames it onto ``dest_dir`` atomically, so a
    crash mid-extraction never leaves a half-populated baseline directory (ADR-0272).

    Raises:
        CategorizedError: ``MISSING_DEPENDENCY`` if the guestfs binding is absent;
            ``INFRASTRUCTURE_FAILURE`` on a libguestfs fault; ``CONFIGURATION_ERROR`` from
            ``select_kernel_and_initrd``.
    """
    try:
        import guestfs  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided
    except ImportError as exc:
        raise CategorizedError(
            "libguestfs (the guestfs Python binding) is required to extract the baseline kernel",
            category=ErrorCategory.MISSING_DEPENDENCY,
        ) from exc
    guest = guestfs.GuestFS(python_return_dict=True)
    try:
        guest.add_drive_opts(str(base), format="qcow2", readonly=True)
        guest.launch()
        roots = guest.inspect_os()
        if not roots:
            raise CategorizedError(
                "could not inspect the rootfs base to extract the baseline kernel",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"base": str(base)},
            )
        guest.mount_ro(roots[0], "/")
        kernel_name, initrd_name = select_kernel_and_initrd(guest.glob_expand("/boot/*"))
        tmp = dest_dir.parent / (dest_dir.name + ".part")
        _reset_dir(tmp)
        guest.download(f"/boot/{kernel_name}", str(tmp / "kernel"))
        if initrd_name is not None:
            guest.download(f"/boot/{initrd_name}", str(tmp / "initrd"))
    except CategorizedError:
        raise
    except Exception as exc:  # noqa: BLE0001 - any libguestfs fault → categorized infra failure
        raise CategorizedError(
            "libguestfs failed extracting the baseline kernel from the rootfs base",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"base": str(base), "error": type(exc).__name__},
        ) from exc
    finally:
        _shutdown(guest)
    os.rename(tmp, dest_dir)
    initrd = dest_dir / "initrd"
    return BaselineKernel(kernel=dest_dir / "kernel", initrd=initrd if initrd_name else None)


def _reset_dir(path: Path) -> None:  # pragma: no cover - live_vm
    import shutil  # noqa: PLC0415

    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)


def _shutdown(guest: object) -> None:  # pragma: no cover - live_vm
    import contextlib  # noqa: PLC0415

    for method in ("shutdown", "close"):
        with contextlib.suppress(Exception):
            getattr(guest, method)()
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/lifecycle/test_baseline_kernel.py -q`
Expected: PASS (8 tests). Then `just lint && just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/lifecycle/baseline_kernel.py tests/providers/local_libvirt/lifecycle/test_baseline_kernel.py
git commit -m "feat(905): add fail-closed baseline-kernel selection + extractor"
```

---

### Task 2: `render_domain_xml` emits a fail-closed direct-kernel `<os>`

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/xml.py:33-101`
- Test: `tests/providers/local_libvirt/test_provisioning.py` (update `_render` helper + the no-kernel test + direct callers)
- Test (same commit — every `render_domain_xml` caller, so no commit leaves the full suite red): `tests/adversarial/test_provider_xml.py`, `tests/providers/local_libvirt/test_live_preserve_attach.py`, `tests/mcp/debug/test_debug_live_attach.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `render_domain_xml(system_id, profile, *, disk_path, gdb_port=None, ssh_port=None, kernel_path: Path | None = None, initrd_path: Path | None = None) -> str`. Module constant `_BASELINE_CMDLINE = "root=/dev/vda console=ttyS0 rw"`.

- [ ] **Step 1: Update the failing tests** in `tests/providers/local_libvirt/test_provisioning.py`

Add near the top constants (after `_DISK`):
```python
_KERNEL = Path("/var/lib/kdive/rootfs/11111111-1111-1111-1111-111111111111-baseline/kernel")
```
Change the `_render` helper to pass a kernel path:
```python
def _render(
    system_id: UUID = _SYS,
    profile: ProvisioningProfile | None = None,
    *,
    disk_path: str = _DISK,
    kernel_path: Path | None = _KERNEL,
) -> str:
    return render_domain_xml(
        system_id, profile or _profile(), disk_path=disk_path, kernel_path=kernel_path
    )
```
Replace `test_render_has_no_kernel_or_cmdline` with:
```python
def test_render_has_baseline_kernel_and_cmdline() -> None:
    root = _safe_fromstring(_render())
    assert root.findtext("os/kernel") == str(_KERNEL)
    assert root.findtext("os/cmdline") == "root=/dev/vda console=ttyS0 rw"


def test_render_requires_a_kernel_path() -> None:
    with pytest.raises(CategorizedError) as exc:
        _render(kernel_path=None)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_render_omits_initrd_when_absent_and_emits_it_when_present() -> None:
    assert _safe_fromstring(_render()).find("os/initrd") is None
    initrd = Path("/var/lib/kdive/rootfs/x-baseline/initrd")
    root = _safe_fromstring(
        render_domain_xml(_SYS, _profile(), disk_path=_DISK, kernel_path=_KERNEL, initrd_path=initrd)
    )
    assert root.findtext("os/initrd") == str(initrd)
```
For the direct `render_domain_xml(...)` callers in this file (the gdbstub/ssh/metadata tests at the lines shown by `rg -n "render_domain_xml" tests/providers/local_libvirt/test_provisioning.py`), add `kernel_path=_KERNEL` to each call that omits it.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -q`
Expected: FAIL (new tests + the direct callers now expect a `<kernel>` the code does not render yet, or raise on `kernel_path` keyword unknown).

- [ ] **Step 3: Implement** in `src/kdive/providers/local_libvirt/lifecycle/xml.py`

Add the import and constant near the top:
```python
from pathlib import Path
...
_BASELINE_CMDLINE = "root=/dev/vda console=ttyS0 rw"
```
Extend the signature and the `<os>` block. Replace the existing `os_el` lines (currently `os_el = ET.SubElement(domain, "os")` + the `<type>` line) with:
```python
def render_domain_xml(
    system_id: UUID,
    profile: ProvisioningProfile,
    *,
    disk_path: str,
    gdb_port: int | None = None,
    ssh_port: int | None = None,
    kernel_path: Path | None = None,
    initrd_path: Path | None = None,
) -> str:
    ...
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=profile.arch, machine=machine).text = "hvm"
    _append_direct_kernel(os_el, kernel_path, initrd_path)
```
Add the helper:
```python
def _append_direct_kernel(
    os_el: ET.Element, kernel_path: Path | None, initrd_path: Path | None
) -> None:
    """Render the direct-kernel `<os>` body (ADR-0272); a local domain always boots a kernel.

    Built with ElementTree (no string interpolation), so no path can inject XML.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no kernel path is supplied — a local-libvirt
            domain must never disk-boot a bootloader-less rootfs.
    """
    if kernel_path is None:
        raise CategorizedError(
            "a local-libvirt direct-kernel domain requires a baseline <kernel> path",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    ET.SubElement(os_el, "kernel").text = str(kernel_path)
    if initrd_path is not None:
        ET.SubElement(os_el, "initrd").text = str(initrd_path)
    ET.SubElement(os_el, "cmdline").text = _BASELINE_CMDLINE
```
Update the `render_domain_xml` docstring to note it now renders the baseline direct-kernel `<os>` and raises `CONFIGURATION_ERROR` without a kernel path.

- [ ] **Step 4: Fix every other `render_domain_xml` caller in the same change**

Run `rg -n "render_domain_xml" tests/ src/`. For each local-libvirt caller that omits `kernel_path`
(`tests/adversarial/test_provider_xml.py:110,125`, and any in `test_live_preserve_attach.py` /
`test_debug_live_attach.py`), add `kernel_path=Path("/var/lib/kdive/rootfs/<sys>-baseline/kernel")`
using that file's existing system-id constant. The adversarial XML-injection test must still pass
`kernel_path` and keep asserting no markup injection from the profile/disk path. Do **not** touch the
remote-libvirt renderer (separate function, unchanged).

- [ ] **Step 5: Run to verify pass (all callers, one commit keeps the suite green)**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py tests/adversarial/test_provider_xml.py tests/providers/local_libvirt/test_live_preserve_attach.py tests/mcp/debug/test_debug_live_attach.py -q`
Expected: PASS. Then `just lint && just type`.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/providers/local_libvirt/lifecycle/xml.py tests/providers/local_libvirt/test_provisioning.py tests/adversarial/test_provider_xml.py tests/providers/local_libvirt/test_live_preserve_attach.py tests/mcp/debug/test_debug_live_attach.py
git commit -m "feat(905): render fail-closed baseline direct-kernel <os> at provision"
```

---

### Task 3: storage.py — per-System baseline directory helper + removal seam

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/storage.py`
- Test: `tests/providers/local_libvirt/test_provisioning.py` (storage-level tests near the existing overlay tests)

**Interfaces:**
- Produces: `baseline_dir(system_id: UUID | str) -> str` returning `f"{ROOTFS_DIR}/{system_id}-baseline"`; `ProvisioningFiles.remove_baseline: RemoveBaseline` seam (default `_real_remove_baseline`) + `ProvisioningFiles.remove_baseline_for_domain(domain_name: str) -> None`; `ProvisioningFiles.baseline_exists: OverlayExists` seam (default `_real_overlay_exists`, reused — both are a path-presence predicate) so the provisioning plane's reuse check is injectable like `overlay_exists`.

- [ ] **Step 1: Write the failing test**

```python
def test_remove_baseline_for_domain_strips_prefix_and_rmtrees() -> None:
    seen: list[str] = []
    files = storage_module.ProvisioningFiles(remove_baseline=seen.append)
    files.remove_baseline_for_domain("kdive-" + str(_SYS))
    assert seen == [storage_module.baseline_dir(_SYS)]


def test_baseline_dir_is_per_system_under_rootfs() -> None:
    assert storage_module.baseline_dir(_SYS) == f"{storage_module.ROOTFS_DIR}/{_SYS}-baseline"


def test_baseline_exists_seam_is_injectable() -> None:
    files = storage_module.ProvisioningFiles(baseline_exists=lambda path: True)
    assert files.baseline_exists(storage_module.baseline_dir(_SYS)) is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -k baseline -q`
Expected: FAIL (`baseline_dir`/`remove_baseline` not defined).

- [ ] **Step 3: Implement** in `src/kdive/providers/local_libvirt/lifecycle/storage.py`

Add `import shutil` (already present). Add after `overlay_path`:
```python
def baseline_dir(system_id: UUID | str) -> str:
    """The per-System directory holding the extracted baseline kernel/initrd (ADR-0272)."""
    return f"{ROOTFS_DIR}/{system_id}-baseline"


def _real_remove_baseline(baseline: str) -> None:
    """Remove a System's baseline directory; an absent directory is the achieved post-state."""
    try:
        shutil.rmtree(baseline)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CategorizedError(
            "failed to remove the per-System baseline kernel directory",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"op": "remove_baseline", "baseline": Path(baseline).name},
        ) from exc
```
Add the seam type next to the others:
```python
type RemoveBaseline = Callable[[str], None]
```
Add the fields + method to `ProvisioningFiles` (frozen dataclass). `baseline_exists` reuses the
existing `OverlayExists` type and `_real_overlay_exists` default (both are a path-presence predicate):
```python
    remove_baseline: RemoveBaseline = _real_remove_baseline
    baseline_exists: OverlayExists = _real_overlay_exists
...
    def remove_baseline_for_domain(self, domain_name: str) -> None:
        self.remove_baseline(baseline_dir(domain_name.removeprefix("kdive-")))
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -k baseline -q`
Expected: PASS. Then `just lint && just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/lifecycle/storage.py tests/providers/local_libvirt/test_provisioning.py
git commit -m "feat(905): per-System baseline-kernel directory + removal seam"
```

---

### Task 4: provisioning.py — extract at provision, pass to renderer, reclaim at teardown

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`
- Test: `tests/providers/local_libvirt/test_provisioning.py`

**Interfaces:**
- Consumes: `BaselineKernel`, `ExtractBaselineKernel`, `_real_extract_baseline_kernel` (Task 1); `baseline_dir` (Task 3); `render_domain_xml(..., kernel_path=, initrd_path=)` (Task 2).
- Produces: `LocalLibvirtProvisioning(__init__)` gains `extract_baseline_kernel: ExtractBaselineKernel | None = None`; private `_prepare_baseline_kernel(system_id, base) -> BaselineKernel`.

- [ ] **Step 1: Write the failing tests** against the suite's real fakes

The suite already defines `_ProvConn` (a fake `connect()` target whose `recorded_xml: list[str]`
captures every `defineXML` payload) and `_ProvDomain`, and constructs the plane inline as
`LocalLibvirtProvisioning(connect=lambda: conn, files=<ProvisioningFiles>, materialize_rootfs=lambda
rootfs, _sid, _arch: <path>, free_port=..., catalog_fetch=...)`. Add a baseline-extract fake +
`baseline_exists` control. Add these tests (matching the file's existing construction style):
```python
def test_provision_extracts_baseline_and_renders_kernel() -> None:
    conn = _ProvConn()
    calls: list[tuple[Path, Path]] = []

    def fake_extract(base: Path, dest: Path) -> BaselineKernel:
        calls.append((base, dest))
        return BaselineKernel(kernel=dest / "kernel", initrd=dest / "initrd")

    prov = LocalLibvirtProvisioning(
        connect=lambda: conn,
        files=storage_module.ProvisioningFiles(
            make_overlay=lambda base, overlay: None,
            overlay_exists=lambda overlay: False,
            baseline_exists=lambda path: False,
            prepare_console_log=lambda path: None,
        ),
        materialize_rootfs=lambda rootfs, _sid, _arch: "/var/lib/kdive/rootfs/base.qcow2",
        free_port=lambda: 40000,
        extract_baseline_kernel=fake_extract,
    )
    prov.provision(_SYS, _profile())

    assert calls == [(Path("/var/lib/kdive/rootfs/base.qcow2"), Path(storage_module.baseline_dir(_SYS)))]
    root = _safe_fromstring(conn.recorded_xml[-1])
    assert root.findtext("os/kernel") == f"{storage_module.baseline_dir(_SYS)}/kernel"
    assert root.findtext("os/cmdline") == "root=/dev/vda console=ttyS0 rw"


def test_provision_reuses_present_baseline_dir() -> None:
    conn = _ProvConn()

    def fake_extract(base: Path, dest: Path) -> BaselineKernel:
        raise AssertionError("must reuse the present baseline dir, not re-extract")

    prov = LocalLibvirtProvisioning(
        connect=lambda: conn,
        files=storage_module.ProvisioningFiles(
            make_overlay=lambda base, overlay: None,
            overlay_exists=lambda overlay: True,
            baseline_exists=lambda path: True,
            prepare_console_log=lambda path: None,
        ),
        materialize_rootfs=lambda rootfs, _sid, _arch: "/var/lib/kdive/rootfs/base.qcow2",
        free_port=lambda: 40000,
        extract_baseline_kernel=fake_extract,
    )
    prov.provision(_SYS, _profile())
    root = _safe_fromstring(conn.recorded_xml[-1])
    assert root.findtext("os/kernel") == f"{storage_module.baseline_dir(_SYS)}/kernel"


def test_teardown_removes_baseline_dir() -> None:
    conn = _ProvConn(defined={domain_name_for(_SYS): _ProvDomain(domain_name_for(_SYS))})
    removed: list[str] = []
    prov = LocalLibvirtProvisioning(
        connect=lambda: conn,
        files=storage_module.ProvisioningFiles(
            remove_overlay=lambda overlay: None,
            remove_baseline=removed.append,
        ),
    )
    prov.teardown(domain_name_for(_SYS))
    assert removed == [storage_module.baseline_dir(_SYS)]
```
(If `_ProvConn`/`_ProvDomain`/the kwargs differ slightly from the above when you read the file,
match the real names/fields — but they are `recorded_xml`, `defined`, and the keyword args shown in
the existing `test_provision_*` tests. In `test_provision_extracts_*` the default
`extract_baseline_kernel` is overridden by passing `extract_baseline_kernel=fake_extract` — add that
kwarg to that constructor; it is shown on the reuse test and applies identically here.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -q`
Expected: FAIL (`extract_baseline_kernel` kwarg unknown / baseline not rendered / teardown does not remove baseline).

- [ ] **Step 3: Implement** in `src/kdive/providers/local_libvirt/lifecycle/provisioning.py`

Add imports:
```python
from kdive.providers.local_libvirt.lifecycle.rootfs.baseline_kernel import (
    BaselineKernel,
    ExtractBaselineKernel,
    _real_extract_baseline_kernel,
)
from kdive.providers.local_libvirt.lifecycle.storage import (
    ROOTFS_DIR,
    ProvisioningFiles,
    baseline_dir,
    overlay_path,
)
```
`__init__`: add param + default:
```python
        extract_baseline_kernel: ExtractBaselineKernel | None = None,
        ...
        self._extract_baseline_kernel = extract_baseline_kernel or _real_extract_baseline_kernel
```
In `provision()`, after `base = self._materialize_rootfs(...)` and before/after `prepare_overlay`:
```python
        base = self._materialize_rootfs(section.rootfs, system_id, profile.arch)
        baseline = self._prepare_baseline_kernel(system_id, base)
        overlay = self._files.prepare_overlay(system_id, base=base)
        ...
        xml = render_domain_xml(  # validates the profile
            system_id,
            profile,
            disk_path=overlay.path,
            gdb_port=gdb_port,
            ssh_port=ssh_port,
            kernel_path=baseline.kernel,
            initrd_path=baseline.initrd,
        )
```
Add the helper:
```python
    def _prepare_baseline_kernel(self, system_id: UUID, base: str) -> BaselineKernel:
        """Extract the rootfs's baseline kernel once; reuse an already-extracted directory.

        Mirrors the overlay's create-only-when-absent contract (ADR-0060/0272): a present
        baseline directory (the atomic all-or-nothing marker) is reused so a provision retry never
        re-mounts the base. Presence is checked through the injected ``baseline_exists`` seam (like
        ``overlay_exists``), so the reuse path is unit-testable without touching the real filesystem.
        """
        dest = Path(baseline_dir(system_id))
        if self._files.baseline_exists(str(dest)):
            initrd = dest / "initrd"
            return BaselineKernel(
                kernel=dest / "kernel", initrd=initrd if self._files.baseline_exists(str(initrd)) else None
            )
        return self._extract_baseline_kernel(Path(base), dest)
```
In `teardown()`, after `self._files.remove_overlay_for_domain(domain_name)`:
```python
        self._files.remove_baseline_for_domain(domain_name)
```
Add `BaselineKernel`/`baseline_dir` to `__all__` if the module exports symbols the tests import via the package facade (match the existing `__all__` style).

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -q`
Expected: PASS. Then `just lint && just type`.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/local_libvirt/lifecycle/provisioning.py tests/providers/local_libvirt/test_provisioning.py
git commit -m "feat(905): extract baseline kernel at provision, reclaim at teardown"
```

---

### Task 5: Correct the walkthrough + `kernel_source_ref` doc wording

(All `render_domain_xml` caller fixes already landed in Task 2 so the suite never goes red.)

**Files:**
- Modify: `docs/operating/providers/local-libvirt-walkthrough.md` (lines ~247, ~443-444) where wording overclaims/underclaims given the fix
- Modify: `src/kdive/profiles/provisioning.py` `kernel_source_ref` docstring only if it now contradicts behavior

**Interfaces:**
- Consumes: nothing (doc-only).

- [ ] **Step 1: Verify the docs now read true**

Read `docs/operating/providers/local-libvirt-walkthrough.md` lines ~240-250 and ~440-445. The claims "Provisioning the inventory above is enough to provision and boot" and "provision boots the rootfs's own kernel from disk" are now **accurate** in spirit (provision renders a direct-kernel `<os>`), except the phrase "from disk" is wrong — it boots via direct-kernel, not disk boot. Correct only factual inaccuracies:
- change "boots the rootfs's own kernel from disk" → "boots the rootfs's own baseline kernel via direct-kernel boot".
- Leave claims that are now satisfied. If the `kernel_source_ref` docstring states provision reaches `ready` on a baseline kernel, it is now accurate — adjust only if it still says provision does not boot.

- [ ] **Step 2: Run the doc guardrails**

Run: `just docs-links && just check-mermaid`
Expected: both pass.

- [ ] **Step 3: Commit**

```bash
git add docs/operating/providers/local-libvirt-walkthrough.md src/kdive/profiles/provisioning.py
git commit -m "docs(905): correct walkthrough wording now that provision boots the baseline kernel"
```

---

## Self-Review

**Spec coverage:**
- R1 (direct-kernel `<os>`, fixed cmdline, no crashkernel) → Task 2.
- R2 (extract from base, persists) → Task 1 (`_real_extract_baseline_kernel`) + Task 4.
- R3 (fail-closed renderer) → Task 2 (`_append_direct_kernel`).
- R4 (no kernel → CONFIGURATION_ERROR) → Task 1 (`select_kernel_and_initrd`) + the live extractor.
- R5 / R5a (atomic per-System dir, reuse-when-present) → Task 1 (rename) + Task 3 (`baseline_dir`) + Task 4 (`_prepare_baseline_kernel`).
- R5b (ordering invariant) → no code; confirmed by inspection in Task 4 step (no re-provision path post-`ready`).
- R6 (teardown reclaims dir) → Task 3 + Task 4.
- R7 (gdbstub/ssh compose) → Task 2 tests keep the existing gdbstub/ssh assertions, now with `kernel_path`.
- R8 (no schema/migration/RBAC/tool/config change) → none added.

**Placeholder scan:** Task 4 step 1 references "the file's existing builder" rather than a literal name — resolved at execution by reading the current `test_provisioning.py` construction helpers (the only non-literal, because the existing fake-construction style must be matched, not invented). All code steps show real code.

**Type consistency:** `BaselineKernel(kernel: Path, initrd: Path | None)`, `select_kernel_and_initrd -> tuple[str, str | None]`, `ExtractBaselineKernel = Callable[[Path, Path], BaselineKernel]`, `baseline_dir(...) -> str`, renderer kwargs `kernel_path`/`initrd_path: Path | None` — consistent across Tasks 1-4.
