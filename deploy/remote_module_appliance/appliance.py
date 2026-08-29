#!/usr/bin/python3
"""Fixed-operation init for the ADR-0585 remote module appliance."""

from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import unicodedata
from pathlib import Path
from re import Pattern
from typing import NoReturn, cast

DEPMOD = "/sbin/depmod"
ENTRY_LIMIT = 200_000
BYTE_LIMIT = 8 * 1024**3
DOCUMENT_LIMIT = 16 * 1024
ROOT = Path("/mnt/root")
SOURCE = Path("/mnt/source")
SCRATCH = Path("/mnt/scratch")
RELEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ERROR_CODES = {
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
}
LINUX_REBOOT_CMD_POWER_OFF = 0x4321FEDC


class ApplianceError(Exception):
    def __init__(self, code: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError("unknown appliance error code")
        super().__init__(code)
        self.code = code


def _matches(pattern: Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _valid_manifest_text(value: str) -> bool:
    return (
        unicodedata.normalize("NFC", value) == value
        and all(not 0xD800 <= ord(character) <= 0xDFFF for character in value)
        and all(unicodedata.category(character) != "Cc" for character in value)
    )


def _read_operation(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            data = stream.read(DOCUMENT_LIMIT + 1)
    except OSError as error:
        raise ApplianceError("INVALID_DOCUMENT") from error
    if len(data) > DOCUMENT_LIMIT or b"\x00" in data or not data.endswith(b"\n"):
        raise ApplianceError("INVALID_DOCUMENT")
    try:
        parsed = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ApplianceError("INVALID_DOCUMENT") from error
    if not isinstance(parsed, dict):
        raise ApplianceError("INVALID_DOCUMENT")
    return _validate_operation(parsed)


def _validate_operation(document: dict[str, object]) -> dict[str, object]:
    common = {
        "protocol",
        "operation",
        "system_id",
        "run_id",
        "plan_identity",
        "operation_nonce",
        "release",
        "root_volume",
        "source_manifest",
        "appliance_image_digest",
    }
    restore = {"capture_manifest", "installed_manifest", "capture_absent"}
    operation = document.get("operation")
    allowed = common | restore if operation == "restore" else common
    document_keys = set(document)
    restore_shapes = (allowed - {"capture_absent"}, allowed - {"capture_manifest"})
    if document_keys != allowed and not (
        operation == "restore" and document_keys in restore_shapes
    ):
        raise ApplianceError("INVALID_DOCUMENT")
    values = (
        document.get("protocol") == "remote-module-operation-v1",
        operation in {"capture_install", "restore"},
        _matches(UUID_RE, document.get("system_id")),
        _matches(UUID_RE, document.get("run_id")),
        _matches(DIGEST_RE, document.get("plan_identity")),
        _matches(HEX32_RE, document.get("operation_nonce")),
        _matches(RELEASE_RE, document.get("release"))
        and document.get("release") not in {".", ".."},
        _matches(DIGEST_RE, document.get("source_manifest")),
        _matches(DIGEST_RE, document.get("appliance_image_digest")),
    )
    root_volume = document.get("root_volume")
    root_mapping = cast(dict[str, object], root_volume) if isinstance(root_volume, dict) else {}
    root_key = root_mapping.get("key")
    root_valid = (
        isinstance(root_volume, dict)
        and set(root_volume) == {"key", "identity"}
        and isinstance(root_key, str)
        and 0 < len(root_key) <= 255
        and _matches(DIGEST_RE, root_mapping.get("identity"))
    )
    if not all(values) or not root_valid:
        raise ApplianceError("INVALID_DOCUMENT")
    if operation == "restore":
        capture_manifest = document.get("capture_manifest")
        absent = document.get("capture_absent") is True
        installed = document.get("installed_manifest")
        if bool(capture_manifest) == absent or not isinstance(installed, str):
            raise ApplianceError("INVALID_DOCUMENT")
        if capture_manifest is not None and (
            not isinstance(capture_manifest, str) or not DIGEST_RE.fullmatch(capture_manifest)
        ):
            raise ApplianceError("INVALID_DOCUMENT")
        if not DIGEST_RE.fullmatch(installed):
            raise ApplianceError("INVALID_DOCUMENT")
    return document


def _installed_metadata(path: str, metadata: os.stat_result) -> dict[str, object]:
    attributes: dict[str, str] = {}
    supported = True
    try:
        for name in sorted(os.listxattr(path, follow_symlinks=False)):
            if unicodedata.normalize("NFC", name) != name:
                raise ApplianceError("SOURCE_INVALID")
            value = os.getxattr(path, name, follow_symlinks=False)
            attributes[name] = base64.b64encode(value).decode().rstrip("=")
    except OSError as error:
        if error.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise ApplianceError("FILESYSTEM_FAILURE") from error
        supported = False
    return {
        "gid": metadata.st_gid,
        "uid": metadata.st_uid,
        "xattrs": attributes,
        "xattrs_supported": supported,
    }


def _manifest_entry(
    root: Path, entry: os.DirEntry[str], kind: str
) -> tuple[dict[str, object] | None, int]:
    relative = Path(entry.path).relative_to(root).as_posix()
    if not _valid_manifest_text(relative) or any(
        segment in {"", ".", ".."} for segment in relative.split("/")
    ):
        raise ApplianceError("SOURCE_INVALID")
    metadata = entry.stat(follow_symlinks=False)
    mode = metadata.st_mode & 0o777
    installed = kind != "source"
    extra = _installed_metadata(entry.path, metadata) if installed else {}
    if stat.S_ISDIR(metadata.st_mode):
        return {
            "mode": f"{mode if installed else 0o755:04o}",
            "path": relative,
            "type": "dir",
            **extra,
        }, 0
    if stat.S_ISREG(metadata.st_mode):
        digest = hashlib.sha256()
        try:
            descriptor = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as error:
            raise ApplianceError("FILESYSTEM_FAILURE") from error
        normalized_mode = 0o755 if mode & 0o111 else 0o644
        return {
            "mode": f"{mode if installed else normalized_mode:04o}",
            "path": relative,
            "sha256": f"sha256:{digest.hexdigest()}",
            "size": metadata.st_size,
            "type": "file",
            **extra,
        }, metadata.st_size
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(entry.path)
        if not _valid_manifest_text(target):
            raise ApplianceError("SOURCE_INVALID")
        if kind == "source":
            if relative in {"build", "source"} and target.startswith("/"):
                return None, 0
            target_parts = target.split("/")
            if any(part in {"", "."} for part in target_parts):
                raise ApplianceError("SOURCE_INVALID")
            resolved = os.path.normpath(os.path.join(os.path.dirname(relative), target))
            resolved_parts = resolved.split("/")
            if (
                target.startswith("/")
                or resolved == ".."
                or resolved.startswith("../")
                or any(part in {"", ".", ".."} for part in resolved_parts)
            ):
                raise ApplianceError("SOURCE_INVALID")
        return {"mode": "0777", "path": relative, "target": target, "type": "symlink", **extra}, 0
    raise ApplianceError("SOURCE_INVALID")


def _tree_manifest(root: Path, kind: str = "source") -> tuple[str, int, int]:
    if kind not in {"source", "installed", "recovery"}:
        raise ValueError("unknown module manifest kind")
    entries_document: list[dict[str, object]] = []
    entries = 0
    content_bytes = 0
    regular_inodes: set[tuple[int, int]] = set()
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise ApplianceError("FILESYSTEM_FAILURE") from error
        for entry in children:
            entries += 1
            if entries > ENTRY_LIMIT:
                raise ApplianceError("LIMIT_EXCEEDED")
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                stack.append(Path(entry.path))
            if stat.S_ISREG(metadata.st_mode):
                inode = (metadata.st_dev, metadata.st_ino)
                if inode in regular_inodes:
                    raise ApplianceError("SOURCE_INVALID")
                regular_inodes.add(inode)
            document, size = _manifest_entry(root, entry, kind)
            content_bytes += size
            if content_bytes > BYTE_LIMIT:
                raise ApplianceError("LIMIT_EXCEEDED")
            if document is not None:
                entries_document.append(document)
    entries_document.sort(key=lambda item: str(item["path"]).encode())
    schema = {
        "source": "module-source-manifest-v1",
        "installed": "module-installed-tree-v1",
        "recovery": "recovery-module-tree-v1",
    }[kind]
    prefix = f"kdive-{schema}".encode()
    encoded = json.dumps(
        {"entries": entries_document, "schema": schema},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return f"sha256:{hashlib.sha256(prefix + b'\0' + encoded).hexdigest()}", entries, content_bytes


def _remove_owned_tree(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ApplianceError("FILESYSTEM_FAILURE") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ApplianceError("RECOVERY_CONFLICT")
    try:
        shutil.rmtree(path)
    except OSError as error:
        raise ApplianceError("FILESYSTEM_FAILURE") from error


def _normalize_source_tree(root: Path) -> None:
    paths = [root]
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        paths.extend(Path(directory) / name for name in subdirectories)
        paths.extend(Path(directory) / name for name in files)
        for name in subdirectories:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o755)
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            mode = path.stat(follow_symlinks=False).st_mode
            path.chmod(0o755 if mode & 0o111 else 0o644)
        Path(directory).chmod(0o755)
    for path in paths:
        try:
            names = os.listxattr(path, follow_symlinks=False)
        except OSError as error:
            if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                names = []
            else:
                raise ApplianceError("FILESYSTEM_FAILURE") from error
        for name in names:
            try:
                os.removexattr(path, name, follow_symlinks=False)
            except PermissionError as error:
                if os.geteuid() == 0 and name != "security.selinux":
                    raise ApplianceError("FILESYSTEM_FAILURE") from error
            except OSError as error:
                if error.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                    raise ApplianceError("FILESYSTEM_FAILURE") from error
        try:
            os.chown(path, 0, 0, follow_symlinks=False)
        except PermissionError as error:
            if os.geteuid() == 0:
                raise ApplianceError("FILESYSTEM_FAILURE") from error
        except OSError as error:
            if error.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise ApplianceError("FILESYSTEM_FAILURE") from error


def _copy_tree(source: Path, destination: Path, kind: str = "source") -> tuple[str, int, int]:
    expected = _tree_manifest(source, kind)
    if destination.exists() or destination.is_symlink():
        raise ApplianceError("RECOVERY_CONFLICT")
    try:
        shutil.copytree(source, destination, symlinks=True)
    except OSError as error:
        raise ApplianceError("FILESYSTEM_FAILURE") from error
    if kind == "source":
        _normalize_source_tree(destination)
    if _tree_manifest(destination, kind) != expected:
        raise ApplianceError("FILESYSTEM_FAILURE")
    _sync_tree(destination)
    return expected


def _sync_tree(root: Path) -> None:
    try:
        directories: list[Path] = []
        for directory, subdirectories, files in os.walk(root, followlinks=False):
            current = Path(directory)
            directories.append(current)
            for name in files:
                path = current / name
                if path.is_symlink():
                    continue
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            subdirectories[:] = [
                name for name in subdirectories if not (current / name).is_symlink()
            ]
        for directory in reversed(directories):
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _sync_path(root)
    except OSError as error:
        raise ApplianceError("FLUSH_FAILURE") from error


def _sync_path(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
            if hasattr(os, "syncfs"):
                os.syncfs(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ApplianceError("FLUSH_FAILURE") from error


def _write_json(path: Path, document: dict[str, object]) -> None:
    data = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        try:
            metadata = temporary.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(metadata.st_mode):
                raise ApplianceError("RECOVERY_CONFLICT")
            temporary.unlink()
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_path(path.parent)
    except OSError as error:
        raise ApplianceError("FLUSH_FAILURE") from error


def _identity(document: dict[str, object]) -> dict[str, object]:
    root_volume = document["root_volume"]
    assert isinstance(root_volume, dict)
    root_mapping = cast(dict[str, object], root_volume)
    return {
        "system_id": document["system_id"],
        "run_id": document["run_id"],
        "plan_identity": document["plan_identity"],
        "operation_nonce": document["operation_nonce"],
        "release": document["release"],
        "root_volume_key": root_mapping["key"],
        "root_volume_identity": root_mapping["identity"],
        "source_manifest": document["source_manifest"],
        "appliance_image_digest": document["appliance_image_digest"],
    }


def _checkpoint(document: dict[str, object], phase: str, **fields: object) -> dict[str, object]:
    result = {
        "protocol": "remote-module-result-v1",
        "status": "success",
        "phase": phase,
        **_identity(document),
        **fields,
    }
    _write_json(SCRATCH / "result-v1.json", result)
    return result


def _existing_checkpoint(document: dict[str, object]) -> dict[str, object] | None:
    path = SCRATCH / "result-v1.json"
    if not path.exists():
        return None
    try:
        checkpoint = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ApplianceError("RECOVERY_CONFLICT") from error
    if not isinstance(checkpoint, dict):
        raise ApplianceError("RECOVERY_CONFLICT")
    allowed = {
        "protocol",
        "status",
        "phase",
        "error_code",
        *set(_identity(document)),
        "installed_manifest",
        "capture_manifest",
        "capture_absent",
        "entry_count",
        "content_bytes",
    }
    status = checkpoint.get("status")
    valid_status = status == "success" and "error_code" not in checkpoint
    valid_status = valid_status or (
        status == "failure" and checkpoint.get("error_code") in ERROR_CODES
    )
    if (
        set(checkpoint) - allowed
        or checkpoint.get("protocol") != "remote-module-result-v1"
        or checkpoint.get("phase")
        not in {
            "captured",
            "staging-intent",
            "replacement-ready",
            "installed",
            "restore-ready",
            "restored",
        }
        or not valid_status
    ):
        raise ApplianceError("RECOVERY_CONFLICT")
    expected = _identity(document)
    if any(checkpoint.get(key) != value for key, value in expected.items()):
        raise ApplianceError("IDENTITY_MISMATCH")
    return checkpoint


def _count_fields(checkpoint: dict[str, object]) -> dict[str, int]:
    count = checkpoint.get("entry_count")
    content_bytes = checkpoint.get("content_bytes")
    if (
        not isinstance(count, int)
        or isinstance(count, bool)
        or not 0 <= count <= ENTRY_LIMIT
        or not isinstance(content_bytes, int)
        or isinstance(content_bytes, bool)
        or not 0 <= content_bytes <= BYTE_LIMIT
    ):
        raise ApplianceError("RECOVERY_CONFLICT")
    return {"entry_count": count, "content_bytes": content_bytes}


def _capture_fields(checkpoint: dict[str, object]) -> dict[str, object]:
    manifest = checkpoint.get("capture_manifest")
    if isinstance(manifest, str) and DIGEST_RE.fullmatch(manifest):
        return {"capture_manifest": manifest}
    if checkpoint.get("capture_absent") is True:
        return {"capture_absent": True}
    raise ApplianceError("RECOVERY_CONFLICT")


def _module_paths(document: dict[str, object]) -> tuple[Path, Path, Path, Path]:
    release = str(document["release"])
    nonce = str(document["operation_nonce"])
    descriptors: list[int] = []
    try:
        descriptor = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        for component in ("lib", "modules"):
            descriptor = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            descriptors.append(descriptor)
    except OSError as error:
        raise ApplianceError("RECOVERY_CONFLICT") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    parent = ROOT / "lib" / "modules"
    staged = parent / f".kdive-{nonce}-new"
    staged_tree = staged / "lib" / "modules" / release
    displaced = parent / f".kdive-{nonce}-old"
    return parent / release, staged, staged_tree, displaced


def _manifest_or_none(path: Path, kind: str) -> str | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ApplianceError("FILESYSTEM_FAILURE") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ApplianceError("RECOVERY_CONFLICT")
    return _tree_manifest(path, kind)[0]


def _path_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ApplianceError("FILESYSTEM_FAILURE") from error
    return True


def _stage_manifest(staged: Path, staged_tree: Path, kind: str) -> str | None:
    try:
        metadata = staged.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ApplianceError("FILESYSTEM_FAILURE") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ApplianceError("RECOVERY_CONFLICT")
    return _manifest_or_none(staged_tree, kind)


def _remove_empty_stage(staged: Path) -> None:
    if not stat.S_ISDIR(staged.lstat().st_mode):
        raise ApplianceError("RECOVERY_CONFLICT")
    try:
        relative_entries = sorted(path.relative_to(staged).as_posix() for path in staged.rglob("*"))
    except OSError as error:
        raise ApplianceError("FILESYSTEM_FAILURE") from error
    if relative_entries != ["lib", "lib/modules"] or any(
        path.is_symlink() for path in staged.rglob("*")
    ):
        raise ApplianceError("RECOVERY_CONFLICT")
    _remove_owned_tree(staged)


def _capture_material(checkpoint: dict[str, object]) -> dict[str, object]:
    fields = _capture_fields(checkpoint)
    capture = SCRATCH / "capture"
    if "capture_manifest" in fields:
        if _manifest_or_none(capture, "recovery") != fields["capture_manifest"]:
            raise ApplianceError("RECOVERY_CONFLICT")
    elif _manifest_or_none(capture, "recovery") is not None:
        raise ApplianceError("RECOVERY_CONFLICT")
    return fields


def _original_manifest(fields: dict[str, object]) -> str | None:
    value = fields.get("capture_manifest")
    return str(value) if value is not None else None


def _transition_replacement(
    document: dict[str, object], checkpoint: dict[str, object]
) -> dict[str, object]:
    destination, staged, staged_tree, displaced = _module_paths(document)
    fields = _capture_material(checkpoint)
    original = _original_manifest(fields)
    installed = checkpoint.get("installed_manifest")
    if not isinstance(installed, str) or not DIGEST_RE.fullmatch(installed):
        raise ApplianceError("RECOVERY_CONFLICT")
    destination_installed = _manifest_or_none(destination, "installed")
    destination_original = _manifest_or_none(destination, "recovery")
    staged_manifest = _stage_manifest(staged, staged_tree, "installed")
    displaced_manifest = _manifest_or_none(displaced, "recovery")

    if (
        destination_original == original
        and staged_manifest == installed
        and displaced_manifest is None
    ):
        if destination_original is not None:
            destination.rename(displaced)
            _sync_path(destination.parent)
    elif destination_installed == installed:
        pass
    elif not (
        destination_original is None
        and staged_manifest == installed
        and displaced_manifest == original
    ):
        raise ApplianceError("RECOVERY_CONFLICT")

    if _manifest_or_none(destination, "installed") is None:
        staged_tree.rename(destination)
        _remove_empty_stage(staged)
        _sync_path(destination.parent)
    elif _path_present(staged):
        _remove_empty_stage(staged)
        _sync_path(destination.parent)
    if _manifest_or_none(destination, "installed") != installed:
        raise ApplianceError("RECOVERY_CONFLICT")
    if _manifest_or_none(displaced, "recovery") != original:
        raise ApplianceError("RECOVERY_CONFLICT")
    result = _checkpoint(
        document,
        "installed",
        installed_manifest=installed,
        **_count_fields(checkpoint),
        **fields,
    )
    _remove_owned_tree(displaced)
    _sync_path(destination.parent)
    return result


def _finish_installed(
    document: dict[str, object], checkpoint: dict[str, object]
) -> dict[str, object]:
    destination, staged, staged_tree, displaced = _module_paths(document)
    fields = _capture_material(checkpoint)
    installed = checkpoint.get("installed_manifest")
    if not isinstance(installed, str) or _manifest_or_none(destination, "installed") != installed:
        raise ApplianceError("RECOVERY_CONFLICT")
    if _stage_manifest(staged, staged_tree, "installed") is not None:
        raise ApplianceError("RECOVERY_CONFLICT")
    if _path_present(staged):
        _remove_empty_stage(staged)
        _sync_path(destination.parent)
    displaced_manifest = _manifest_or_none(displaced, "recovery")
    if displaced_manifest not in {None, _original_manifest(fields)}:
        raise ApplianceError("RECOVERY_CONFLICT")
    result = _checkpoint(
        document,
        "installed",
        installed_manifest=installed,
        **_count_fields(checkpoint),
        **fields,
    )
    _remove_owned_tree(displaced)
    _sync_path(destination.parent)
    return result


def _capture_install(document: dict[str, object]) -> dict[str, object]:
    destination, staged, staged_tree, displaced = _module_paths(document)
    source = SOURCE / "modules"
    if _manifest_or_none(source, "source") != document["source_manifest"]:
        raise ApplianceError("SOURCE_INVALID")
    checkpoint = _existing_checkpoint(document)
    capture = SCRATCH / "capture"
    if checkpoint is None:
        if _path_present(staged) or _manifest_or_none(displaced, "recovery") is not None:
            raise ApplianceError("RECOVERY_CONFLICT")
        _remove_owned_tree(capture)
        original = _manifest_or_none(destination, "recovery")
        if original is None:
            (SCRATCH / "capture-absent").write_bytes(b"")
            _sync_path(SCRATCH)
            fields: dict[str, object] = {"capture_absent": True}
            checkpoint = _checkpoint(document, "captured", entry_count=0, content_bytes=0, **fields)
        else:
            manifest, count, size = _copy_tree(destination, capture, "recovery")
            fields = {"capture_manifest": manifest}
            checkpoint = _checkpoint(
                document, "captured", entry_count=count, content_bytes=size, **fields
            )
    phase = checkpoint.get("phase")
    if phase == "installed":
        return _finish_installed(document, checkpoint)
    if phase == "replacement-ready":
        return _transition_replacement(document, checkpoint)
    if phase not in {"captured", "staging-intent"}:
        raise ApplianceError("RECOVERY_CONFLICT")
    fields = _capture_material(checkpoint)
    if _manifest_or_none(destination, "recovery") != _original_manifest(fields):
        raise ApplianceError("RECOVERY_CONFLICT")
    if _manifest_or_none(displaced, "recovery") is not None:
        raise ApplianceError("RECOVERY_CONFLICT")
    if phase == "captured":
        checkpoint = _checkpoint(document, "staging-intent", **fields)
    _remove_owned_tree(staged)
    staged_tree.parent.mkdir(parents=True)
    _copy_tree(source, staged_tree)
    try:
        completed = subprocess.run(
            [DEPMOD, "-b", str(staged), str(document["release"])],
            check=False,
            env={"PATH": "/sbin"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise ApplianceError("DEPMOD_FAILURE") from error
    if completed.returncode != 0:
        raise ApplianceError("DEPMOD_FAILURE")
    _sync_tree(staged_tree)
    _sync_path(staged)
    installed, count, size = _tree_manifest(staged_tree, "installed")
    checkpoint = _checkpoint(
        document,
        "replacement-ready",
        installed_manifest=installed,
        entry_count=count,
        content_bytes=size,
        **fields,
    )
    return _transition_replacement(document, checkpoint)


def _restore(document: dict[str, object]) -> dict[str, object]:
    destination, staged, staged_tree, displaced = _module_paths(document)
    checkpoint = _existing_checkpoint(document)
    if checkpoint is None or checkpoint.get("phase") not in {
        "installed",
        "restore-ready",
        "restored",
    }:
        raise ApplianceError("RECOVERY_CONFLICT")
    absent = document.get("capture_absent") is True
    fields: dict[str, object] = (
        {"capture_absent": True} if absent else {"capture_manifest": document["capture_manifest"]}
    )
    installed = str(document["installed_manifest"])
    capture = SCRATCH / "capture"
    captured = None if absent else str(document["capture_manifest"])
    if not absent and _manifest_or_none(capture, "recovery") != captured:
        raise ApplianceError("RECOVERY_CONFLICT")
    if checkpoint.get("phase") == "installed":
        if _manifest_or_none(destination, "installed") != installed:
            raise ApplianceError("RECOVERY_CONFLICT")
        if _path_present(staged) or _manifest_or_none(displaced, "installed") is not None:
            raise ApplianceError("RECOVERY_CONFLICT")
        if not absent:
            staged_tree.parent.mkdir(parents=True)
            _copy_tree(capture, staged_tree, "recovery")
        checkpoint = _checkpoint(document, "restore-ready", installed_manifest=installed, **fields)
    destination_installed = _manifest_or_none(destination, "installed")
    destination_captured = _manifest_or_none(destination, "recovery")
    staged_captured = _stage_manifest(staged, staged_tree, "recovery")
    displaced_installed = _manifest_or_none(displaced, "installed")
    if checkpoint.get("phase") == "restore-ready":
        if destination_installed == installed and displaced_installed is None:
            invalid_stage = (absent and staged_captured is not None) or (
                not absent and staged_captured != captured
            )
            if invalid_stage:
                raise ApplianceError("RECOVERY_CONFLICT")
            destination.rename(displaced)
            _sync_path(destination.parent)
        elif not (destination_installed is None and displaced_installed == installed):
            if not (destination_captured == captured and displaced_installed == installed):
                raise ApplianceError("RECOVERY_CONFLICT")
        if not absent and _manifest_or_none(destination, "recovery") is None:
            staged_tree.rename(destination)
            _remove_empty_stage(staged)
            _sync_path(destination.parent)
        elif _path_present(staged):
            _remove_empty_stage(staged)
            _sync_path(destination.parent)
        if absent and _manifest_or_none(destination, "installed") is not None:
            raise ApplianceError("RECOVERY_CONFLICT")
        if _manifest_or_none(destination, "recovery") != captured:
            raise ApplianceError("RECOVERY_CONFLICT")
        checkpoint = _checkpoint(document, "restored", installed_manifest=installed, **fields)
    if _manifest_or_none(destination, "recovery") != captured:
        raise ApplianceError("RECOVERY_CONFLICT")
    displaced_manifest = _manifest_or_none(displaced, "installed")
    if displaced_manifest not in {None, installed}:
        raise ApplianceError("RECOVERY_CONFLICT")
    _remove_owned_tree(displaced)
    _sync_path(destination.parent)
    return _checkpoint(document, "restored", installed_manifest=installed, **fields)


def execute(document: dict[str, object]) -> dict[str, object]:
    if document["operation"] == "capture_install":
        return _capture_install(document)
    return _restore(document)


def _mount(source: str, target: Path, filesystem: str, flags: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    target.mkdir(parents=True, exist_ok=True)
    result = libc.mount(source.encode(), os.fsencode(target), filesystem.encode(), flags, None)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _unmount(target: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.umount2(os.fsencode(target), 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _mount_root() -> None:
    candidates = ["/dev/vda", *[f"/dev/vda{index}" for index in range(1, 17)]]
    matches: list[tuple[str, str]] = []
    for device in candidates:
        for filesystem in ("ext4", "xfs", "btrfs"):
            try:
                _mount(device, ROOT, filesystem, 1 | 2 | 4 | 8)
            except OSError as error:
                if error.errno not in {errno.ENOENT, errno.EINVAL, errno.ENODEV}:
                    continue
            else:
                if (ROOT / "lib" / "modules").is_dir():
                    matches.append((device, filesystem))
                _unmount(ROOT)
    if len(matches) != 1:
        raise ApplianceError("ROOT_DISCOVERY_FAILED")
    _mount(matches[0][0], ROOT, matches[0][1], 2 | 4 | 8)


def _reboot() -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    return int(libc.reboot(LINUX_REBOOT_CMD_POWER_OFF))


def _poweroff(document: dict[str, object] | None) -> NoReturn:
    os.sync()
    _reboot()
    failure = ApplianceError("SHUTDOWN_FAILURE")
    try:
        _mount("/dev/vdc", SCRATCH, "ext4", 2 | 4 | 8)
        _write_failure(document, failure)
    except (ApplianceError, OSError) as error:
        raise failure from error
    finally:
        if SCRATCH.is_mount():
            try:
                _unmount(SCRATCH)
            except OSError as error:
                raise failure from error
    _reboot()
    raise failure


def _write_failure(document: dict[str, object] | None, error: ApplianceError) -> None:
    if error.code == "IDENTITY_MISMATCH" and (SCRATCH / "result-v1.json").exists():
        return
    failure: dict[str, object] = {
        "protocol": "remote-module-result-v1",
        "status": "failure",
        "phase": "accepted",
        "error_code": error.code,
    }
    if document is not None:
        try:
            checkpoint = _existing_checkpoint(document)
        except ApplianceError:
            checkpoint = None
        if checkpoint is not None:
            failure.update(checkpoint)
        else:
            failure.update(_identity(document))
        failure["status"] = "failure"
        failure["error_code"] = error.code
    _write_json(SCRATCH / "result-v1.json", failure)


def _finish_mounts(
    document: dict[str, object] | None, operation_error: ApplianceError | None
) -> ApplianceError | None:
    failure = operation_error
    if failure is None:
        for target in (ROOT, SCRATCH):
            if target.is_mount():
                try:
                    _sync_path(target)
                except ApplianceError as error:
                    failure = error
                    break
    if failure is not None and SCRATCH.is_mount():
        _write_failure(document, failure)
    for target in (SOURCE, ROOT):
        if not target.is_mount():
            continue
        try:
            _unmount(target)
        except OSError:
            failure = ApplianceError("FLUSH_FAILURE")
    if failure is not None and SCRATCH.is_mount():
        _write_failure(document, failure)
    if SCRATCH.is_mount():
        try:
            _unmount(SCRATCH)
        except OSError:
            failure = ApplianceError("FLUSH_FAILURE")
            _write_failure(document, failure)
    return failure


def main() -> NoReturn:
    document: dict[str, object] | None = None
    operation_error: ApplianceError | None = None
    try:
        _mount("devtmpfs", Path("/dev"), "devtmpfs", 2 | 4 | 8)
        _mount_root()
        _mount("/dev/vdb", SOURCE, "ext4", 1 | 2 | 4 | 8)
        _mount("/dev/vdc", SCRATCH, "ext4", 2 | 4 | 8)
        document = _read_operation(SOURCE / "operation-v1.json")
        execute(document)
    except ApplianceError as error:
        operation_error = error
    except OSError:
        operation_error = ApplianceError("FILESYSTEM_FAILURE")
    try:
        _finish_mounts(document, operation_error)
    finally:
        _poweroff(document)


if __name__ == "__main__":
    main()
