"""Unit tests for the resource-boundary carrier's oracles and guards (#3118)."""

from __future__ import annotations

import json
import shlex
from functools import partial
from pathlib import Path

import pytest

from scripts.coverage_campaign.contract import Contract, build_contract
from scripts.coverage_campaign.evidence import Context, Evidence, InputBindings, Outcome
from scripts.coverage_campaign.results import qualify
from tests.integration.live_stack import resource_limits as rl
from tests.integration.live_stack.evidence import EvidenceWriter, RunIdentity, build_record
from tests.integration.live_stack.scenario import CellRun

_JOB = "0b9ee7c4-5d0e-4c58-9a51-6c1f1b1f0a01"
_OTHER = "7f3b1c55-21a4-4a8e-8c3a-0d0d6a4f9b22"
_RELEASE = "6.18.54-kdive"


def _cell(failure: str, root: Path) -> CellRun:
    cell = next(c for c in rl.cells("local-libvirt") if rl.failure(c) == failure)
    return CellRun(cell, EvidenceWriter(root))


def test_cells_are_the_six_x86_limit_cells() -> None:
    selected = rl.cells()
    assert {(c.provider, rl.failure(c)) for c in selected} == {
        (p, f)
        for p in ("local-libvirt", "remote-libvirt")
        for f in ("minimum-cpu-ram", "insufficient-disk", "slow-boot")
    }
    assert {c.guest_arch for c in selected} == {"x86_64"}


def test_cells_select_by_owner() -> None:
    contract_ids = {c.id for c in build_contract().cells if c.owner == 3118}
    assert {c.id for c in rl.cells()} == contract_ids
    faults = rl.cells(owner=3117)
    assert {(c.provider, rl.failure(c)) for c in faults} == {
        (p, f)
        for p in ("local-libvirt", "remote-libvirt")
        for f in ("worker-interruption", "lost-response", "backing-service")
    }


def test_bindings_select_by_owner(tmp_path: Path) -> None:
    image = tmp_path / "rocky.qcow2"
    image.write_bytes(b"qcow")
    bind = partial(
        rl.bindings,
        "a" * 40,
        root=tmp_path,
        host_os="rocky:10",
        host_arch="x86_64",
        matrix="b" * 64,
        staged=lambda _name: image,
        fixture_at=_unbuilt,
    )
    default = bind()
    assert set(default.cells) == {
        c.id for c in build_contract().cells if c.owner == 3118 and c.provider == "local-libvirt"
    }
    faults = bind(owner=3117)
    assert set(faults.cells) == {c.id for c in rl.cells("local-libvirt", owner=3117)}
    host = rl.RemoteHost("op@host", "default", "rocky:10.2", "x86_64", "kvm")
    remote = rl.remote_bindings(
        "a" * 40,
        root=None,
        matrix="b" * 64,
        host=host,
        digest=lambda *_a: "c" * 64,
        staged=lambda _name: "rocky-10-kdive-remote-base.qcow2",
        owner=3117,
    )
    bound = {c.id for c in rl.cells("remote-libvirt", owner=3117) if c.node_id is not None}
    assert set(remote.cells) == bound


def _unbuilt(*_a: object) -> tuple[Path, dict[str, object]]:
    raise ValueError("no fixture")


def _journal(*lines: tuple[str, str]) -> str:
    # The fixed worker logs JSON lines; the failure text sits inside the message field.
    return "\n".join(
        json.dumps({"event": f"job {job} failed: {category}", "level": "warning"})
        for job, category in lines
    )


def test_attempt_categories_reads_only_this_jobs_lines_in_order() -> None:
    journal = _journal(
        (_JOB, "boot_timeout"), (_OTHER, "transport_failure"), (_JOB, "transport_failure")
    )
    assert rl.attempt_categories(journal, _JOB) == ["boot_timeout", "transport_failure"]


