import os
import shutil
import socket
import subprocess
import time
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from kdive.health.aux_bind import PROCESS_DEFAULT_PORTS

ROOT = Path(__file__).resolve().parents[2]


def _grafana_supports_arch(arch: str) -> bool:
    """Source lib.sh and return the exit status of `grafana_supports_arch <arch>` as a bool."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{ROOT}/scripts/live-stack/lib.sh" && grafana_supports_arch "$1"',
            "_",
            arch,
        ],
        check=False,
    )
    return result.returncode == 0


def _require_free_http_port(port: int) -> subprocess.CompletedProcess[str]:
    """Source lib.sh and run `require_free_http_port` with KDIVE_HTTP_PORT=<port>."""
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "{ROOT}/scripts/live-stack/lib.sh" '
            f'&& KDIVE_HTTP_PORT="$1" require_free_http_port',
            "_",
            str(port),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@contextmanager
def _listening_port() -> Generator[int]:
    """Hold a real LISTEN socket open on a loopback port for the duration of the block."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        yield sock.getsockname()[1]


def _free_port() -> int:
    """Return a port number that is free at call time (bound then released)."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _lib(snippet: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Source lib.sh and run `snippet`, with `env` overlaid on the current environment."""
    return subprocess.run(
        ["bash", "-c", f'source "{ROOT}/scripts/live-stack/lib.sh"\n{snippet}'],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **env},
    )


def _start_workers(tmp_path: Path, worker_count: str) -> list[Path]:
    """Really run `restart_host_processes` against a stub interpreter; return the worker logs.

    Stubs only the environment the launch loop cannot have in a unit test — the interpreter, the
    log directory, and the three helpers that touch the live process table — so the loop itself,
    the per-worker log naming, and the per-worker health-bind override are the code under test.
    The stub records the aux bind address it was handed, which is the collision this guards.
    """
    stub = tmp_path / "python-stub"
    stub.write_text(
        '#!/usr/bin/env bash\necho "argv=$* health=${KDIVE_HEALTH_BIND_ADDR:-<unset>}"\n'
    )
    stub.chmod(0o755)
    log_dir = tmp_path / "logs"
    result = _lib(
        f'py="{stub}"\n'
        f'log_dir="{log_dir}"\n'
        "stop_daemons() { :; }\n"
        "require_free_http_port() { :; }\n"
        "wait_for_daemons_to_settle() { :; }\n"
        "restart_host_processes\n"
        'echo "DAEMON_COUNT=${DAEMON_COUNT}"\n',
        KDIVE_WORKER_COUNT=worker_count,
        KDIVE_WORKER_AS_ROOT="0",
    )
    assert result.returncode == 0, result.stderr
    expected = 2 + int(worker_count)
    assert f"DAEMON_COUNT={expected}" in result.stdout, (
        f"settle check must expect server + reconciler + {worker_count} workers: {result.stdout}"
    )
    # The launches are detached (`setsid nohup ... &`), so poll rather than `wait` on them.
    deadline = time.monotonic() + 10
    logs: list[Path] = []
    while time.monotonic() < deadline:
        logs = sorted(p for p in log_dir.glob("worker*.log") if p.read_text().strip())
        if len(logs) >= int(worker_count):
            break
        time.sleep(0.1)
    return logs


def test_configured_worker_count_defaults_to_one_and_rejects_nonsense() -> None:
    """The knob must fail loud on a value that would silently start the wrong number of workers."""
    assert _lib("configured_worker_count").stdout == "1"
    assert _lib("configured_worker_count", KDIVE_WORKER_COUNT="3").stdout == "3"
    # Empty reads as unset, as every other knob in lib.sh does (`${VAR:-default}`).
    assert _lib("configured_worker_count", KDIVE_WORKER_COUNT="").stdout == "1"
    for bad in ("0", "-1", "abc", "2.5"):
        result = _lib("configured_worker_count", KDIVE_WORKER_COUNT=bad)
        assert result.returncode != 0, f"{bad!r} must be rejected"
        assert "positive integer" in result.stderr


