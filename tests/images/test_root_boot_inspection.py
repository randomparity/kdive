"""Bounded remote root inspection tests (ADR-0583, #2106)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, cast
from xml.etree.ElementTree import ParseError

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes.provenance_probes import (
    ROOT_INSPECTION_MAX_OUTPUT_BYTES,
    _kill_inspector_group,
    _parse_root_boot,
    _run_bounded_inspector,
    inspect_root_boot,
)


def test_bounded_runner_accepts_exact_combined_cap() -> None:
    code = f"import os; os.write(1, b'x' * {ROOT_INSPECTION_MAX_OUTPUT_BYTES})"
    stdout, stderr = _run_bounded_inspector([sys.executable, "-c", code], timeout_s=5)
    assert len(stdout) + len(stderr) == ROOT_INSPECTION_MAX_OUTPUT_BYTES


def test_bounded_runner_kills_cap_plus_one() -> None:
    code = f"import os; os.write(1, b'x' * {ROOT_INSPECTION_MAX_OUTPUT_BYTES + 1})"
    with pytest.raises(CategorizedError) as exc:
        _run_bounded_inspector([sys.executable, "-c", code], timeout_s=5)
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert exc.value.details["reason"] == "output_limit"


def test_bounded_runner_kills_timeout() -> None:
    with pytest.raises(CategorizedError) as exc:
        _run_bounded_inspector([sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=0.01)
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert exc.value.details["reason"] == "timeout"


def test_bounded_runner_kills_partial_output_then_timeout() -> None:
    code = "import os,time; os.write(1,b'prefix'); time.sleep(5)"
    with pytest.raises(CategorizedError) as exc:
        _run_bounded_inspector([sys.executable, "-c", code], timeout_s=0.01)
    assert exc.value.details["reason"] == "timeout"


def test_bounded_runner_keeps_deadline_after_output_closes() -> None:
    code = "import os,time; os.close(1); os.close(2); time.sleep(5)"
    with pytest.raises(CategorizedError) as exc:
        _run_bounded_inspector([sys.executable, "-c", code], timeout_s=0.01)
    assert exc.value.details["reason"] == "timeout"


def test_bounded_runner_kills_descendants(tmp_path: Path) -> None:
    sentinel = tmp_path / "descendant-survived"
    code = (
        "import os,time; pid=os.fork(); "
        "pid and time.sleep(5); os.close(1); os.close(2); time.sleep(.2); "
        f"open({str(sentinel)!r},'w').close()"
    )
    with pytest.raises(CategorizedError):
        _run_bounded_inspector([sys.executable, "-c", code], timeout_s=0.01)
    time.sleep(0.3)
    assert not sentinel.exists()


def test_process_group_cleanup_tolerates_natural_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reaped: list[bool] = []

    class _ExitedProcess:
        pid = 123

        def wait(self) -> None:
            reaped.append(True)

    def missing_group(_pid: int, _signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr("os.killpg", missing_group)
    _kill_inspector_group(cast("Any", _ExitedProcess()))
    assert reaped == [True]


def test_parser_builds_digest_bound_root_spec() -> None:
    xml = b"""<operatingsystems><operatingsystem><mountpoints>
      <mountpoint dev='/dev/sda2'>/</mountpoint></mountpoints><filesystems>
      <filesystem dev='/dev/sda2'><type>xfs</type><uuid>abc</uuid></filesystem>
      </filesystems></operatingsystem></operatingsystems>"""
    result = _parse_root_boot(xml, "x86_64", "sha256:" + "a" * 64)
    assert result.root == "UUID=abc"
    assert result.source.identity == "sha256:" + "a" * 64


_DIGEST = "sha256:" + "a" * 64
_FS_UUID = "00000000-0000-4000-8000-0000000000aa"


def _inspector_xml(
    root_device: str, filesystem_devices: tuple[str, ...], fs: str = "btrfs"
) -> bytes:
    """virt-inspector's mountpoint and filesystem shape for a Fedora Cloud style image."""
    filesystems = "".join(
        f"<filesystem dev='{device}'><type>{fs}</type><uuid>{_FS_UUID}</uuid></filesystem>"
        for device in filesystem_devices
    )
    return (
        "<operatingsystems><operatingsystem><mountpoints>"
        f"<mountpoint dev='{root_device}'>/</mountpoint>"
        "<mountpoint dev='btrfsvol:/dev/sda3/var'>/var</mountpoint>"
        "<mountpoint dev='/dev/sda2'>/boot/efi</mountpoint>"
        f"</mountpoints><filesystems>{filesystems}"
        "<filesystem dev='/dev/sda2'><type>vfat</type><uuid>5BCC-12A9</uuid></filesystem>"
        "</filesystems></operatingsystem></operatingsystems>"
    ).encode()