@pytest.mark.parametrize(
    ("lines", "attempts", "terminal", "expected"),
    [
        (["boot_timeout"] * 3, 3, "boot_timeout", ["boot_timeout"] * 3),
        # A lapsed attempt logs nothing: the count no longer reconciles, so nothing is labelled.
        (["boot_timeout"] * 2, 3, "boot_timeout", ["unknown"] * 3),
        # The last line must be the terminal category the job row reports.
        (["boot_timeout", "transport_failure"], 2, "boot_timeout", ["unknown"] * 2),
    ],
)
def test_attempt_record_labels_only_a_reconciled_log(
    lines: list[str], attempts: int, terminal: str, expected: list[str]
) -> None:
    assert rl.attempt_record(lines, attempts, terminal) == expected


def _sampler(*samples: rl.Sample) -> rl.Sampler:
    sampler = rl.Sampler("alloc", 4, set)
    sampler.samples.extend(samples)
    return sampler


def test_sampler_accepts_held_state() -> None:
    sampler = _sampler(rl.Sample(1.0, "granted", 5, ()), rl.Sample(2.0, "active", 5, ("kdive-a",)))
    assert sampler.problems() == []


@pytest.mark.parametrize(
    ("sample", "problem"),
    [
        (rl.Sample(1.0, "released", 5, ()), "allocation released"),
        (rl.Sample(1.0, "releasing", 5, ()), "allocation releasing"),
        (rl.Sample(1.0, "active", 4, ()), "capacity 4 not above 4"),
        (rl.Sample(1.0, "active", 5, ("kdive-a", "kdive-b")), "2 owned domains"),
    ],
)
def test_sampler_flags_early_release_and_duplicates(sample: rl.Sample, problem: str) -> None:
    (found,) = _sampler(sample).problems()
    assert found.startswith(problem)


_XML = """<domain type='kvm'><name>kdive-x</name>
  <memory unit='KiB'>1048576</memory><vcpu placement='static'>1</vcpu>
  <devices><disk type='file'><source file='/var/lib/kdive/x.qcow2'/></disk></devices>
</domain>"""
_SMALL: dict[str, object] = {"name": "small", "vcpus": 1, "memory_mb": 1024, "disk_gb": 10}


def test_prove_minimum_accepts_the_small_floor(tmp_path: Path) -> None:
    run = _cell("minimum-cpu-ram", tmp_path)
    rl.prove_minimum(run, _SMALL, _XML, ["/x"], size=lambda _p: 10 * rl.GIB)
    stored = tmp_path / "artifacts" / run.assertions["minimum-cpu-ram"]
    assert json.loads(stored.read_text())["domain"] == {"vcpu": 1, "memory_mib": 1024}


@pytest.mark.parametrize(
    ("shape", "xml", "disks", "capacity"),
    [
        ({**_SMALL, "vcpus": 2}, _XML, ["/x"], 10 * rl.GIB),
        (_SMALL, _XML.replace(">1</vcpu>", ">2</vcpu>"), ["/x"], 10 * rl.GIB),
        (_SMALL, _XML.replace("1048576", "2097152"), ["/x"], 10 * rl.GIB),
        (_SMALL, _XML, ["/x", "/y"], 10 * rl.GIB),
        (_SMALL, _XML, ["/x"], 20 * rl.GIB),
    ],
    ids=["shape", "vcpu", "memory", "two-overlays", "larger-disk"],
)
def test_prove_minimum_rejects_any_larger_size(
    shape: dict[str, object], xml: str, disks: list[str], capacity: int, tmp_path: Path
) -> None:
    run = _cell("minimum-cpu-ram", tmp_path)
    with pytest.raises(AssertionError):
        rl.prove_minimum(run, shape, xml, disks, size=lambda _p: capacity)
    assert run.assertions == {}


def test_delay_unit_cannot_be_cut_short_and_runs_only_on_the_test_kernel() -> None:
    unit = rl.delay_unit(_RELEASE, 240)
    assert f"ConditionKernelVersion={_RELEASE}\n" in unit
    assert "TimeoutStartSec=infinity\n" in unit and "Type=oneshot\n" in unit
    assert "Before=kdive-ready.service qemu-guest-agent.service\n" in unit
    assert "sleep 240;" in unit


