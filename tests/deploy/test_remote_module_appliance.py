"""Contract, confinement, image, and provisioning proofs for ADR-0585."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
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


def test_operation_document_is_regular_file_only_and_nofollow(tmp_path: Path) -> None:
    appliance = _module()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_operation()) + "\n", encoding="utf-8")
    operation = tmp_path / "operation-v1.json"
    operation.symlink_to(outside)

    with pytest.raises(appliance.ApplianceError, match="INVALID_DOCUMENT"):
        appliance._read_operation(operation)


def _framed(document: dict[str, object]) -> bytes:
    """The ADR-0585 canonical wire bytes, written without going through the appliance."""
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        + b"\n"
    )


def test_appliance_hashes_and_writes_one_byte_form(tmp_path: Path) -> None:
    """The digest input at `_tree_manifest` and the writer at `_write_json` are one encoding.

    Before #2176 the appliance hashed with `ensure_ascii=False` and wrote with the `json.dumps`
    default of `ensure_ascii=True`, so the bytes it hashed and the bytes it wrote disagreed about
    the same value.
    """
    appliance = _module()
    document = {"root_volume_key": "rüt-1", "b": "a", "a": "b"}
    path = tmp_path / "result-v1.json"
    tree = tmp_path / "modules"
    tree.mkdir()
    (tree / "mödule.ko").write_bytes(b"module")

    appliance._write_json(path, document)

    written = path.read_bytes()
    assert written == b'{"a":"b","b":"a","root_volume_key":"r\xc3\xbct-1"}\n'
    assert b"\\u" not in written
    assert written == appliance._canonical_bytes(document) + b"\n"

    manifest, _count, _size = appliance._tree_manifest(tree)
    entry = {
        "mode": "0644",
        "path": "mödule.ko",
        "sha256": "sha256:" + hashlib.sha256(b"module").hexdigest(),
        "size": 6,
        "type": "file",
    }
    encoded = _framed({"entries": [entry], "schema": "module-source-manifest-v1"})[:-1]
    assert "mödule.ko".encode() in encoded and b"\\u" not in encoded
    digest = hashlib.sha256(b"kdive-module-source-manifest-v1\0" + encoded).hexdigest()
    assert manifest == f"sha256:{digest}"


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("ascii escape", lambda data: data.replace(b'"root-1"', b'"\\u0072oot-1"')),
        (
            "duplicate member",
            lambda data: data.replace(b'"operation":', b'"operation":"restore","operation":', 1),
        ),
        ("unsorted members", lambda data: json.dumps(_operation()).encode() + b"\n"),
        ("indented", lambda data: json.dumps(_operation(), indent=2).encode() + b"\n"),
        ("second newline", lambda data: data + b"\n"),
        ("no newline", lambda data: data[:-1]),
        ("leading whitespace", lambda data: b" " + data),
    ],
)
def test_operation_reader_refuses_every_non_canonical_byte_form(
    tmp_path: Path, name: str, mutate: Any
) -> None:
    """Driven through `_read_operation`, the real reader, not around it through `json.loads`.

    A duplicate member is the classic parser differential: two readers may disagree about which
    value wins, so a document the appliance accepted could mean something else to the provider.
    Nothing in `json.loads` rejects it -- only comparing the re-serialized bytes does.
    """
    appliance = _module()
    path = tmp_path / "operation-v1.json"
    canonical = _framed(_operation())
    path.write_bytes(canonical)
    assert appliance._read_operation(path) == _operation()

    mutated = mutate(canonical)
    assert mutated != canonical
    path.write_bytes(mutated)
    with pytest.raises(appliance.ApplianceError, match="INVALID_DOCUMENT"):
        appliance._read_operation(path)


def test_checkpoint_reader_refuses_a_result_the_writer_would_not_have_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance, operation, _destination, scratch = _appliance_fixture(tmp_path, monkeypatch)
    document = appliance._validate_operation(operation)
    appliance.execute(document)
    checkpoint_path = scratch / "result-v1.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))

    assert appliance._existing_checkpoint(document) == checkpoint
    checkpoint_path.write_bytes(json.dumps(checkpoint).encode() + b"\n")
    with pytest.raises(appliance.ApplianceError, match="RECOVERY_CONFLICT"):
        appliance._existing_checkpoint(document)


@pytest.mark.parametrize(
    ("key", "accepted"),
    [("é" * 255, False), ("é" * 127 + "a", True), ("a" * 255, True), ("a" * 256, False)],
)
def test_volume_key_is_bounded_in_bytes_not_characters(key: str, accepted: bool) -> None:
    """255 characters of a two-byte character is 510 bytes, which can never name a volume."""
    appliance = _module()
    document = _operation()
    document["root_volume"] = {"key": key, "identity": "sha256:" + "c" * 64}
    schema_valid = not list(
        Draft202012Validator(_json("operation-v1.schema.json")).iter_errors(document)
    )

    assert schema_valid == (len(key) <= 255)
    if accepted:
        assert appliance._validate_operation(document) == document
    else:
        with pytest.raises(appliance.ApplianceError, match="INVALID_DOCUMENT"):
            appliance._validate_operation(document)


@pytest.mark.parametrize(
    ("name", "pointer"),
    [
        ("operation-v1.schema.json", ("root_volume", "key")),
        ("result-v1.schema.json", ("root_volume_key",)),
    ],
)
def test_schemas_state_the_length_unit_maxlength_cannot_express(
    name: str, pointer: tuple[str, ...]
) -> None:
    """`maxLength` counts characters, so the schema has to say which unit is normative."""
    node = cast(dict[str, Any], _json(name)["properties"])
    for step in pointer[:-1]:
        node = node[step]["properties"]
    field = node[pointer[-1]]

    assert field["maxLength"] == 255
    assert "255 UTF-8 BYTES" in field["description"]


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
        "release": _operation()["release"],
        "root_volume_key": "root-1",
        "root_volume_identity": "sha256:" + "c" * 64,
        "source_manifest": "sha256:" + "d" * 64,
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
    hook = runtime / "usr/lib/python3.14/sitecustomize.py"
    hook.write_bytes(b"raise RuntimeError\n")
    with pytest.raises(subprocess.CalledProcessError):
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
                str(tmp_path / "rejected.tar"),
            ],
            check=True,
        )


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
    assert source.startswith("#!/usr/bin/python3 -S\n")
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
        assert _kwargs["stdout"] is subprocess.DEVNULL
        assert _kwargs["stderr"] is subprocess.DEVNULL
        assert _kwargs["timeout"] == 300
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


def test_depmod_timeout_maps_to_the_stable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance, operation, _old_tree, _scratch = _appliance_fixture(tmp_path, monkeypatch)

    def timeout(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired("depmod", 300)

    monkeypatch.setattr(appliance.subprocess, "run", timeout)
    with pytest.raises(appliance.ApplianceError, match="DEPMOD_FAILURE"):
        appliance.execute(appliance._validate_operation(operation))


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


def test_install_retry_removes_empty_stage_left_after_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance, operation, old_tree, _scratch = _appliance_fixture(tmp_path, monkeypatch)
    original_remove = appliance._remove_empty_stage
    interrupted = False

    def interrupt_remove(path: Path) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise appliance.ApplianceError("FILESYSTEM_FAILURE")
        original_remove(path)

    monkeypatch.setattr(appliance, "_remove_empty_stage", interrupt_remove)
    with pytest.raises(appliance.ApplianceError):
        appliance.execute(appliance._validate_operation(operation))
    monkeypatch.setattr(appliance, "_remove_empty_stage", original_remove)

    result = appliance.execute(appliance._validate_operation(operation))

    assert result["phase"] == "installed"
    assert not any(path.name.endswith("-new") for path in old_tree.parent.iterdir())


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


def test_restore_retry_removes_empty_stage_left_after_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    original_remove = appliance._remove_empty_stage
    interrupted = False

    def interrupt_remove(path: Path) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise appliance.ApplianceError("FILESYSTEM_FAILURE")
        original_remove(path)

    monkeypatch.setattr(appliance, "_remove_empty_stage", interrupt_remove)
    with pytest.raises(appliance.ApplianceError):
        appliance.execute(restore)
    monkeypatch.setattr(appliance, "_remove_empty_stage", original_remove)

    result = appliance.execute(restore)

    assert result["phase"] == "restored"
    assert not any(path.name.endswith("-new") for path in old_tree.parent.iterdir())


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


def test_absent_capture_marker_does_not_follow_retained_scratch_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance, operation, destination, scratch = _appliance_fixture(tmp_path, monkeypatch)
    (destination / "old.ko").unlink()
    destination.rmdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"unchanged")
    (scratch / "capture-absent").symlink_to(outside)

    with pytest.raises(appliance.ApplianceError, match="RECOVERY_CONFLICT"):
        appliance.execute(appliance._validate_operation(operation))

    assert outside.read_bytes() == b"unchanged"


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
    # Written canonically so the rejection is the shape rule, not the byte form (#2176).
    checkpoint_path.write_bytes(appliance._canonical_bytes(checkpoint) + b"\n")

    with pytest.raises(appliance.ApplianceError, match="RECOVERY_CONFLICT"):
        appliance.execute(appliance._validate_operation(operation))


def test_identity_mismatch_preserves_the_prior_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance, operation, _destination, scratch = _appliance_fixture(tmp_path, monkeypatch)
    appliance.execute(appliance._validate_operation(operation))
    checkpoint_path = scratch / "result-v1.json"
    before = checkpoint_path.read_bytes()
    wrong_operation = {
        **operation,
        "run_id": "00000000-0000-4000-8000-000000000099",
    }

    appliance._write_failure(
        appliance._validate_operation(wrong_operation),
        appliance.ApplianceError("IDENTITY_MISMATCH"),
    )

    assert checkpoint_path.read_bytes() == before


def test_recovery_conflict_preserves_malformed_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance = _module()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(appliance, "SCRATCH", scratch)
    checkpoint = scratch / "result-v1.json"
    checkpoint.write_bytes(b"{malformed\n")
    before = checkpoint.read_bytes()

    appliance._write_failure(
        appliance._validate_operation(_operation()),
        appliance.ApplianceError("RECOVERY_CONFLICT"),
    )

    assert checkpoint.read_bytes() == before


def test_accepted_failure_restarts_before_the_first_durable_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance, operation, destination, scratch = _appliance_fixture(tmp_path, monkeypatch)
    partial_capture = scratch / "capture"
    partial_capture.mkdir()
    (partial_capture / "partial").write_bytes(b"incomplete")
    document = appliance._validate_operation(operation)
    appliance._write_failure(document, appliance.ApplianceError("FILESYSTEM_FAILURE"))

    result = appliance.execute(document)

    assert result["phase"] == "installed"
    assert (destination / "new.ko").read_bytes() == b"new"
    assert not (partial_capture / "partial").exists()


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"status": "success", "error_code": None},
        {"status": "failure", "error_code": "FILESYSTEM_FAILURE", "entry_count": 1},
    ],
)
def test_accepted_retry_rejects_impossible_checkpoint_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_fields: dict[str, object],
) -> None:
    appliance, operation, _destination, scratch = _appliance_fixture(tmp_path, monkeypatch)
    document = appliance._validate_operation(operation)
    checkpoint = {
        "protocol": "remote-module-result-v1",
        "status": "failure",
        "phase": "accepted",
        "error_code": "FILESYSTEM_FAILURE",
        **appliance._identity(document),
        **invalid_fields,
    }
    if checkpoint.get("error_code") is None:
        checkpoint.pop("error_code")
    (scratch / "result-v1.json").write_bytes(appliance._canonical_bytes(checkpoint) + b"\n")

    with pytest.raises(appliance.ApplianceError, match="RECOVERY_CONFLICT"):
        appliance._existing_checkpoint(document)


def test_checkpoint_write_reconciles_a_crash_temp(tmp_path: Path) -> None:
    appliance = _module()
    result = tmp_path / "result-v1.json"
    temporary = tmp_path / ".result-v1.json.tmp"
    temporary.write_bytes(b"interrupted")

    appliance._write_json(result, {"phase": "captured"})

    assert json.loads(result.read_text(encoding="utf-8")) == {"phase": "captured"}
    assert not temporary.exists()


@pytest.mark.parametrize("kind", ["symlink", "oversized"])
def test_checkpoint_read_is_nofollow_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    appliance = _module()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(appliance, "SCRATCH", scratch)
    checkpoint = scratch / "result-v1.json"
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("{}", encoding="utf-8")
        checkpoint.symlink_to(outside)
    else:
        checkpoint.write_bytes(b"x" * (appliance.DOCUMENT_LIMIT + 1))

    with pytest.raises(appliance.ApplianceError, match="RECOVERY_CONFLICT"):
        appliance._existing_checkpoint(appliance._validate_operation(_operation()))


def test_source_xattrs_are_discarded_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance, operation, destination, _scratch = _appliance_fixture(tmp_path, monkeypatch)
    source_module = tmp_path / "source" / "modules" / "new.ko"
    try:
        os.setxattr(source_module, "user.unverified", b"attacker")
    except OSError as error:
        pytest.skip(f"xattrs unavailable: {error}")
    operation["source_manifest"] = appliance._tree_manifest(source_module.parent)[0]

    appliance.execute(appliance._validate_operation(operation))

    assert "user.unverified" not in os.listxattr(destination / "new.ko", follow_symlinks=False)


def test_source_xattr_removal_failure_stops_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance = _module()
    tree = tmp_path / "modules"
    tree.mkdir()
    (tree / "module.ko").write_bytes(b"module")
    monkeypatch.setattr(appliance.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        appliance.os,
        "listxattr",
        lambda *_args, **_kwargs: ["security.selinux"],
    )

    def deny_remove(*_args: object, **_kwargs: object) -> None:
        raise PermissionError

    monkeypatch.setattr(appliance.os, "removexattr", deny_remove)

    with pytest.raises(appliance.ApplianceError, match="FILESYSTEM_FAILURE"):
        appliance._normalize_source_tree(tree)


def test_recovery_xattrs_share_a_bounded_manifest_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance = _module()
    tree = tmp_path / "modules"
    tree.mkdir()
    module = tree / "module.ko"
    module.write_bytes(b"module")
    try:
        os.setxattr(module, "user.large", b"metadata")
    except OSError as error:
        pytest.skip(f"xattrs unavailable: {error}")
    monkeypatch.setattr(appliance, "XATTR_BYTE_LIMIT", 1)

    with pytest.raises(appliance.ApplianceError, match="LIMIT_EXCEEDED"):
        appliance._tree_manifest(tree, "recovery")


def test_regular_file_limit_is_checked_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance = _module()
    tree = tmp_path / "modules"
    tree.mkdir()
    (tree / "oversized.ko").write_bytes(b"too large")
    monkeypatch.setattr(appliance, "BYTE_LIMIT", 1)
    monkeypatch.setattr(
        appliance,
        "_manifest_entry",
        lambda *_args: (_ for _ in ()).throw(AssertionError("hashed before limit")),
    )

    with pytest.raises(appliance.ApplianceError, match="LIMIT_EXCEEDED"):
        appliance._tree_manifest(tree)


def test_release_and_manifest_path_grammar_matches_v1(tmp_path: Path) -> None:
    appliance = _module()
    validator = Draft202012Validator(_json("operation-v1.schema.json"))
    valid = _operation()
    valid["release"] = "k" * 64
    assert not list(validator.iter_errors(valid))
    appliance._validate_operation(valid)
    invalid = {**valid, "release": "k" * 65}
    assert list(validator.iter_errors(invalid))
    with pytest.raises(appliance.ApplianceError, match="INVALID_DOCUMENT"):
        appliance._validate_operation(invalid)
    tree = tmp_path / "modules"
    tree.mkdir()
    (tree / "bad\nname.ko").write_bytes(b"module")
    manifest, count, size = appliance._tree_manifest(tree)
    assert manifest.startswith("sha256:")
    assert (count, size) == (1, 6)


def test_undecodable_xattr_name_is_a_stable_recovery_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance = _module()
    tree = tmp_path / "modules"
    tree.mkdir()
    module = tree / "module.ko"
    module.write_bytes(b"module")
    original_listxattr = appliance.os.listxattr

    def listxattr(path: object, **kwargs: object) -> list[str]:
        if str(path).endswith("module.ko"):
            return ["user.\udcff"]
        return original_listxattr(path, **kwargs)

    monkeypatch.setattr(appliance.os, "listxattr", listxattr)
    with pytest.raises(appliance.ApplianceError, match="RECOVERY_CONFLICT"):
        appliance._tree_manifest(tree, "recovery")


def test_restored_checkpoint_rejects_unclassified_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    appliance.execute(restore)
    staged = old_tree.parent / f".kdive-{operation['operation_nonce']}-new"
    staged.mkdir()
    (staged / "junk").write_bytes(b"unclassified")

    with pytest.raises(appliance.ApplianceError, match="RECOVERY_CONFLICT"):
        appliance.execute(restore)


def test_shutdown_failure_replaces_terminal_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    appliance = _module()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(appliance, "SCRATCH", scratch)
    mounted: set[Path] = set()
    monkeypatch.setattr(Path, "is_mount", lambda path: path in mounted)
    monkeypatch.setattr(appliance, "_reboot", lambda: -1)
    monkeypatch.setattr(appliance.os, "sync", lambda: None)
    monkeypatch.setattr(appliance, "_mount", lambda *_args: mounted.add(scratch))
    monkeypatch.setattr(appliance, "_unmount", lambda path: mounted.remove(path))
    document = appliance._validate_operation(_operation())

    with pytest.raises(appliance.ApplianceError, match="SHUTDOWN_FAILURE"):
        appliance._poweroff(document)

    result = json.loads((scratch / "result-v1.json").read_text(encoding="utf-8"))
    assert result["status"] == "failure"
    assert result["error_code"] == "SHUTDOWN_FAILURE"


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


@pytest.mark.parametrize(
    "field",
    ["release", "root_volume_key", "root_volume_identity", "source_manifest"],
)
def test_accepted_failure_requires_complete_operation_identity(field: str) -> None:
    validator = Draft202012Validator(_json("result-v1.schema.json"))
    operation = _operation()
    root_volume = cast(dict[str, object], operation["root_volume"])
    failure = {
        "protocol": "remote-module-result-v1",
        "status": "failure",
        "phase": "accepted",
        "error_code": "FILESYSTEM_FAILURE",
        "system_id": operation["system_id"],
        "run_id": operation["run_id"],
        "plan_identity": operation["plan_identity"],
        "operation_nonce": operation["operation_nonce"],
        "release": operation["release"],
        "root_volume_key": root_volume["key"],
        "root_volume_identity": root_volume["identity"],
        "source_manifest": operation["source_manifest"],
        "appliance_image_digest": operation["appliance_image_digest"],
    }
    failure.pop(field)
    assert list(validator.iter_errors(failure))


def test_preacceptance_shutdown_failure_is_closed_and_identity_free() -> None:
    validator = Draft202012Validator(_json("result-v1.schema.json"))
    failure = {
        "protocol": "remote-module-result-v1",
        "status": "failure",
        "phase": "accepted",
        "error_code": "SHUTDOWN_FAILURE",
    }
    assert not list(validator.iter_errors(failure))
    failure["system_id"] = _operation()["system_id"]
    assert list(validator.iter_errors(failure))


@pytest.mark.parametrize("name", ["operation-v1.schema.json", "result-v1.schema.json"])
def test_schema_documents_are_self_valid(name: str) -> None:
    Draft202012Validator.check_schema(_json(name))
