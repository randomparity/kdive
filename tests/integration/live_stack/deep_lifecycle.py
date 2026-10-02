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
import hashlib
import os
import platform
import struct
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kdive.images.rootfs.catalog import load_rootfs_catalog
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Context, InputBindings
from scripts.kernel_fixtures import identity, verify
from tests.integration.live_stack.evidence import os_identity
from tests.integration.live_stack.image_smoke import PROBE, staged_image

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
    ("suse", "x86_64"): "opensuse-tumbleweed-kdive-ready",
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