def test_arm_command_writes_the_unit_verbatim() -> None:
    words = shlex.split(rl.arm_command(_RELEASE, 240))
    assert words[:3] == ["printf", "%s", rl.delay_unit(_RELEASE, 240)]
    assert words[3:5] == [">", f"/etc/systemd/system/{rl.DELAY_UNIT}"]


def _entries(written: dict[str, float] | None = None, /, **at: float) -> str:
    """Journal lines received at ``at``; a delay mark carries the uptime it was ``written`` at."""
    wrote = {**at, **(written or {})}
    rows = {
        "delay_start": {
            "MESSAGE": f"{rl.DELAY_MARK}-start {wrote.get('delay_start', 0):.2f}",
            "_SYSTEMD_UNIT": rl.DELAY_UNIT,
        },
        "delay_end": {
            "MESSAGE": f"{rl.DELAY_MARK}-end {wrote.get('delay_end', 0):.2f}",
            "_SYSTEMD_UNIT": rl.DELAY_UNIT,
        },
        "agent_start": {
            "MESSAGE": "Started qemu-guest-agent.service - QEMU Guest Agent.",
            "UNIT": "qemu-guest-agent.service",
        },
        "ready_start": {
            "MESSAGE": "Starting kdive-ready.service - Signal kdive serial readiness",
            "UNIT": "kdive-ready.service",
        },
    }
    return "\n".join(
        json.dumps({**rows[name], "__MONOTONIC_TIMESTAMP": str(int(t * 1e6))})
        for name, t in sorted(at.items(), key=lambda kv: kv[1])
    )


def test_delay_held_when_the_delay_runs_in_full_before_both_dependents() -> None:
    marks = rl.delay_ordering(
        _entries(delay_start=10, delay_end=250.5, agent_start=251, ready_start=252)
    )
    assert marks == {
        "delay_start": 10,
        "uptime_start": 10,
        "delay_end": 250.5,
        "uptime_end": 250.5,
        "agent_start": 251,
        "ready_start": 252,
    }
    assert rl.delay_held(marks, 240) == []


def test_delay_length_is_the_guests_own_stamps_not_journal_receipt() -> None:
    # The start line reached the journal late; the unit's own stamps still span the full delay.
    late = _entries({"delay_start": 10.0}, delay_start=11.0, delay_end=250.0, ready_start=251)
    assert rl.delay_held(rl.delay_ordering(late), 240) == []
    short = _entries({"delay_end": 200.0}, delay_start=10, delay_end=250.0, ready_start=251)
    assert rl.delay_held(rl.delay_ordering(short), 240) == ["delay lasted 190 s"]


@pytest.mark.parametrize(
    ("at", "problem"),
    [
        ({"delay_start": 10, "delay_end": 100, "ready_start": 101}, "delay lasted 90 s"),
        ({"delay_start": 10, "delay_end": 260, "ready_start": 30}, "ready_start preceded"),
        (
            {"delay_start": 10, "delay_end": 260, "agent_start": 5, "ready_start": 261},
            "agent_start preceded",
        ),
        ({"delay_start": 10, "ready_start": 30}, "journal lacks ['delay_end']"),
    ],
    ids=["cut-short", "ready-early", "agent-early", "no-end"],
)
def test_delay_held_refuses_a_delay_off_the_readiness_path(
    at: dict[str, float], problem: str
) -> None:
    (found,) = rl.delay_held(rl.delay_ordering(_entries(**at)), 240)
    assert found.startswith(problem)


def test_inside_delay_needs_margin_under_half_the_window() -> None:
    assert rl.inside_delay(40.0, 900.0) == 100
    assert rl.inside_delay(200.0, 900.0) is None  # 200 + 260 crosses 450
    assert rl.beyond_delay(900.0) > 900 + 60


