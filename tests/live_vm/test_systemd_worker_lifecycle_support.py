"""Unit coverage for the real-host systemd worker proof harness."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import tests.live_vm.systemd_worker_lifecycle_support as support

pytest_plugins = ("pytester",)

_ROOT = Path(__file__).resolve().parents[2]
_HOSTED_PROOF = "tests/live_vm/test_systemd_worker_lifecycle.py"


def _live_proof_function(name: str) -> tuple[ast.FunctionDef, str]:
    source = (_ROOT / _HOSTED_PROOF).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    )
    return function, ast.get_source_segment(source, function) or ""


def _write_cgroup_events(tmp_path: Path, content: bytes) -> Path:
    events = tmp_path / "system.slice" / "worker.service" / "cgroup.events"
    events.parent.mkdir(parents=True)
    events.write_bytes(content)
    return events


@pytest.mark.parametrize(("value", "expected"), [(b"0", False), (b"1", True)])
def test_cgroup_populated_reads_exact_boolean_record(
    tmp_path: Path, value: bytes, expected: bool
) -> None:
    _write_cgroup_events(tmp_path, b"populated " + value + b"\nfrozen 0\n")

    assert support.cgroup_populated("/system.slice/worker.service", root=tmp_path) is expected


def test_cgroup_populated_accepts_blank_or_disappeared_group(tmp_path: Path) -> None:
    assert support.cgroup_populated("", root=tmp_path) is False
    assert support.cgroup_populated("/system.slice/absent.service", root=tmp_path) is False


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"frozen 0\n",
        b"populated\n",
        b"populated 2\n",
        b"populated 0 extra\n",
        b"populated 0\npopulated 1\n",
        b"populated \xff\n",
    ],
)
def test_cgroup_populated_rejects_malformed_present_evidence(
    tmp_path: Path, content: bytes
) -> None:
    _write_cgroup_events(tmp_path, content)

    with pytest.raises((UnicodeDecodeError, ValueError)):
        support.cgroup_populated("/system.slice/worker.service", root=tmp_path)


def test_cgroup_populated_propagates_non_not_found_io_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_cgroup_events(tmp_path, b"populated 0\n")

    def refuse_read(*_args: object, **_kwargs: object) -> str:
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "read_text", refuse_read)

    with pytest.raises(PermissionError, match="permission denied"):
        support.cgroup_populated("/system.slice/worker.service", root=tmp_path)


def test_hosted_systemd_proof_collects_exactly_six_cases() -> None:
    """The gated suite never runs in CI, so this list is the only thing that notices a change.

    A proof silently added, renamed, or lost there would be invisible until someone ran the live
    tier by hand, which is exactly when nobody is checking the roster.
    """
    result = subprocess.run(
        (sys.executable, "-m", "pytest", _HOSTED_PROOF, "--collect-only", "-q"),
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    node_ids = tuple(
        line for line in result.stdout.splitlines() if line.startswith(f"{_HOSTED_PROOF}::")
    )
    assert node_ids == (
        f"{_HOSTED_PROOF}::test_real_systemd_workers_register_heartbeat_and_terminate[1]",
        f"{_HOSTED_PROOF}::test_real_systemd_workers_register_heartbeat_and_terminate[3]",
        f"{_HOSTED_PROOF}::test_database_outage_retains_exact_invocation_until_stop_retry",
        f"{_HOSTED_PROOF}::test_recover_clears_the_failed_identity_that_blocks_the_next_start",
        f"{_HOSTED_PROOF}::test_recover_retires_a_restarted_slot_and_releases_its_fence_in_one_call",
        f"{_HOSTED_PROOF}::test_recover_refuses_a_live_slot_without_releasing_its_fence",
    )


class _RoleQueryConnection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.query = ""

    def __enter__(self) -> _RoleQueryConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, _parameters: object) -> _RoleQueryConnection:
        self.query = query
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


def _runtime_role_rows(attributes: tuple[bool, ...]) -> list[tuple[Any, ...]]:
    return [
        (member, *attributes, [capability]) for member, capability in support.ROLE_MEMBERS.items()
    ]


def test_exact_runtime_roles_accept_only_safe_login_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _RoleQueryConnection(
        _runtime_role_rows((True, True, False, False, False, False, False))
    )
    monkeypatch.setattr(support.psycopg, "connect", lambda _dsn: connection)

    support.assert_exact_runtime_roles("postgresql://migration")

    for attribute in (
        "rolcanlogin",
        "rolinherit",
        "rolsuper",
        "rolcreatedb",
        "rolcreaterole",
        "rolreplication",
        "rolbypassrls",
    ):
        assert attribute in connection.query


@pytest.mark.parametrize("attribute_index", range(7))
def test_exact_runtime_roles_reject_attribute_drift(
    monkeypatch: pytest.MonkeyPatch, attribute_index: int
) -> None:
    attributes = [True, True, False, False, False, False, False]
    attributes[attribute_index] = not attributes[attribute_index]
    connection = _RoleQueryConnection(_runtime_role_rows(tuple(attributes)))
    monkeypatch.setattr(support.psycopg, "connect", lambda _dsn: connection)

    with pytest.raises(AssertionError):
        support.assert_exact_runtime_roles("postgresql://migration")


def _stopped_postgres(container_id: str) -> dict[str, Any]:
    return {
        "Id": container_id,
        "Config": {
            "Labels": {
                "com.docker.compose.service": "postgres",
                "com.docker.compose.project.working_dir": str(_ROOT),
                "com.docker.compose.project.config_files": str(_ROOT / "docker-compose.yml"),
            }
        },
        "State": {"Running": False, "Health": {"Status": "unhealthy"}},
    }


def test_outage_recovery_restores_after_stop_timeout_with_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "current-postgres"
    commands: list[tuple[str, ...]] = []
    cleaned: list[bool] = []
    monkeypatch.setattr(
        support,
        "docker_inspect",
        lambda _kind, inspected: _stopped_postgres(inspected),
    )
    monkeypatch.setattr(
        support,
        "run",
        lambda *argv, **_kwargs: commands.append(argv) or container_id,
    )
    monkeypatch.setattr(support, "wait_for_postgres", lambda _container_id: None)
    original = subprocess.TimeoutExpired(("docker", "stop"), 30)

    retained = support.recover_after_outage(
        original,
        restore_database=lambda: support.restore_postgres(container_id),
        prove_retained_row=lambda: None,
        cleanup_workers=lambda: cleaned.append(True),
    )

    assert retained is original
    assert commands == [("docker", "start", container_id)]
    assert cleaned == [True]


@pytest.mark.parametrize("failure_stage", ("restart", "health", "worker_cleanup"))
def test_outage_recovery_failure_preserves_primary_and_gates_unit_cleanup(
    monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    container_id = "current-postgres"
    recovery_error = RuntimeError(f"{failure_stage} failed")
    cleaned: list[bool] = []
    monkeypatch.setattr(
        support,
        "docker_inspect",
        lambda _kind, inspected: _stopped_postgres(inspected),
    )

    def run(*_argv: str, **_kwargs: object) -> str:
        if failure_stage == "restart":
            raise recovery_error
        return container_id

    def wait(_container_id: str) -> None:
        if failure_stage == "health":
            raise recovery_error

    monkeypatch.setattr(support, "run", run)
    monkeypatch.setattr(support, "wait_for_postgres", wait)

    def cleanup() -> None:
        cleaned.append(True)
        if failure_stage == "worker_cleanup":
            raise recovery_error

    original = subprocess.TimeoutExpired(("docker", "stop"), 30)

    retained = support.recover_after_outage(
        original,
        restore_database=lambda: support.restore_postgres(container_id),
        prove_retained_row=lambda: None,
        cleanup_workers=cleanup,
    )

    assert retained is original
    assert cleaned == ([True] if failure_stage == "worker_cleanup" else [])
    expected_note = (
        "worker cleanup failed"
        if failure_stage == "worker_cleanup"
        else "PostgreSQL restoration failed"
    )
    note = next(note for note in original.__notes__ if expected_note in note)
    assert str(recovery_error) in note
    assert "Traceback (most recent call last)" in note
    assert "test_systemd_worker_lifecycle_support.py" in note


@pytest.mark.parametrize("failure_stage", (None, "restore", "reset"))
def test_cleanup_after_case_orders_and_reports_failures(failure_stage: str | None) -> None:
    events: list[str] = []

    def restore() -> None:
        events.append("restore")
        if failure_stage == "restore":
            raise RuntimeError("restore failed")

    def reset() -> None:
        events.append("reset")
        if failure_stage == "reset":
            raise RuntimeError("reset failed")

    if failure_stage is None:
        support.cleanup_after_case(
            restore_database=restore,
            cleanup_workers=reset,
        )
    else:
        with pytest.raises(RuntimeError, match=f"{failure_stage} failed"):
            support.cleanup_after_case(
                restore_database=restore,
                cleanup_workers=reset,
            )
    expected = ["restore"] if failure_stage == "restore" else ["restore", "reset"]
    assert events == expected


def _proof_plugin_conftest(events: Path, *, reset_failure: bool = False) -> str:
    reset_body = (
        'raise RuntimeError("cleanup boom")'
        if reset_failure
        else f'Path({str(events)!r}).open("a", encoding="utf-8").write("reset\\n")'
    )
    return f"""
