"""Tests for the local-libvirt kdump host-side overlay harvest (ADR-0203).

The ``live_vm``-gated acceptance test at the bottom of this module validates the full
in-guest kdump capture arc — ``control.force_crash`` → real ``/var/crash/<ts>/vmcore``
on the overlay → ``LocalLibvirtRetrieve.capture(method=KDUMP)`` harvests it host-side
without staging a core — on an operator KVM host (ADR-0203).  See §4b of
``docs/operating/runbooks/four-method-live-run.md`` for the manual end-to-end runbook.

The test is collected but skipped in CI (``just test`` deselects ``live_vm``).  Run it
with ``just test-live`` on a host that satisfies:
  - ``KDIVE_LIBVIRT_URI`` pointing at the local libvirtd
  - ``KDIVE_LIVE_VM_SYSTEM_ID`` set to a System UUID already installed with a
    kdump-capable kernel (crashkernel= reserved, kdump service active)
  - ``KDIVE_S3_*`` object-store env vars (object-store endpoint + bucket)
  - ``guestfs`` (libguestfs Python binding) and ``drgn`` importable in the worker venv
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest

from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.retrieve import (
    KDUMP_CORE_INCOMPLETE_REMEDIATION,
    LocalLibvirtRetrieve,
)
from kdive.providers.local_libvirt.retrieve_kdump import (
    GuestCoreReader,
    HarvestOutcome,
    VmcoreEntry,
    extract_dmesg_or_sentinel,
    file_sha256_b64,
    harvest_vmcore,
    read_via_tempfile,
    redact_dmesg,
    select_newest,
)
from kdive.providers.shared.debug_common.core_file import DMESG_UNAVAILABLE, MAX_CORE_BYTES
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.live_vm import require_live_vm_provisioned

_OVERLAY = "/var/lib/kdive/rootfs/sys-overlay.qcow2"
_SYS = UUID("33333333-3333-3333-3333-333333333333")
_RUN = UUID("44444444-4444-4444-4444-444444444444")


@dataclass
class _FakeReader:
    entries: list[VmcoreEntry]
    blobs: dict[str, bytes] = field(default_factory=dict)
    downloads: list[str] = field(default_factory=list)

    def list_vmcores(self, overlay: str) -> list[VmcoreEntry]:
        return list(self.entries)

    def download_vmcore(self, overlay: str, path: str, dest: Path) -> None:
        self.downloads.append(path)
        dest.write_bytes(self.blobs[path])


def test_select_newest_picks_highest_mtime() -> None:
    entries = [
        VmcoreEntry("/var/crash/a/vmcore", 100.0, 10),
        VmcoreEntry("/var/crash/c/vmcore", 300.0, 30),
        VmcoreEntry("/var/crash/b/vmcore", 200.0, 20),
    ]
    assert select_newest(entries) == entries[1]


def test_select_newest_empty_is_none() -> None:
    assert select_newest([]) is None


def test_harvest_downloads_newest_core_to_dest(tmp_path: Path) -> None:
    reader = _FakeReader(
        entries=[
            VmcoreEntry("/var/crash/old/vmcore", 100.0, 3),
            VmcoreEntry("/var/crash/new/vmcore", 200.0, 5),
        ],
        blobs={"/var/crash/new/vmcore": b"NEWER"},
    )
    dest = tmp_path / "core.vmcore"
    out = harvest_vmcore(reader, _OVERLAY, dest=dest, max_bytes=1024)
    assert out == HarvestOutcome(core=dest, incomplete_found=False)
    assert dest.read_bytes() == b"NEWER"
    assert reader.downloads == ["/var/crash/new/vmcore"]


def test_harvest_absent_core_returns_none(tmp_path: Path) -> None:
    reader = _FakeReader(entries=[])
    dest = tmp_path / "core.vmcore"
    assert harvest_vmcore(reader, _OVERLAY, dest=dest, max_bytes=1024) == HarvestOutcome(
        core=None, incomplete_found=False
    )
    assert not dest.exists()


def test_harvest_oversize_core_is_configuration_error(tmp_path: Path) -> None:
    reader = _FakeReader(
        entries=[VmcoreEntry("/var/crash/big/vmcore", 100.0, 4096)],
        blobs={"/var/crash/big/vmcore": b"X"},
    )
    dest = tmp_path / "core.vmcore"
    with pytest.raises(CategorizedError) as exc:
        harvest_vmcore(reader, _OVERLAY, dest=dest, max_bytes=1024)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert reader.downloads == []  # rejected before downloading the bytes
    assert not dest.exists()


def test_harvest_only_incomplete_core_is_not_promoted(tmp_path: Path) -> None:
    """A lone vmcore-incomplete is never downloaded/returned; it only flags the disclosure."""
    reader = _FakeReader(
        entries=[VmcoreEntry("/var/crash/x/vmcore-incomplete", 100.0, 5, incomplete=True)],
        blobs={"/var/crash/x/vmcore-incomplete": b"PARTIAL"},
    )
    dest = tmp_path / "core.vmcore"
    out = harvest_vmcore(reader, _OVERLAY, dest=dest, max_bytes=1024)
    assert out == HarvestOutcome(core=None, incomplete_found=True)
    assert reader.downloads == []
    assert not dest.exists()


def test_harvest_prefers_complete_core_over_incomplete(tmp_path: Path) -> None:
    """With both present (even when the incomplete one is newer) the complete vmcore wins."""
    reader = _FakeReader(
        entries=[
            VmcoreEntry("/var/crash/done/vmcore", 100.0, 5),
            VmcoreEntry("/var/crash/aborted/vmcore-incomplete", 300.0, 7, incomplete=True),
        ],
        blobs={"/var/crash/done/vmcore": b"COMPLETE"},
    )
    dest = tmp_path / "core.vmcore"
    out = harvest_vmcore(reader, _OVERLAY, dest=dest, max_bytes=1024)
    assert out == HarvestOutcome(core=dest, incomplete_found=True)
    assert dest.read_bytes() == b"COMPLETE"
    assert reader.downloads == ["/var/crash/done/vmcore"]


def _kdump_capture_via_reader(reader: _FakeReader, spool: Path) -> None:
    """Drive ``capture(KDUMP)`` with ``reader`` behind the wait seam (no live host, no store).

    The success path is exercised in ``test_retrieve.py``; here the store/build-id seams must
    never run because every scenario raises before any core is streamed.
    """

    def wait(_system_id: UUID) -> HarvestOutcome:
        spool.mkdir(parents=True, exist_ok=True)
        return harvest_vmcore(reader, _OVERLAY, dest=spool / "vmcore", max_bytes=MAX_CORE_BYTES)

    retriever = LocalLibvirtRetrieve(
        tenant="local",
        store_factory=lambda: pytest.fail("store used on a no-core path"),
        wait_for_vmcore=wait,
        read_vmcore_build_id=lambda _d: pytest.fail("build-id seam used on a no-core path"),
        read_vmcore_build_id_from_file=lambda _p: pytest.fail("build-id seam used"),
        extract_redacted_from_file=lambda _p: pytest.fail("redaction seam used"),
        host_dump_capture=lambda _s: pytest.fail("host_dump seam used on the kdump path"),
        secret_registry=SecretRegistry(),
    )
    retriever.capture(_SYS, _RUN, CaptureMethod.KDUMP)


def test_capture_incomplete_core_is_readiness_failure_with_remediation(tmp_path: Path) -> None:
    reader = _FakeReader(
        entries=[VmcoreEntry("/var/crash/x/vmcore-incomplete", 100.0, 5, incomplete=True)],
        blobs={},
    )
    with pytest.raises(CategorizedError) as exc:
        _kdump_capture_via_reader(reader, tmp_path / "spool")
    assert exc.value.category is ErrorCategory.READINESS_FAILURE
    assert exc.value.details["reason"] == "kdump_core_incomplete"
    assert exc.value.details["remediation"] == KDUMP_CORE_INCOMPLETE_REMEDIATION
    assert exc.value.details["system_id"] == str(_SYS)


def test_capture_empty_crashdir_keeps_no_core_path(tmp_path: Path) -> None:
    """A genuinely empty /var/crash stays the existing _no_core readiness failure (no reason)."""
    reader = _FakeReader(entries=[], blobs={})
    with pytest.raises(CategorizedError) as exc:
        _kdump_capture_via_reader(reader, tmp_path / "spool")
    assert exc.value.category is ErrorCategory.READINESS_FAILURE
    assert exc.value.details.get("reason") != "kdump_core_incomplete"


def test_guest_core_reader_protocol_is_runtime_checkable() -> None:
    assert isinstance(_FakeReader(entries=[]), GuestCoreReader)


def test_file_sha256_b64_matches_hashlib(tmp_path: Path) -> None:
    core = tmp_path / "core.bin"
    core.write_bytes(b"COREBYTES")
    expected = base64.b64encode(hashlib.sha256(b"COREBYTES").digest()).decode("ascii")
    assert file_sha256_b64(core) == expected


def test_read_via_tempfile_passes_a_path_holding_the_bytes() -> None:
    seen: dict[str, bytes] = {}

    def reader(path: Path) -> str:
        seen["bytes"] = path.read_bytes()
        return "deadbeef"

    assert read_via_tempfile(b"COREBYTES", reader) == "deadbeef"
    assert seen["bytes"] == b"COREBYTES"


def test_read_via_tempfile_removes_the_temp_file() -> None:
    captured: list[Path] = []

    def reader(path: Path) -> str:
        captured.append(path)
        return "x"

    read_via_tempfile(b"X", reader)
    assert not captured[0].exists()


def test_extract_dmesg_success_returns_bytes(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.write_bytes(b"core")
    assert extract_dmesg_or_sentinel(core, lambda _p: b"kernel log") == b"kernel log"


def test_extract_dmesg_degrades_infrastructure_failure_to_sentinel(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.write_bytes(b"core")

    def boom(_p: Path) -> bytes:
        raise CategorizedError("no debuginfo", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    assert extract_dmesg_or_sentinel(core, boom) == DMESG_UNAVAILABLE


def test_extract_dmesg_reraises_missing_dependency(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.write_bytes(b"core")

    def no_drgn(_p: Path) -> bytes:
        raise CategorizedError("drgn missing", category=ErrorCategory.MISSING_DEPENDENCY)

    with pytest.raises(CategorizedError) as exc:
        extract_dmesg_or_sentinel(core, no_drgn)
    assert exc.value.category is ErrorCategory.MISSING_DEPENDENCY


def test_redact_dmesg_scrubs_a_registered_secret(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.write_bytes(b"core")
    registry = SecretRegistry()
    registry.register("hunter2", scope=None)
    out = redact_dmesg(core, lambda _p: b"login password=hunter2 done", registry)
    assert b"hunter2" not in out
    assert b"password=" in out


# ---------------------------------------------------------------------------
# live_vm acceptance: real panic → kdump core → harvest without staging
# ---------------------------------------------------------------------------
#
# Validates the full #654 arc on an operator KVM host.  CI deselects ``live_vm``
# (``just test`` runs ``-m "not live_vm and not live_stack"``), so this is SKIPPED
# unless the operator runs ``just test-live`` with all prerequisites satisfied.
#
# Manual end-to-end runbook (including kdump service wire-up and drgn/libguestfs
# venv prep): docs/operating/runbooks/four-method-live-run.md §4b.

_FORCE_CRASH_WAIT_S = 60  # seconds to poll for the vmcore after the NMI
_VMCORE_POLL_INTERVAL_S = 5


@pytest.mark.live_vm
@pytest.mark.live_vm_provisioned
def test_live_vm_kdump_capture_arc_no_staging() -> None:  # pragma: no cover - live_vm
    """Force-crash a kdump System; verify vmcore.fetch harvests a real core host-side.

    Asserts (real-seam, not mocked):
      1. ``control.force_crash`` injects NMI over the real libvirt connection.
      2. ``LocalLibvirtRetrieve.capture(method=KDUMP)`` drives the libguestfs overlay
         harvest (``_real_wait_for_vmcore``), streams the core to the object store, and
         returns a ``CaptureOutput`` with a non-empty ``vmcore_build_id`` and a positive
         ``raw_size_bytes`` — confirming a real ``/var/crash/<ts>/vmcore`` was written
         by the in-guest kdump service without any staging step.
      3. No staging artifact exists: the raw ref lives under ``systems/<uuid>/vmcore-kdump``
         (not a per-run staging path).

    Gated by ``require_live_vm_provisioned`` (the provisioned-System family gate): skips when
    ``KDIVE_LIVE_VM_SYSTEM_ID`` is absent, and fails loud when it is set but the ``KDIVE_S3_*``
    backend is incomplete — a mis-provisioned runner must not masquerade as "no environment".
    """
    import shutil

    contract = require_live_vm_provisioned()
    if not shutil.which("virsh"):
        pytest.skip("virsh not on PATH; local kdump acceptance needs a local libvirt install")

    from kdive.domain.capture import CaptureMethod
    from kdive.providers.local_libvirt.lifecycle.control import LocalLibvirtControl
    from kdive.providers.local_libvirt.retrieve import LocalLibvirtRetrieve
    from kdive.providers.shared.runtime_paths import domain_name_for
    from kdive.security.secrets.secret_registry import SecretRegistry

    system_id = UUID(contract.system_id)
    domain_name = domain_name_for(system_id)

    # Step 1: panic the guest via NMI over the real libvirt connection.
    control = LocalLibvirtControl.from_env()
    control.force_crash(domain_name)

    # Step 2: poll until the vmcore appears on the overlay or the window expires.
    # ``_real_wait_for_vmcore`` (called from capture) owns this; we give kdump time to write
    # the core before capture opens the libguestfs appliance.
    deadline = time.monotonic() + _FORCE_CRASH_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(_VMCORE_POLL_INTERVAL_S)

    # Step 3: harvest the core via the real seam — libguestfs reads the overlay read-only,
    # streams the core to a worker spool dir, then uploads it to the object store.
    retriever = LocalLibvirtRetrieve.from_env(secret_registry=SecretRegistry())
    run_id = UUID("44444444-4444-4444-4444-444444444444")
    out = retriever.capture(system_id, run_id, CaptureMethod.KDUMP)

    # The core was real: non-empty build-id and positive byte count.
    assert out.vmcore_build_id, "vmcore_build_id is empty — core may be corrupt or wrong method"
    assert out.raw_size_bytes > 0, "raw_size_bytes is zero — no core was actually captured"

    # The raw artifact lives under the System's prefix, not a per-run staging path.
    assert f"systems/{system_id}" in out.raw.key, (
        f"unexpected raw artifact key {out.raw.key!r}: expected systems/<uuid>/vmcore-kdump"
    )
    assert out.raw.key.endswith("vmcore-kdump"), (
        f"raw key {out.raw.key!r} does not end with vmcore-kdump"
    )
    assert out.redacted.key.endswith("vmcore-kdump-redacted"), (
        f"redacted key {out.redacted.key!r} does not end with vmcore-kdump-redacted"
    )