def test_demote_substeps_keeps_only_the_cells_assertions(tmp_path: Path) -> None:
    run = _cell("minimum-cpu-ram", tmp_path)
    run.assertions = {"minimum-cpu-ram": "a" * 64, "upload": "b" * 64, "modules": "c" * 64}
    rl.demote_substeps(run)
    assert run.assertions == {"minimum-cpu-ram": "a" * 64}
    assert sorted(run.artifacts) == ["b" * 64, "c" * 64]


def test_unmount_refuses_a_changed_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    mount = rl.BoundedMount(Path("/tmp/img"), "/dev/loop7", Path(rl.UPLOADS_DIR) / _JOB)
    monkeypatch.setattr(rl, "mounted_source", lambda _t: "/dev/loop9")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(rl, "_sudo", lambda *argv, **_k: calls.append(argv) or "")
    with pytest.raises(AssertionError, match="mount changed"):
        rl.unmount_bounded(mount)
    assert calls == []


def test_unmount_refuses_while_the_filesystem_holds_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mount = rl.BoundedMount(Path("/tmp/img"), "/dev/loop7", Path(rl.UPLOADS_DIR) / _JOB)
    monkeypatch.setattr(rl, "mounted_source", lambda _t: "/dev/loop7")
    calls: list[tuple[str, ...]] = []

    def sudo(*argv: str, **_k: object) -> str:
        calls.append(argv)
        return "token.qcow2\n" if argv[0] == "find" else ""

    monkeypatch.setattr(rl, "_sudo", sudo)
    with pytest.raises(AssertionError, match="still holds"):
        rl.unmount_bounded(mount)
    assert [c[0] for c in calls] == ["find"]


def test_bindings_bind_every_local_cell_and_leave_unbuilt_inputs_null(tmp_path: Path) -> None:
    image = tmp_path / "rocky.qcow2"
    image.write_bytes(b"qcow")

    def unbuilt(*_a: object) -> tuple[Path, dict[str, object]]:
        raise ValueError("no fixture")

    inputs = rl.bindings(
        "a" * 40,
        root=tmp_path,
        host_os="rocky:10",
        host_arch="x86_64",
        matrix="b" * 64,
        staged=lambda _name: image,
        fixture_at=unbuilt,
    )
    assert set(inputs.cells) == {c.id for c in rl.cells("local-libvirt")}
    for context in inputs.cells.values():
        assert (context.guest_os, context.accelerator) == ("rocky:10", "kvm")
        assert context.image_sha256 == rl.file_sha256(image)
        assert context.kernel_build_id is None


def test_a_failed_mount_undoes_the_loop_and_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: set[Path] = set()
    calls: list[str] = []

    def sudo(*argv: str, **_k: object) -> str:
        calls.append(argv[0] if argv[0] != "losetup" else " ".join(argv[:2]))
        if argv[0] == "mkdir":
            created.add(Path(argv[-1]))
        if argv[0] == "rmdir":
            created.discard(Path(argv[-1]))
        assert argv[0] != "mount", "mount refused"
        return "/dev/loop7\n" if argv[:2] == ("losetup", "--find") else ""

    monkeypatch.setattr(rl, "_sudo", sudo)
    monkeypatch.setattr(rl, "_exists", lambda p: p == Path(rl.UPLOADS_DIR) or p in created)
    monkeypatch.setattr(rl, "mounted_source", lambda _t: None)
    monkeypatch.setattr(rl, "_selinux_context", lambda _p: None)
    with pytest.raises(AssertionError, match="mount refused"):
        rl.mount_bounded(_JOB, tmp_path, 1)
    assert calls == ["losetup --find", "mkfs.ext4", "mkdir", "mount", "rmdir", "losetup -d"]
    assert not created


def test_a_host_without_the_uploads_directory_is_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(rl, "_exists", lambda _p: False)
    with pytest.raises(rl.ScenarioStop) as stop:
        rl.mount_bounded(_JOB, tmp_path, 1)
    assert stop.value.outcome is rl.Outcome.BLOCKED


