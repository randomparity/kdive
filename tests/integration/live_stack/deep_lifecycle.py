"""Deep-lifecycle representatives, kernel inputs, guest probes and bindings (#2809, ADR-0715).

Each ``deep-lifecycle`` cell names a family and a pinned baseline. The family is run on one
representative catalog image (:data:`REPRESENTATIVES`); the baseline is the fixture tree
``$KDIVE_FIXTURE_ROOT/<baseline>`` that ``scripts/kernel_fixtures.py build`` produced and
``verify`` checks. ``python -m tests.integration.live_stack.deep_lifecycle bindings --candidate SHA
--out FILE`` writes the qualifier's expected ``Context`` for every local deep cell native to this
host, after the images are staged and the fixtures built. The live test is
``tests/integration/test_deep_lifecycle_live.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import platform
import struct
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kdive.images.rootfs.catalog import RootfsCatalogEntry, load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Context, InputBindings
from scripts.kernel_fixtures import identity, verify
from tests.integration.live_stack.evidence import os_identity
from tests.integration.live_stack.image_smoke import (
    PROBE,
    os_matches,
    parse_probe,
    ssh,
    staged_image,
)
from tests.integration.live_stack.scenario import (
    CellRun,
    authorize_ssh,
    probe_new_boot,
    ssh_endpoint,
    ssh_probe,
)
from tests.integration.live_stack.spine import (
    build_and_upload_kernel,
    build_profile,
    drain_job,
    ok,
    scalar,
)
from tests.mcp.json_data import data_mapping

FIXTURE_ROOT_ENV = "KDIVE_FIXTURE_ROOT"
# A loadable module of every fixture build: CONFIG_BLK_DEV_LOOP=m in fixtures/kernel/debug.config.
MODULE = "loop"
# One catalog image per (family, arch); Fedora and Enterprise Linux stay distinct (#2803 req 7).
REPRESENTATIVES = {
    ("debian", "x86_64"): "debian-kdive-ready-13",
    ("fedora", "x86_64"): "fedora-kdive-ready-44",
    ("fedora", "ppc64le"): "fedora-kdive-ready-44-ppc64le",
    ("enterprise", "x86_64"): "rocky-kdive-ready-10",
    ("enterprise", "ppc64le"): "rocky-kdive-ready-10-ppc64le",
    ("suse", "x86_64"): "opensuse-leap-kdive-ready-15.6",
}
KERNEL_PROBE = (
    PROBE + '; printf "release=%s\\nnotes=%s\\n" "$(uname -r)" "$(base64 -w0 /sys/kernel/notes)"'
)
MODULE_PROBE = (
    f"modprobe {MODULE} && p=$(modinfo -n {MODULE}) && "
    'printf "initstate=%s\\npath=%s\\nvermagic=%s\\nsha256=%s\\n" '
    f'"$(cat /sys/module/{MODULE}/initstate)" "$p" "$(modinfo -F vermagic {MODULE})" '
    '"$(sha256sum "$p" | cut -d" " -f1)"'
)
_GNU_BUILD_ID = 3

Fixture = Callable[[Path, str, str], tuple[Path, dict[str, Any]]]


def native_cells(arch: str | None = None) -> list[Cell]:
    """The local-libvirt ``deep-lifecycle`` cells whose guest is ``arch`` (the host's)."""
    arch = arch or platform.machine()
    return [
        cell
        for cell in build_contract().cells
        if cell.operation == "deep-lifecycle"
        and cell.provider == "local-libvirt"
        and cell.guest_arch == arch
    ]


def baseline(cell: Cell) -> str:
    """The pinned baseline (``longterm`` or ``stable``) a deep cell boots."""
    return cell.scenario_id.rsplit("/", 1)[1]


def representative(cell: Cell) -> str:
    return REPRESENTATIVES[(str(cell.family), str(cell.guest_arch))]


def load_fixture(root: Path, name: str, arch: str) -> tuple[Path, dict[str, Any]]:
    """The verified fixture tree for baseline ``name``; ``ValueError`` when absent or changed."""
    tree = root / name
    return tree, verify(tree, baseline=name, arch=arch)


def _privileged_sha256(path: str) -> str:
    result = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["sudo", "-n", "sha256sum", "--", path],
        capture_output=True,
        text=True,
        timeout=120.0,
        check=False,
    )
    assert result.returncode == 0, f"cannot read {path}: sudo sha256sum exited {result.returncode}"
    return result.stdout.split()[0]


def file_sha256(path: str | Path) -> str:
    """SHA-256 of ``path``; a worker-owned file the test cannot open is read with ``sudo -n``."""
    try:
        with open(path, "rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except PermissionError:
        return _privileged_sha256(str(path))


def boot_kernel_sha256(tree: Path, arch: str) -> str:
    """The digest of the boot member ``spine.combined_kernel_tar`` uploads for ``arch``."""
    if arch == "x86_64":
        return file_sha256(tree / "arch/x86/boot/bzImage")
    with tempfile.TemporaryDirectory() as scratch:  # ppc64le uploads a stripped vmlinux copy
        stripped = Path(scratch) / "vmlinuz"
        subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
            ["strip", "-s", str(tree / "vmlinux"), "-o", str(stripped)], check=True
        )
        return file_sha256(stripped)


