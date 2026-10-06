"""Catalog image-smoke probes and independent bindings (#2808, ADR-0715).

``python -m tests.integration.live_stack.image_smoke bindings --candidate SHA --out FILE`` writes
the qualifier's expected ``Context`` for every ``image-smoke`` cell native to this host. It runs
after the images are staged with ``examples/local-libvirt/build-image.sh`` and before the smoke,
so each binding names the bytes the operator staged. The rest of the module is the guest-side
evidence the live test (``tests/integration/test_image_smoke_live.py``) collects.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import shlex
import subprocess  # noqa: S404 - fixed ssh argv, no shell  # nosec B404
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, Protocol

from kdive.images.families import family_for
from kdive.images.rootfs.catalog import RootfsCatalogEntry, load_rootfs_catalog
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.model import StagedPathSource
from kdive.inventory.path import systems_toml_path
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Context, InputBindings
from tests.integration.live_stack.evidence import key_values, os_identity

# One line per fact, then the guest's os-release, all in KEY=VALUE form.
PROBE = (
    'printf "uid=%s\\nboot_id=%s\\nmachine=%s\\n" "$(id -u)" '
    '"$(cat /proc/sys/kernel/random/boot_id)" "$(uname -m)"; cat /etc/os-release'
)
# os-release IDs that differ from the catalog distro name.
_DISTRO_FOR_ID = {"centos": "centos-stream"}
_RPM_FAMILIES = frozenset({"rhel", "suse"})
_SSH_RETRY_S = 5.0


class Endpoint(NamedTuple):
    """Where an SSH probe connects: the host and port ``systems.ssh_info`` returns."""

    host: str
    port: int


class GuestIdentity(Protocol):
    """The OS a guest must report: a catalog entry or a remote base image (#2810)."""

    @property
    def distro(self) -> str: ...
    @property
    def version(self) -> str: ...
    @property
    def arch(self) -> str: ...


def native_cells(arch: str | None = None) -> list[Cell]:
    """The contract's ``image-smoke`` cells whose guest architecture is ``arch`` (the host's)."""
    arch = arch or platform.machine()
    return [
        cell
        for cell in build_contract().cells
        if cell.operation == "image-smoke" and cell.guest_arch == arch
    ]


def parse_probe(stdout: str) -> dict[str, str]:
    """The :data:`PROBE` facts plus the guest's os-release fields."""
    return key_values(stdout)


def os_matches(entry: GuestIdentity, probe: dict[str, str]) -> bool:
    """Whether the guest is the catalog row's distro, version (or a point release) and arch."""
    distro = _DISTRO_FOR_ID.get(probe.get("ID", ""), probe.get("ID", ""))
    version = probe.get("VERSION_ID", "")
    return (
        distro == entry.distro
        and (version == entry.version or version.startswith(f"{entry.version}."))
        and probe.get("machine") == entry.arch
    )


def toolchain_command(entry: RootfsCatalogEntry) -> str:
    """Prove the build row's package set is installed and ``make``/``gcc`` build a program."""
    packages = family_for(entry.family).packages("build", entry.distro, entry.version)
    query = "rpm -q" if entry.family in _RPM_FAMILIES else "dpkg -s"
    return (
        f"{query} {' '.join(shlex.quote(p) for p in packages)} >/dev/null && "
        'cd "$(mktemp -d)" && '
        "printf 'int main(void){return 0;}\\n' > t.c && "
        "printf 't: t.c\\n\\tgcc -o t t.c\\n' > Makefile && make t && ./t"
    )


def ssh(
    endpoint: Endpoint, key: Path, command: str, *, deadline_s: float = 300.0
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` as root at ``endpoint``, retrying while sshd is down."""
    argv = [
        "ssh",
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=10",
        "-p",
        str(endpoint.port),
        f"root@{endpoint.host}",
        "--",
        command,
    ]
    deadline = time.monotonic() + deadline_s
    while True:
        result = subprocess.run(  # noqa: S603 - fixed argv  # nosec B603
            argv, capture_output=True, text=True, timeout=120.0, check=False
        )
        # 255 is ssh's own failure (sshd not answering yet); anything else is the command's.
        if result.returncode != 255 or time.monotonic() >= deadline:
            return result
        time.sleep(_SSH_RETRY_S)


def staged_image(name: str) -> Path | None:
    """The staged qcow2 that ``systems.toml`` registers for local-libvirt image ``name``."""
    doc = load_inventory_optional(systems_toml_path())
    for image in doc.image if doc is not None else ():
        if image.provider == "local-libvirt" and image.name == name:
            source = image.source
            if isinstance(source, StagedPathSource) and Path(source.path).is_file():
                return Path(source.path)
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def bindings(
    candidate: str,
    *,
    host_os: str,
    host_arch: str,
    matrix: str,
    staged: Callable[[str], Path | None] = staged_image,
) -> InputBindings:
    """The expected ``Context`` of every native ``image-smoke`` cell (null digest: not staged)."""
    catalog = load_rootfs_catalog()
    cells = {}
    for cell in native_cells(host_arch):
        entry = catalog[str(cell.image)]
        image = staged(str(cell.image))
        cells[cell.id] = Context.model_validate(
            {
                "host_os": host_os,
                "host_arch": host_arch,
                "guest_os": f"{entry.distro}:{entry.version}",
                "guest_arch": entry.arch,
                "accelerator": cell.accelerator,
                "image_sha256": _sha256(image) if image is not None else None,
            }
        )
    return InputBindings(version=1, candidate_sha=candidate, matrix_sha256=matrix, cells=cells)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write image-smoke input bindings.")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("bindings")
    write.add_argument("--candidate", required=True)
    write.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    inputs = bindings(
        args.candidate,
        host_os=os_identity(Path("/etc/os-release").read_text(encoding="utf-8")),
        host_arch=platform.machine(),
        matrix=build_contract().matrix_sha256,
    )
    args.out.write_text(inputs.model_dump_json(indent=1) + "\n", encoding="utf-8")
    staged = sum(c.image_sha256 is not None for c in inputs.cells.values())
    print(f"wrote {len(inputs.cells)} binding(s); {staged} with a staged image")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