_HEAD = "a" * 40
_CONTEXT = {
    "host_os": "rocky:10.2",
    "host_arch": "x86_64",
    "guest_os": "rocky:10",
    "guest_arch": "x86_64",
    "accelerator": "kvm",
    "image_sha256": "1" * 64,
    "kernel_sha256": "2" * 64,
    "kernel_source_sha": "3" * 40,
    "kernel_config_sha256": "4" * 64,
    "compiler_id": "5" * 64,
    "kernel_build_id": "6" * 40,
}


def _record(run: CellRun, contract: Contract) -> Evidence:
    roles = {"server": _HEAD, "worker": _HEAD, "reconciler": _HEAD}
    identity = RunIdentity(_HEAD, contract.matrix_sha256, "rocky:10.2", "x86_64", True, roles)
    return build_record(
        run.cell,
        identity,
        outcome=Outcome.SUCCESS,
        context=Context.model_validate(_CONTEXT),
        duration_s=1.0,
        assertions=run.assertions,
        artifacts=run.artifacts,
    )


def test_each_local_cell_qualifies_only_with_its_exact_assertions(tmp_path: Path) -> None:
    contract = build_contract()
    inputs = InputBindings(
        version=1,
        candidate_sha=_HEAD,
        matrix_sha256=contract.matrix_sha256,
        cells={c.id: Context.model_validate(_CONTEXT) for c in rl.cells("local-libvirt")},
    )
    for cell in rl.cells("local-libvirt"):
        run = CellRun(cell, EvidenceWriter(tmp_path))
        for name in (*cell.assertions, "upload", "boot-identity"):  # deep_body's sub-proofs too
            run.prove(name, {"cell": cell.id})
        undemoted = _record(run, contract)
        rl.demote_substeps(run)
        results = qualify(contract, inputs, [_record(run, contract)]).cells
        verdict = next(v for v in results if v.cell.id == cell.id)
        assert verdict.qualified, (cell.id, verdict.reasons)
        results = qualify(contract, inputs, [undemoted]).cells
        assert (
            "required-assertions-mismatch"
            in next(v for v in results if v.cell.id == cell.id).reasons
        )


def test_remote_bindings_cover_only_the_bound_remote_cells() -> None:
    host = rl.RemoteHost("op@host", "default", "rocky:10.2", "x86_64", "kvm")
    inputs = rl.remote_bindings(
        "a" * 40,
        root=None,
        matrix="b" * 64,
        host=host,
        digest=lambda *_a: "c" * 64,
        staged=lambda _name: "rocky-10-kdive-remote-base.qcow2",
    )
    bound = {c.id for c in rl.cells("remote-libvirt") if c.node_id is not None}
    assert set(inputs.cells) == bound and len(bound) == 3
    for context in inputs.cells.values():
        assert (context.host_os, context.guest_os, context.image_sha256) == (
            "rocky:10.2",
            "rocky:10",
            "c" * 64,
        )


@pytest.mark.parametrize(
    ("mount", "backing", "blocked"),
    [
        ("/dev/loop3 ext4\n", "/var/lib/q/pool.img\n", False),
        ("/dev/sda3 xfs\n", "", True),  # the host's own filesystem is never filled
        ("/dev/loop3 ext4\n", "/var/lib/other.img\n", True),  # someone else's loop
        (None, "", True),  # not a mount point: findmnt exits 1
    ],
    ids=["owned", "shared-fs", "foreign-loop", "unmounted"],
)
def test_only_the_owned_loop_pool_may_be_filled(
    monkeypatch: pytest.MonkeyPatch, mount: str | None, backing: str, blocked: bool
) -> None:
    def host_run(_dest: str, *argv: str, **_k: object) -> str:
        if argv[0] == "findmnt":
            assert mount is not None, "provider-host findmnt exited 1"
            return mount
        return backing

    monkeypatch.setattr(rl, "host_run", host_run)
    if blocked:
        with pytest.raises(rl.ScenarioStop):
            rl.bounded_pool("op@host", Path("/var/lib/q/pool"), "/var/lib/q/pool.img")
    else:
        rl.bounded_pool("op@host", Path("/var/lib/q/pool"), "/var/lib/q/pool.img")
