"""Unit tests for the remote deep-lifecycle frame, observer and bindings (#2810); no stack."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from pathlib import Path
from typing import Any, cast

import libvirt
import pytest
import yaml

from kdive.images.rootfs.catalog import load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient
from kdive.profiles.provisioning import ProvisioningProfile
from scripts.coverage_campaign.contract import image_family
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack import remote_lifecycle
from tests.integration.live_stack.deep_lifecycle import baseline
from tests.integration.live_stack.evidence import EvidenceWriter, RunIdentity
from tests.integration.live_stack.image_smoke import Endpoint
from tests.integration.live_stack.remote_lifecycle import (
    REMOTE_BLOCKED,
    REMOTE_REPRESENTATIVES,
    RemoteHost,
    bindings,
    destination,
    guest_boot_kernel,
    host_probe,
    observe_host,
    remote_cells,
    remote_cleanup,
    remote_kdive_domains,
    remote_profile,
    volume_absent,
)
from tests.integration.live_stack.scenario import CellRun, ScenarioStop

_ROOT = Path(__file__).resolve().parents[3]
_HOST = RemoteHost("dave@lab-a.example", "default", "rocky:10.2", "x86_64", "kvm")
_IDENTITY = RunIdentity(
    candidate_sha="a" * 40,
    matrix_sha256="b" * 64,
    host_os="fedora:44",
    host_arch="x86_64",
    clean=True,
    deployed_roles={},
)


def test_every_remote_cell_family_is_represented_or_blocked() -> None:
    cells = remote_cells()
    assert len(cells) == 8 and {c.guest_arch for c in cells} == {"x86_64"}
    for cell in cells:
        assert (cell.family in REMOTE_REPRESENTATIVES) != (cell.family in REMOTE_BLOCKED)
    assert {"#3081", "#3082"} == {reason.split(":")[0] for reason in REMOTE_BLOCKED.values()}


def test_representatives_are_ansible_catalog_rows_of_their_family() -> None:
    group_vars = _ROOT / "deploy/ansible/inventory/group_vars/all.yml"
    catalog = yaml.safe_load(group_vars.read_text(encoding="utf-8"))["kdive_image_catalog"]
    rows = {row["name"]: row for row in catalog}
    local = load_rootfs_catalog().values()
    for family, image in REMOTE_REPRESENTATIVES.items():
        row = rows[image.name]
        assert (row["distro"], str(row["version"])) == (image.distro, image.version)
        assert image.arch == "x86_64"
        assert family in {image_family(e) for e in local if e.distro == image.distro}


def test_destination_rejects_option_and_shell_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(remote_lifecycle.HOST_SSH_ENV, raising=False)
    assert destination() is None
    monkeypatch.setenv(remote_lifecycle.HOST_SSH_ENV, "dave@lab-a.example")
    assert destination() == "dave@lab-a.example"
    for hostile in ("-oProxyCommand=x", "a b", "dave@host;id"):
        monkeypatch.setenv(remote_lifecycle.HOST_SSH_ENV, hostile)
        with pytest.raises(ValueError, match="SSH destination"):
            destination()


def _completed(code: int, stdout: str) -> Any:
    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert argv[:5] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        assert argv[5] == "dave@lab-a.example"
        return subprocess.CompletedProcess(argv, code, stdout, "")

    return run


def test_host_probe_reads_os_arch_and_virtualization(monkeypatch: pytest.MonkeyPatch) -> None:
    out = 'NAME="Rocky Linux"\nID="rocky"\nVERSION_ID="10.2"\nmachine=x86_64\nvirt=kvm\n'
    monkeypatch.setattr(subprocess, "run", _completed(0, out))
    assert host_probe("dave@lab-a.example") == {
        "host_os": "rocky:10.2",
        "host_arch": "x86_64",
        "virt": "kvm",
    }
    monkeypatch.setattr(subprocess, "run", _completed(255, ""))
    with pytest.raises(AssertionError, match="probe exited 255"):
        host_probe("dave@lab-a.example")


class _NoVolume(libvirt.libvirtError):
    def get_error_code(self) -> int:
        return libvirt.VIR_ERR_NO_STORAGE_VOL


class _Denied(libvirt.libvirtError):
    def get_error_code(self) -> int:
        return libvirt.VIR_ERR_ACCESS_DENIED


class _Pool:
    def __init__(self) -> None:
        self.refreshed = 0

    def refresh(self, _flags: int) -> int:
        self.refreshed += 1
        return 0


class _Domain:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _Conn:
    def __init__(self, error: libvirt.libvirtError | None = None, domains: tuple[str, ...] = ()):
        self.pools = [_Pool(), _Pool()]
        self.error = error
        self.domains = domains
        self.closed = False

    def listAllStoragePools(self, _flags: int) -> list[_Pool]:  # noqa: N802 - libvirt name
        return self.pools

    def storageVolLookupByPath(self, path: str) -> object:  # noqa: N802 - libvirt name
        if self.error is not None:
            raise self.error
        return object()

    def listAllDomains(self, _flags: int) -> list[_Domain]:  # noqa: N802 - libvirt name
        return [_Domain(name) for name in self.domains]

    def close(self) -> int:
        self.closed = True
        return 0


def _conn(fake: _Conn) -> libvirt.virConnect:
    return cast(libvirt.virConnect, fake)


def test_volume_absent_refreshes_every_pool_before_lookup() -> None:
    gone = _Conn(_NoVolume("gone"))
    assert volume_absent(_conn(gone), "/pool/overlay.qcow2") is True
    assert [p.refreshed for p in gone.pools] == [1, 1]
    assert volume_absent(_conn(_Conn()), "/pool/overlay.qcow2") is False
    with pytest.raises(libvirt.libvirtError):
        volume_absent(_conn(_Conn(_Denied("denied"))), "/pool/overlay.qcow2")


def test_remote_kdive_domains_lists_only_kdive_domains() -> None:
    conn = _Conn(domains=("kdive-1", "other", "kdive-build-2"))
    assert remote_kdive_domains(_conn(conn)) == {"kdive-1", "kdive-build-2"}


def test_guest_boot_kernel_reads_the_release_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def ssh(endpoint: Endpoint, key: Path, command: str) -> subprocess.CompletedProcess[str]:
        assert endpoint == Endpoint("192.0.2.10", 47201)
        sent.append(command)
        return subprocess.CompletedProcess([], 0, "d" * 64 + "  /boot/vmlinuz\n", "")

    monkeypatch.setattr(remote_lifecycle, "ssh", ssh)
    endpoint = Endpoint("192.0.2.10", 47201)
    assert guest_boot_kernel("s", endpoint, Path("k"), "6.18.54") == ("d" * 64, None)
    assert sent[-1] == "sha256sum -- /boot/vmlinuz-6.18.54"
    guest_boot_kernel("s", endpoint, Path("k"), "6.1'x")
    assert sent[-1] == "sha256sum -- " + shlex.quote("/boot/vmlinuz-6.1'x")


def test_remote_profile_validates() -> None:
    profile = ProvisioningProfile.parse(remote_profile("x86_64", "fedora-base.qcow2"))
    assert profile.boot_method.value == "disk-image"
    assert profile.provider.remote_libvirt.base_image_volume == "fedora-base.qcow2"


def _run(tmp_path: Path) -> CellRun:
    return CellRun(remote_cells()[0], EvidenceWriter(tmp_path))


def test_observe_host_overrides_the_control_plane_identity(tmp_path: Path) -> None:
    run = _run(tmp_path)
    observe_host(run, _HOST)
    context = run.context(_IDENTITY)
    assert (context.host_os, context.host_arch) == ("rocky:10.2", "x86_64")
    assert len(run.artifacts) == 1
    stored = (tmp_path / "artifacts" / run.artifacts[0]).read_text()
    assert '"virtualization":"kvm"' in stored and "lab-a" not in stored
    bound = _bindings(tmp_path).cells[run.cell.id]
    assert (bound.host_os, bound.host_arch) == (context.host_os, context.host_arch)


def test_observe_host_blocks_a_foreign_architecture(tmp_path: Path) -> None:
    host = RemoteHost("dave@lab-a.example", "default", "fedora:43", "ppc64le", "none")
    with pytest.raises(ScenarioStop) as stop:
        observe_host(_run(tmp_path), host)
    assert stop.value.outcome is Outcome.BLOCKED


def _manifest() -> dict[str, Any]:
    return {
        "arch": "x86_64",
        "source": {"commit": "c" * 40},
        "artifacts": {".config": "d" * 64},
        "toolchain": {"gcc": "gcc 15"},
        "build_id": "e" * 40,
        "release": "6.18.54",
    }


def _bindings(tmp_path: Path) -> Any:
    def fixture(root: Path, name: str, arch: str) -> tuple[Path, dict[str, Any]]:
        tree = root / name
        (tree / "arch/x86/boot").mkdir(parents=True, exist_ok=True)
        (tree / "arch/x86/boot/bzImage").write_bytes(b"kernel")
        return tree, _manifest()

    return bindings(
        "a" * 40,
        root=tmp_path,
        matrix="b" * 64,
        host=_HOST,
        digest=lambda _dest, _pool, _volume: "f" * 64,
        fixture=fixture,
        staged=lambda name: f"{name}.qcow2",
    )


def test_bindings_bind_every_remote_cell(tmp_path: Path) -> None:
    inputs = _bindings(tmp_path)
    assert set(inputs.cells) == {c.id for c in remote_cells()}
    for cell in remote_cells():
        context = inputs.cells[cell.id]
        assert (context.host_os, context.accelerator) == ("rocky:10.2", "kvm")
        image = REMOTE_REPRESENTATIVES.get(str(cell.family))
        if image is None:
            assert context.guest_os is None and context.image_sha256 is None
            continue
        assert context.guest_os == f"{image.distro}:{image.version}"
        assert context.image_sha256 == "f" * 64
        assert context.kernel_build_id == "e" * 40, baseline(cell)


def _cleanup(
    monkeypatch: pytest.MonkeyPatch, before: set[str], after: tuple[str, ...]
) -> dict[str, object]:
    async def verified(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"domain": "absent"}

    conn = _Conn(domains=after)
    monkeypatch.setattr(remote_lifecycle, "release_and_verify", verified)
    monkeypatch.setattr(remote_lifecycle, "observer", lambda _dest: _conn(conn))
    client = cast(LiveStackClient, object())
    system = "00000000-0000-0000-0000-000000000001"
    return asyncio.run(remote_cleanup(client, _HOST, "a", system, [], 0, before))


def test_remote_cleanup_requires_the_prior_domain_set(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _cleanup(monkeypatch, {"kdive-x"}, ("kdive-x",)) == {
        "domain": "absent",
        "remote_domains": "unchanged",
    }
    with pytest.raises(AssertionError, match="kdive-y"):
        _cleanup(monkeypatch, {"kdive-x"}, ("kdive-x", "kdive-y"))


def test_volume_sha256_hashes_on_the_provider_host(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        sent.append(argv)
        return subprocess.CompletedProcess(argv, 0, "a" * 64 + "  -\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    remote_lifecycle.volume_sha256.cache_clear()
    assert remote_lifecycle.volume_sha256("dave@lab-a.example", "default", "f b.qcow2") == "a" * 64
    command = sent[0][-1]
    assert "set -o pipefail" in command and "--pool default 'f b.qcow2' /dev/stdout" in command
    remote_lifecycle.volume_sha256.cache_clear()
    monkeypatch.setattr(subprocess, "run", _completed(1, ""))
    with pytest.raises(AssertionError, match="volume digest exited 1"):
        remote_lifecycle.volume_sha256("dave@lab-a.example", "default", "x.qcow2")
