#!/usr/bin/env python3
"""Build, verify, and root-install the capture bootstrap attestation manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_DESTINATION = Path("/usr/share/kdive/capture-bootstrap-manifest.json")
_INTERPRETER_RE = re.compile(r"Requesting program interpreter:\s*([^\]]+)")
_LOADER_MAPPING_RE = re.compile(
    r"^(?:(?P<name>[^\s=>]+)\s+=>\s+)?(?P<path>/[^\s]+)\s+\(0x[0-9a-fA-F]+\)$"
)
_LOADER_VIRTUAL_RE = re.compile(r"^linux-(?:vdso|gate)[^\s]*\s+\(0x[0-9a-fA-F]+\)$")
_LOADER_TRY_RE = re.compile(r"\btrying file=(?P<path>/[^\s]+)\s*$")
_MAX_LOADER_OUTPUT_BYTES = 1_048_576
_LOADER_TIMEOUT_SECONDS = 10.0
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


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


def _elf_interpreter(path: Path) -> Path | None:
    with path.open("rb") as source:
        if source.read(4) != b"\x7fELF":
            return None
    executable = shutil.which("readelf", path=_environment()["PATH"])
    if executable is None:
        raise RuntimeError("readelf is required to resolve the ELF dependency closure")
    output = _run([executable, "-W", "-l", str(path)])
    interpreter_match = _INTERPRETER_RE.search(output)
    return Path(interpreter_match.group(1)).resolve(strict=True) if interpreter_match else None


def _bounded_loader_run(command: list[str], *, debug: bool = False) -> tuple[str, str]:
    environment = _environment()
    if debug:
        # Diagnostics report the chosen path but do not alter the scrubbed loader search.
        environment["LD_DEBUG"] = "libs,files"
    process = subprocess.Popen(  # noqa: S603 - fixed attested loader and arguments
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    streams = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    deadline = time.monotonic() + _LOADER_TIMEOUT_SECONDS
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise RuntimeError("runtime loader trace exceeded 10 seconds")
            events = selector.select(remaining)
            if not events:
                continue
            for key, _ in events:
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = streams[key.fd]
                buffer.extend(chunk)
                if len(buffer) > _MAX_LOADER_OUTPUT_BYTES:
                    process.kill()
                    process.wait()
                    raise RuntimeError("runtime loader trace exceeds 1048576 bytes")
        returncode = process.wait()
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    try:
        stdout = bytes(streams[stdout_fd]).decode("utf-8")
        stderr = bytes(streams[stderr_fd]).decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("runtime loader trace is not UTF-8") from error
    if returncode != 0:
        detail = stderr.strip() or stdout.strip()
        raise RuntimeError(f"runtime loader trace failed: {detail}")
    return stdout, stderr


def _normalize_loader_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        raise RuntimeError("runtime loader trace returned a non-absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RuntimeError(f"runtime loader trace returned an unavailable path: {path}") from error
    if not resolved.is_file():
        raise RuntimeError(f"runtime loader trace returned a non-file path: {path}")
    return resolved


def _parse_loader_list(output: str) -> set[Path]:
    if len(output.encode()) > _MAX_LOADER_OUTPUT_BYTES:
        raise RuntimeError("runtime loader trace exceeds 1048576 bytes")
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if lines == ["statically linked"]:
        return set()
    paths: set[Path] = set()
    selections: dict[str, Path] = {}
    for line in lines:
        if "=> not found" in line:
            raise RuntimeError(f"runtime loader trace contains an unresolved dependency: {line}")
        if _LOADER_VIRTUAL_RE.fullmatch(line):
            continue
        match = _LOADER_MAPPING_RE.fullmatch(line)
        if match is None:
            raise RuntimeError(f"runtime loader trace contains an unparseable line: {line}")
        path = _normalize_loader_path(match.group("path"))
        name = match.group("name")
        if name is not None:
            prior = selections.setdefault(name, path)
            if prior != path:
                raise RuntimeError(f"runtime loader trace is ambiguous for {name}")
        paths.add(path)
    if not lines:
        raise RuntimeError("runtime loader trace is empty")
    return paths


def _loader_dependencies(loader: Path, root: Path) -> set[Path]:
    stdout, stderr = _bounded_loader_run([str(loader), "--list", str(root)])
    if stderr:
        raise RuntimeError("runtime loader trace wrote unexpected diagnostics")
    return _parse_loader_list(stdout)


def _resolve_loader_soname(loader: Path, soname: str) -> tuple[Path, set[Path]]:
    if not soname or "/" in soname or any(character.isspace() for character in soname):
        raise ValueError("required bootstrap library must be a plain SONAME")
    stdout, diagnostics = _bounded_loader_run(
        [str(loader), "--list", soname],
        debug=True,
    )
    dependencies = _parse_loader_list(stdout)
    attempts: list[str] = []
    selected: list[Path] = []
    marker = re.compile(rf"\bfile={re.escape(soname)} \[0\];\s+generating link map\s*$")
    for line in diagnostics.splitlines():
        attempt = _LOADER_TRY_RE.search(line)
        if attempt is not None:
            attempts.append(attempt.group("path"))
        if marker.search(line):
            if not attempts:
                raise RuntimeError("runtime loader trace omitted the selected SONAME path")
            selected.append(_normalize_loader_path(attempts[-1]))
    if len(set(selected)) != 1:
        raise RuntimeError(f"runtime loader trace is ambiguous for required {soname}")
    return selected[0], dependencies


def _elf_closure(
    roots: list[Path], *, required_libraries: tuple[str, ...] = ()
) -> tuple[set[Path], set[Path]]:
    resolved_roots = {path.resolve(strict=True) for path in roots}
    root_interpreters = {
        interpreter
        for path in resolved_roots
        if (interpreter := _elf_interpreter(path)) is not None
    }
    if len(root_interpreters) != 1:
        raise RuntimeError("ELF roots must identify one exact PT_INTERP")
    loader = next(iter(root_interpreters))
    closure = set(resolved_roots)
    interpreters = {loader}
    for path in sorted(resolved_roots, key=str):
        with path.open("rb") as source:
            if source.read(4) != b"\x7fELF":
                continue
        root_interpreter = _elf_interpreter(path)
        if root_interpreter is not None and root_interpreter != loader:
            raise RuntimeError(f"ELF root {path} names a conflicting PT_INTERP")
        # `--list` returns this root's transitive, runtime-selected dependency closure.
        closure.update(_loader_dependencies(loader, path))
    for soname in required_libraries:
        selected, dependencies = _resolve_loader_soname(loader, soname)
        closure.add(selected)
        closure.update(dependencies)
    for path in sorted(closure - resolved_roots, key=str):
        with path.open("rb") as source:
            if source.read(4) != b"\x7fELF":
                raise RuntimeError(f"runtime loader trace returned a non-ELF dependency: {path}")
    return closure, interpreters


def _manifest(interpreter_arg: str, source_root: Path) -> dict[str, Any]:
    interpreter = Path(interpreter_arg).resolve(strict=True)
    architecture = _ARCHITECTURES.get(platform.machine().lower())
    if architecture is None:
        raise RuntimeError(f"unsupported architecture: {platform.machine()}")
    modules, module_files = _bootstrap_trace(interpreter, source_root)
    elf_files, elf_interpreters = _elf_closure(
        [interpreter, *module_files], required_libraries=("libseccomp.so.2",)
    )
    kinds: dict[Path, str] = {path: "elf-dependency" for path in elf_files}
    for path in module_files:
        kinds[path] = "bootstrap-python" if path.suffix == ".py" else "bootstrap-extension"
    for path in elf_interpreters:
        kinds[path] = "elf-interpreter"
    kinds[interpreter] = "python-interpreter"
    files = [
        {"kind": kinds[path], "path": str(path), "sha256": _sha256(path)}
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


def _install(args: argparse.Namespace) -> None:
    if os.geteuid() != 0:
        raise PermissionError("capture bootstrap manifest installation requires root")
    staged = args.staged.resolve(strict=True)
    data = staged.read_bytes()
    changed = _atomic_write(args.destination, data, 0o644)
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
