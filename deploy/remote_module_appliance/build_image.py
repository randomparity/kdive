#!/usr/bin/python3
"""Build a deterministic ADR-0585 kernel/initramfs bundle from pinned inputs."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path

ARCHITECTURES = ("x86_64", "ppc64le")
BLOCKED_PARTS = {"bin/sh", "bin/ash", "bin/bash", "usr/bin/sh", "usr/bin/bash"}
BLOCKED_NAMES = {"socket.py", "socket.pyc", "socketserver.py", "socketserver.pyc"}
STARTUP_HOOK_NAMES = {
    "sitecustomize.py",
    "sitecustomize.pyc",
    "usercustomize.py",
    "usercustomize.pyc",
}


def _blocked_runtime_member(relative: str, name: str) -> bool:
    if relative in BLOCKED_PARTS or name in BLOCKED_NAMES:
        return True
    return name.startswith("_socket.") and name.endswith(".so")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _runtime_files(root: Path) -> list[tuple[str, bytes, int]]:
    selected: list[tuple[str, bytes, int]] = []
    required = {"usr/bin/python3", "sbin/depmod"}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.name in STARTUP_HOOK_NAMES or path.suffix == ".pth":
            raise ValueError(f"runtime root contains a Python startup hook: {relative}")
        if _blocked_runtime_member(relative, path.name):
            continue
        include = (
            relative in {"usr/bin/python3", "sbin/depmod"}
            or relative.startswith("lib/")
            or relative.startswith("usr/lib/")
        )
        if not include or path.is_dir():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"runtime member must be a regular file: {relative}")
        mode = 0o555 if relative in required else 0o444
        selected.append((relative, path.read_bytes(), mode))
    present = {name for name, _data, _mode in selected}
    missing = sorted(required - present)
    if missing:
        raise ValueError(f"runtime root lacks required files: {', '.join(missing)}")
    return selected


def _newc_entry(*, inode: int, name: str, data: bytes, mode: int, links: int = 1) -> bytes:
    name_data = name.encode() + b"\x00"
    fields = (
        inode,
        mode,
        0,
        0,
        links,
        0,
        len(data),
        0,
        0,
        0,
        0,
        len(name_data),
        0,
    )
    header = b"070701" + b"".join(f"{field:08x}".encode() for field in fields)
    name_padding = b"\x00" * (-(len(header) + len(name_data)) % 4)
    data_padding = b"\x00" * (-len(data) % 4)
    return header + name_data + name_padding + data + data_padding


def _initramfs(files: list[tuple[str, bytes, int]]) -> bytes:
    directories: set[str] = set()
    for name, _data, _mode in files:
        parent = Path(name).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    output = bytearray()
    inode = 1
    for directory in sorted(directories):
        output.extend(
            _newc_entry(
                inode=inode,
                name=directory,
                data=b"",
                mode=stat.S_IFDIR | 0o555,
                links=2,
            )
        )
        inode += 1
    for name, data, mode in sorted(files):
        output.extend(
            _newc_entry(
                inode=inode,
                name=name,
                data=data,
                mode=stat.S_IFREG | mode,
            )
        )
        inode += 1
    output.extend(
        _newc_entry(
            inode=inode,
            name="TRAILER!!!",
            data=b"",
            mode=stat.S_IFREG,
        )
    )
    output.extend(b"\x00" * (-len(output) % 512))
    return bytes(output)


def _add_bytes(archive: tarfile.TarFile, name: str, data: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def build(*, architecture: str, kernel: Path, runtime_root: Path, output: Path) -> str:
    if architecture not in ARCHITECTURES:
        raise ValueError(f"unsupported architecture: {architecture}")
    kernel_data = kernel.read_bytes()
    init_data = (Path(__file__).with_name("appliance.py")).read_bytes()
    initramfs_files = [("init", init_data, 0o555), *_runtime_files(runtime_root)]
    initramfs = _initramfs(initramfs_files)
    members = [
        ("image/vmlinuz", kernel_data, 0o444),
        ("image/initramfs.cpio", initramfs, 0o444),
    ]
    manifest = {
        "format": "kdive-remote-module-appliance-v1",
        "architecture": architecture,
        "initramfs_files": [
            {"path": name, "sha256": _sha256(data), "size_bytes": len(data)}
            for name, data, _mode in sorted(initramfs_files)
        ],
        "files": [
            {"path": name, "sha256": _sha256(data), "size_bytes": len(data)}
            for name, data, _mode in members
        ],
    }
    manifest_data = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    with temporary.open("wb") as raw:
        with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, data, mode in sorted(members):
                _add_bytes(archive, name, data, mode)
            _add_bytes(archive, "manifest.json", manifest_data, 0o444)
        raw.flush()
        os.fsync(raw.fileno())
    temporary.replace(output)
    return _sha256(output.read_bytes())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--architecture", choices=ARCHITECTURES, required=True)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    digest = build(
        architecture=arguments.architecture,
        kernel=arguments.kernel,
        runtime_root=arguments.runtime_root,
        output=arguments.output,
    )
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