from pathlib import Path
import sys
import pytest
sys.path.insert(0, {str(_ROOT)!r})
import tests.live_vm.systemd_worker_lifecycle_support as support
import tests.live_vm.test_systemd_worker_lifecycle as live

pytest_plugins = ("tests.live_vm.test_systemd_worker_lifecycle",)

@pytest.fixture
def proof_context(monkeypatch):
    context = live.ProofContext(
        admin_dsn="postgresql://admin",
        worker_dsn="postgresql://worker",
        postgres=live.ComposePostgres(container_id="sentinel-postgres", volume_name="volume"),
    )
    def restore(container_id):
        assert container_id == "sentinel-postgres"
        Path({str(events)!r}).open("a", encoding="utf-8").write("restore\\n")
    def reset():
        {reset_body}
    monkeypatch.setattr(support, "restore_postgres", restore)
    monkeypatch.setattr(live, "_reset_fleet", reset)
    return context
"""


def test_live_fixture_isolates_body_failures_and_reports_teardown_failure(
    pytester: pytest.Pytester,
) -> None:
    events = pytester.path / "events"
    pytester.makeconftest(_proof_plugin_conftest(events, reset_failure=True))
    pytester.makepyfile(
        """
def test_startup_failure():
    raise RuntimeError("startup boom")

