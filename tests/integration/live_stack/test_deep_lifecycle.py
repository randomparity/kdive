"""Unit tests for the shared cell frame and the deep-lifecycle inputs (#2809); no stack."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from kdive.images.rootfs.catalog import load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell, build_contract, image_family
from scripts.kernel_fixtures import identity
from tests.integration.live_stack import deep_lifecycle
from tests.integration.live_stack.deep_lifecycle import (
    baseline,
    bindings,
    boot_kernel_sha256,
    gnu_build_id,
    kernel_inputs,
    native_cells,
    prove_install,
    representative,
    staged_module,
)
from tests.integration.live_stack.evidence import EvidenceWriter, RunIdentity
from tests.integration.live_stack.scenario import CellRun

_IDENTITY = RunIdentity(
    candidate_sha="a" * 40,
    matrix_sha256="b" * 64,
    host_os="ubuntu:26.04",
    host_arch="x86_64",
    clean=True,
    deployed_roles={},
)


def test_cell_run_context_merges_observed(tmp_path: Path) -> None:
    cell = Cell("c", "s", 1, "op", "obs", ("effect",))
    run = CellRun(cell, EvidenceWriter(tmp_path))
    assert run.context(_IDENTITY).accelerator == "none"
    run.observed |= {"guest_os": "fedora:44", "guest_arch": "x86_64", "accelerator": "kvm"}
    context = run.context(_IDENTITY)
    assert (context.host_os, context.host_arch) == ("ubuntu:26.04", "x86_64")
    assert (context.guest_os, context.accelerator) == ("fedora:44", "kvm")


def _note(name: bytes, kind: int, desc: bytes) -> bytes:
    def pad(data: bytes) -> bytes:
        return data + b"\0" * (-len(data) % 4)

    return struct.pack("<III", len(name), len(desc), kind) + pad(name) + pad(desc)


def test_every_deep_cell_has_a_family_representative() -> None:
    catalog = load_rootfs_catalog()
    cells = [
        c
        for c in build_contract().cells
        if c.operation == "deep-lifecycle" and c.provider == "local-libvirt"
    ]
    assert {c.guest_arch for c in cells} == {"x86_64", "ppc64le"}
    for cell in cells:
        entry = catalog[representative(cell)]
        assert (image_family(entry), entry.arch) == (cell.family, cell.guest_arch)
    assert len(native_cells("x86_64")) == 8


def test_gnu_build_id_reads_the_gnu_note() -> None:
    notes = _note(b"Xen\0", 3, b"\x01\x02") + _note(b"GNU\0", 3, bytes.fromhex("abcdef0123"))
    assert gnu_build_id(notes) == "abcdef0123"
    assert gnu_build_id(_note(b"GNU\0", 1, b"\x00" * 4)) is None
    assert gnu_build_id(b"") is None


def test_staged_module_stays_under_modstage(tmp_path: Path) -> None:
    expected = (tmp_path / "lib/modules/R/kernel/x.ko").resolve()
    assert staged_module(tmp_path, "/usr/lib/modules/R/kernel/x.ko") == expected
    assert staged_module(tmp_path, "/lib/modules/R/kernel/x.ko") == expected
    for hostile in ("/lib/modules/../../../etc/passwd", "/opt/x.ko", ""):
        with pytest.raises(AssertionError):
            staged_module(tmp_path, hostile)


def _manifest(arch: str = "x86_64") -> dict[str, Any]:
    return {
        "arch": arch,
        "source": {"commit": "c" * 40},
        "artifacts": {".config": "d" * 64},
        "toolchain": {"gcc": "gcc 15"},
        "build_id": "e" * 40,
        "release": "6.18.54",
    }


def test_kernel_inputs_come_from_the_manifest(tmp_path: Path) -> None:
    bzimage = tmp_path / "arch/x86/boot/bzImage"
    bzimage.parent.mkdir(parents=True)
    bzimage.write_bytes(b"kernel")
    inputs = kernel_inputs(tmp_path, _manifest())
    assert inputs == {
        "kernel_sha256": hashlib.sha256(b"kernel").hexdigest(),
        "kernel_source_sha": "c" * 40,
        "kernel_config_sha256": "d" * 64,
        "compiler_id": identity({"gcc": "gcc 15"}),
        "kernel_build_id": "e" * 40,
    }


@pytest.mark.skipif(shutil.which("strip") is None, reason="needs binutils strip")
def test_ppc64le_kernel_digest_is_the_stripped_vmlinux(tmp_path: Path) -> None:
    elf = Path(sys.executable).resolve()
    shutil.copy(elf, tmp_path / "vmlinux")
    stripped = tmp_path / "stripped"
    subprocess.run(["strip", "-s", str(elf), "-o", str(stripped)], check=True)
    digest = boot_kernel_sha256(tmp_path, "ppc64le")
    assert digest == hashlib.sha256(stripped.read_bytes()).hexdigest()


def test_bindings_bind_every_native_cell(tmp_path: Path) -> None:
    def fixture(root: Path, baseline: str, arch: str) -> tuple[Path, dict[str, Any]]:
        if baseline == "stable":
            raise ValueError("unbuilt")
        tree = root / baseline
        (tree / "arch/x86/boot").mkdir(parents=True, exist_ok=True)
        (tree / "arch/x86/boot/bzImage").write_bytes(b"kernel")
        return tree, _manifest(arch)

    image = tmp_path / "image.qcow2"
    image.write_bytes(b"image")
    inputs = bindings(
        "a" * 40,
        root=tmp_path,
        host_os="ubuntu:26.04",
        host_arch="x86_64",
        matrix="b" * 64,
        staged=lambda _name: image,
        fixture=fixture,
    )
    catalog = load_rootfs_catalog()
    assert set(inputs.cells) == {c.id for c in native_cells("x86_64")}
    for cell in native_cells("x86_64"):
        context = inputs.cells[cell.id]
        entry = catalog[representative(cell)]
        assert context.guest_os == f"{entry.distro}:{entry.version}"
        assert context.image_sha256 == hashlib.sha256(b"image").hexdigest()
        assert context.accelerator == "kvm"
        built = baseline(cell) == "longterm"
        assert (context.kernel_build_id is not None) is built
        assert (context.kernel_sha256 is not None) is built


def test_prove_install_owns_the_path_and_checks_the_digest(tmp_path: Path) -> None:
    cell = Cell("c", "s", 1, "deep-lifecycle", "obs", ("install",))
    run = CellRun(cell, EvidenceWriter(tmp_path))
    owned: list[str] = []
    steps = {"install": "succeeded", "boot": "succeeded"}
    prove_install(run, steps, ("d" * 64, "/kernels/vmlinuz"), owned, "d" * 64)
    assert owned == ["/kernels/vmlinuz"]
    assert run.observed["kernel_sha256"] == "d" * 64 and "install" in run.assertions
    prove_install(run, steps, ("d" * 64, None), owned, "d" * 64)
    assert owned == ["/kernels/vmlinuz"]
    with pytest.raises(AssertionError, match="not the uploaded boot member"):
        prove_install(run, steps, ("e" * 64, None), owned, "d" * 64)


def test_install_and_boot_use_the_step() -> None:
    calls: list[tuple[str, str]] = []

    class _Op:
        async def call_tool(self, name: str, **args: object) -> ToolResponse:
            if name == "jobs.wait":
                return ToolResponse.success(str(args["job_id"]), "succeeded")
            assert name == "runs.get", f"unexpected operator call {name}"
            steps = {"install": "succeeded", "boot": "succeeded"}
            return ToolResponse.success("r", "succeeded", data={"steps": steps})

    async def step(name: str, run_id: str) -> ToolResponse:
        calls.append((name, run_id))
        return ToolResponse.success(f"job-{name}", "queued")

    op = cast(LiveStackClient, _Op())
    steps = asyncio.run(deep_lifecycle._install_and_boot(op, "r", step))
    assert calls == [("install", "r"), ("boot", "r")]
    assert steps["boot"] == "succeeded"
