#!/usr/bin/env python3
"""Build, verify, and root-install the capture bootstrap attestation manifest."""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from kdive.jobs.capture_operations.bootstrap_attestation import fingerprint, read_manifest
from kdive.jobs.capture_operations.bootstrap_elf import runtime_elf_closure

SCHEMA_VERSION = 1
DEFAULT_DESTINATION = Path("/usr/share/kdive/capture-bootstrap-manifest.json")
_MAX_MANIFEST_BYTES = 1_048_576
_ARCHITECTURES = {"amd64": "x86_64", "x86_64": "x86_64", "ppc64le": "ppc64le"}


def _environment(source_root: Path | None = None) -> dict[str, str]:
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
    if source_root is not None:
        environment["PYTHONPATH"] = str(source_root.resolve())
    return environment


def _run(command: list[str], *, source_root: Path | None = None) -> str:
    result = subprocess.run(
        command,
        env=_environment(source_root),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(command)} failed: {result.stderr.strip()}")
    return result.stdout


def _bootstrap_trace(interpreter: Path, source_root: Path) -> tuple[list[str], list[Path]]:
    code = (
        "import sys\n"
        "import kdive.jobs.capture_operations.bootstrap_entrypoint\n"
        "from kdive.jobs.capture_operations import sandbox\n"
        "del sandbox\n"
        "for name in sorted(sys.modules):\n"
        " module = sys.modules[name]\n"
        " path = getattr(module, '__file__', None)\n"
        " if path:\n"
        "  print(name + '\\t' + path)\n"
    )
    output = _run([str(interpreter), "-S", "-c", code], source_root=source_root)
    modules: list[str] = []
    files: list[Path] = []
    for line in output.splitlines():
        name, raw_path = line.split("\t", 1)
        path = Path(raw_path)
        if path.suffix == ".pyc" and "__pycache__" in path.parts:
            stem = path.name.split(".", 1)[0] + ".py"
            path = path.parent.parent / stem
        path = path.resolve(strict=True)
        modules.append(name)
        files.append(path)
    return modules, files


def _manifest(interpreter_arg: str, source_root: Path) -> dict[str, Any]:
    interpreter = Path(interpreter_arg).resolve(strict=True)
    architecture = _ARCHITECTURES.get(platform.machine().lower())
    if architecture is None:
        raise RuntimeError(f"unsupported architecture: {platform.machine()}")
    modules, module_files = _bootstrap_trace(interpreter, source_root)
    elf_files, elf_interpreters = runtime_elf_closure(
        [interpreter, *module_files], required_libraries=("libseccomp.so.2",)
    )
    kinds: dict[Path, str] = {path: "elf-dependency" for path in elf_files}
    for path in module_files:
        kinds[path] = "bootstrap-python" if path.suffix == ".py" else "bootstrap-extension"
    for path in elf_interpreters:
        kinds[path] = "elf-interpreter"
    kinds[interpreter] = "python-interpreter"
    files = [
        {
            "kind": kinds[path],
            "path": str(path),
            "sha256": fingerprint(path, expected_uid=os.geteuid()),
        }
        for path in sorted(kinds, key=str)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "architecture": architecture,
        "interpreter": str(interpreter),
        "bootstrap_modules": modules,
        "files": files,
    }


def _canonical(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _atomic_write(path: Path, data: bytes, mode: int) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        metadata = path.lstat()
        if (
            stat.S_ISREG(metadata.st_mode)
            and not path.is_symlink()
            and stat.S_IMODE(metadata.st_mode) == mode
            and path.read_bytes() == data
        ):
            return False
    except FileNotFoundError:
        pass
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=True) as destination:
            destination.write(data)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return True


