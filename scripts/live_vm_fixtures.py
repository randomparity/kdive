#!/usr/bin/env python3
"""Record and verify live-VM fixture evidence (ADR-0687)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = (
    "rootfs.qcow2",
    "vmlinux",
    "vmlinux.debug",
    "rootfs.qcow2.provenance.json",
    "rootfs.qcow2.config",
)
MANIFEST = "MANIFEST"
MAX_MANIFEST_BYTES = 64 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _check_import() -> None:
    try:
        import kdive
    except ModuleNotFoundError as exc:
        raise ValueError(
            "selected Python cannot import kdive; run uv sync in this source-overlay checkout"
        ) from exc

    actual = Path(kdive.__file__).resolve() if kdive.__file__ else None
    expected = (ROOT / "src/kdive/__init__.py").resolve()
    if actual != expected:
        raise ValueError(
            f"selected Python imports kdive from {actual}, expected {expected}; "
            "run uv sync in this checkout or set KDIVE_PYTHON to its source-overlay venv"
        )


def builder_revision() -> str:
    for command in (("rev-parse", "HEAD"), ("status", "--porcelain", "--untracked-files=all")):
        try:
            result = subprocess.run(
                ["git", "-C", str(ROOT), *command],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("cannot identify builder revision; check Git and retry") from exc
        if result.returncode:
            raise ValueError("cannot identify clean builder checkout; use a clean Git worktree")
        if command[0] == "rev-parse":
            revision = result.stdout.strip()
            if not SHA256.fullmatch(revision) and not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise ValueError("unknown builder revision; use a clean Git worktree")
        elif result.stdout:
            raise ValueError(
                "builder source is not clean; commit or remove tracked/untracked changes"
            )
    return revision


def inputs(image: str) -> dict[str, object]:
    _check_import()
    from kdive.domain.errors import CategorizedError
    from kdive.images.rootfs.catalog import CloudImageSource, resolve_rootfs_entry

    try:
        entry = resolve_rootfs_entry(image)
    except CategorizedError as exc:
        raise ValueError(f"cannot resolve catalog image {image}; choose a valid image") from exc
    if not isinstance(entry.source, CloudImageSource) or not SHA256.fullmatch(entry.source.sha256):
        raise ValueError(f"image {image} is not checksum-pinned; choose a cloud-image catalog row")
    return {"catalog": asdict(entry), "builder_revision": builder_revision()}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _regular(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"missing artifact {path}; rebuild fixture") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"artifact {path} is not a regular file; rebuild fixture")


def _digest(path: Path) -> str:
    _regular(path)
    hash_ = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hash_.update(block)
    return hash_.hexdigest()


def _provenance(directory: Path, selection: dict[str, object]) -> dict[str, object]:
    from kdive.images.rootfs.specs import source_image_digest
    from kdive.images.rootfs.staged_provenance import read_config_sibling, read_sidecar

    rootfs = directory / "rootfs.qcow2"
    for name in ARTIFACTS:
        _regular(directory / name)
    provenance = read_sidecar(rootfs)
    expected = source_image_digest(
        # Use the selected catalog row, never a filename or source claim from the manifest.
        _selected_source(selection)
    )
    packages = provenance.get("package_versions") if isinstance(provenance, dict) else None
    if (
        not isinstance(provenance, dict)
        or provenance.get("source_image_digest") != expected
        or not isinstance(packages, dict)
        or not packages
        or any(
            not isinstance(name, str) or not name or not isinstance(version, str) or not version
            for name, version in packages.items()
        )
    ):
        raise ValueError("missing or mismatched source/package provenance; rebuild fixture")
    if not read_config_sibling(rootfs):
        raise ValueError("missing or empty kernel config; rebuild fixture")
    return provenance


def _selected_source(selection: dict[str, object]):
    from kdive.images.rootfs.catalog import CloudImageSource

    catalog = selection["catalog"]
    if not isinstance(catalog, dict):
        raise ValueError("invalid catalog selection; rebuild fixture")
    source = cast("dict[str, object]", catalog).get("source")
    if not isinstance(source, dict):
        raise ValueError("invalid catalog source; rebuild fixture")
    url = cast("dict[str, object]", source).get("url")
    sha256 = cast("dict[str, object]", source).get("sha256")
    if not isinstance(url, str) or not isinstance(sha256, str):
        raise ValueError("invalid catalog source; rebuild fixture")
    return CloudImageSource(url=url, sha256=sha256)


def _record(directory: Path, image: str, nvr: str, build_id: str) -> dict[str, object]:
    if not nvr or not build_id:
        raise ValueError("kernel NVR and build ID are required; inspect extracted kernel")
    selection = inputs(image)
    provenance = _provenance(directory, selection)
    record: dict[str, object] = {
        "schema": "kdive.live-vm-fixture.v1",
        "inputs": selection,
        "kernel_nvr": nvr,
        "build_id": build_id,
        "artifacts": {name: _digest(directory / name) for name in ARTIFACTS},
        "provenance": provenance,
    }
    record["fixture_id"] = hashlib.sha256(_canonical(record)).hexdigest()
    return record


def write(directory: Path, image: str, nvr: str, build_id: str) -> None:
    record = _record(directory, image, nvr, build_id)
    payload = _canonical(record) + b"\n"
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ValueError("fixture manifest exceeds size limit; inspect provenance")
    fd, path = tempfile.mkstemp(dir=directory, prefix=".manifest-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
        os.replace(path, directory / MANIFEST)
    finally:
        Path(path).unlink(missing_ok=True)


def verify(directory: Path, image: str, nvr: str) -> bool:
    try:
        manifest = directory / MANIFEST
        _regular(manifest)
        with manifest.open("rb") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            return False
        record = json.loads(raw)
        if not isinstance(record, dict) or set(record) != {
            "schema",
            "inputs",
            "kernel_nvr",
            "build_id",
            "artifacts",
            "provenance",
            "fixture_id",
        }:
            return False
        if record["schema"] != "kdive.live-vm-fixture.v1" or record["kernel_nvr"] != nvr:
            return False
        if not isinstance(record["build_id"], str) or not record["build_id"]:
            return False
        if raw != _canonical(record) + b"\n":
            return False
        identity = record.pop("fixture_id")
        if not isinstance(identity, str) or not SHA256.fullmatch(identity):
            return False
        if hashlib.sha256(_canonical(record)).hexdigest() != identity:
            return False
        if not isinstance(record["artifacts"], dict) or set(record["artifacts"]) != set(ARTIFACTS):
            return False
        if record["inputs"] != inputs(image):
            return False
        if record["provenance"] != _provenance(directory, record["inputs"]):
            return False
        return all(record["artifacts"][name] == _digest(directory / name) for name in ARTIFACTS)
    except OSError, ValueError, KeyError, TypeError, AttributeError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inputs", "write", "verify"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("image")
    parser.add_argument("nvr", nargs="?", default="")
    parser.add_argument("build_id", nargs="?", default="")
    args = parser.parse_args()
    try:
        if args.action == "inputs":
            print(_canonical(inputs(args.image)).decode())
        elif args.action == "write":
            write(args.directory, args.image, args.nvr, args.build_id)
        elif not verify(args.directory, args.image, args.nvr):
            raise ValueError("fixture verification failed; rebuild staged set")
    except (OSError, ValueError) as exc:
        print(f"fixture {args.action}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
