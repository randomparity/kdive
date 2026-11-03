"""Unit tests for the image-smoke probes and bindings (#2808); no stack required."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from kdive.images.families import family_for
from kdive.images.rootfs.catalog import load_rootfs_catalog
from kdive.mcp.responses import ToolResponse
from kdive.serialization import JsonValue
from scripts.coverage_campaign.contract import build_contract
from scripts.coverage_campaign.evidence import Outcome
from scripts.coverage_campaign.results import qualify
from tests.integration.live_stack.evidence import RunIdentity, build_record
from tests.integration.live_stack.image_smoke import (
    Endpoint,
    bindings,
    native_cells,
    os_matches,
    parse_probe,
    provenance_matches,
    ssh,
    toolchain_command,
)

_HEAD = "a" * 40
_CATALOG = load_rootfs_catalog()
_PROBE_OUT = """uid=0
boot_id=0f7c
machine=x86_64
NAME="Rocky Linux"
ID="rocky"
VERSION_ID="9.8"
"""


def test_native_cells_are_the_hosts_image_smoke_cells() -> None:
    cells = native_cells("x86_64")
    expected = {n for n, e in _CATALOG.items() if e.arch == "x86_64"}
    assert {c.image for c in cells} == expected
    assert all(c.operation == "image-smoke" and c.guest_arch == "x86_64" for c in cells)
    assert {c.image for c in native_cells("ppc64le")}.isdisjoint(expected)


def test_parse_probe_reads_the_probe_and_os_release() -> None:
    probe = parse_probe(_PROBE_OUT)
    assert probe["uid"] == "0" and probe["boot_id"] == "0f7c" and probe["machine"] == "x86_64"
    assert (probe["ID"], probe["VERSION_ID"]) == ("rocky", "9.8")


def test_os_matches_accepts_a_point_release_of_the_catalog_major() -> None:
    probe = parse_probe(_PROBE_OUT)
    assert os_matches(_CATALOG["rocky-kdive-ready-9"], probe)
    assert not os_matches(_CATALOG["rocky-kdive-ready-10"], probe)
    assert not os_matches(_CATALOG["rocky-kdive-ready-9"], {**probe, "machine": "ppc64le"})


def test_os_matches_maps_centos_to_centos_stream_and_bounds_the_prefix() -> None:
    probe = {"ID": "centos", "VERSION_ID": "10", "machine": "x86_64"}
    assert os_matches(_CATALOG["centos-stream-kdive-ready-10"], probe)
    assert not os_matches(_CATALOG["centos-stream-kdive-ready-9"], {**probe, "VERSION_ID": "90"})


def test_toolchain_command_checks_every_build_package() -> None:
    entry = _CATALOG["fedora-kdive-build-44"]
    command = toolchain_command(entry)
    assert command.startswith("rpm -q ")
    for package in family_for(entry.family).packages("build", entry.distro, entry.version):
        assert f" {package} " in command
    assert "make" in command and "gcc" in command


def _binding_inputs(tmp_path: Path):
    image = tmp_path / "rocky.qcow2"
    image.write_bytes(b"rootfs bytes")
    staged = {"rocky-kdive-ready-9": image}
    return image, bindings(
        _HEAD,
        host_os="ubuntu:26.04",
        host_arch="x86_64",
        matrix="c" * 64,
        staged=staged.get,
    )


def test_bindings_carry_the_full_expected_context(tmp_path: Path) -> None:
    image, inputs = _binding_inputs(tmp_path)
    rocky = inputs.cells["image-smoke/rocky-kdive-ready-9/x86_64"]
    assert rocky.image_sha256 == hashlib.sha256(image.read_bytes()).hexdigest()
    assert (rocky.guest_os, rocky.guest_arch, rocky.accelerator) == ("rocky:9", "x86_64", "kvm")
    assert (rocky.host_os, rocky.host_arch) == ("ubuntu:26.04", "x86_64")
    unstaged = inputs.cells["image-smoke/debian-kdive-ready-13/x86_64"]
    assert unstaged.image_sha256 is None
    assert set(inputs.cells) == {c.id for c in native_cells("x86_64")}


def test_a_success_record_from_the_binding_qualifies_without_an_authority(
    tmp_path: Path,
) -> None:
    _, inputs = _binding_inputs(tmp_path)
    cell_id = "image-smoke/rocky-kdive-ready-9/x86_64"
    node = (
        "tests/integration/live_stack/test_image_smoke.py"
        "::test_native_cells_are_the_hosts_image_smoke_cells"
    )
    contract = build_contract()
    contract = replace(
        contract,
        cells=tuple(replace(c, node_id=node) if c.id == cell_id else c for c in contract.cells),
    )
    inputs = inputs.model_copy(update={"matrix_sha256": contract.matrix_sha256})
    cell = next(c for c in contract.cells if c.id == cell_id)
    roles = {"server": _HEAD, "worker": _HEAD, "reconciler": _HEAD}
    identity = RunIdentity(_HEAD, contract.matrix_sha256, "ubuntu:26.04", "x86_64", True, roles)
    record = build_record(
        cell,
        identity,
        outcome=Outcome.SUCCESS,
        context=inputs.cells[cell_id],
        duration_s=1.0,
        assertions=dict.fromkeys(cell.assertions, "d" * 64),
    )
    verdict = next(v for v in qualify(contract, inputs, [record]).cells if v.cell.id == cell_id)
    assert verdict.qualified, verdict.reasons
    stale_identity = replace(identity, deployed_roles={**roles, "authority": "e" * 40})
    stale = build_record(
        cell,
        stale_identity,
        outcome=Outcome.SUCCESS,
        context=inputs.cells[cell_id],
        duration_s=1.0,
        assertions=dict.fromkeys(cell.assertions, "d" * 64),
    )
    verdict = next(v for v in qualify(contract, inputs, [stale]).cells if v.cell.id == cell_id)
    assert verdict.reasons == ("deployed-revision-mismatch",)


def test_ssh_connects_to_the_endpoint_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert ssh(Endpoint("192.0.2.10", 47201), tmp_path / "key", "true").returncode == 0
    argv = seen[0]
    assert argv[argv.index("-p") + 1] == "47201"
    assert argv[-3:] == ["root@192.0.2.10", "--", "true"]


def test_ssh_timeout_does_not_replay_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def run(argv: list[str], *, timeout: float, **_kwargs: object):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        ssh(Endpoint("127.0.0.1", 22), tmp_path / "key", "make install")
    assert len(calls) == 1


def test_ssh_forwards_per_attempt_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts = []

    def run(argv: list[str], *, timeout: float, **_kwargs: object):
        timeouts.append(timeout)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    ssh(Endpoint("127.0.0.1", 22), tmp_path / "key", "true", timeout_s=2.0)
    assert timeouts == [2.0]


def test_published_provenance_matches_canonical_producer_fields() -> None:
    described = ToolResponse.success(
        "image",
        "registered",
        data={"provenance": {"os_release": {"id": "fedora", "version_id": "43"}, "arch": "x86_64"}},
    )
    provenance_matches(described, _CATALOG["fedora-kdive-ready-43-cloud"])


@pytest.mark.parametrize(
    "field,value", [("id", "rocky"), ("version_id", "44"), ("id", ""), ("version_id", "")]
)
def test_published_provenance_rejects_wrong_or_missing_os(field: str, value: str) -> None:
    release: dict[str, JsonValue] = {"id": "fedora", "version_id": "43"}
    if value:
        release[field] = value
    else:
        del release[field]
    described = ToolResponse.success(
        "image", "registered", data={"provenance": {"os_release": release, "arch": "x86_64"}}
    )
    with pytest.raises(AssertionError, match="provenance names"):
        provenance_matches(described, _CATALOG["fedora-kdive-ready-43-cloud"])


@pytest.mark.parametrize("arch", ["ppc64le", ""])
def test_published_provenance_rejects_wrong_or_missing_arch(arch: str) -> None:
    described = ToolResponse.success(
        "image",
        "registered",
        data={"provenance": {"os_release": {"id": "fedora", "version_id": "43"}, "arch": arch}},
    )
    with pytest.raises(AssertionError, match="provenance names"):
        provenance_matches(described, _CATALOG["fedora-kdive-ready-43-cloud"])
