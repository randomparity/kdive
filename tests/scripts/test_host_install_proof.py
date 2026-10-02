"""Pure decisions of the host-installation evidence producer (ADR-0716)."""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.coverage_campaign.contract import Cell, Contract, build_contract, digest
from scripts.coverage_campaign.evidence import Context, Evidence, InputBindings, Outcome
from scripts.coverage_campaign.results import qualify
from scripts.host_install_proof import (
    ARCH_LANE,
    CLEAN_STEPS,
    REPEAT_STEPS,
    SSH_OPTIONS,
    HostFacts,
    PhaseRecord,
    Step,
    compose,
    console_has_release,
    family_matches,
    kernel_member_digest,
    label_confined,
    main,
    merge,
    package_digest,
    parse_host,
    qemu_pid,
    read_phase,
    run_step,
    scp_argv,
    ssh_argv,
    step_script,
    validate_name,
    validate_target,
)

SHA = "a" * 40
DIGEST = "b" * 64
NODE = "tests/integration/test_host_install_live.py::test_installed_host_boots_pinned_kernel"
CELL_ID = "host-install/local-libvirt/x86_64/debian"
ROLES = ("server", "worker", "reconciler", "authority")


@pytest.fixture(scope="module")
def contract() -> Contract:
    return build_contract()


@pytest.fixture(scope="module")
def cell(contract: Contract) -> Cell:
    (found,) = [c for c in contract.cells if c.id == CELL_ID]
    return replace(found, node_id=NODE)


def _context(**changes: object) -> Context:
    values: dict[str, object] = {
        "host_os": "ubuntu:26.04",
        "host_arch": "x86_64",
        "guest_os": "fedora:44",
        "guest_arch": "x86_64",
        "accelerator": "kvm",
        "image_sha256": DIGEST,
        "kernel_sha256": DIGEST,
        "kernel_source_sha": SHA,
        "kernel_config_sha256": DIGEST,
        "compiler_id": DIGEST,
        "kernel_build_id": "c" * 40,
    }
    values.update(changes)
    return Context.model_validate(values)


def _phase(phase: str, **changes: object) -> PhaseRecord:
    values: dict[str, object] = {
        "phase": phase,
        "passed": True,
        "booted": True,
        "deployed": dict.fromkeys(ROLES, SHA),
        "context": _context(),
        "host_enforcing": True,
        "guest_label": "system_u:system_r:svirt_t:s0:c1,c2",
        "guest_confined": True,
        "prerequisites": {"worker-imports": True, "lifecycle-socket": True},
        "cleanup": {"system-torn-down": True, "domain-absent": True},
        "failures": [],
    }
    values.update(changes)
    return PhaseRecord.model_validate(values)


def _host(**changes: object) -> HostFacts:
    values: dict[str, object] = {
        "os": "ubuntu:26.04",
        "arch": "x86_64",
        "mode": "Y",
        "kvm": True,
        "sudo": True,
        "markers": (),
    }
    values.update(changes)
    return HostFacts(**values)


def _steps(*, failed: str | None = None, timeout: str | None = None) -> list[Step]:
    steps = []
    for name in (*CLEAN_STEPS, *REPEAT_STEPS):
        code: int | str = 0
        if name == failed:
            code = 1
        if name == timeout:
            code = "timeout"
        steps.append(Step(name, code, 1.0, DIGEST))
        if code != 0:
            break
    return steps


def _compose(cell: Cell, tmp_path: Path, **changes: object) -> Evidence:
    values: dict[str, object] = {
        "cell": cell,
        "candidate": SHA,
        "matrix": DIGEST,
        "binding": _context(),
        "steps": _steps(),
        "first": _phase("first-boot"),
        "second": _phase("second-boot"),
        "host": _host(),
        "operator_sha256": DIGEST,
        "seconds": 10.0,
        "artifacts": tmp_path / "artifacts",
    }
    values.update(changes)
    return compose(**values)


