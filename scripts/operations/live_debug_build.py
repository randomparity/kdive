"""Kernel archive, upload, provisioning, and boot workflow for live-debug."""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import os
import shutil
import subprocess  # noqa: S404 - fixed dev-tool argv  # nosec B404
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from kdive.mcp.dev_harness import LiveStackClient
from kdive.mcp.resources.external_build_contract import EXTERNAL_BUILD_CONTRACT_URI
from scripts.operations.live_debug_transport import _call, _poll, _SchemaResolver, _wait_job

_ARCHIVE_STEP_TIMEOUT_S = 900
KERNEL_SRC = Path(os.environ.get("KDIVE_KERNEL_SRC", str(Path.home() / "src" / "linux")))


@functools.cache
def _required_executable(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} executable is required on PATH")
    return path


def _run_build_step(argv: list[str], *, step: str) -> None:
    """Run a fixed-argv build/compress step, bounded so a stall fails loudly.

    Args:
        argv: The command to run (fixed argv, never a shell string).
        step: A short label used in the timeout error (e.g. ``"tar"``).

    Raises:
        RuntimeError: If the step runs longer than ``_ARCHIVE_STEP_TIMEOUT_S``.
        subprocess.CalledProcessError: If the step exits non-zero.
    """
    try:
        subprocess.run(  # noqa: S603 - fixed argv, no shell  # nosec B603
            argv, check=True, timeout=_ARCHIVE_STEP_TIMEOUT_S
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{step} exceeded {_ARCHIVE_STEP_TIMEOUT_S}s and was aborted; install pigz for "
            "parallel gzip or raise _ARCHIVE_STEP_TIMEOUT_S for very large kernel trees"
        ) from exc


def _combined_kernel_tar(kernel_src: Path, dest_dir: Path) -> Path:
    """Cut the ADR-0234 combined ``kernel`` artifact from a built x86_64 kernel tree.

    Reproduces the ``external-build-upload`` recipe: stage the module tree with
    ``make modules_install`` into ``dest_dir`` and tar ``arch/x86/boot/bzImage`` (renamed to
    ``boot/vmlinuz``, listed first so ``lib/modules`` lands inside the validator's decompress scan
    bound) plus ``lib/modules`` into one gzip tar, dropping the ``build``/``source`` back-symlinks.
    Uses ``pigz`` for parallel gzip when it is on ``PATH`` (it emits a standard gzip stream), else
    falls back to tar's built-in single-threaded gzip. Both slow steps are bounded (see
    :func:`_run_build_step`).

    This reference client packages **x86_64** kernels only. For ppc64le and other arches, follow the
    manual packaging recipe in ``docs/operating/external-build-upload.md``.

    Args:
        kernel_src: A *built* x86_64 kernel tree (must contain ``arch/x86/boot/bzImage``).
        dest_dir: A scratch directory to stage modules and write ``kernel.tar.gz`` into.

    Returns:
        The path to the combined ``kernel.tar.gz`` under ``dest_dir``.

    Raises:
        RuntimeError: If ``kernel_src`` holds no built x86_64 bzImage, or a build step stalls.
    """
    bzimage = kernel_src / "arch/x86/boot/bzImage"
    if not bzimage.is_file():
        raise RuntimeError(
            f"no built x86_64 bzImage at {bzimage}: this reference client packages x86_64 kernels "
            f"only. Build the tree at {kernel_src} (or point KDIVE_KERNEL_SRC at a built x86_64 "
            "tree). For ppc64le and other arches, follow the manual packaging recipe in "
            "docs/operating/external-build-upload.md."
        )
    modstage = dest_dir / "modstage"
    _run_build_step(
        [
            _required_executable("make"),
            "-C",
            str(kernel_src),
            "modules_install",
            f"INSTALL_MOD_PATH={modstage}",
        ],
        step="modules_install",
    )
    tar_path = dest_dir / "kernel.tar.gz"
    pigz = shutil.which("pigz")
    compress = ["-I", pigz, "-cf"] if pigz else ["-czf"]
    _run_build_step(
        [
            _required_executable("tar"),
            *compress,
            str(tar_path),
            "--exclude=*/build",
            "--exclude=*/source",
            "--transform=s|^arch/x86/boot/bzImage$|boot/vmlinuz|",
            "-C",
            str(kernel_src),
            "arch/x86/boot/bzImage",
            "-C",
            str(modstage),
            "lib/modules",
        ],
        step="tar",
    )
    return tar_path


