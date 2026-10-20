"""Unit tests for the owned-cleanup and released-capacity check; no stack required."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import libvirt
import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import LiveStackClient
from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack.cleanup import (
    capacity_in_use,
    disk_absent,
    domain_disks,
    release_and_verify,
    system_failed,
)

_XML = """<domain type='kvm'><devices>
<disk type='file' device='disk'><source file='/var/lib/x/overlay.qcow2'/></disk>
<disk type='file' device='cdrom'><source file='/var/lib/x/seed.iso'/></disk>
<disk type='network' device='disk'><source protocol='nbd' name='n'/></disk>
</devices></domain>"""


class _Client:
    def __init__(
        self, *in_use: int, system: str = "torn_down", released_before: bool = False
    ) -> None:
        self.in_use = list(in_use)
        self.system = system
        self.released = released_before
        self.calls: list[str] = []

    def _next_in_use(self) -> int:
        """Each availability read takes the next value; the last one repeats."""
        return self.in_use.pop(0) if len(self.in_use) > 1 else self.in_use[0]

    async def call_tool(self, name: str, /, **_args: object) -> ToolResponse:
        self.calls.append(name)
        if name == "resources.availability":
            items = [
                ToolResponse.success(str(i), "available", data={"in_use": n})
                for i, n in enumerate((self._next_in_use(), 0))
            ]
            return ToolResponse.collection("resources", "ok", items)
        if name == "systems.get" and self.system == "failed":
            # systems.get renders a failed System as a failure envelope.
            return ToolResponse.failure(
                "x", ErrorCategory.PROVISIONING_FAILURE, data={"current_status": "failed"}
            )
        if name == "allocations.release":
            self.released = True
        allocation = "released" if self.released else "granted"
        status = {"systems.get": self.system, "allocations.wait": allocation}.get(name, "ok")
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


def _verify(
    client: _Client, disks: list[str], *, defined: bool = False, from_failed: bool = False
) -> dict[str, object]:
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
            from_failed=from_failed,
        )
    )


def test_domain_disks_lists_only_file_sources() -> None:
    assert domain_disks(_XML) == ["/var/lib/x/overlay.qcow2", "/var/lib/x/seed.iso"]


def test_verified_cleanup_reports_every_observation(tmp_path: Path) -> None:
    client = _Client(1, 0)
    result = _verify(client, [str(tmp_path / "gone.qcow2")])
    assert result == {
        "system": "torn_down",
        "allocation": "released",
        "domain": "absent",
        "disks_absent": 1,
        "in_use": [0, 1, 0],
        "released_by": "frame",
    }
    assert client.calls[:3] == ["allocations.wait", "resources.availability", "allocations.release"]


def test_an_allocation_the_body_released_is_proven_without_a_second_release() -> None:
    client = _Client(0, released_before=True)
    result = _verify(client, [])
    assert (result["released_by"], result["in_use"]) == ("body", [0, None, 0])
    assert "allocations.release" not in client.calls
    assert "systems.get" in client.calls


def test_a_body_released_allocation_still_waits_for_torn_down() -> None:
    with pytest.raises(AssertionError):
        _verify(_Client(0, system="ready", released_before=True), [])


def test_a_body_released_allocation_still_proves_absence(tmp_path: Path) -> None:
    disk = tmp_path / "overlay.qcow2"
    disk.write_bytes(b"x")
    with pytest.raises(AssertionError, match="disk"):
        _verify(_Client(0, released_before=True), [str(disk)])
    with pytest.raises(AssertionError, match="domain"):
        _verify(_Client(0, released_before=True), [], defined=True)
    with pytest.raises(AssertionError, match="not the 0 held before"):
        _verify(_Client(1, released_before=True), [])


def test_a_failed_system_is_released_without_a_teardown() -> None:
    # `failed` is terminal: the release frees the Allocation and capacity, the System stays put.
    result = _verify(_Client(1, 0, system="failed"), [], from_failed=True)
    assert (result["system"], result["allocation"], result["in_use"]) == (
        "failed",
        "released",
        [0, 1, 0],
    )


def test_an_unrelated_error_envelope_is_not_a_failed_system() -> None:
    error = ToolResponse.failure("x", ErrorCategory.TRANSPORT_FAILURE)
    assert not system_failed(error)


def test_a_failed_release_must_find_the_system_still_failed() -> None:
    with pytest.raises(AssertionError, match="failed System is now"):
        _verify(_Client(1, 0, system="ready"), [], from_failed=True)


def test_a_surviving_disk_fails(tmp_path: Path) -> None:
    disk = tmp_path / "overlay.qcow2"
    disk.write_bytes(b"x")
    with pytest.raises(AssertionError, match="disk"):
        _verify(_Client(1, 0), [str(disk)])


def test_a_defined_domain_fails(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="domain"):
        _verify(_Client(1, 0), [], defined=True)


def test_capacity_that_did_not_return_fails() -> None:
    with pytest.raises(AssertionError, match="not the 0 held before"):
        _verify(_Client(1, 1), [])


def test_capacity_that_never_counted_the_allocation_fails() -> None:
    with pytest.raises(AssertionError, match="does not see this allocation"):
        _verify(_Client(0), [])


def test_capacity_sums_every_resource() -> None:
    assert asyncio.run(capacity_in_use(cast(LiveStackClient, _Client(3)))) == 3


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


def test_cleanup_uses_the_absent_predicate() -> None:
    with pytest.raises(AssertionError, match="kept.qcow2"):
        asyncio.run(
            release_and_verify(
                cast(LiveStackClient, _Client(1, 0)),
                allocation_id="a",
                system_id="s",
                domain="kdive-s",
                disks=["/remote/gone.qcow2", "/remote/kept.qcow2"],
                in_use_before=0,
                connect=lambda: _Conn(False),
                absent=lambda path: path != "/remote/kept.qcow2",
                deadline_s=1.0,
                poll_s=0.0,
            )
        )