def test_complete_run_qualifies_through_the_shipped_checker(
    contract: Contract, cell: Cell, tmp_path: Path
) -> None:
    evidence = _compose(cell, tmp_path, matrix=contract.matrix_sha256)
    assert evidence.outcome is Outcome.SUCCESS
    assert set(evidence.assertions) == set(cell.assertions)
    for name, value in evidence.assertions.items():
        stored = tmp_path / "artifacts" / f"{value}.json"
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == value, name
    single = Contract(1, contract.matrix_sha256, (cell,), contract.inventory)
    inputs = InputBindings(
        version=1,
        candidate_sha=SHA,
        matrix_sha256=contract.matrix_sha256,
        cells={CELL_ID: _context()},
    )
    report = qualify(single, inputs, [evidence])
    assert report.passed, report.cells[0].reasons


@pytest.mark.parametrize(
    "changes",
    [
        {"steps": _steps(failed="prepare")},
        {"steps": _steps(timeout="stack")},
        {"second": None},
        {"second": _phase("first-boot")},
        {"first": _phase("first-boot", passed=False, booted=False)},
        {"second": _phase("second-boot", deployed={**dict.fromkeys(ROLES, SHA), "worker": None})},
        {"first": _phase("first-boot", deployed={**dict.fromkeys(ROLES, SHA), "server": "d" * 40})},
        {"second": _phase("second-boot", context=_context(image_sha256="e" * 64))},
        {"first": _phase("first-boot", guest_confined=False)},
        {"second": _phase("second-boot", host_enforcing=False)},
        {"host": _host(mode="N")},
        {"first": _phase("first-boot", prerequisites={"worker-imports": False})},
        {"second": _phase("second-boot", cleanup={"domain-absent": False})},
    ],
)
def test_incomplete_or_mismatched_runs_fail(
    cell: Cell, tmp_path: Path, changes: dict[str, object]
) -> None:
    evidence = _compose(cell, tmp_path, **changes)
    assert evidence.outcome is Outcome.FAILURE
    assert evidence.impediments == []


def test_a_stack_deployed_from_another_revision_fails(cell: Cell, tmp_path: Path) -> None:
    other = dict.fromkeys(ROLES, "d" * 40)
    evidence = _compose(
        cell,
        tmp_path,
        first=_phase("first-boot", deployed=other),
        second=_phase("second-boot", deployed=other),
    )
    assert evidence.outcome is Outcome.FAILURE
    assert evidence.deployed_roles == other


def test_boot_assertions_hold_apart_from_confinement(cell: Cell, tmp_path: Path) -> None:
    unconfined = {"passed": False, "guest_label": "unconfined", "guest_confined": False}
    evidence = _compose(
        cell,
        tmp_path,
        first=_phase("first-boot", **unconfined),
        second=_phase("second-boot", **unconfined),
    )
    holds = {
        name: json.loads((tmp_path / "artifacts" / f"{value}.json").read_text())["holds"]
        for name, value in evidence.assertions.items()
    }
    assert evidence.outcome is Outcome.FAILURE
    assert [name for name, held in holds.items() if not held] == ["confinement"]


def test_a_failed_phase_verdict_fails_the_cell(cell: Cell, tmp_path: Path) -> None:
    failed = _phase("second-boot", passed=False, failures=["a later node check"])
    assert _compose(cell, tmp_path, second=failed).outcome is Outcome.FAILURE


def test_missing_role_is_omitted_rather_than_guessed(cell: Cell, tmp_path: Path) -> None:
    second = _phase("second-boot", deployed={**dict.fromkeys(ROLES, SHA), "authority": None})
    evidence = _compose(cell, tmp_path, second=second)
    assert "authority" not in evidence.deployed_roles


def test_mismatched_phase_reports_the_first_observed_context(cell: Cell, tmp_path: Path) -> None:
    observed = _context(image_sha256="e" * 64)
    evidence = _compose(cell, tmp_path, first=_phase("first-boot", context=observed))
    assert evidence.context == observed
    assert evidence.input_sha256 == digest(_context())


@pytest.mark.parametrize("host", [_host(markers=("~/kdive",)), _host(sudo=False), _host(kvm=False)])
def test_unprepared_host_is_blocked(cell: Cell, tmp_path: Path, host: HostFacts) -> None:
    evidence = _compose(cell, tmp_path, host=host, steps=_steps()[:1], first=None, second=None)
    assert evidence.outcome is Outcome.BLOCKED
    assert evidence.impediments == ["missing-prerequisite"]
    assert evidence.context == _context()


