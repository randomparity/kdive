"""Exact target-loader ELF closure resolution for capture bootstrap attestation (ADR-0558)."""

from __future__ import annotations

import os
import re
import selectors
import shutil
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path

_INTERPRETER_RE = re.compile(r"Requesting program interpreter:\s*([^\]]+)")
_LOADER_MAPPING_RE = re.compile(
    r"^(?:(?P<name>[^\s=>]+)\s+=>\s+)?(?P<path>/[^\s]+)\s+\(0x[0-9a-fA-F]+\)$"
)
_LOADER_VIRTUAL_RE = re.compile(r"^linux-(?:vdso|gate)[^\s]*\s+\(0x[0-9a-fA-F]+\)$")
_LOADER_TRY_RE = re.compile(r"\btrying file=(?P<path>/[^\s]+)\s*$")
_MAX_LOADER_OUTPUT_BYTES = 1_048_576
_LOADER_TIMEOUT_SECONDS = 10.0
_TOOL_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"


def _environment(*, debug: bool = False) -> dict[str, str]:
    environment = {"PATH": _TOOL_PATH, "LC_ALL": "C"}
    if debug:
        # Diagnostics report the selected file but do not change the scrubbed loader search.
        environment["LD_DEBUG"] = "libs,files"
    return environment


def _readelf(path: Path) -> str:
    executable = shutil.which("readelf", path=_TOOL_PATH)
    if executable is None:
        raise RuntimeError("readelf is required to resolve the ELF dependency closure")
    result = subprocess.run(
        [executable, "-W", "-l", str(path)],
        env=_environment(),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"readelf failed for ELF root {path}: {result.stderr.strip()}")
    return result.stdout


def elf_interpreter(path: Path) -> Path | None:
    """Return one ELF root's normalized PT_INTERP, or none for a non-ELF/static object."""
    with path.open("rb") as source:
        if source.read(4) != b"\x7fELF":
            return None
    interpreter_match = _INTERPRETER_RE.search(_readelf(path))
    if interpreter_match is None:
        return None
    try:
        return Path(interpreter_match.group(1)).resolve(strict=True)
    except OSError as error:
        raise RuntimeError(f"ELF root {path} names an unavailable PT_INTERP") from error


def _bounded_loader_run(command: list[str], *, debug: bool = False) -> tuple[str, str]:
    process = subprocess.Popen(  # noqa: S603 - caller supplies only attested loader/roots
        command,
        env=_environment(debug=debug),
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


def parse_loader_list(output: str) -> set[Path]:
    """Strictly parse one bounded target-loader `--list` result."""
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
    return parse_loader_list(stdout)


def _resolve_loader_soname(loader: Path, soname: str) -> tuple[Path, set[Path]]:
    if not soname or "/" in soname or any(character.isspace() for character in soname):
        raise ValueError("required bootstrap library must be a plain SONAME")
    stdout, diagnostics = _bounded_loader_run([str(loader), "--list", soname], debug=True)
    dependencies = parse_loader_list(stdout)
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


def runtime_elf_closure(
    roots: Iterable[Path], *, required_libraries: tuple[str, ...] = ()
) -> tuple[set[Path], set[Path]]:
    """Return exact normalized roots/dependencies and their single target loader."""
    resolved_roots = {path.resolve(strict=True) for path in roots}
    root_interpreters = {
        interpreter for path in resolved_roots if (interpreter := elf_interpreter(path)) is not None
    }
    if len(root_interpreters) != 1:
        raise RuntimeError("ELF roots must identify one exact PT_INTERP")
    loader = next(iter(root_interpreters))
    closure = set(resolved_roots)
    for path in sorted(resolved_roots, key=str):
        with path.open("rb") as source:
            if source.read(4) != b"\x7fELF":
                continue
        root_interpreter = elf_interpreter(path)
        if root_interpreter is not None and root_interpreter != loader:
            raise RuntimeError(f"ELF root {path} names a conflicting PT_INTERP")
        closure.update(_loader_dependencies(loader, path))
    for soname in required_libraries:
        selected, dependencies = _resolve_loader_soname(loader, soname)
        closure.add(selected)
        closure.update(dependencies)
    for path in sorted(closure - resolved_roots, key=str):
        with path.open("rb") as source:
            if source.read(4) != b"\x7fELF":
                raise RuntimeError(f"runtime loader trace returned a non-ELF dependency: {path}")
    return closure, {loader}