def test_extra_workers_get_health_ports_clear_of_the_registered_defaults() -> None:
    """Worker 1 keeps the process default; extras must not land on ANOTHER process's port.

    uvicorn's bind is exclusive, so an extra worker that reused 9465 — or stepped up onto the
    reconciler's 9466 — would die at startup instead of claiming jobs, and the multi-worker stack
    would silently degrade back to the single-worker serialization this knob exists to escape.
    """
    assert _lib("extra_worker_health_bind 1").stdout == "", "worker 1 must keep the default bind"
    ports = {
        int(_lib(f"extra_worker_health_bind {index}").stdout.rsplit(":", 1)[1])
        for index in (2, 3, 4)
    }
    assert len(ports) == 3, f"each extra worker needs its own port, got {ports}"
    assert not ports & set(PROCESS_DEFAULT_PORTS.values()), (
        f"extra-worker ports {ports} collide with the registered defaults {PROCESS_DEFAULT_PORTS}"
    )
    # An explicit operator bind still wins for worker 1, so extras must step off ITS host.
    explicit = _lib("extra_worker_health_bind 2", KDIVE_HEALTH_BIND_ADDR="0.0.0.0:9500").stdout
    assert explicit.startswith("0.0.0.0:")


def test_worker_log_paths_are_distinct_and_keep_the_first_unsuffixed() -> None:
    """Worker 1 keeps the name recorded runbooks and proof records already cite."""
    root_logs = [_lib(f"worker_log_path {i}", KDIVE_WORKER_AS_ROOT="1").stdout for i in (1, 2, 3)]
    user_logs = [_lib(f"worker_log_path {i}", KDIVE_WORKER_AS_ROOT="0").stdout for i in (1, 2, 3)]
    assert root_logs[0].endswith("/worker-root.log")
    assert user_logs[0].endswith("/worker.log")
    assert len(set(root_logs)) == 3, root_logs
    assert len(set(user_logs)) == 3, user_logs


def test_restart_host_processes_starts_one_worker_by_default(tmp_path: Path) -> None:
    logs = _start_workers(tmp_path, "1")
    assert [p.name for p in logs] == ["worker.log"]
    assert "health=<unset>" in logs[0].read_text(), "the sole worker keeps the process default"


def test_restart_host_processes_starts_every_configured_worker(tmp_path: Path) -> None:
    """Three workers must really be launched, each with its own log and its own aux port."""
    logs = _start_workers(tmp_path, "3")
    assert [p.name for p in logs] == ["worker-2.log", "worker-3.log", "worker.log"]
    binds = [p.read_text().split("health=")[1].strip() for p in logs]
    assert binds.count("<unset>") == 1, f"exactly one worker keeps the default bind: {binds}"
    explicit = [b for b in binds if b != "<unset>"]
    assert len(set(explicit)) == 2, f"extra workers must not share a bind: {binds}"


def test_live_stack_env_exports_required_defaults() -> None:
    env = (ROOT / "scripts/live-stack/env.sh").read_text()
    required = [
        "KDIVE_DATABASE_URL",
        "KDIVE_OIDC_ISSUER",
        "KDIVE_OIDC_JWKS_URI",
        "KDIVE_OIDC_AUDIENCE",
        "KDIVE_S3_ENDPOINT_URL",
        "KDIVE_S3_BUCKET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "KDIVE_BUILD_WORKSPACE",
        "KDIVE_BUILD_COMPONENT_ROOTS",
        "KDIVE_INSTALL_STAGING",
        "KDIVE_STACK_BASE_URL",
        # Configurable compose backend host ports (single source of truth for publish + client URL).
        "KDIVE_POSTGRES_PORT",
        "KDIVE_MINIO_PORT",
        "KDIVE_MINIO_CONSOLE_PORT",
        "KDIVE_OIDC_PORT",
        "KDIVE_PROMETHEUS_PORT",
        "KDIVE_GRAFANA_PORT",
    ]
    for name in required:
        assert f"export {name}=" in env


def test_client_urls_derive_from_the_configurable_ports() -> None:
    # The port var must be the SINGLE source of truth: the client-facing DSN/endpoint defaults must
    # reference the port var, not a second hardcoded literal that could silently drift from compose.
    env = (ROOT / "scripts/live-stack/env.sh").read_text()
    assert "localhost:${KDIVE_POSTGRES_PORT}/kdive" in env
    assert "http://localhost:${KDIVE_MINIO_PORT}" in env
    assert "http://localhost:${KDIVE_OIDC_PORT}/default" in env