def kernel_inputs(tree: Path, manifest: dict[str, Any]) -> dict[str, str]:
    """The five kernel ``Context`` fields bound from a verified fixture manifest."""
    return {
        "kernel_sha256": boot_kernel_sha256(tree, manifest["arch"]),
        "kernel_source_sha": manifest["source"]["commit"],
        "kernel_config_sha256": manifest["artifacts"][".config"],
        "compiler_id": identity(manifest["toolchain"]),
        "kernel_build_id": manifest["build_id"],
    }


def gnu_build_id(notes: bytes) -> str | None:
    """The GNU build ID in a little-endian ELF note section such as ``/sys/kernel/notes``."""
    offset = 0
    while offset + 12 <= len(notes):
        namesz, descsz, kind = struct.unpack_from("<III", notes, offset)
        name_at = offset + 12
        desc_at = name_at + (namesz + 3) // 4 * 4
        offset = desc_at + (descsz + 3) // 4 * 4
        if kind == _GNU_BUILD_ID and notes[name_at : name_at + namesz] == b"GNU\0":
            return notes[desc_at : desc_at + descsz].hex()
    return None


def staged_module(modstage: Path, guest_path: str) -> Path:
    """The ``modules_install`` copy under ``modstage`` of the guest module at ``guest_path``.

    ``guest_path`` is guest output; a path outside ``modstage`` fails rather than naming a host
    file.
    """
    _, sep, rest = guest_path.partition("/lib/modules/")
    path = (modstage / "lib/modules" / rest).resolve()
    assert sep and rest and path.is_relative_to(modstage.resolve()), f"module path {guest_path!r}"
    return path


def _bound_kernel(root: Path | None, name: str, arch: str, fixture: Fixture) -> dict[str, str]:
    """Kernel inputs of baseline ``name``, or none when no verified fixture exists."""
    if root is None:
        return {}
    try:
        tree, manifest = fixture(root, name, arch)
    except ValueError:
        return {}
    return kernel_inputs(tree, manifest)


def bindings(
    candidate: str,
    *,
    root: Path | None,
    host_os: str,
    host_arch: str,
    matrix: str,
    staged: Callable[[str], Path | None] = staged_image,
    fixture: Fixture = load_fixture,
) -> InputBindings:
    """The expected ``Context`` of every native local deep cell; null where unstaged or unbuilt."""
    catalog = load_rootfs_catalog()
    kernels: dict[str, dict[str, str]] = {}
    cells = {}
    for cell in native_cells(host_arch):
        name = representative(cell)
        entry = catalog[name]
        if baseline(cell) not in kernels:
            kernels[baseline(cell)] = _bound_kernel(root, baseline(cell), host_arch, fixture)
        image = staged(name)
        cells[cell.id] = Context.model_validate(
            {
                "host_os": host_os,
                "host_arch": host_arch,
                "guest_os": f"{entry.distro}:{entry.version}",
                "guest_arch": entry.arch,
                "accelerator": cell.accelerator,
                "image_sha256": file_sha256(image) if image is not None else None,
                **kernels[baseline(cell)],
            }
        )
    return InputBindings(version=1, candidate_sha=candidate, matrix_sha256=matrix, cells=cells)


async def deep_body(
    run: CellRun,
    op: LiveStackClient,
    system_id: str,
    owned: list[str],
    *,
    project: str,
    entry: RootfsCatalogEntry,
    tree: Path,
    manifest: dict[str, Any],
    tmp: Path,
    staged_kernel: Callable[[str], str],
) -> None:
    """Upload → install → boot → reconnect → build identity → module load, on a ready System.

    Provider-neutral: ``staged_kernel(system_id)`` names the installed kernel file on the
    provider host; it joins ``owned``, the paths the caller's cleanup proves absent.
    """
    port, key = await asyncio.wait_for(
        authorize_ssh(op, system_id, tmp, "deep-lifecycle"), timeout=900
    )
    before = await asyncio.to_thread(ssh_probe, port, key)
    assert before.get("uid") == "0", f"ssh as root reported uid {before.get('uid')!r}"
    assert os_matches(entry, before), f"guest {before.get('ID')} is not catalog {entry.distro}"
    run.observed |= {"guest_os": f"{entry.distro}:{entry.version}", "guest_arch": entry.arch}
    investigation = ok(
        await scalar(op, "investigations.open", project=project, title="deep lifecycle"), "open"
    ).object_id
    try:
        upload = tmp / "upload"
        run_id = ok(
            await scalar(
                op,
                "runs.create",
                investigation_id=investigation,
                system_id=system_id,
                build_profile=build_profile(entry.arch),
            ),
            "create-run",
        ).object_id
        await _upload(run, op, run_id, tree, manifest, upload)
        await _install_and_boot(run, op, run_id, system_id, owned, tree, manifest, staged_kernel)
        port = await ssh_endpoint(op, system_id)
        after = await asyncio.to_thread(probe_new_boot, port, key, before["boot_id"], KERNEL_PROBE)
        assert after.get("uid") == "0", "ssh after boot did not authenticate as root"
        run.prove("reconnect", {"user": "root", "same_key": True, "boot_id_changed": True})
        _prove_boot_identity(run, after, manifest, upload)
        await _prove_module(run, port, key, after["release"], upload / "modstage")
    finally:
        closed = await scalar(
            op,
            "investigations.close",
            investigation_id=investigation,
            summary="deep lifecycle finished",
        )
        assert closed.status == "closed", f"investigation not closed: {closed.status}"