def _sha256_b64(path: Path) -> str:
    """The base64 SHA-256 the upload manifest declares (S3 ``x-amz-checksum-sha256``)."""
    return base64.b64encode(hashlib.sha256(path.read_bytes()).digest()).decode()


async def _put_presigned(item: dict[str, Any], path: Path) -> None:
    """PUT ``path`` to a ``create_run_upload`` item's presigned URL with exactly its headers.

    Sends only ``data.required_headers`` (the presign signs a fixed header set; any extra header —
    a default ``Content-Type`` in particular — breaks the signature).
    """
    url = item["refs"]["upload_url"]
    raw_headers = item.get("data", {}).get("required_headers", {})
    headers = {k: str(v) for k, v in raw_headers.items()} if isinstance(raw_headers, dict) else {}
    async with httpx.AsyncClient(timeout=180.0) as http:
        resp = await http.put(url, content=path.read_bytes(), headers=headers)
        resp.raise_for_status()


def _elf_build_id(vmlinux: Path) -> str:
    """The GNU build-id note of ``vmlinux``, as ``runs.complete_build`` requires it.

    Raises:
        RuntimeError: ``readelf`` prints no build-id note for ``vmlinux``.
    """
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell  # nosec B603
        [_required_executable("readelf"), "-n", str(vmlinux)],
        check=True,
        capture_output=True,
        text=True,
        timeout=_ARCHIVE_STEP_TIMEOUT_S,
    ).stdout
    for line in out.splitlines():
        if "Build ID:" in line:
            return line.split("Build ID:", 1)[1].strip()
    raise RuntimeError(
        f"no GNU build-id note in {vmlinux}; rebuild with CONFIG_DEBUG_INFO_DWARF5=y"
    )


async def _upload_kernel(
    client: LiveStackClient,
    schemas: _SchemaResolver,
    *,
    run_id: str,
    kernel_tar: Path,
    vmlinux: Path,
) -> None:
    """Drive the external-upload lane for one Run: discover -> declare -> PUT -> complete_build.

    Uploads the combined ``kernel`` tar **and** the uncompressed ``vmlinux`` ELF. The vmlinux is
    what publishes the Run's ``debuginfo_ref``; without it every gdb-MI op short-circuits with
    ``no_debuginfo``, so the stepping proof cannot run at all.
    """
    contract = json.loads(await client.read_text_resource(EXTERNAL_BUILD_CONTRACT_URI))
    upload_contracts = contract.get("upload_contracts", {})
    run_contract = upload_contracts.get("run", {}) if isinstance(upload_contracts, dict) else {}
    accepted = (
        set(run_contract.get("accepted_names", [])) if isinstance(run_contract, dict) else set()
    )
    missing = {"kernel", "vmlinux"} - accepted
    if missing:
        raise RuntimeError(f"upload contract no longer accepts run artifact(s) {missing}")
    sources = {"kernel": kernel_tar, "vmlinux": vmlinux}
    decls = [
        {"name": name, "sha256": _sha256_b64(path), "size_bytes": path.stat().st_size}
        for name, path in sources.items()
    ]
    upload = await _call(
        client, "artifacts.create_run_upload", {"run_id": run_id, "artifacts": decls}, schemas
    )
    items = {(it.get("data") or {}).get("name"): it for it in upload.get("items", [])}
    for name, path in sources.items():
        if name not in items:
            raise RuntimeError(f"create_run_upload returned no {name!r} upload item")
        await _put_presigned(items[name], path)
    # A declared vmlinux is rejected unless complete_build carries its matching ELF build-id.
    await _call(
        client,
        "runs.complete_build",
        {"run_id": run_id, "build_id": _elf_build_id(vmlinux)},
        schemas,
    )


# --- lifecycle to a stopped session --------------------------------------------------------


