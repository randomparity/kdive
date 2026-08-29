"""Contract, confinement, image, and provisioning proofs for ADR-0585."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
APPLIANCE = ROOT / "deploy" / "remote_module_appliance"
ROLE = ROOT / "deploy" / "ansible" / "roles" / "remote_libvirt_module_appliance"


def _json(name: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads((APPLIANCE / name).read_text(encoding="utf-8")))


def _operation(operation: str = "capture_install") -> dict[str, object]:
    return {
        "protocol": "remote-module-operation-v1",
        "operation": operation,
        "system_id": "00000000-0000-4000-8000-000000000001",
        "run_id": "00000000-0000-4000-8000-000000000002",
        "plan_identity": "sha256:" + "a" * 64,
        "operation_nonce": "b" * 32,
        "release": "6.12.0-kdive",
        "root_volume": {"key": "root-1", "identity": "sha256:" + "c" * 64},
        "source_manifest": "sha256:" + "d" * 64,
        "appliance_image_digest": "sha256:" + "e" * 64,
    }


def _module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "remote_module_appliance", APPLIANCE / "appliance.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _newc_names(data: bytes) -> list[str]:
    names: list[str] = []
    offset = 0
    while True:
        header = data[offset : offset + 110]
        assert header[:6] == b"070701"
        file_size = int(header[54:62], 16)
        name_size = int(header[94:102], 16)
        offset += 110
        name = data[offset : offset + name_size - 1].decode("utf-8")
        offset = (offset + name_size + 3) & ~3
        if name == "TRAILER!!!":
            return names
        names.append(name)
        offset = (offset + file_size + 3) & ~3


def test_protocol_is_closed_and_rejects_caller_paths_or_commands() -> None:
    schema = _json("operation-v1.schema.json")
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(_operation()))
    for forbidden in ("path", "device", "mount_options", "command", "environment"):
        document = _operation()
        document[forbidden] = "/attacker-controlled"
        assert list(validator.iter_errors(document)), forbidden
    document = _operation("shell")
    assert list(validator.iter_errors(document))


def test_result_schema_has_stable_closed_errors_and_failure_coupling() -> None:
    schema = _json("result-v1.schema.json")
    definitions = cast(dict[str, object], schema["$defs"])
    error_definition = cast(dict[str, object], definitions["error_code"])
    error_codes = error_definition["enum"]
    assert error_codes == [
        "INVALID_DOCUMENT",
        "IDENTITY_MISMATCH",
        "LIMIT_EXCEEDED",
        "SOURCE_INVALID",
        "ROOT_DISCOVERY_FAILED",
        "FILESYSTEM_FAILURE",
        "RECOVERY_CONFLICT",
        "DEPMOD_FAILURE",
        "FLUSH_FAILURE",
        "SHUTDOWN_FAILURE",
    ]
    validator = Draft202012Validator(schema)
    result = {
        "protocol": "remote-module-result-v1",
        "status": "failure",
        "phase": "captured",
        "error_code": "LIMIT_EXCEEDED",
        "system_id": _operation()["system_id"],
        "run_id": _operation()["run_id"],
        "plan_identity": "sha256:" + "a" * 64,
        "operation_nonce": "b" * 32,
        "appliance_image_digest": "sha256:" + "e" * 64,
    }
    assert not list(validator.iter_errors(result))
    result.pop("error_code")
    assert list(validator.iter_errors(result))
    invalid_document = {
        "protocol": "remote-module-result-v1",
        "status": "failure",
        "phase": "accepted",
        "error_code": "INVALID_DOCUMENT",
    }
    assert not list(validator.iter_errors(invalid_document))


@pytest.mark.parametrize("architecture", ["x86_64", "ppc64le"])
def test_image_build_is_reproducible_and_excludes_shell_and_network(
    tmp_path: Path, architecture: str
) -> None:
    kernel = tmp_path / "vmlinuz"
    kernel.write_bytes(b"kernel\n")
    runtime = tmp_path / "runtime"
    for relative, content in {
        "usr/bin/python3": b"python",
        "sbin/depmod": b"depmod",
        "lib/ld-musl-test.so.1": b"loader",
        "usr/lib/python3.14/json/__init__.py": b"json",
        "usr/lib/python3.14/socket.py": b"network",
        "usr/lib/python3.14/lib-dynload/_socket.so": b"network-extension",
        "usr/lib/python3.14/lib-dynload/_socket.cpython-314-x86_64-linux-musl.so": b"network-abi",
        "bin/sh": b"shell",
    }.items():
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    outputs = [tmp_path / "one.tar", tmp_path / "two.tar"]
    for output in outputs:
        subprocess.run(
            [
                sys.executable,
                str(APPLIANCE / "build_image.py"),
                "--architecture",
                architecture,
                "--kernel",
                str(kernel),
                "--runtime-root",
                str(runtime),
                "--output",
                str(output),
            ],
            check=True,
        )
    assert (
        hashlib.sha256(outputs[0].read_bytes()).digest()
        == hashlib.sha256(outputs[1].read_bytes()).digest()
    )
    with tarfile.open(outputs[0]) as archive:
        names = set(archive.getnames())
        initramfs = archive.extractfile("image/initramfs.cpio")
        assert initramfs is not None
        initramfs_names = set(_newc_names(initramfs.read()))
    assert names == {"image/initramfs.cpio", "image/vmlinuz", "manifest.json"}
    assert "bin/sh" not in initramfs_names
    assert not any("socket" in name.lower() for name in initramfs_names)
    assert {"init", "usr/bin/python3", "sbin/depmod"} <= initramfs_names


@pytest.mark.parametrize(
    ("architecture", "machine"),
    [("x86_64", "q35"), ("ppc64le", "pseries")],
)
def test_domain_definition_has_only_three_disks_and_bounded_console(
    architecture: str, machine: str
) -> None:
    root = ET.parse(APPLIANCE / f"domain-v1-{architecture}.xml").getroot()
    os_type = root.find("os/type")
    assert os_type is not None
    assert (os_type.get("arch"), os_type.get("machine")) == (architecture, machine)
    devices = root.find("devices")
    assert devices is not None
    assert [disk.get("device") for disk in devices.findall("disk")] == ["disk", "disk", "disk"]
    targets = []
    for disk in devices.findall("disk"):
        target = disk.find("target")
        assert target is not None
        targets.append(target.get("dev"))
    assert targets == ["vda", "vdb", "vdc"]
    assert devices.find("interface") is None
    assert devices.find("filesystem") is None
    assert devices.find("hostdev") is None
    assert devices.find("graphics") is None
    console = devices.find("console")
    assert console is not None and console.get("type") == "pty"
    on_crash = root.find("on_crash")
    assert on_crash is not None and on_crash.text == "destroy"


def test_ansible_requires_both_architectures_and_verifies_digest() -> None:
    defaults = cast(
        dict[str, object],
        yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8")),
    )
    images = cast(dict[str, object], defaults["remote_libvirt_module_appliance_images"])
    assert list(images) == ["x86_64", "ppc64le"]
    tasks = (ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
    assert "ansible.builtin.get_url:" in tasks
    assert "ansible.builtin.unarchive:" in tasks
    assert 'checksum: "sha256:' in tasks
    assert "checksum_algorithm: sha256" in tasks
    assert "remote_libvirt_module_appliance_install_dir" in tasks
    assert 'mode: "0444"' in tasks
    site = (ROOT / "deploy" / "ansible" / "site.yml").read_text(encoding="utf-8")
    assert "role: remote_libvirt_module_appliance" in site
    assert "remote_libvirt_module_appliance_enabled | bool" in site


def test_image_config_closes_architectures_bounds_and_paths() -> None:
    config = _json("image-v1.json")
    assert config["architectures"] == ["x86_64", "ppc64le"]
    assert config["source_date_epoch"] == 0
    assert config["entry_limit"] == 200_000
    assert config["content_byte_limit"] == 8 * 1024**3
    assert config["scratch_capacity_bytes"] == 10 * 1024**3
    assert config["operation_path"] == "/mnt/source/operation-v1.json"
    assert config["result_path"] == "/mnt/scratch/result-v1.json"


def test_appliance_source_has_fixed_depmod_and_no_guest_exec_or_network() -> None:
    source = (APPLIANCE / "appliance.py").read_text(encoding="utf-8")
    assert 'DEPMOD = "/sbin/depmod"' in source
    assert "shell=True" not in source
    assert "socket" not in source
    assert "os.exec" not in source
    assert "subprocess.run(" in source and '[DEPMOD, "-b"' in source
    assert "200_000" in source
    assert "8 * 1024**3" in source


def test_capture_install_and_restore_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance = _module()
    root = tmp_path / "root"
    source = tmp_path / "source"
    scratch = tmp_path / "scratch"
    release = str(_operation()["release"])
    old_tree = root / "lib" / "modules" / release
    new_tree = source / "modules"
    old_tree.mkdir(parents=True)
    new_tree.mkdir(parents=True)
    (old_tree / "old.ko").write_bytes(b"old")
    (new_tree / "new.ko").write_bytes(b"new")
    scratch.mkdir()
    monkeypatch.setattr(appliance, "ROOT", root)
    monkeypatch.setattr(appliance, "SOURCE", source)
    monkeypatch.setattr(appliance, "SCRATCH", scratch)
    monkeypatch.setattr(
        appliance.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    operation = _operation()
    operation["source_manifest"] = appliance._tree_manifest(new_tree)[0]

    installed = appliance.execute(appliance._validate_operation(operation))

    assert installed["phase"] == "installed"
    assert (old_tree / "new.ko").read_bytes() == b"new"
    assert (scratch / "capture" / "old.ko").read_bytes() == b"old"
    restore = {
        **operation,
        "operation": "restore",
        "capture_manifest": installed["capture_manifest"],
        "installed_manifest": installed["installed_manifest"],
    }

    restored = appliance.execute(appliance._validate_operation(restore))

    assert restored["phase"] == "restored"
    assert (old_tree / "old.ko").read_bytes() == b"old"
    assert not (old_tree / "new.ko").exists()


def test_capture_retry_resumes_after_durable_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance = _module()
    root = tmp_path / "root"
    source = tmp_path / "source"
    scratch = tmp_path / "scratch"
    release = str(_operation()["release"])
    old_tree = root / "lib" / "modules" / release
    new_tree = source / "modules"
    old_tree.mkdir(parents=True)
    new_tree.mkdir(parents=True)
    (old_tree / "old.ko").write_bytes(b"old")
    (new_tree / "new.ko").write_bytes(b"new")
    scratch.mkdir()
    monkeypatch.setattr(appliance, "ROOT", root)
    monkeypatch.setattr(appliance, "SOURCE", source)
    monkeypatch.setattr(appliance, "SCRATCH", scratch)
    monkeypatch.setattr(
        appliance.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    operation = _operation()
    operation["source_manifest"] = appliance._tree_manifest(new_tree)[0]
    original_copy = appliance._copy_tree
    copies = 0

    def interrupt_second_copy(
        source_path: Path, destination_path: Path, kind: str = "source"
    ) -> object:
        nonlocal copies
        copies += 1
        if copies == 2:
            raise appliance.ApplianceError("FILESYSTEM_FAILURE")
        return original_copy(source_path, destination_path, kind)

    monkeypatch.setattr(appliance, "_copy_tree", interrupt_second_copy)
    with pytest.raises(appliance.ApplianceError):
        appliance.execute(appliance._validate_operation(operation))
    monkeypatch.setattr(appliance, "_copy_tree", original_copy)

    result = appliance.execute(appliance._validate_operation(operation))

    assert result["phase"] == "installed"
    assert (old_tree / "new.ko").read_bytes() == b"new"


def _appliance_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, dict[str, object], Path, Path]:
    appliance = _module()
    root = tmp_path / "root"
    source = tmp_path / "source"
    scratch = tmp_path / "scratch"
    release = str(_operation()["release"])
    old_tree = root / "lib" / "modules" / release
    new_tree = source / "modules"
    old_tree.mkdir(parents=True)
    new_tree.mkdir(parents=True)
    (old_tree / "old.ko").write_bytes(b"old")
    (new_tree / "new.ko").write_bytes(b"new")
    scratch.mkdir()
    monkeypatch.setattr(appliance, "ROOT", root)
    monkeypatch.setattr(appliance, "SOURCE", source)
    monkeypatch.setattr(appliance, "SCRATCH", scratch)

    def depmod(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        base = Path(arguments[2])
        staged_tree = base / "lib" / "modules" / release
        assert (staged_tree / "new.ko").read_bytes() == b"new"
        (staged_tree / "modules.dep").write_bytes(b"new.ko:\n")
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(appliance.subprocess, "run", depmod)
    operation = _operation()
    operation["source_manifest"] = appliance._tree_manifest(new_tree)[0]
    return appliance, operation, old_tree, scratch


def test_depmod_indexes_the_staged_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance, operation, old_tree, _scratch = _appliance_fixture(tmp_path, monkeypatch)

    result = appliance.execute(appliance._validate_operation(operation))

    assert result["phase"] == "installed"
    assert (old_tree / "modules.dep").read_bytes() == b"new.ko:\n"


@pytest.mark.parametrize("interrupted_phase", ["replacement-ready", "installed"])
def test_install_retry_reconciles_every_durable_terminal_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted_phase: str
) -> None:
    appliance, operation, old_tree, _scratch = _appliance_fixture(tmp_path, monkeypatch)
    original_checkpoint = appliance._checkpoint
    interrupted = False

    def interrupt_after_checkpoint(
        document: dict[str, object], phase: str, **fields: object
    ) -> dict[str, object]:
        nonlocal interrupted
        result = original_checkpoint(document, phase, **fields)
        if phase == interrupted_phase and not interrupted:
            interrupted = True
            raise appliance.ApplianceError("FILESYSTEM_FAILURE")
        return result

    monkeypatch.setattr(appliance, "_checkpoint", interrupt_after_checkpoint)
    with pytest.raises(appliance.ApplianceError):
        appliance.execute(appliance._validate_operation(operation))
    monkeypatch.setattr(appliance, "_checkpoint", original_checkpoint)

    result = appliance.execute(appliance._validate_operation(operation))

    assert result["phase"] == "installed"
    assert (old_tree / "new.ko").read_bytes() == b"new"
    assert not any(path.name.endswith("-old") for path in old_tree.parent.iterdir())


@pytest.mark.parametrize("interrupted_phase", ["restore-ready", "restored"])
def test_restore_retry_reconciles_every_durable_terminal_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted_phase: str
) -> None:
    appliance, operation, old_tree, _scratch = _appliance_fixture(tmp_path, monkeypatch)
    installed = appliance.execute(appliance._validate_operation(operation))
    restore = appliance._validate_operation(
        {
            **operation,
            "operation": "restore",
            "capture_manifest": installed["capture_manifest"],
            "installed_manifest": installed["installed_manifest"],
        }
    )
    original_checkpoint = appliance._checkpoint
    interrupted = False

    def interrupt_after_ready(
        document: dict[str, object], phase: str, **fields: object
    ) -> dict[str, object]:
        nonlocal interrupted
        result = original_checkpoint(document, phase, **fields)
        if phase == interrupted_phase and not interrupted:
            interrupted = True
            raise appliance.ApplianceError("FILESYSTEM_FAILURE")
        return result

    monkeypatch.setattr(appliance, "_checkpoint", interrupt_after_ready)
    with pytest.raises(appliance.ApplianceError):
        appliance.execute(restore)
    monkeypatch.setattr(appliance, "_checkpoint", original_checkpoint)

    result = appliance.execute(restore)

    assert result["phase"] == "restored"
    assert (old_tree / "old.ko").read_bytes() == b"old"


def test_absent_capture_install_and_restore_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance, operation, destination, _scratch = _appliance_fixture(tmp_path, monkeypatch)
    (destination / "old.ko").unlink()
    destination.rmdir()

    installed = appliance.execute(appliance._validate_operation(operation))

    assert installed["capture_absent"] is True
    assert (destination / "new.ko").read_bytes() == b"new"
    restore = appliance._validate_operation(
        {
            **operation,
            "operation": "restore",
            "capture_absent": True,
            "installed_manifest": installed["installed_manifest"],
        }
    )
    restored = appliance.execute(restore)
    assert restored["phase"] == "restored"
    assert not destination.exists()


def test_guest_symlink_cannot_redirect_module_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance = _module()
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    source = tmp_path / "source"
    scratch = tmp_path / "scratch"
    (root / "lib").mkdir(parents=True)
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"unchanged")
    (root / "lib" / "modules").symlink_to(outside, target_is_directory=True)
    (source / "modules").mkdir(parents=True)
    (source / "modules" / "new.ko").write_bytes(b"new")
    scratch.mkdir()
    monkeypatch.setattr(appliance, "ROOT", root)
    monkeypatch.setattr(appliance, "SOURCE", source)
    monkeypatch.setattr(appliance, "SCRATCH", scratch)
    operation = _operation()
    operation["source_manifest"] = appliance._tree_manifest(source / "modules")[0]

    with pytest.raises(appliance.ApplianceError, match="RECOVERY_CONFLICT"):
        appliance.execute(appliance._validate_operation(operation))

    assert (outside / "sentinel").read_bytes() == b"unchanged"
    assert not (outside / str(operation["release"])).exists()


def test_unmount_failure_replaces_success_with_flush_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance = _module()
    root = tmp_path / "root"
    source = tmp_path / "source"
    scratch = tmp_path / "scratch"
    for path in (root, source, scratch):
        path.mkdir()
    monkeypatch.setattr(appliance, "ROOT", root)
    monkeypatch.setattr(appliance, "SOURCE", source)
    monkeypatch.setattr(appliance, "SCRATCH", scratch)
    mounted = {root, source, scratch}
    monkeypatch.setattr(Path, "is_mount", lambda path: path in mounted)
    monkeypatch.setattr(appliance, "_sync_path", lambda _path: None)

    def unmount(path: Path) -> None:
        if path == root:
            raise OSError("busy")
        mounted.remove(path)

    monkeypatch.setattr(appliance, "_unmount", unmount)
    document = appliance._validate_operation(_operation())

    result = appliance._finish_mounts(document, None)

    assert result is not None and result.code == "FLUSH_FAILURE"
    persisted = json.loads((scratch / "result-v1.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "failure"
    assert persisted["error_code"] == "FLUSH_FAILURE"


def test_retry_rejects_a_malformed_durable_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance, operation, _destination, scratch = _appliance_fixture(tmp_path, monkeypatch)
    appliance.execute(appliance._validate_operation(operation))
    checkpoint_path = scratch / "result-v1.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint.pop("entry_count")
    checkpoint_path.write_text(json.dumps(checkpoint) + "\n", encoding="utf-8")

    with pytest.raises(appliance.ApplianceError, match="RECOVERY_CONFLICT"):
        appliance.execute(appliance._validate_operation(operation))


def test_success_result_shapes_require_terminal_phase_fields() -> None:
    validator = Draft202012Validator(_json("result-v1.schema.json"))
    operation = _operation()
    root_volume = cast(dict[str, object], operation["root_volume"])
    identity = {
        "protocol": "remote-module-result-v1",
        "status": "success",
        "phase": "installed",
        "system_id": operation["system_id"],
        "run_id": operation["run_id"],
        "plan_identity": operation["plan_identity"],
        "operation_nonce": operation["operation_nonce"],
        "release": operation["release"],
        "root_volume_key": "root-1",
        "root_volume_identity": root_volume["identity"],
        "source_manifest": operation["source_manifest"],
        "appliance_image_digest": operation["appliance_image_digest"],
    }
    assert list(validator.iter_errors(identity))
    identity["installed_manifest"] = "sha256:" + "f" * 64
    identity["capture_absent"] = True
    identity["entry_count"] = 1
    identity["content_bytes"] = 3
    assert not list(validator.iter_errors(identity))
    accepted = {**identity, "phase": "accepted"}
    assert list(validator.iter_errors(accepted))


@pytest.mark.parametrize("name", ["operation-v1.schema.json", "result-v1.schema.json"])
def test_schema_documents_are_self_valid(name: str) -> None:
    Draft202012Validator.check_schema(_json(name))
