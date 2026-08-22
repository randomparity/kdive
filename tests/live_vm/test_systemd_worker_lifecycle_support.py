"""Unit coverage for the real-host systemd worker proof harness."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import tests.live_vm.systemd_worker_lifecycle_support as support

_ROOT = Path(__file__).resolve().parents[2]
_HOSTED_PROOF = "tests/live_vm/test_systemd_worker_lifecycle.py"


def test_hosted_systemd_proof_collects_exactly_three_cases() -> None:
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
    assert any(expected_note in note for note in original.__notes__)
