"""Broken/fixed kernel corpus cases, outcome checks and bindings (#2817).

Each ``kernel-corpus`` cell names a case and a revision. A case runs one reproducer on two
fixture builds from ``fixtures/kernel/corpus.toml``: the fix commit and its first parent, built
by ``scripts/kernel_fixtures.py build --baseline case-<n>-<revision>`` into
``$KDIVE_FIXTURE_ROOT/case-<n>-<revision>``. The human notes under
``docs/development/kernel-test-cases/`` chose the cases; nothing here reads them.
``python -m tests.integration.live_stack.kernel_corpus bindings --candidate SHA --out FILE``
writes the qualifier's expected ``Context`` for every native cell. The live test is
``tests/integration/test_kernel_corpus_live.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import platform
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kdive.domain.lifecycle.crash_signatures import first_crash_signature
from kdive.images.rootfs.catalog import load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Context, InputBindings
from scripts.kernel_fixtures import identity
from tests.integration.live_stack.deep_lifecycle import (
    FIXTURE_ROOT_ENV,
    KERNEL_PROBE,
    Fixture,
    InstalledKernel,
    boot_kernel_sha256,
    bound_kernel,
    file_sha256,
    gnu_build_id,
    load_fixture,
)
from tests.integration.live_stack.evidence import os_identity
from tests.integration.live_stack.image_smoke import (
    Endpoint,
    GuestIdentity,
    os_matches,
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
    full_artifact_text,
    ok,
    scalar,
)
from tests.mcp.json_data import data_mapping

# The Fedora representative; both revisions of every case boot on it.
IMAGE = "fedora-kdive-ready-44"


@dataclass(frozen=True)
class Case:
    """One reproducer, applied unchanged to both revisions of a case."""

    note: str
    cmdline: str | None = None
    expected_boot_failure: Mapping[str, str] | None = None
    probe: str = KERNEL_PROBE


# Every directory under /proc/bus/pci as `path:nlink:subdirectories:other entries;`.
_PCI_INVENTORY = (
    KERNEL_PROBE + "; printf 'pci_dirs='; find /proc/bus/pci -type d | while read -r d; do "
    'printf "%s:%s:%s:%s;" "$d" "$(stat -c %h "$d")" '
    '"$(find "$d" -mindepth 1 -maxdepth 1 -type d | wc -l)" '
    '"$(find "$d" -mindepth 1 -maxdepth 1 ! -type d | wc -l)"; done; echo'
)
CASES = {
    "case-1": Case(
        note="05-dcache-dhash-entries-oob-read.md",
        cmdline="dhash_entries=1",
        expected_boot_failure={"kind": "console_crash", "pattern": "RIP: 0010:__d_lookup"},
        probe=KERNEL_PROBE + '; printf "cmdline=%s\\n" "$(cat /proc/cmdline)"',
    ),
    "case-2": Case(note="16-proc-parent-nlink-leak.md", probe=_PCI_INVENTORY),
}
# `sym+0xOFF/0xSIZE`; a `? ` prefix marks an unreliable stack guess, which is not resolved.
_FRAME = re.compile(r"(\? )?(?<![\w.])([A-Za-z_][\w.]*)\+0x([0-9a-f]+)/0x([0-9a-f]+)")


def native_cells(arch: str | None = None) -> list[Cell]:
    """The ``kernel-corpus`` cells whose guest is ``arch`` (the host's)."""
    arch = arch or platform.machine()
    return [
        cell
        for cell in build_contract().cells
        if cell.operation == "kernel-corpus" and cell.guest_arch == arch
    ]


def case_of(cell: Cell) -> str:
    """``case-1`` of ``kernel-corpus/case-1/broken``."""
    return cell.scenario_id.split("/")[1]


def revision(cell: Cell) -> str:
    """``broken`` or ``fixed``."""
    return cell.scenario_id.split("/")[2]


def selection(case: str, rev: str) -> str:
    """The ``corpus.toml`` selection and fixture directory name of one revision."""
    return f"{case}-{rev}"


def same_config(manifests: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """The shared configuration digests of a case's two verified manifests."""
    digests = {
        name: {m["artifacts"][name] for m in manifests.values()}
        for name in ("input.config", ".config")
    }
    for name, values in digests.items():
        assert len(values) == 1, f"the case's revisions differ in {name}: {sorted(values)}"
    return {name: next(iter(values)) for name, values in digests.items()}


def symbol_sizes(system_map: str) -> dict[str, set[int]]:
    """Each ``System.map`` symbol's kallsyms size: the distance to the next higher address."""
    entries = sorted(
        (int(fields[0], 16), fields[2])
        for line in system_map.splitlines()
        if len(fields := line.split()) == 3
    )
    addresses = sorted({address for address, _ in entries})
    following = dict(zip(addresses, addresses[1:], strict=False))
    sizes: dict[str, set[int]] = {}
    for address, name in entries:
        if address in following:
            sizes.setdefault(name, set()).add(following[address] - address)
    return sizes


def oops_frames(console: str) -> list[str]:
    """The first oops's RIP line and call-trace lines, in console order."""
    lines = console.splitlines()
    start = next((i for i, line in enumerate(lines) if "RIP: 0010:" in line), None)
    assert start is not None, "the console shows no x86_64 RIP line"
    trace = next((i for i in range(start, len(lines)) if "Call Trace:" in lines[i]), None)
    assert trace is not None, "the console shows no call trace after the RIP line"
    end = next((i for i in range(trace, len(lines)) if "</TASK>" in lines[i]), None)
    assert end is not None, "the call trace has no closing </TASK>"
    return [lines[start], *lines[trace + 1 : end]]


def resolve_frames(frames: list[str], sizes: Mapping[str, set[int]]) -> list[dict[str, object]]:
    """Resolve every reliable ``symbol+0xOFF/0xSIZE`` frame against the build's symbol sizes."""
    resolved = []
    for line in frames:
        match = _FRAME.search(line)
        if match is None or match[1]:
            continue
        name, offset, size = match[2], int(match[3], 16), int(match[4], 16)
        assert size in sizes.get(name, set()), f"{name}/{size:#x} is not in this build's map"
        assert offset < size, f"{name}+{offset:#x} is past its size {size:#x}"
        resolved.append({"symbol": name, "offset": offset, "size": size})
    assert len(resolved) >= 3, f"only {len(resolved)} oops frame(s) resolved"
    return resolved


def crash_evidence(console: str, system_map: str, release: str) -> dict[str, object]:
    """Case-1 broken: the dcache oops on this build, resolved against its ``System.map``."""
    for needle in (
        f"Linux version {release} ",
        "Dentry cache hash table entries: 1 ",
        "BUG: unable to handle page fault for address",
        "RIP: 0010:__d_lookup",
    ):
        assert needle in console, f"the broken console lacks {needle!r}"
    frames = oops_frames(console)
    resolved = resolve_frames(frames, symbol_sizes(system_map))
    assert resolved[0]["symbol"] in {"__d_lookup", "__d_lookup_rcu"}, resolved[0]
    banner = next(line for line in console.splitlines() if f"Linux version {release} " in line)
    return {"release": release, "banner": banner.strip(), "frames": resolved}


def clean_console(console: str, release: str) -> dict[str, object]:
    """Case-1 fixed: the fixed build booted with two hash buckets and no crash signature."""
    assert f"Linux version {release} " in console, "the console names another kernel"
    assert "Dentry cache hash table entries: 2 " in console, "the fix did not set two buckets"
    signature = first_crash_signature(console)
    assert signature is None, f"the fixed console shows {signature[0]!r}"
    return {"release": release, "dentry_hash_entries": 2, "crash_signature": None}


def pci_inventory(value: str) -> list[tuple[str, int, int, int]]:
    """Parse ``path:nlink:subdirectories:files;`` records from the case-2 probe."""
    records = []
    for record in filter(None, value.split(";")):
        path, nlink, dirs, files = record.rsplit(":", 3)
        records.append((path, int(nlink), int(dirs), int(files)))
    return records


def nlink_outcome(inventory: list[tuple[str, int, int, int]]) -> str | None:
    """``broken``, ``fixed``, or ``None`` when the inventory is empty, file-less or mixed."""
    if not any(files for *_, files in inventory):
        return None
    if all(nlink == 2 + dirs for _, nlink, dirs, _ in inventory):
        return "fixed"
    if all(nlink >= 2 + dirs + files for _, nlink, dirs, files in inventory):
        return "broken"
    return None


async def corpus_body(
    run: CellRun,
    op: LiveStackClient,
    system_id: str,
    owned: list[str],
    *,
    project: str,
    entry: GuestIdentity,
    tree: Path,
    manifest: dict[str, Any],
    tmp: Path,
    installed_kernel: InstalledKernel,
) -> None:
    """Upload, install and boot the cell's revision with its case's reproducer, then judge it."""
    case, rev = CASES[case_of(run.cell)], revision(run.cell)
    endpoint, key = await asyncio.wait_for(
        authorize_ssh(op, system_id, tmp, "kernel-corpus"), timeout=900
    )
    before = await asyncio.to_thread(ssh_probe, endpoint, key)
    assert os_matches(entry, before), f"guest {before.get('ID')} is not catalog {entry.distro}"
    run.observed |= {"guest_os": f"{entry.distro}:{entry.version}", "guest_arch": entry.arch}
    investigation = ok(
        await scalar(op, "investigations.open", project=project, title="kernel corpus"), "open"
    ).object_id
    try:
        run_id = ok(
            await scalar(
                op,
                "runs.create",
                investigation_id=investigation,
                system_id=system_id,
                build_profile=build_profile(entry.arch),
                **(
                    {"expected_boot_failure": dict(case.expected_boot_failure)}
                    if case.expected_boot_failure
                    else {}
                ),
            ),
            "create-run",
        ).object_id
        upload = tmp / "upload"
        await _upload(run, op, run_id, tree, manifest, upload)
        got = await _install_and_boot(op, run_id, case.cmdline)
        installed = await asyncio.to_thread(
            installed_kernel, system_id, endpoint, key, manifest["release"]
        )
        digest, path = installed
        if path is not None:
            owned.append(path)
        assert digest == boot_kernel_sha256(tree, manifest["arch"]), "installed kernel differs"
        run.observed["kernel_sha256"] = digest
        console = await full_artifact_text(op, got.refs["console"], "console")
        if case.expected_boot_failure is not None and rev == "broken":
            outcome = _expected_crash(got.data)
            system_map = (tree / "System.map").read_text(encoding="utf-8")
            evidence = crash_evidence(console, system_map, manifest["release"])
            # The guest never reaches userspace; the domain runs the uploaded boot image, whose
            # vmlinux build ID runs.complete_build accepted.
            run.observed["kernel_build_id"] = manifest["build_id"]
        else:
            endpoint = await ssh_endpoint(op, system_id)
            outcome, evidence = await _observe(
                run, endpoint, key, before["boot_id"], case, manifest, got
            )
            if case.expected_boot_failure is not None:
                evidence |= clean_console(console, manifest["release"])
        run.prove(rev, outcome)
        run.prove(
            "debug-or-crash-evidence", {"run_id": run_id, "kernel_sha256": digest, **evidence}
        )
    finally:
        closed = await scalar(
            op, "investigations.close", investigation_id=investigation, summary="corpus finished"
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
    assert record["build_id"] == manifest["build_id"], "uploaded vmlinux is not the fixture's"
    assert record["result"]["status"] == "succeeded", record["result"]["status"]
    run.observed |= {
        "kernel_source_sha": manifest["source"]["commit"],
        "kernel_config_sha256": file_sha256(upload / "effective_config"),
        "compiler_id": identity(manifest["toolchain"]),
    }


async def _install_and_boot(op: LiveStackClient, run_id: str, cmdline: str | None) -> ToolResponse:
    """Drain ``runs.install`` (with the case's ``cmdline``) and ``runs.boot``; read the Run."""
    install = {"cmdline": cmdline} if cmdline else {}
    env = ok(await scalar(op, "runs.install", run_id=run_id, **install), "install")
    await drain_job(op, "install", env.object_id)
    env = ok(await scalar(op, "runs.boot", run_id=run_id), "boot")
    await drain_job(op, "boot", env.object_id)
    got = ok(await scalar(op, "runs.get", run_id=run_id), "read-back")
    steps = data_mapping(got, "steps")
    assert (steps.get("install"), steps.get("boot")) == ("succeeded", "succeeded"), steps
    assert got.refs.get("console"), "the booted Run names no console artifact"
    return got


def _expected_crash(data: Mapping[str, object]) -> dict[str, object]:
    """The declared crash was matched, so the boot succeeded as ``expected_crash_observed``."""
    line = data.get("expected_boot_failure_matched_line")
    assert isinstance(line, str) and "RIP: 0010:__d_lookup" in line, f"matched line {line!r}"
    assert "boot_outcome" not in data, f"a ready boot reproduced nothing: {data['boot_outcome']}"
    return {"boot": "expected_crash_observed", "matched_line": line}


async def _observe(
    run: CellRun,
    endpoint: Endpoint,
    key: Path,
    boot_id: str,
    case: Case,
    manifest: dict[str, Any],
    got: ToolResponse,
) -> tuple[dict[str, object], dict[str, object]]:
    """A ready boot of the fixture's build, judged by the case's probe: (outcome, evidence)."""
    outcome = data_mapping(got, "boot_outcome")
    assert outcome.get("outcome") == "ready", outcome
    assert "expected_boot_failure_matched_line" not in got.data, "the fixed boot crashed"
    after = await asyncio.to_thread(probe_new_boot, endpoint, key, boot_id, case.probe)
    build_id = gnu_build_id(base64.b64decode(after.get("notes", "")))
    assert after.get("release") == manifest["release"], f"running release {after.get('release')}"
    assert build_id == manifest["build_id"], f"running build ID {build_id!r} is not the fixture's"
    run.observed["kernel_build_id"] = build_id
    rev = revision(run.cell)
    evidence: dict[str, object] = {"release": after["release"], "build_id": build_id}
    if case.cmdline is not None:
        assert case.cmdline in after.get("cmdline", "").split(), "the reproducer was not applied"
        evidence["cmdline"] = case.cmdline
        return {"boot": "ready", "cmdline": case.cmdline}, evidence
    inventory = pci_inventory(after.get("pci_dirs", ""))
    verdict = nlink_outcome(inventory)
    assert verdict == rev, f"/proc/bus/pci link counts read {verdict!r}, not {rev}"
    evidence["pci_dirs"] = [list(record) for record in inventory]
    return {"boot": "ready", "nlink": verdict}, evidence


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
    """The expected ``Context`` of every native corpus cell; null where unstaged or unbuilt."""
    entry = load_rootfs_catalog()[IMAGE]
    image = staged(IMAGE)
    cells = {
        cell.id: Context.model_validate(
            {
                "host_os": host_os,
                "host_arch": host_arch,
                "guest_os": f"{entry.distro}:{entry.version}",
                "guest_arch": entry.arch,
                "accelerator": cell.accelerator,
                "image_sha256": file_sha256(image) if image is not None else None,
                **bound_kernel(root, selection(case_of(cell), revision(cell)), host_arch, fixture),
            }
        )
        for cell in native_cells(host_arch)
    }
    return InputBindings(version=1, candidate_sha=candidate, matrix_sha256=matrix, cells=cells)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write kernel-corpus input bindings.")
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
