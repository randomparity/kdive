"""Safety tests for the one disposable private-authority fixture script."""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import pytest

_SYSTEM_ID = UUID("11111111-1111-1111-1111-111111111111")
_SOURCE_NAME = "live-vm-provisioned-rootfs.qcow2"


def _script() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts/live-vm/provision-authority-fixture.py"
    spec = importlib.util.spec_from_file_location("provision_authority_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _profile(script: ModuleType, source: Path) -> Any:
    return script.ProvisioningProfile.model_validate(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 2,
            "memory_mb": 4096,
            "disk_gb": 10,
            "boot_method": "direct-kernel",
            "kernel_source_ref": "fixture",
            "provider": {"local-libvirt": {"rootfs": {"kind": "local", "path": str(source)}}},
        }
    )


def _qcow2(*, backing: bytes = b"", external_data: bool = False) -> bytes:
    header = bytearray(104)
    header[:4] = b"QFI\xfb"
    header[4:8] = (3).to_bytes(4, "big")
    if backing:
        header[8:16] = (len(header)).to_bytes(8, "big")
        header[16:20] = len(backing).to_bytes(4, "big")
    if external_data:
        header[72:80] = (4).to_bytes(8, "big")
    return bytes(header) + backing + b"fixture-payload"


def _fixture_roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    source_root = tmp_path / "worker"
    private_root = tmp_path / "authority"
    source_root.mkdir()
    private_root.mkdir(mode=0o700)
    source = source_root / _SOURCE_NAME
    return source_root, private_root, source


def test_stage_fixture_base_copies_pinned_standalone_image_without_mutating_profile(
    tmp_path: Path,
) -> None:
    script = _script()
    source_root, private_root, source = _fixture_roots(tmp_path)
    image = _qcow2()
    source.write_bytes(image)
    sibling = private_root / "unrelated.qcow2"
    sibling.write_bytes(b"survives")
    profile = _profile(script, source)

    staged = script._stage_fixture_base(
        _SYSTEM_ID,
        profile,
        authority_uid=os.geteuid(),
        authority_gid=os.getegid(),
        source_root=source_root,
        private_root=private_root,
        maximum_bytes=len(image),
    )

    destination = private_root / f"{_SYSTEM_ID}-fixture-base.qcow2"
    assert destination.read_bytes() == image
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert sibling.read_bytes() == b"survives"
    assert profile.provider.local_libvirt.rootfs.path == str(source)
    assert staged.provider.local_libvirt.rootfs.path == str(destination)


def test_stage_fixture_base_refuses_existing_destination_without_overwrite(
    tmp_path: Path,
) -> None:
    script = _script()
    source_root, private_root, source = _fixture_roots(tmp_path)
    source.write_bytes(_qcow2())
    destination = private_root / f"{_SYSTEM_ID}-fixture-base.qcow2"
    destination.write_bytes(b"prior")

    with pytest.raises(FileExistsError):
        script._stage_fixture_base(
            _SYSTEM_ID,
            _profile(script, source),
            authority_uid=os.geteuid(),
            authority_gid=os.getegid(),
            source_root=source_root,
            private_root=private_root,
        )
    assert destination.read_bytes() == b"prior"


def test_stage_fixture_base_detects_path_replacement_after_pinned_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script()
    source_root, private_root, source = _fixture_roots(tmp_path)
    original = _qcow2() + b"original"
    replacement = _qcow2() + b"replacement"
    source.write_bytes(original)
    validate = script._validate_standalone_qcow2

    def replace_after_open(descriptor: int, size: int) -> None:
        validate(descriptor, size)
        candidate = source_root / "replacement.qcow2"
        candidate.write_bytes(replacement)
        candidate.replace(source)

    monkeypatch.setattr(script, "_validate_standalone_qcow2", replace_after_open)
    with pytest.raises(ValueError, match="changed while it was copied"):
        script._stage_fixture_base(
            _SYSTEM_ID,
            _profile(script, source),
            authority_uid=os.geteuid(),
            authority_gid=os.getegid(),
            source_root=source_root,
            private_root=private_root,
        )

    assert source.read_bytes() == replacement
    assert not (private_root / f"{_SYSTEM_ID}-fixture-base.qcow2").exists()