def _load(path: Path) -> dict[str, Any]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise RuntimeError("manifest must be a regular file, not a symlink")
    if stat.S_IMODE(metadata.st_mode) != 0o644:
        raise RuntimeError("manifest must have mode 0644")
    try:
        payload = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("manifest contains malformed JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError("manifest must be a JSON object")
    return payload


def _verify(manifest_path: Path, interpreter_arg: str, source_root: Path) -> None:
    payload = _load(manifest_path)
    expected = _manifest(interpreter_arg, source_root)
    if payload.get("interpreter") != expected["interpreter"]:
        raise RuntimeError("manifest interpreter does not match selected worker interpreter")
    if payload.get("bootstrap_modules") != expected["bootstrap_modules"]:
        raise RuntimeError("bootstrap import trace does not match the selected interpreter")
    if payload != expected:
        raise RuntimeError("manifest fingerprint does not match selected worker runtime")
    if manifest_path.read_bytes() != _canonical(payload):
        raise RuntimeError("manifest is not canonical JSON")


def _build(args: argparse.Namespace) -> None:
    payload = _manifest(args.interpreter, args.source_root)
    changed = _atomic_write(args.output, _canonical(payload), 0o644)
    _verify(args.output, args.interpreter, args.source_root)
    print("changed" if changed else "unchanged")


def _absolute_components(path: Path, *, label: str, allow_root: bool) -> tuple[str, ...]:
    if not path.is_absolute():
        raise RuntimeError(f"{label} must be absolute")
    components = path.parts[1:]
    if (not allow_root and not components) or any(
        component in {"", ".", ".."} for component in components
    ):
        raise RuntimeError(f"{label} contains an invalid path component")
    return components


def _approved_owner(metadata: os.stat_result, expected_uid: int) -> bool:
    return metadata.st_uid in {0, expected_uid}


def _verify_ancestor(
    metadata: os.stat_result,
    child: os.stat_result,
    *,
    expected_uid: int,
    label: str,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or not _approved_owner(metadata, expected_uid):
        raise RuntimeError(f"{label} has an unapproved ancestor")
    writable_by_others = bool(metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    sticky_protected = bool(metadata.st_mode & stat.S_ISVTX) and _approved_owner(
        child, expected_uid
    )
    if writable_by_others and not sticky_protected:
        raise RuntimeError(f"{label} has a replaceable ancestor")


def _verify_creation_parent(
    metadata: os.stat_result,
    *,
    expected_uid: int,
    label: str,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or not _approved_owner(metadata, expected_uid):
        raise RuntimeError(f"{label} has an unapproved ancestor")
    writable_by_others = bool(metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    if writable_by_others and not metadata.st_mode & stat.S_ISVTX:
        raise RuntimeError(f"{label} has a replaceable ancestor")


def _normalize_install_directory(
    descriptor: int,
    *,
    owner_uid: int,
    group_gid: int,
) -> bool:
    metadata = os.fstat(descriptor)
    ownership_changed = metadata.st_uid != owner_uid or metadata.st_gid != group_gid
    mode_changed = stat.S_IMODE(metadata.st_mode) != 0o755
    if ownership_changed:
        os.fchown(descriptor, owner_uid, group_gid)
    if mode_changed:
        os.fchmod(descriptor, 0o755)
    return ownership_changed or mode_changed


def _prepare_install_parent(path: Path, *, owner_uid: int, group_gid: int) -> tuple[int, bool]:
    components = _absolute_components(
        path,
        label="manifest destination parent",
        allow_root=True,
    )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    metadata = os.fstat(descriptor)
    changed = False
    try:
        if not components:
            if (
                metadata.st_uid != owner_uid
                or metadata.st_gid != group_gid
                or stat.S_IMODE(metadata.st_mode) != 0o755
            ):
                raise RuntimeError("manifest destination root has unexpected metadata")
            os.fsync(descriptor)
            return descriptor, False
        for index, component in enumerate(components):
            created = False
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                _verify_creation_parent(
                    metadata,
                    expected_uid=owner_uid,
                    label="manifest destination path",
                )
                os.mkdir(component, 0o755, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
                created = True
            except OSError as error:
                raise RuntimeError(
                    "manifest destination parent must be a real directory"
                ) from error
            try:
                child_metadata = os.fstat(child)
                _verify_ancestor(
                    metadata,
                    child_metadata,
                    expected_uid=owner_uid,
                    label="manifest destination path",
                )
                final_component = index == len(components) - 1
                replaceable_intermediate = (
                    not final_component
                    and child_metadata.st_uid == owner_uid
                    and bool(child_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
                    and not child_metadata.st_mode & stat.S_ISVTX
                )
                normalize_component = created or final_component or replaceable_intermediate
                normalized = (
                    _normalize_install_directory(
                        child,
                        owner_uid=owner_uid,
                        group_gid=group_gid,
                    )
                    if normalize_component
                    else False
                )
                if normalize_component:
                    os.fsync(child)
                    os.fsync(descriptor)
                changed = changed or created or normalized
                child_metadata = os.fstat(child)
                if normalize_component and (
                    child_metadata.st_uid != owner_uid
                    or child_metadata.st_gid != group_gid
                    or stat.S_IMODE(child_metadata.st_mode) != 0o755
                ):
                    raise RuntimeError("manifest destination directory could not be hardened")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
            metadata = child_metadata
        return descriptor, changed
    except BaseException:
        os.close(descriptor)
        raise


def _open_install_parent(
    path: Path,
    *,
    owner_uid: int,
    group_gid: int,
) -> int:
    components = _absolute_components(
        path,
        label="manifest destination parent",
        allow_root=True,
    )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    metadata = os.fstat(descriptor)
    try:
        for component in components:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except OSError as error:
                raise RuntimeError("manifest destination parent is not safely openable") from error
            try:
                child_metadata = os.fstat(child)
                _verify_ancestor(
                    metadata,
                    child_metadata,
                    expected_uid=owner_uid,
                    label="manifest destination path",
                )
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
            metadata = child_metadata
        if (
            metadata.st_uid != owner_uid
            or metadata.st_gid != group_gid
            or stat.S_IMODE(metadata.st_mode) != 0o755
        ):
            raise RuntimeError("manifest destination parent has unexpected metadata")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _assert_install_parent_selected(
    path: Path,
    selected_fd: int,
    *,
    owner_uid: int,
    group_gid: int,
) -> None:
    try:
        verification_fd = _open_install_parent(
            path,
            owner_uid=owner_uid,
            group_gid=group_gid,
        )
    except RuntimeError as error:
        raise RuntimeError("destination parent changed during installation") from error
    try:
        selected = os.fstat(selected_fd)
        verification = os.fstat(verification_fd)
        if (selected.st_dev, selected.st_ino) != (verification.st_dev, verification.st_ino):
            raise RuntimeError("destination parent changed during installation")
    finally:
        os.close(verification_fd)


def _atomic_write_at(
    parent_fd: int,
    name: str,
    data: bytes,
    mode: int,
    *,
    owner_uid: int,
    group_gid: int,
) -> bool:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        existing_fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise RuntimeError("manifest destination must be a regular file") from error
    else:
        try:
            metadata = os.fstat(existing_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("manifest destination must be a regular file")
            existing = os.read(existing_fd, len(data) + 1)
            if (
                stat.S_IMODE(metadata.st_mode) == mode
                and metadata.st_uid == owner_uid
                and metadata.st_gid == group_gid
                and existing == data
            ):
                return False
        finally:
            os.close(existing_fd)
    temporary = f".{name}.{secrets.token_hex(8)}"
    temporary_fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        mode,
        dir_fd=parent_fd,
    )
    try:
        try:
            os.fchmod(temporary_fd, mode)
            os.fchown(temporary_fd, owner_uid, group_gid)
            view = memoryview(data)
            while view:
                view = view[os.write(temporary_fd, view) :]
            os.fsync(temporary_fd)
        finally:
            os.close(temporary_fd)
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        else:
            os.fsync(parent_fd)
    return True


def _read_regular_at(
    parent_fd: int,
    name: str,
    *,
    maximum: int,
    owner_uid: int | None = None,
    group_gid: int | None = None,
    mode: int | None = None,
) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        raise RuntimeError("manifest leaf must be a regular file") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise RuntimeError("manifest leaf must be a bounded regular file")
        if owner_uid is not None and metadata.st_uid != owner_uid:
            raise RuntimeError("installed manifest has the wrong owner")
        if group_gid is not None and metadata.st_gid != group_gid:
            raise RuntimeError("installed manifest has the wrong group")
        if mode is not None and stat.S_IMODE(metadata.st_mode) != mode:
            raise RuntimeError("installed manifest has the wrong mode")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining and (chunk := os.read(descriptor, remaining)):
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise RuntimeError("manifest leaf exceeds the size bound")
        return data
    finally:
        os.close(descriptor)


def _invoking_uid() -> int:
    raw_uid = os.environ.get("SUDO_UID")
    if raw_uid is None:
        return os.getuid()
    if not raw_uid.isascii() or not raw_uid.isdecimal() or len(raw_uid) > 10:
        raise RuntimeError("SUDO_UID must be a decimal user id")
    uid = int(raw_uid)
    if uid > 4_294_967_295:
        raise RuntimeError("SUDO_UID is outside the supported user-id range")
    return uid


def _read_staged_manifest(path: Path, *, expected_uid: int | None = None) -> bytes:
    selected_path = path if path.is_absolute() else Path.cwd() / path
    selected_uid = os.getuid() if expected_uid is None else expected_uid
    try:
        data = read_manifest(
            selected_path,
            expected_uid=selected_uid,
            maximum_size=_MAX_MANIFEST_BYTES,
        )
    except PermissionError as error:
        if str(error) != "capture bootstrap manifest has the wrong owner":
            raise RuntimeError("staged manifest path is not safely openable") from error
        try:
            data = read_manifest(
                selected_path,
                expected_uid=0,
                maximum_size=_MAX_MANIFEST_BYTES,
            )
        except PermissionError as root_error:
            raise RuntimeError("staged manifest path is not safely openable") from root_error
    try:
        payload = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("staged manifest contains malformed JSON") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("staged manifest has an unsupported structure")
    return data


def _install(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise PermissionError("capture bootstrap manifest installation requires root")
    if (
        not args.destination.is_absolute()
        or not args.destination.name
        or args.destination.name in {".", ".."}
    ):
        raise RuntimeError("manifest destination must be an absolute file path")
    data = _read_staged_manifest(args.staged, expected_uid=_invoking_uid())
    parent_fd, changed = _prepare_install_parent(args.destination.parent, owner_uid=0, group_gid=0)
    try:
        changed = (
            _atomic_write_at(
                parent_fd,
                args.destination.name,
                data,
                0o644,
                owner_uid=0,
                group_gid=0,
            )
            or changed
        )
        installed = _read_regular_at(
            parent_fd,
            args.destination.name,
            maximum=_MAX_MANIFEST_BYTES,
            owner_uid=0,
            group_gid=0,
            mode=0o644,
        )
        _assert_install_parent_selected(
            args.destination.parent,
            parent_fd,
            owner_uid=0,
            group_gid=0,
        )
    finally:
        os.close(parent_fd)
    if installed != data:
        raise RuntimeError("installed manifest bytes differ from staged manifest")
    print("changed" if changed else "unchanged")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--interpreter", required=True)
        command.add_argument("--source-root", type=Path, required=True)
        if name == "build":
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--manifest", type=Path, required=True)
    install = commands.add_parser("install")
    install.add_argument("--staged", type=Path, required=True)
    install.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one deterministic build, verification, or privileged installation action."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            _build(args)
        elif args.command == "verify":
            _verify(args.manifest, args.interpreter, args.source_root)
        else:
            _install(args)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
