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
from contextlib import suppress
from pathlib import Path
from typing import Any

from kdive.jobs.capture_operations.bootstrap_attestation import fingerprint
from kdive.jobs.capture_operations.bootstrap_elf import runtime_elf_closure

SCHEMA_VERSION = 1
DEFAULT_DESTINATION = Path("/usr/share/kdive/capture-bootstrap-manifest.json")
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
        "import kdive.capture_bootstrap\n"
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


def _prepare_install_parent(path: Path, *, owner_uid: int, group_gid: int) -> tuple[int, bool]:
    if not path.is_absolute():
        raise RuntimeError("manifest destination parent must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open("/", flags)
    changed = False
    try:
        for component in path.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, 0o755, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
                os.fchown(child, owner_uid, group_gid)
                os.fchmod(child, 0o755)
                changed = True
            except OSError as error:
                raise RuntimeError(
                    "manifest destination parent must be a real directory"
                ) from error
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        ownership_changed = metadata.st_uid != owner_uid or metadata.st_gid != group_gid
        mode_changed = stat.S_IMODE(metadata.st_mode) != 0o755
        if ownership_changed:
            os.fchown(descriptor, owner_uid, group_gid)
        if mode_changed:
            os.fchmod(descriptor, 0o755)
        return descriptor, changed or ownership_changed or mode_changed
    except BaseException:
        os.close(descriptor)
        raise


def _atomic_write_at(
    parent_fd: int,
    name: str,
    data: bytes,
    mode: int,
    *,
    owner_uid: int,
    group_gid: int,
) -> bool:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        existing_fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    else:
        try:
            metadata = os.fstat(existing_fd)
            existing = os.read(existing_fd, len(data) + 1)
            if (
                stat.S_ISREG(metadata.st_mode)
                and stat.S_IMODE(metadata.st_mode) == mode
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
        os.fchmod(temporary_fd, mode)
        os.fchown(temporary_fd, owner_uid, group_gid)
        view = memoryview(data)
        while view:
            view = view[os.write(temporary_fd, view) :]
        os.fsync(temporary_fd)
    finally:
        os.close(temporary_fd)
    try:
        os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=parent_fd)
    return True


def _install(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise PermissionError("capture bootstrap manifest installation requires root")
    staged = args.staged.resolve(strict=True)
    data = staged.read_bytes()
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
    finally:
        os.close(parent_fd)
    installed = args.destination.stat()
    if installed.st_uid != 0 or installed.st_gid != 0:
        os.chown(args.destination, 0, 0)
        changed = True
    if args.destination.read_bytes() != data:
        raise RuntimeError("installed manifest bytes differ from staged manifest")
    metadata = args.destination.stat()
    if metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o644:
        raise RuntimeError("installed manifest is not root-owned mode 0644")
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