def test_stage_fixture_base_accepts_only_the_fixed_worker_source(tmp_path: Path) -> None:
    script = _script()
    source_root, private_root, _source = _fixture_roots(tmp_path)
    other = source_root / "caller-selected.qcow2"
    other.write_bytes(_qcow2())

    with pytest.raises(ValueError, match="fixed worker rootfs input"):
        script._stage_fixture_base(
            _SYSTEM_ID,
            _profile(script, other),
            authority_uid=os.geteuid(),
            authority_gid=os.getegid(),
            source_root=source_root,
            private_root=private_root,
        )
    assert list(private_root.iterdir()) == []


@pytest.mark.parametrize(
    ("image", "message"),
    [
        (_qcow2(backing=b"/outside/attacker-selected.qcow2"), "backing file"),
        (_qcow2(external_data=True), "external data file"),
    ],
)
def test_stage_fixture_base_refuses_non_standalone_qcow2_without_destination(
    tmp_path: Path, image: bytes, message: str
) -> None:
    script = _script()
    source_root, private_root, source = _fixture_roots(tmp_path)
    source.write_bytes(image)

    with pytest.raises(ValueError, match=message):
        script._stage_fixture_base(
            _SYSTEM_ID,
            _profile(script, source),
            authority_uid=os.geteuid(),
            authority_gid=os.getegid(),
            source_root=source_root,
            private_root=private_root,
        )
    assert not (private_root / f"{_SYSTEM_ID}-fixture-base.qcow2").exists()


def test_stage_fixture_base_rejects_symlink_and_oversized_source(tmp_path: Path) -> None:
    script = _script()
    source_root, private_root, source = _fixture_roots(tmp_path)
    target = tmp_path / "target.qcow2"
    target.write_bytes(_qcow2())
    source.symlink_to(target)

    with pytest.raises(OSError):
        script._stage_fixture_base(
            _SYSTEM_ID,
            _profile(script, source),
            authority_uid=os.geteuid(),
            authority_gid=os.getegid(),
            source_root=source_root,
            private_root=private_root,
        )

    source.unlink()
    source.write_bytes(_qcow2())
    with pytest.raises(ValueError, match="byte bound"):
        script._stage_fixture_base(
            _SYSTEM_ID,
            _profile(script, source),
            authority_uid=os.geteuid(),
            authority_gid=os.getegid(),
            source_root=source_root,
            private_root=private_root,
            maximum_bytes=source.stat().st_size - 1,
        )
    assert not (private_root / f"{_SYSTEM_ID}-fixture-base.qcow2").exists()


def test_existing_private_domain_is_refused_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script()
    closed = False

    class Connection:
        def lookupByName(self, name: str) -> object:
            assert name == f"kdive-{_SYSTEM_ID}"
            return object()

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(script.libvirt, "open", lambda uri: Connection())
    with pytest.raises(ValueError, match="existing private authority fixture domain"):
        script._refuse_existing_authority_domain(_SYSTEM_ID)
    assert closed


def test_main_checks_private_domain_before_worker_or_file_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _script()
    mutated = False

    class Provider:
        local_libvirt_section = object()

    class Profile:
        provider = Provider()

    def mutation(*_args: object) -> None:
        nonlocal mutated
        mutated = True

    monkeypatch.setattr(script.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sys, "argv", ["provision-authority-fixture.py", str(_SYSTEM_ID)])
    monkeypatch.setattr(script.json, "load", lambda _stream: {})
    monkeypatch.setattr(script.ProvisioningProfile, "model_validate", lambda _value: Profile())
    monkeypatch.setattr(
        script,
        "_refuse_existing_authority_domain",
        lambda _system_id: (_ for _ in ()).throw(ValueError("existing private domain")),
    )
    monkeypatch.setattr(script, "_undefine_worker_domain", mutation)
    monkeypatch.setattr(script, "_remove_regular", mutation)
    monkeypatch.setattr(script, "_remove_directory", mutation)

    with pytest.raises(ValueError, match="existing private domain"):
        script.main()
    assert not mutated


def test_baseline_removal_rejects_symlink(tmp_path: Path) -> None:
    script = _script()
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "baseline"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="unexpected fixture baseline"):
        script._remove_directory(link)
    assert link.is_symlink()
    assert target.is_dir()