async def _find_booted_run(client: LiveStackClient, schemas: _SchemaResolver) -> str | None:
    """A Run already booted on a ready System, or None.

    ``systems.list`` does not populate ``active_run.state``, so confirm each ready System's
    booted Run via ``systems.get`` (which does carry it).
    """
    systems = await _call(client, "systems.list", {}, schemas)
    for item in systems.get("items", []):
        if (item.get("data") or {}).get("state") != "ready" and item.get("status") != "ready":
            continue
        detail = await _call(client, "systems.get", {"system_id": item["object_id"]}, schemas)
        active = (detail.get("data") or {}).get("active_run") or {}
        if active.get("id") and active.get("state") in {"booted", "succeeded", "ready"}:
            return str(active["id"])
    return None


async def _provision_boot_run(
    client: LiveStackClient, schemas: _SchemaResolver, *, project: str
) -> str:
    """Full lifecycle: investigation -> allocation -> provision -> upload/install/boot -> run_id."""
    resources = await _call(client, "resources.list", {}, schemas)
    resource_id = (resources["items"][0]["object_id"]) if resources.get("items") else None
    if not resource_id:
        raise RuntimeError("no registered local-libvirt resource; run setup-local-libvirt.sh")
    inv = await _call(
        client, "investigations.open", {"project": project, "title": "live-debug"}, schemas
    )
    inv_id = inv["object_id"]
    print(f"  investigation {inv_id}", file=sys.stderr)
    await _call(
        client,
        "allocations.request",
        {
            "project": project,
            "shape": "medium",
            "window": 24,
            "resource": {"mode": "id", "resource_id": resource_id},
        },
        schemas,
    )
    allocs = await _call(client, "allocations.list", {"project": project}, schemas)
    alloc_id = allocs["items"][0]["object_id"]
    profile = {
        "schema_version": 1,
        "arch": "x86_64",
        "boot_method": "direct-kernel",
        "kernel_source_ref": "linux-live-debug",
        "provider": {
            "local-libvirt": {
                "rootfs": {
                    "kind": "catalog",
                    "provider": "local-libvirt",
                    "name": "fedora-kdive-ready-44",
                },
                "debug": {"gdbstub": True},
            }
        },
    }
    sysenv = await _call(
        client, "systems.provision", {"allocation_id": alloc_id, "profile": profile}, schemas
    )
    system_id = sysenv["data"]["system_id"]
    await _poll(
        client,
        "systems.get",
        {"system_id": system_id},
        schemas,
        done={"ready", "failed", "cordoned"},
        timeout_sec=600,
        label="provision",
    )
    print(f"  system {system_id} (alloc {alloc_id})", file=sys.stderr)
    run = await _call(
        client,
        "runs.create",
        {
            "investigation_id": inv_id,
            "system_id": system_id,
            "build_profile": {"schema_version": 1, "arch": "x86_64"},
        },
        schemas,
    )
    run_id = run["object_id"]
    with tempfile.TemporaryDirectory(prefix="kdive-live-debug-") as scratch:
        kernel_tar = _combined_kernel_tar(KERNEL_SRC, Path(scratch))
        vmlinux = KERNEL_SRC / "vmlinux"
        if not vmlinux.is_file():
            raise RuntimeError(
                f"no vmlinux at {vmlinux}; the debug tier needs the uncompressed ELF with DWARF "
                "(build the tree with CONFIG_DEBUG_INFO_DWARF5=y)"
            )
        print(
            f"  uploading {kernel_tar.name} ({kernel_tar.stat().st_size} bytes) "
            f"+ vmlinux ({vmlinux.stat().st_size} bytes)",
            file=sys.stderr,
        )
        await _upload_kernel(client, schemas, run_id=run_id, kernel_tar=kernel_tar, vmlinux=vmlinux)
    for step, terminal in (("runs.install", 600), ("runs.boot", 600)):
        kind = step.split(".")[1]
        await _call(client, step, {"run_id": run_id}, schemas)
        await _wait_job(client, schemas, kind=kind, timeout_sec=terminal)
    print(f"  run {run_id} booted", file=sys.stderr)
    return str(run_id)