def test_post_start_assertion_failure():
    from pathlib import Path
    Path("events").open("a", encoding="utf-8").write("started\\n")
    assert False, "body boom"
"""
    )

    result = pytester.runpytest_subprocess("-q")

    result.assert_outcomes(failed=2, errors=2)
    output = result.stdout.str()
    assert "startup boom" in output
    assert "body boom" in output
    assert "cleanup boom" in output
    assert events.read_text(encoding="utf-8").splitlines() == [
        "restore",
        "started",
        "restore",
    ]


def test_live_fixture_wires_exact_case_cleanup_boundary() -> None:
    fixture, source = _live_proof_function("_isolate_proof_case")
    decorator = next(
        item
        for item in fixture.decorator_list
        if isinstance(item, ast.Call)
        and isinstance(item.func, ast.Attribute)
        and item.func.attr == "fixture"
    )
    keywords = {item.arg: ast.literal_eval(item.value) for item in decorator.keywords}

    assert keywords == {"autouse": True}
    assert [argument.arg for argument in fixture.args.args] == ["proof_context"]
    assert "support.cleanup_after_case(" in source
    assert "support.restore_postgres(proof_context.postgres.container_id)" in source
    assert "cleanup_workers=_reset_fleet" in source


def test_basic_worker_cases_keep_terminal_proof_on_success() -> None:
    _, source = _live_proof_function("test_real_systemd_workers_register_heartbeat_and_terminate")

    assert "_assert_stopped(proof_context, rows)" in source


def test_expected_lifecycle_failures_use_nonchecking_runner() -> None:
    _, source = _live_proof_function("_assert_retained_after_database_outage")

    assert "status, response = _lifecycle_result(operation)" in source
    assert "assert status == 4" in source
    assert "_lifecycle(operation)" not in source


def test_outage_cleanup_retires_then_recovers_failed_identity() -> None:
    _, source = _live_proof_function("_assert_stopped_after_outage")

    retire = source.index("_assert_terminated(context, rows)")
    recover = source.index('_lifecycle("recover")')
    inactive = source.index("_assert_units_inactive(rows)")
    assert retire < recover < inactive


def test_out_of_band_restart_waits_for_terminal_systemd_state() -> None:
    _, source = _live_proof_function("_restart_out_of_band")

    assert "deadline = time.monotonic() + 10" in source
    assert 'properties["ActiveState"] == "failed"' in source
    assert "time.sleep(0.1)" in source