def test_live_stack_scripts_are_strict_bash() -> None:
    for name in ("env.sh", "apply-migrations.sh", "up.sh", "down.sh", "status.sh"):
        text = (ROOT / "scripts/live-stack" / name).read_text()
        assert text.startswith("#!/usr/bin/env bash\n"), f"{name}: missing bash shebang"
        assert "\nset -euo pipefail\n" in text, f"{name}: missing 'set -euo pipefail'"


def test_restart_host_processes_starts_all_three() -> None:
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    assert "restart_host_processes" in text
    assert "-m kdive server" in text
    assert "-m kdive reconciler" in text
    assert "-m kdive worker" in text


def test_sudo_root_worker_forwards_backend_endpoints() -> None:
    # sudo resets the environment, so the root worker re-sources env.sh and would re-default any
    # relocated backend port. The resolved DB + S3 endpoints must be forwarded into the sudo shell
    # so a KDIVE_POSTGRES_PORT/KDIVE_MINIO_PORT override reaches the worker, not just the same-user
    # server/reconciler. The forward must appear inside the `sudo bash -c` block.
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    sudo_block = text[text.index("sudo bash -c") : text.index("-m kdive worker >>")]
    assert "KDIVE_DATABASE_URL='${KDIVE_DATABASE_URL}'" in sudo_block
    assert "KDIVE_S3_ENDPOINT_URL='${KDIVE_S3_ENDPOINT_URL}'" in sudo_block


def test_grafana_gate_skips_ppc64le_and_keeps_other_arches() -> None:
    """The arch gate must skip grafana only where it has no manifest (ppc64le), not elsewhere.

    Executes the real predicate so an inverted or gutted gate fails, unlike a substring check.
    """
    assert _grafana_supports_arch("ppc64le") is False, "grafana has no ppc64le manifest (ADR-0356)"
    assert _grafana_supports_arch("x86_64") is True
    assert _grafana_supports_arch("aarch64") is True
    # An empty/unknown arch (no `uname`) must not silently skip grafana — attempt it best-effort.
    assert _grafana_supports_arch("") is True


@pytest.mark.skipif(shutil.which("ss") is None, reason="ss (iproute2) required to inspect ports")
def test_require_free_http_port_fails_when_the_port_is_held() -> None:
    """A foreign listener on KDIVE_HTTP_PORT must fail the guard with a remediation, not proceed."""
    with _listening_port() as port:
        result = _require_free_http_port(port)
    assert result.returncode != 0, "guard must fail when the port is occupied"
    assert str(port) in result.stderr
    assert "KDIVE_HTTP_PORT=8001" in result.stderr  # remediation names the override


@pytest.mark.skipif(shutil.which("ss") is None, reason="ss (iproute2) required to inspect ports")
def test_require_free_http_port_passes_when_the_port_is_free() -> None:
    result = _require_free_http_port(_free_port())
    assert result.returncode == 0, result.stderr


def test_restart_host_processes_guards_the_port_after_stopping_daemons() -> None:
    # The guard must run AFTER stop_daemons (so a kdive server we just stopped is not mis-flagged)
    # and BEFORE the server launches (so it actually prevents the lost bind race).
    text = (ROOT / "scripts/live-stack/lib.sh").read_text()
    stop = text.index("\n  stop_daemons\n")
    guard = text.index("require_free_http_port || return 1")
    launch = text.index('setsid nohup "$py" -m kdive server')
    assert stop < guard < launch, "guard must sit between stop_daemons and the server launch"


def test_up_starts_prometheus_independently_of_grafana() -> None:
    """Prometheus comes up in its own `compose up`, so a grafana failure can't abort it (#1261)."""
    text = (ROOT / "scripts/live-stack/up.sh").read_text()
    assert "up -d prometheus" in text, "prometheus must be brought up on its own"
    assert "grafana_supports_arch" in text, "grafana must be gated on host arch"
    assert "#1261" in text, "the skip must be traceable to its tracking issue"