def test_operator_step_is_optional_only_without_a_file(cell: Cell, tmp_path: Path) -> None:
    steps = [s for s in _steps() if s.name != "operator-prerequisites"]
    assert _compose(cell, tmp_path, steps=steps, operator_sha256=None).outcome is Outcome.SUCCESS
    assert _compose(cell, tmp_path, steps=steps).outcome is Outcome.FAILURE


def test_parse_host_reads_identity_and_markers() -> None:
    text = "os=rocky:10.2\narch=x86_64\nmode=Enforcing\nkvm=yes\nsudo=yes\nmarker=~/kdive\n"
    host = parse_host(text)
    assert host == _host(os="rocky:10.2", mode="Enforcing", markers=("~/kdive",))
    assert host is not None and host.enforcing and not host.clean


@pytest.mark.parametrize(
    "text", ["", "os=private.host:1\narch=x86_64\n", "os=ubuntu:26.04\narch=sparc\n"]
)
def test_parse_host_refuses_an_unidentified_host(text: str) -> None:
    assert parse_host(text) is None


@pytest.mark.parametrize("value", ["a@b;rm -rf /", "root@", "@host", "user@ho st", "-oProxy@x"])
def test_target_validation_rejects_shell_and_option_text(value: str) -> None:
    with pytest.raises(ValueError, match="target"):
        validate_target(value)


def test_names_and_targets_accept_plain_values() -> None:
    assert validate_target("operator@lab-host.example") == "operator@lab-host.example"
    assert validate_name("ubuntu-kdive-ready-26.04") == "ubuntu-kdive-ready-26.04"
    with pytest.raises(ValueError, match="name"):
        validate_name("../etc")


def test_step_script_quotes_every_value() -> None:
    script = step_script("ls {p}", p="a b'c;id")
    assert shlex.split(script) == ["ls", "a b'c;id"]


def test_remote_argv_pins_host_keys_and_sends_the_script_as_one_argument() -> None:
    known = Path("/private/known_hosts")
    argv = ssh_argv("op@lab", known, "echo 'x y'")
    assert argv[0] == "ssh"
    assert all(option in argv for option in SSH_OPTIONS)
    assert f"UserKnownHostsFile={known}" in argv
    assert argv[-2] == "op@lab"
    assert shlex.split(argv[-1]) == ["bash", "-lc", "echo 'x y'"]
    copy = scp_argv(known, "src", "op@lab:dst")
    assert copy[0] == "scp" and all(option in copy for option in SSH_OPTIONS)
    assert f"UserKnownHostsFile={known}" in copy and copy[-2:] == ["src", "op@lab:dst"]


def test_run_step_closes_stdin_and_records_timeouts(tmp_path: Path) -> None:
    ok = run_step("read", ["bash", "-c", "read -r x || echo eof"], tmp_path / "a.log", 30)
    assert ok.exit_code == 0 and (tmp_path / "a.log").read_text() == "eof\n"
    assert ok.transcript_sha256 == hashlib.sha256(b"eof\n").hexdigest()
    slow = run_step("slow", ["sleep", "5"], tmp_path / "b.log", 0.2)
    assert slow.exit_code == "timeout" and not slow.ok


def test_arch_lanes_bind_their_accelerator_and_boot_member() -> None:
    assert ARCH_LANE == {
        "x86_64": ("kvm", "arch/x86/boot/bzImage"),
        "ppc64le": ("kvm-hv", "vmlinux"),
    }


def test_family_matches_uses_catalog_ownership() -> None:
    assert family_matches("rocky", "enterprise")
    assert family_matches("ubuntu", "debian")
    assert family_matches("fedora", "fedora")
    assert not family_matches("rocky", "fedora")
    assert not family_matches("arch", "debian")


def test_read_phase_round_trips_and_refuses_bad_files(tmp_path: Path) -> None:
    path = tmp_path / "phase.json"
    assert read_phase(path) is None
    record = _phase("first-boot")
    path.write_text(record.model_dump_json())
    assert read_phase(path) == record
    data = json.loads(record.model_dump_json())
    for bad in ({**data, "extra": 1}, {**data, "passed": "yes"}):
        path.write_text(json.dumps(bad))
        assert read_phase(path) is None
    path.write_text(" " * (1024 * 1024 + 1))
    assert read_phase(path) is None


