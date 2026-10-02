"""Unit tests for the owned-cleanup and released-capacity check; no stack required."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import libvirt
import pytest

from kdive.mcp.dev_harness import LiveStackClient
from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack.cleanup import (
    capacity_in_use,
    disk_absent,
    domain_disks,
    release_and_verify,
)

_XML = """<domain type='kvm'><devices>
<disk type='file' device='disk'><source file='/var/lib/x/overlay.qcow2'/></disk>
<disk type='file' device='cdrom'><source file='/var/lib/x/seed.iso'/></disk>
<disk type='network' device='disk'><source protocol='nbd' name='n'/></disk>
</devices></domain>"""


class _Client:
    def __init__(self, in_use: int) -> None:
        self.in_use = in_use
        self.calls: list[str] = []

    async def call_tool(self, name: str, /, **_args: object) -> ToolResponse:
        self.calls.append(name)
        if name == "resources.availability":
            items = [
                ToolResponse.success(str(i), "available", data={"in_use": n})
                for i, n in enumerate((self.in_use, 0))
            ]
            return ToolResponse.collection("resources", "ok", items)
        status = {"systems.get": "torn_down", "allocations.wait": "released"}.get(name, "ok")
        return ToolResponse.success("x", status)


class _NoDomain(libvirt.libvirtError):
    def get_error_code(self) -> int:
        return libvirt.VIR_ERR_NO_DOMAIN


class _Conn:
    def __init__(self, defined: bool) -> None:
        self.defined = defined

    def lookupByName(self, name: str) -> object:  # noqa: N802 - libvirt binding name
        if self.defined:
            return object()
        raise _NoDomain(name)

    def close(self) -> int:
        return 0


def _verify(client: _Client, disks: list[str], *, defined: bool = False) -> dict[str, object]:
    return asyncio.run(
        release_and_verify(
            cast(LiveStackClient, client),
            allocation_id="a",
            system_id="s",
            domain="kdive-s",
            disks=disks,
            in_use_before=0,
            connect=lambda: _Conn(defined),
            deadline_s=1.0,
            poll_s=0.0,
        )
    )


def test_domain_disks_lists_only_file_sources() -> None:
    assert domain_disks(_XML) == ["/var/lib/x/overlay.qcow2", "/var/lib/x/seed.iso"]


def test_verified_cleanup_reports_every_observation(tmp_path: Path) -> None:
    client = _Client(in_use=0)
    result = _verify(client, [str(tmp_path / "gone.qcow2")])
    assert result == {
        "system": "torn_down",
        "allocation": "released",
        "domain": "absent",
        "disks_absent": 1,
        "in_use": [0, 0],
    }
    assert client.calls[0] == "allocations.release"


def test_a_surviving_disk_fails(tmp_path: Path) -> None:
    disk = tmp_path / "overlay.qcow2"
    disk.write_bytes(b"x")
    with pytest.raises(AssertionError, match="disk"):
        _verify(_Client(in_use=0), [str(disk)])


def test_a_defined_domain_fails(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="domain"):
        _verify(_Client(in_use=0), [], defined=True)


def test_capacity_that_did_not_return_fails() -> None:
    with pytest.raises(AssertionError, match="capacity"):
        _verify(_Client(in_use=1), [])


def test_capacity_sums_every_resource() -> None:
    assert asyncio.run(capacity_in_use(cast(LiveStackClient, _Client(in_use=3)))) == 3


@pytest.fixture
def unsearchable(tmp_path: Path) -> Iterator[Path]:
    parent = tmp_path / "private"
    parent.mkdir()
    (parent / "overlay.qcow2").write_bytes(b"x")
    parent.chmod(0)
    yield parent / "overlay.qcow2"
    parent.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_a_disk_under_an_unsearchable_parent_is_not_absent(unsearchable: Path) -> None:
    assert disk_absent(str(unsearchable), privileged_test=lambda _p: 0) is False
    assert disk_absent(str(unsearchable), privileged_test=lambda _p: 1) is True
    with pytest.raises(AssertionError, match="cannot observe"):
        disk_absent(str(unsearchable), privileged_test=lambda _p: 2)