async def _upload(
    run: CellRun,
    op: LiveStackClient,
    run_id: str,
    tree: Path,
    manifest: dict[str, Any],
    upload: Path,
) -> None:
    # with_vmlinux sets the build's debuginfo reference, which is what makes install inject
    # lib/modules into the guest; require_network keeps the reconnect path in the kernel.
    await build_and_upload_kernel(
        op,
        run_id=run_id,
        arch=manifest["arch"],
        kernel_tree=tree,
        evidence_dir=upload,
        with_vmlinux=True,
        require_network=True,
        root_fs="ext4",
    )
    record = json.loads((upload / "upload.json").read_text(encoding="utf-8"))
    status = record["result"]["status"]
    assert record["build_id"] == manifest["build_id"], "uploaded vmlinux is not the fixture's"
    assert status == "succeeded", f"runs.complete_build returned {status}"
    run.prove(
        "upload",
        {
            "declared": {a["name"]: a["sha256"] for a in record["artifacts"]},
            "build_id": record["build_id"],
            "complete_build": status,
        },
    )


async def _install_and_boot(
    run: CellRun,
    op: LiveStackClient,
    run_id: str,
    system_id: str,
    owned: list[str],
    tree: Path,
    manifest: dict[str, Any],
    staged_kernel: Callable[[str], str],
) -> None:
    for step in ("install", "boot"):
        env = ok(await scalar(op, f"runs.{step}", run_id=run_id), step)
        await drain_job(op, step, env.object_id)
    steps = data_mapping(ok(await scalar(op, "runs.get", run_id=run_id), "read-back"), "steps")
    assert (steps.get("install"), steps.get("boot")) == ("succeeded", "succeeded"), steps
    kernel = staged_kernel(system_id)
    owned.append(kernel)
    digest = file_sha256(kernel)
    assert digest == boot_kernel_sha256(tree, manifest["arch"]), (
        "the installed kernel is not the uploaded boot member"
    )
    run.observed["kernel_sha256"] = digest
    run.prove("install", {"steps": dict(steps), "kernel_sha256": digest})


def _prove_boot_identity(
    run: CellRun, after: dict[str, str], manifest: dict[str, Any], upload: Path
) -> None:
    build_id = gnu_build_id(base64.b64decode(after.get("notes", "")))
    assert after.get("release") == manifest["release"], (
        f"running release {after.get('release')!r} is not {manifest['release']!r}"
    )
    assert build_id == manifest["build_id"], f"running build ID {build_id!r} is not the fixture's"
    run.observed |= {
        "kernel_build_id": build_id,
        "kernel_source_sha": manifest["source"]["commit"],
        "kernel_config_sha256": file_sha256(upload / "effective_config"),
        "compiler_id": identity(manifest["toolchain"]),
    }
    run.prove("boot-identity", {"release": after["release"], "build_id": build_id})


async def _prove_module(run: CellRun, port: int, key: Path, release: str, modstage: Path) -> None:
    result = await asyncio.to_thread(ssh, port, key, MODULE_PROBE)
    assert result.returncode == 0, f"module probe exit {result.returncode}: {result.stderr[-500:]}"
    module = parse_probe(result.stdout)
    assert module.get("initstate") == "live", f"{MODULE} initstate {module.get('initstate')!r}"
    vermagic = module.get("vermagic", "").split()
    assert vermagic[:1] == [release], f"{MODULE} vermagic {vermagic[:1]} is not {release}"
    staged = file_sha256(staged_module(modstage, module.get("path", "")))
    assert module.get("sha256") == staged, f"loaded {MODULE} is not the uploaded module"
    run.prove("modules", {"module": MODULE, "initstate": "live", "sha256": staged})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write deep-lifecycle input bindings.")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("bindings")
    write.add_argument("--candidate", required=True)
    write.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    root = os.environ.get(FIXTURE_ROOT_ENV)
    inputs = bindings(
        args.candidate,
        root=Path(root) if root else None,
        host_os=os_identity(Path("/etc/os-release").read_text(encoding="utf-8")),
        host_arch=platform.machine(),
        matrix=build_contract().matrix_sha256,
    )
    args.out.write_text(inputs.model_dump_json(indent=1) + "\n", encoding="utf-8")
    bound = sum(c.kernel_build_id is not None for c in inputs.cells.values())
    print(f"wrote {len(inputs.cells)} binding(s); {bound} with a verified kernel fixture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