def test_console_release_must_match_the_whole_token() -> None:
    line = "[    0.000000] Linux version 6.18.54-g1b357ecb3213 (builder@kernel-fixture) #1"
    assert console_has_release(line, "6.18.54-g1b357ecb3213")
    assert not console_has_release(line, "6.18.5")
    assert not console_has_release(
        "Linux version 6.18.54-g1b357ecb3213+ (x)", "6.18.54-g1b357ecb3213"
    )


@pytest.mark.parametrize(
    "label,confined",
    [
        ("system_u:system_r:svirt_t:s0:c1,c2", True),
        ("libvirt-0b6c (enforce)", True),
        ("libvirt-0b6c (complain)", False),
        ("libvirt-ffff (enforce)", False),
        ("unconfined_u:unconfined_r:unconfined_t:s0-s0:c0.c1023", False),
        ("unconfined", False),
        ("-", False),
    ],
)
def test_guest_label_classification(label: str, confined: bool) -> None:
    assert label_confined(label, "0b6c") is confined


def test_qemu_pid_selects_the_one_domain_process() -> None:
    rows = (
        "  41 /usr/bin/qemu-system-x86_64 -name guest=kdive-a,debug-threads=on -m 2048\n"
        "  42 /usr/bin/qemu-system-x86_64 -name guest=kdive-ab,debug-threads=on\n"
        "  43 bash -c grep guest=kdive-a\n"
    )
    assert qemu_pid(rows, "kdive-a") == 41
    assert qemu_pid(rows, "kdive-missing") is None
    assert qemu_pid(rows + rows, "kdive-a") is None


def test_package_digest_tracks_python_sources_only(tmp_path: Path) -> None:
    (tmp_path / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n")
    before = package_digest(tmp_path)
    (tmp_path / "pkg" / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"\0")
    (tmp_path / "pkg" / "notes.txt").write_text("ignored")
    assert package_digest(tmp_path) == before
    (tmp_path / "pkg" / "a.py").write_text("x = 2\n")
    assert package_digest(tmp_path) != before


def test_kernel_member_digest_hashes_the_boot_member(tmp_path: Path) -> None:
    tar_path = tmp_path / "kernel.tar.gz"
    with tarfile.open(tar_path, "w:gz") as archive:
        for name, data in (("boot/vmlinuz", b"kernel"), ("lib/modules/x/a.ko", b"m")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    assert kernel_member_digest(tar_path) == hashlib.sha256(b"kernel").hexdigest()


def _write_run(root: Path, cell_id: str, candidate: str = SHA) -> Path:
    root.mkdir()
    inputs = InputBindings(
        version=1, candidate_sha=candidate, matrix_sha256=DIGEST, cells={cell_id: _context()}
    )
    (root / "binding.json").write_text(inputs.model_dump_json())
    (root / "result.json").write_text("[]")
    return root


def test_merge_combines_runs_and_refuses_collisions(tmp_path: Path) -> None:
    first = _write_run(tmp_path / "a", CELL_ID)
    second = _write_run(tmp_path / "b", "host-install/local-libvirt/x86_64/fedora")
    merge([first, second], tmp_path / "out")
    merged = InputBindings.model_validate_json((tmp_path / "out" / "inputs.json").read_text())
    assert set(merged.cells) == {CELL_ID, "host-install/local-libvirt/x86_64/fedora"}
    assert json.loads((tmp_path / "out" / "results.json").read_text()) == []
    with pytest.raises(ValueError, match="cell"):
        merge([first, _write_run(tmp_path / "c", CELL_ID)], tmp_path / "out2")
    with pytest.raises(ValueError, match="candidate"):
        merge([first, _write_run(tmp_path / "d", "x/y", candidate="f" * 40)], tmp_path / "out3")


def test_cli_names_its_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["--help"])
    assert exited.value.code == 0
    out = capsys.readouterr().out
    assert all(name in out for name in ("bundle", "run", "merge"))
