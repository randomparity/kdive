"""Attestation of the installed capture-bootstrap manifest."""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from kdive.jobs.capture_operations.bootstrap.bootstrap_attestation import fingerprint, read_manifest
from kdive.jobs.capture_operations.bootstrap.bootstrap_elf import runtime_elf_closure

_DEFAULT_MANIFEST = Path("/usr/share/kdive/capture-bootstrap-manifest.json")
_ARCHITECTURES = {"amd64": "x86_64", "x86_64": "x86_64", "ppc64le": "ppc64le"}
_MANIFEST_KEYS = {"schema_version", "architecture", "interpreter", "bootstrap_modules", "files"}
_FINGERPRINT_KINDS = {
    "python-interpreter",
    "elf-interpreter",
    "elf-dependency",
    "bootstrap-python",
    "bootstrap-extension",
}
_ELF_FINGERPRINT_KINDS = {
    "python-interpreter",
    "elf-interpreter",
    "elf-dependency",
    "bootstrap-extension",
}
_ELF_ROOT_KINDS = {"python-interpreter", "bootstrap-extension"}


def _validate_manifest_shape(payload: object, raw: bytes) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _MANIFEST_KEYS:
        raise RuntimeError("capture bootstrap manifest has an unsupported schema")
    manifest = cast(dict[str, Any], payload)
    canonical = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if raw != canonical:
        raise RuntimeError("capture bootstrap manifest is not canonical JSON")
    modules = manifest.get("bootstrap_modules")
    files = manifest.get("files")
    module_names = cast(list[str], modules) if isinstance(modules, list) else []
    if (
        not isinstance(modules, list)
        or not all(isinstance(module, str) for module in modules)
        or module_names != sorted(set(module_names))
        or not isinstance(files, list)
        or not files
    ):
        raise RuntimeError("capture bootstrap manifest has malformed trace data")
    seen: set[str] = set()
    ordered_paths: list[str] = []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"kind", "path", "sha256"}:
            raise RuntimeError("capture bootstrap manifest fingerprint entry is malformed")
        fingerprint = cast(dict[str, Any], entry)
        candidate = fingerprint["path"]
        digest = fingerprint["sha256"]
        if (
            fingerprint["kind"] not in _FINGERPRINT_KINDS
            or not isinstance(candidate, str)
            or not candidate.startswith("/")
            or candidate in seen
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise RuntimeError("capture bootstrap manifest fingerprint entry is malformed")
        seen.add(candidate)
        ordered_paths.append(candidate)
    if ordered_paths != sorted(ordered_paths):
        raise RuntimeError("capture bootstrap manifest fingerprint paths are not sorted")
    return manifest


def _verified_manifest_paths(files: list[object]) -> list[tuple[str, Path, str]]:
    verified: list[tuple[str, Path, str]] = []
    for raw_entry in files:
        assert isinstance(raw_entry, dict)
        entry = cast(dict[str, object], raw_entry)
        kind = entry["kind"]
        expected = entry["sha256"]
        assert isinstance(kind, str) and isinstance(expected, str)
        candidate = Path(str(entry["path"]))
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise RuntimeError(
                f"capture bootstrap fingerprint path unavailable: {candidate}"
            ) from error
        if resolved != candidate:
            raise RuntimeError(f"capture bootstrap fingerprint path is a symlink: {candidate}")
        verified.append((kind, resolved, expected))
    return verified


def _verify_runtime_elf_paths(files: list[tuple[str, Path, str]]) -> None:
    expected = {path for kind, path, _digest in files if kind in _ELF_FINGERPRINT_KINDS}
    roots = [path for kind, path, _digest in files if kind in _ELF_ROOT_KINDS]
    closure, interpreters = runtime_elf_closure(roots, required_libraries=("libseccomp.so.2",))
    selected = closure | interpreters
    if selected != expected:
        raise RuntimeError("capture bootstrap runtime ELF closure drift")


def _read_manifest(path: Path, expected_uid: int) -> bytes:
    return read_manifest(path, expected_uid=expected_uid, maximum_size=1_048_576)


def _decode_manifest(raw: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("capture bootstrap manifest contains malformed JSON") from error
    return _validate_manifest_shape(decoded, raw)


def _verify_manifest_header(payload: Mapping[str, Any], interpreter: Path) -> None:
    architecture = _ARCHITECTURES.get(platform.machine().lower())
    if payload.get("schema_version") != 1:
        raise RuntimeError("capture bootstrap manifest has an unsupported schema")
    if payload.get("architecture") != architecture:
        raise RuntimeError("capture bootstrap manifest architecture drift")
    resolved_interpreter = interpreter.resolve(strict=True)
    if payload.get("interpreter") != str(resolved_interpreter):
        raise RuntimeError("capture bootstrap manifest interpreter drift")


def _verify_manifest_fingerprints(payload: Mapping[str, Any], expected_uid: int) -> None:
    files = payload.get("files")
    assert isinstance(files, list)
    verified_files = _verified_manifest_paths(cast(list[object], files))
    for _kind, candidate, expected in verified_files:
        actual = fingerprint(candidate, expected_uid=expected_uid)
        if actual != expected:
            raise RuntimeError(f"capture bootstrap fingerprint drift: {candidate}")
    _verify_runtime_elf_paths(verified_files)


def _verify_manifest_modules(payload: Mapping[str, Any]) -> None:
    modules = payload.get("bootstrap_modules")
    required = {
        "kdive",
        "kdive.jobs",
        "kdive.jobs.capture_operations",
        "kdive.jobs.capture_operations.process.sandbox",
        "kdive.jobs.capture_operations.bootstrap.bootstrap_entrypoint",
    }
    assert isinstance(modules, list)
    if not required.issubset(modules):
        raise RuntimeError("capture bootstrap import-trace drift")


def _verify_manifest(path: Path, interpreter: Path, expected_uid: int) -> dict[str, Any]:
    payload = _decode_manifest(_read_manifest(path, expected_uid))
    _verify_manifest_header(payload, interpreter)
    _verify_manifest_fingerprints(payload, expected_uid)
    _verify_manifest_modules(payload)
    return payload


def verify_capture_bootstrap_manifest(
    manifest_path: Path = _DEFAULT_MANIFEST,
    interpreter: Path = Path(sys.executable),
    *,
    expected_uid: int = 0,
) -> None:
    """Fail readiness when the root/image-owned bootstrap attestation is stale or malformed."""
    _verify_manifest(manifest_path, interpreter, expected_uid)