def test_a_btrfs_subvolume_root_carries_its_subvolume() -> None:
    xml = _inspector_xml(
        "btrfsvol:/dev/sda3/root", ("btrfsvol:/dev/sda3/root", "btrfsvol:/dev/sda3/var")
    )

    result = _parse_root_boot(xml, "x86_64", _DIGEST)

    assert result.arguments == (
        f"root=UUID={_FS_UUID}",
        "rootfstype=btrfs",
        "rootflags=subvol=root",
    )


def test_a_nested_btrfs_subvolume_is_kept_whole() -> None:
    xml = _inspector_xml("btrfsvol:/dev/sda3/os/root", ("btrfsvol:/dev/sda3/os/root",))

    result = _parse_root_boot(xml, "x86_64", _DIGEST)

    assert "rootflags=subvol=os/root" in result.arguments


def test_a_top_level_btrfs_root_has_no_rootflags() -> None:
    result = _parse_root_boot(_inspector_xml("/dev/sda3", ("/dev/sda3",)), "x86_64", _DIGEST)

    assert result.arguments == (f"root=UUID={_FS_UUID}", "rootfstype=btrfs")


def test_a_non_btrfs_root_has_no_rootflags() -> None:
    result = _parse_root_boot(
        _inspector_xml("/dev/sda3", ("/dev/sda3",), fs="xfs"), "x86_64", _DIGEST
    )

    assert result.arguments == (f"root=UUID={_FS_UUID}", "rootfstype=xfs")


@pytest.mark.parametrize(
    "device",
    [
        "btrfsvol:/dev/sda3/",
        "btrfsvol:/dev/sda3",
        "btrfsvol:/dev/sda3/a b",
        "btrfsvol:/dev/sda3/a=b",
        "btrfsvol:/dev/sda3/a,b",
        "btrfsvol:/dev/sda3/../x",
        "btrfsvol:/dev/mapper/vg-root/root",
        "btrfsvol:/dev/sda3/" + "x" * 200,
    ],
)
def test_an_unusable_subvolume_fails_closed(device: str) -> None:
    with pytest.raises(ValueError, match="subvolume"):
        _parse_root_boot(_inspector_xml(device, (device,)), "x86_64", _DIGEST)


@pytest.mark.parametrize("xml", [b"not xml", b"<x/>", b"<!DOCTYPE x><x/>"])
def test_parser_rejects_malformed_or_incomplete_output(xml: bytes) -> None:
    with pytest.raises((ValueError, ParseError)):
        _parse_root_boot(xml, "x86_64", "sha256:" + "a" * 64)


def test_inspector_maps_malformed_xml_to_provisioning_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "kdive.images.planes.provenance_probes._run_bounded_inspector",
        lambda *_args, **_kwargs: (b"not xml", b""),
    )
    with pytest.raises(CategorizedError) as exc:
        inspect_root_boot(tmp_path / "root.qcow2", "x86_64", "sha256:" + "a" * 64)
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert exc.value.details["reason"] == "malformed_output"


def test_an_unsupported_subvolume_is_reported_as_an_actionable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    device = "btrfsvol:/dev/mapper/vg-root/root"
    monkeypatch.setattr(
        "kdive.images.planes.provenance_probes._run_bounded_inspector",
        lambda *_args, **_kwargs: (_inspector_xml(device, (device,)), b""),
    )
    with pytest.raises(CategorizedError) as exc:
        inspect_root_boot(tmp_path / "root.qcow2", "x86_64", _DIGEST)
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert exc.value.details["reason"] == "unsupported_btrfs_root"
