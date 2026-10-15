"""Unit tests for the kernel corpus cases, outcome checks and bindings (#2817); no stack."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from kdive.images.rootfs.catalog import load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient
from kdive.mcp.responses import ToolResponse
from scripts.kernel_fixtures import ROOT, selections
from tests.integration.live_stack import kernel_corpus
from tests.integration.live_stack.kernel_corpus import (
    CASES,
    IMAGE,
    bindings,
    case_of,
    clean_console,
    crash_evidence,
    native_cells,
    nlink_outcome,
    pci_inventory,
    resolve_frames,
    revision,
    same_config,
    selection,
    symbol_sizes,
)

_MAP = """\
ffffffff81000000 T __d_lookup
ffffffff810000f0 T __pfx_d_lookup
ffffffff81000100 T d_lookup
ffffffff81000140 t lookup_dcache
ffffffff810001c0 T kernel_init
ffffffff81000200 T __pfx_kernel_init
"""
_OOPS = """\
[    0.000000] Linux version 6.19.0 (builder@kernel-fixture) #1 SMP
[    0.010000] Dentry cache hash table entries: 1 (order: 0, 8 bytes, linear)
[    0.200000] BUG: unable to handle page fault for address: ff1bfb9d3c8af3c0
[    0.200000] RIP: 0010:__d_lookup+0x3c/0xf0
[    0.200000] Call Trace:
[    0.200000]  <TASK>
[    0.200000]  d_lookup+0x24/0x40
[    0.200000]  ? kernel_init+0x40/0x40
[    0.200000]  lookup_dcache+0x1c/0x80
[    0.200000]  kernel_init+0x15/0x40
[    0.200000]  </TASK>
[    0.200000] Kernel panic - not syncing: Attempted to kill init! exitcode=0x00000009
"""


def test_native_cells_are_the_four_cases_on_the_representative() -> None:
    cells = native_cells("x86_64")
    assert sorted((case_of(c), revision(c)) for c in cells) == [
        (case, rev) for case in sorted(CASES) for rev in ("broken", "fixed")
    ]
    entry = load_rootfs_catalog()[IMAGE]
    assert (entry.distro, entry.arch) == ("fedora", "x86_64")
    assert {selection(case_of(c), revision(c)) for c in cells} <= set(selections())


def test_corpus_selections_pin_full_commits_and_existing_notes() -> None:
    notes = ROOT / "docs/development/kernel-test-cases"
    for case, spec in CASES.items():
        broken, fixed = (selections()[selection(case, rev)] for rev in ("broken", "fixed"))
        assert broken["note"] == fixed["note"] == spec.note
        assert (notes / spec.note).is_file()
        assert broken["commit"] != fixed["commit"]
        assert all(len(s["commit"]) == 40 for s in (broken, fixed))


def test_same_config_requires_identical_digests() -> None:
    def manifest(config: str) -> dict[str, Any]:
        return {"artifacts": {"input.config": "a" * 64, ".config": config}}

    assert same_config({"broken": manifest("b" * 64), "fixed": manifest("b" * 64)}) == {
        "input.config": "a" * 64,
        ".config": "b" * 64,
    }
    with pytest.raises(AssertionError, match=r"differ in \.config"):
        same_config({"broken": manifest("b" * 64), "fixed": manifest("c" * 64)})


def test_symbol_sizes_are_distances_to_the_next_address() -> None:
    sizes = symbol_sizes(_MAP)
    assert sizes["__d_lookup"] == {0xF0} and sizes["d_lookup"] == {0x40}
    assert "__pfx_kernel_init" not in sizes  # the last symbol has no known size


def test_crash_evidence_resolves_reliable_frames() -> None:
    evidence = crash_evidence(_OOPS, _MAP, "6.19.0")
    assert str(evidence["banner"]).endswith("Linux version 6.19.0 (builder@kernel-fixture) #1 SMP")
    frames = cast(list[dict[str, object]], evidence["frames"])
    assert [f["symbol"] for f in frames] == [
        "__d_lookup",
        "d_lookup",
        "lookup_dcache",
        "kernel_init",
    ]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("d_lookup+0x24/0x40", "d_lookup+0x24/0x50"), "not in this build's map"),
        (("lookup_dcache+0x1c/0x80", "other_fn+0x1c/0x80"), "not in this build's map"),
        (("kernel_init+0x15/0x40", "kernel_init+0x40/0x40"), "past its size"),
        (("Linux version 6.19.0 ", "Linux version 6.19.1 "), "Linux version 6.19.0"),
        (("entries: 1 ", "entries: 2 "), "Dentry cache hash table entries: 1"),
        (("RIP: 0010:__d_lookup+", "RIP: 0010:d_lookup+"), "RIP: 0010:__d_lookup"),
        (("Call Trace:", "Trace:"), "no call trace"),
    ],
)
def test_crash_evidence_rejects_other_builds_and_outcomes(
    change: tuple[str, str], message: str
) -> None:
    with pytest.raises(AssertionError, match=message):
        crash_evidence(_OOPS.replace(*change), _MAP, "6.19.0")


def test_too_few_resolved_frames_fail() -> None:
    frames = ["RIP: 0010:__d_lookup+0x3c/0xf0", "? kernel_init+0x40/0x40"]
    with pytest.raises(AssertionError, match="only 1 oops frame"):
        resolve_frames(frames, symbol_sizes(_MAP))


def test_clean_console_needs_two_buckets_and_no_crash() -> None:
    clean = "Linux version 6.19.0 (b@h)\nDentry cache hash table entries: 2 (order: 0)\n"
    assert clean_console(clean, "6.19.0")["dentry_hash_entries"] == 2
    with pytest.raises(AssertionError, match="two buckets"):
        clean_console(clean.replace("entries: 2", "entries: 1"), "6.19.0")
    with pytest.raises(AssertionError, match="Oops:"):
        clean_console(clean + "Oops: 0000 [#1] SMP\n", "6.19.0")
    with pytest.raises(AssertionError, match="another kernel"):
        clean_console(clean, "6.19.1")


@pytest.mark.parametrize(
    ("probe", "verdict"),
    [
        ("/proc/bus/pci:3:1:1;/proc/bus/pci/00:2:0:6;", "fixed"),
        ("/proc/bus/pci:4:1:1;/proc/bus/pci/00:8:0:6;", "broken"),
        ("/proc/bus/pci:5:1:1;/proc/bus/pci/00:9:0:6;", "broken"),
        ("/proc/bus/pci:3:1:1;/proc/bus/pci/00:8:0:6;", None),
        ("/proc/bus/pci:4:1:1;/proc/bus/pci/00:5:0:6;", None),
        ("/proc/bus/pci:3:1:0;/proc/bus/pci/00:2:0:0;", None),
        ("", None),
    ],
)
def test_nlink_outcome(probe: str, verdict: str | None) -> None:
    assert nlink_outcome(pci_inventory(probe)) == verdict


def test_pci_inventory_keeps_colons_in_paths() -> None:
    assert pci_inventory("/proc/bus/pci/0000:00:2:0:6;") == [("/proc/bus/pci/0000:00", 2, 0, 6)]


def test_bindings_bind_each_cell_to_its_own_revision(tmp_path: Path) -> None:
    def fixture(root: Path, name: str, arch: str) -> tuple[Path, dict[str, Any]]:
        if name == "case-2-fixed":
            raise ValueError("unbuilt")
        tree = root / name
        (tree / "arch/x86/boot").mkdir(parents=True, exist_ok=True)
        (tree / "arch/x86/boot/bzImage").write_bytes(name.encode())
        manifest = {
            "arch": arch,
            "source": {"commit": hashlib.sha1(name.encode()).hexdigest()},  # noqa: S324
            "artifacts": {".config": "d" * 64},
            "toolchain": {"gcc": "gcc 16"},
            "build_id": "e" * 40,
        }
        return tree, manifest

    image = tmp_path / "image.qcow2"
    image.write_bytes(b"image")
    inputs = bindings(
        "a" * 40,
        root=tmp_path,
        host_os="fedora:44",
        host_arch="x86_64",
        matrix="b" * 64,
        staged=lambda _name: image,
        fixture=fixture,
    )
    assert set(inputs.cells) == {c.id for c in native_cells("x86_64")}
    for cell in native_cells("x86_64"):
        context = inputs.cells[cell.id]
        name = selection(case_of(cell), revision(cell))
        assert context.guest_os == "fedora:44" and context.accelerator == "kvm"
        assert context.image_sha256 == hashlib.sha256(b"image").hexdigest()
        if name == "case-2-fixed":
            assert context.kernel_sha256 is None and context.kernel_build_id is None
        else:
            assert context.kernel_sha256 == hashlib.sha256(name.encode()).hexdigest()


@pytest.mark.parametrize("cmdline", ["dhash_entries=1", None])
def test_install_passes_the_case_cmdline(cmdline: str | None) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class _Op:
        async def call_tool(self, name: str, **args: object) -> ToolResponse:
            calls.append((name, args))
            if name == "runs.get":
                steps = {"install": "succeeded", "boot": "succeeded"}
                return ToolResponse.success(
                    "r", "succeeded", data={"steps": steps}, refs={"console": "c"}
                )
            return ToolResponse.success(f"job-{name}", "succeeded")

    asyncio.run(kernel_corpus._install_and_boot(cast(LiveStackClient, _Op()), "r", cmdline))
    install = next(args for name, args in calls if name == "runs.install")
    assert install == ({"run_id": "r", "cmdline": cmdline} if cmdline else {"run_id": "r"})


def test_expected_crash_requires_the_declared_match_and_no_ready_outcome() -> None:
    line = "[    0.2] RIP: 0010:__d_lookup+0x3c/0xf0"
    data: dict[str, object] = {"expected_boot_failure_matched_line": line}
    assert kernel_corpus._expected_crash(data)["matched_line"] == line
    with pytest.raises(AssertionError, match="matched line"):
        kernel_corpus._expected_crash({})
    with pytest.raises(AssertionError, match="reproduced nothing"):
        kernel_corpus._expected_crash({**data, "boot_outcome": {"outcome": "ready"}})
