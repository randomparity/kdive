"""Structural test for the reference compose (ADR-0088 Phase 3).

Gated on the ``docker compose`` plugin being available (it is on
``ubuntu-latest``, so this runs in the normal ``just test`` job and gates every
PR). It does not build or pull anything — ``docker compose config`` only parses
the committed file, resolves the ``x-backends`` anchor / merge keys, and renders
the canonical service model.

It locks the load-bearing ADR-0088 decision-4 ordering contract: the app
services depend on the ``migrate`` one-shot with
``service_completed_successfully`` (a bare ``depends_on`` would let them boot
before migrations finish), and ``migrate`` itself waits for a healthy Postgres.
A future edit that weakens the condition fails here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml


def _docker_compose_available() -> bool:
    # Gate on the compose *plugin*, not just the `docker` binary: a host with docker
    # but no compose plugin would otherwise hard-fail instead of skipping.
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "compose", "version"],
                capture_output=True,
                timeout=30,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError) as _exc:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_compose_available(),
    reason="the docker compose plugin is required to render the compose model",
)

_COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
_APP_SERVICES = ("server", "worker", "reconciler")
_LOCAL_LOGIN_MEMBERS = {
    "server": ("kdive-server-member", "kdive_server"),
    "worker": ("kdive-worker-member", "kdive_worker"),
    "reconciler": ("kdive-reconciler-member", "kdive_reconciler"),
    "lifecycle-witness": ("kdive-witness-member", "kdive_lifecycle_witness"),
}

_VERSIONING_REPLIES = (
    (
        "enabled-omitted-exclusions",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""}}',
        0,
    ),
    (
        "enabled-explicit-empty-exclusions",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":"","ExcludedPrefixes":[]}}',
        0,
    ),
    (
        "suspended",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Suspended","MFADelete":""}}',
        1,
    ),
    (
        "excluded-prefix",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":"",'
        '"ExcludedPrefixes":["tmp/"]}}',
        1,
    ),
    (
        "excluded-folders",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":"","ExcludeFolders":true}}',
        1,
    ),
    (
        "missing-status",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"MFADelete":""}}',
        1,
    ),
    (
        "mfa-delete-enabled",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":"Enabled"}}',
        1,
    ),
    (
        "missing-mfa-delete",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled"}}',
        1,
    ),
    (
        "malformed-exclusions",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":"","ExcludedPrefixes":null}}',
        1,
    ),
    (
        "compatible-decoy-before-suspended-real-state",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"decoy":{"versioning":{"status":"Enabled","MFADelete":""}},'
        '"versioning":{"status":"Suspended","MFADelete":""}}',
        1,
    ),
    (
        "duplicate-versioning-keys",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""},'
        '"versioning":{"status":"Suspended","MFADelete":""}}',
        1,
    ),
    (
        "trailing-junk",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""}}not-json',
        1,
    ),
    (
        "error-status-decoy",
        '{"Op":"info","status":"error","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""}}',
        1,
    ),
    (
        "reordered-fields",
        '{"status":"success","Op":"info","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""}}',
        1,
    ),
    (
        "extra-top-level-field",
        '{"Op":"info","status":"success","url":"local/kdive-artifacts",'
        '"versioning":{"status":"Enabled","MFADelete":""},"extra":true}',
        1,
    ),
)


def _config(
    env_overrides: dict[str, str] | None = None, *, obs: bool = False, managed_worker: bool = True
) -> dict[str, Any]:
    # `docker compose config` drops profile-gated services (prometheus/grafana) unless the profile
    # is active, so pass `--profile obs` to render them into the model.
    profile = ["--profile", "managed-worker"] if managed_worker else []
    if obs:
        profile.extend(("--profile", "obs"))
    res = subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), *profile, "config", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=60,
        # compose interpolates ${VAR} from the process environment, so overrides go here.
        env={**os.environ, **(env_overrides or {})},
    )
    assert res.returncode == 0, f"compose config invalid: {res.stderr}"
    return json.loads(res.stdout)


def _services() -> dict[str, Any]:
    return _config()["services"]


def test_worker_death_verifier_uses_inspect_only_private_proxy() -> None:
    model = _config()
    services = model["services"]
    proxy = services["worker-death-api"]

    assert services["worker"]["environment"]["KDIVE_WORKER_INCARNATION_KIND"] == "docker"
    assert services["server"]["environment"]["KDIVE_WORKER_DEATH_VERIFIER"] == "docker"
    assert proxy["entrypoint"] == [
        "python",
        "-m",
        "kdive.processes.lifecycle.docker_death_api",
    ]
    assert proxy["user"] == "root"
    assert proxy["networks"] == {"worker-death": None}
    assert model["networks"]["worker-death"]["internal"] is True


def test_worker_requires_evidence_preserving_compose_wrapper() -> None:
    model = _config(managed_worker=False)
    worker = _config({"KDIVE_WORKER_INCARNATION_NONCE": "a" * 32})["services"]["worker"]

    assert "worker" not in model["services"]
    assert worker["profiles"] == ["managed-worker"]
    assert worker["labels"]["io.kdive.managed-worker"] == "true"
    assert worker["environment"]["KDIVE_WORKER_INCARNATION_ID"] == f"docker:{'a' * 32}"


def test_worker_receives_only_worker_database_authority_and_credential_handoff() -> None:
    worker_dsn = "postgresql://worker-only@postgres/kdive"
    model = _config(
        {
            "KDIVE_WORKER_DATABASE_URL": worker_dsn,
            "KDIVE_MIGRATION_DATABASE_URL": "postgresql://migration-owner@postgres/kdive",
            "KDIVE_LIFECYCLE_WITNESS_DATABASE_URL": (
                "postgresql://lifecycle-witness@localhost/kdive"
            ),
        }
    )
    worker = model["services"]["worker"]

    assert worker["environment"]["KDIVE_DATABASE_URL"] == worker_dsn
    assert "KDIVE_MIGRATION_DATABASE_URL" not in worker["environment"]
    assert "KDIVE_LIFECYCLE_WITNESS_DATABASE_URL" not in worker["environment"]
    assert not any("INCARNATION_CREDENTIAL" in key for key in worker["environment"])
    assert not any("ENVELOPE" in key for key in worker["environment"])
    assert all("docker.sock" not in str(volume) for volume in worker.get("volumes", []))
    assert all("worker-incarnation-credential" not in str(volume) for volume in worker["volumes"])


def test_migrate_receives_migration_database_authority_only() -> None:
    migration_dsn = "postgresql://migration-owner@postgres/kdive"
    migrate = _config({"KDIVE_MIGRATION_DATABASE_URL": migration_dsn})["services"]["migrate"]

    assert migrate["environment"]["KDIVE_DATABASE_URL"] == migration_dsn
    assert "KDIVE_WORKER_DATABASE_URL" not in migrate["environment"]
    assert "KDIVE_LIFECYCLE_WITNESS_DATABASE_URL" not in migrate["environment"]


def test_clean_local_database_bootstraps_distinct_exact_login_members() -> None:
    services = _services()
    postgres = services["postgres"]
    bootstrap = services["role-bootstrap"]

    assert postgres["environment"] == {
        "POSTGRES_DB": "kdive",
        "POSTGRES_PASSWORD": "kdive",  # pragma: allowlist secret — asserted local default
        "POSTGRES_USER": "kdive",
    }
    assert any(
        volume["target"] == "/docker-entrypoint-initdb.d/010-migration-owner.sql"
        and volume["read_only"] is True
        for volume in postgres["volumes"]
    )
    migration_owner = (
        _COMPOSE_FILE.parent / "deploy/compose/bootstrap-migration-owner.sql"
    ).read_text()
    assert 'CREATE ROLE "kdive-migration" LOGIN' in migration_owner
    assert "kdive-migration-local" in migration_owner
    assert bootstrap["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert bootstrap["entrypoint"] == ["/bin/bash", "/bootstrap/bootstrap-runtime-roles.sh"]
    assert bootstrap["environment"]["KDIVE_LOCAL_ROLE_BOOTSTRAP"] == "1"
    script = (_COMPOSE_FILE.parent / "deploy/compose/bootstrap-runtime-roles.sh").read_text()
    for member, capability in _LOCAL_LOGIN_MEMBERS.values():
        assert member in script
        assert capability in script


def test_every_runtime_waits_for_exact_local_role_bootstrap() -> None:
    services = _services()
    for service in _APP_SERVICES:
        assert services[service]["depends_on"]["role-bootstrap"]["condition"] == (
            "service_completed_successfully"
        )


def test_local_runtime_dsns_are_distinct_and_migration_owner_is_absent() -> None:
    services = _services()
    runtime = {name: services[name]["environment"]["KDIVE_DATABASE_URL"] for name in _APP_SERVICES}

    assert len(set(runtime.values())) == len(runtime)
    assert "kdive-server-member" in runtime["server"]
    assert "kdive-worker-member" in runtime["worker"]
    assert "kdive-reconciler-member" in runtime["reconciler"]
    assert all("kdive-migration" not in dsn for dsn in runtime.values())


def test_external_role_provisioning_can_disable_local_bootstrap_and_override_runtime_dsns() -> None:
    model = _config(
        {
            "KDIVE_LOCAL_ROLE_BOOTSTRAP": "0",
            "KDIVE_SERVER_DATABASE_URL": "postgresql://external-server@db/kdive",
            "KDIVE_WORKER_DATABASE_URL": "postgresql://external-worker@db/kdive",
            "KDIVE_RECONCILER_DATABASE_URL": "postgresql://external-reconciler@db/kdive",
        }
    )["services"]

    assert model["role-bootstrap"]["environment"]["KDIVE_LOCAL_ROLE_BOOTSTRAP"] == "0"
    assert model["server"]["environment"]["KDIVE_DATABASE_URL"] == (
        "postgresql://external-server@db/kdive"
    )
    assert model["worker"]["environment"]["KDIVE_DATABASE_URL"] == (
        "postgresql://external-worker@db/kdive"
    )
    assert model["reconciler"]["environment"]["KDIVE_DATABASE_URL"] == (
        "postgresql://external-reconciler@db/kdive"
    )


def _minio_init_script() -> str:
    entrypoint = _services()["minio-init"]["entrypoint"]
    assert entrypoint[:2] == ["/bin/sh", "-c"]
    # ``docker compose config`` preserves the source escape as ``$$``; Compose converts it to
    # one dollar when it creates the container. Mirror that final argv here before executing it.
    return entrypoint[2].replace("$$", "$")


def _fake_mc(tmp_path: Path) -> Path:
    calls = tmp_path / "mc-calls"
    executable = tmp_path / "mc"
    executable.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        'printf \'%s\\n\' "$*" >>"$MC_CALLS"\n'
        'case "$*" in\n'
        '  "version info --json "*)\n'
        '    [ "${MC_INFO_FAIL:-0}" = 0 ] || exit 23\n'
        "    printf '%s\\n' \"$MC_VERSION_INFO\"\n"
        "    ;;\n"
        "esac\n"
    )
    executable.chmod(0o755)
    return calls


def _run_minio_init(
    tmp_path: Path, reply: str, *, info_fails: bool = False
) -> tuple[int, list[str]]:
    calls = _fake_mc(tmp_path)
    result = subprocess.run(
        ["/bin/bash", "-c", _minio_init_script()],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "MC_CALLS": str(calls),
            "MC_VERSION_INFO": reply,
            "MC_INFO_FAIL": "1" if info_fails else "0",
        },
    )
    return result.returncode, calls.read_text().splitlines()


def _published_ports(service: dict[str, Any]) -> set[str]:
    return {str(p.get("published")) for p in service.get("ports", [])}


def test_compose_config_is_valid() -> None:
    # `docker compose config -q` is the issue's acceptance gate; rendering to JSON
    # exercises the same parse and gives us the model the rest of the file asserts on.
    assert _services()  # non-empty → parsed


@pytest.mark.parametrize(("_case", "reply", "expected"), _VERSIONING_REPLIES)
def test_minio_init_fails_closed_on_bucket_versioning(
    tmp_path: Path, _case: str, reply: str, expected: int
) -> None:
    returncode, calls = _run_minio_init(tmp_path, reply)
    assert (returncode == 0) is (expected == 0), _case
    assert calls[:3] == [
        "alias set local http://minio:9000 minioadmin minioadmin",
        "mb --ignore-existing local/kdive-artifacts",
        "version enable local/kdive-artifacts",
    ]
    assert calls[3:] == ["version info --json local/kdive-artifacts"]


def test_minio_init_propagates_version_info_command_failure(tmp_path: Path) -> None:
    returncode, calls = _run_minio_init(tmp_path, "", info_fails=True)
    assert returncode != 0
    assert calls[-1] == "version info --json local/kdive-artifacts"


def test_migrate_one_shot_runs_command_and_waits_for_postgres() -> None:
    migrate = _services()["migrate"]
    assert migrate["command"] == ["migrate"]
    assert migrate["depends_on"]["postgres"]["condition"] == "service_healthy"


@pytest.mark.parametrize("service", _APP_SERVICES)
def test_app_service_waits_for_migrate_completion(service: str) -> None:
    # The ADR-0088 ordering fix: completion, not mere start. A bare depends_on
    # (condition "service_started") would let the app hit the DB pre-migration.
    dep = _services()[service]["depends_on"]
    assert dep["migrate"]["condition"] == "service_completed_successfully"


_LONG_RUNNING_SERVICES = (*_APP_SERVICES, "postgres", "minio", "oidc")


@pytest.mark.parametrize("service", ("prometheus", "grafana"))
def test_obs_profile_service_restarts_too(service: str) -> None:
    # Same policy, rendered through the obs profile because these are absent from the
    # default model. They are named separately rather than folded into the list below so
    # this file keeps saying which services the default graph contains.
    assert _services_with_obs_profile()[service]["restart"] == "on-failure"


@pytest.mark.parametrize("service", _LONG_RUNNING_SERVICES)
def test_long_running_service_restarts_so_an_outage_is_recoverable(service: str) -> None:
    # ADR-0449 makes an unreachable database at start exit the process, and puts the retry
    # in the supervisor. `depends_on` only orders the *first* `up`, so without a restart
    # policy every later recreate during a backend outage — a postgres image bump, a bare
    # `compose restart` — leaves the app container Exited(1) permanently, where before it
    # came up and recovered on its own.
    #
    # The backends carry it too: policing only the app tier leaves them restarting against a
    # backend that can never answer, which reads as transient while being unable to progress.
    #
    # `on-failure`, not `unless-stopped`, on two counts. It is the policy ADR-0114 section 4
    # documents and the systemd units ship, which is the contract ADR-0449 cites. And
    # `unless-stopped` additionally starts a container on *daemon start*, which would make
    # this stack's demo-credential MinIO and token-minting mock issuer — both published on
    # host ports — come back on every reboot of any machine that ever ran the stack.
    #
    # The `migrate` and `minio-init` one-shots are deliberately excluded: they are meant to
    # run once and exit, and `on-failure` would still re-run a genuinely failing one forever.
    assert _services()[service]["restart"] == "on-failure"


@pytest.mark.parametrize("service", _APP_SERVICES)
def test_app_service_waits_for_bucket_creation(service: str) -> None:
    # All three app processes do object-store I/O, so they wait for the minio-init
    # one-shot to complete — which transitively guarantees minio is healthy and the
    # artifacts bucket exists. Without this edge a bare `up <service>` starts a
    # process whose first S3 call fails (no bucket).
    dep = _services()[service]["depends_on"]
    assert dep["minio-init"]["condition"] == "service_completed_successfully"


def test_server_waits_for_the_issuer() -> None:
    # The server validates bearer tokens against the issuer; a bare `up server`
    # must start oidc too. The mock issuer has no healthcheck, so this is a
    # start-ordering edge, not a health gate.
    dep = _services()["server"]["depends_on"]
    assert "oidc" in dep


@pytest.mark.parametrize("service", ("migrate", *_APP_SERVICES))
def test_shared_backend_env_is_merged_into_every_app_service(service: str) -> None:
    # Shared non-database backends remain present beside each process-specific DSN.
    env = _services()[service]["environment"]
    assert env["KDIVE_DATABASE_URL"].startswith("postgresql://")


def test_server_binds_all_interfaces_and_publishes_its_port() -> None:
    server = _services()["server"]
    assert server["environment"]["KDIVE_HTTP_HOST"] == "0.0.0.0"
    assert "8000" in _published_ports(server)


# Configurable host-published ports: the publish (left) side is a ${VAR:-default} override; the
# container-internal port and the env the process binds inside stay fixed. Each case renders the
# model with the override set and asserts ONLY the host mapping moved.
@pytest.mark.parametrize(
    ("service", "env_var", "internal", "obs"),
    [
        ("postgres", "KDIVE_POSTGRES_PORT", "5432", False),
        ("minio", "KDIVE_MINIO_PORT", "9000", False),
        ("oidc", "KDIVE_OIDC_PORT", "8080", False),
        ("server", "KDIVE_HTTP_PORT", "8000", False),
        ("prometheus", "KDIVE_PROMETHEUS_PORT", "9090", True),
        ("grafana", "KDIVE_GRAFANA_PORT", "3000", True),
    ],
)
def test_backend_host_port_is_overridable(
    service: str, env_var: str, internal: str, obs: bool
) -> None:
    override = "17777"
    svc = _config({env_var: override}, obs=obs)["services"][service]
    published = _published_ports(svc)
    assert override in published, f"{service}: override {env_var} did not move the host port"
    assert internal not in published, f"{service}: default host port must be gone after override"
    # The container-internal target is unchanged: the service still binds its canonical port.
    targets = {str(p.get("target")) for p in svc.get("ports", [])}
    assert internal in targets


# Per-process aux health/metrics ports (ADR-0090 §5): the loopback default ports the
# bind decision keeps, but bound to 0.0.0.0 inside each container so the compose
# healthcheck (and an in-network scrape) can reach a side port that is never published.
_AUX_PORTS = {"server": 9464, "worker": 9465, "reconciler": 9466}


@pytest.mark.parametrize("service", _APP_SERVICES)
def test_aux_health_listener_binds_container_interface_on_its_port(service: str) -> None:
    # Each process runs in its own container/network namespace, so the aux listener is
    # bound 0.0.0.0:<per-process-port> via an explicit KDIVE_HEALTH_BIND_ADDR (loopback
    # default is not reachable by a healthcheck that execs in the container's PID/net ns
    # only via 127.0.0.1, and never by an in-network scrape). The container ns IS the
    # trust boundary: the aux port is never published to the host.
    env = _services()[service]["environment"]
    assert env["KDIVE_HEALTH_BIND_ADDR"] == f"0.0.0.0:{_AUX_PORTS[service]}"


@pytest.mark.parametrize("service", _APP_SERVICES)
def test_aux_port_is_not_published_to_the_host(service: str) -> None:
    # The aux listener carries no auth; the network boundary is its access control. It
    # must never be in a host port mapping (only the server's 8000 MCP port is).
    published = {str(p.get("published")) for p in _services()[service].get("ports", [])}
    assert str(_AUX_PORTS[service]) not in published


@pytest.mark.parametrize("service", _APP_SERVICES)
def test_app_service_has_healthcheck_against_its_aux_readyz(service: str) -> None:
    # The compose liveness/readiness wiring probes /readyz on the process's own aux port.
    healthcheck = _services()[service]["healthcheck"]
    test_cmd = healthcheck["test"]
    joined = " ".join(test_cmd) if isinstance(test_cmd, list) else str(test_cmd)
    assert f"127.0.0.1:{_AUX_PORTS[service]}/readyz" in joined


# --- Opt-in Prometheus on the `obs` profile (ADR-0189) ------------------------------------

_PROM_CONFIG = Path(__file__).resolve().parents[2] / "deploy" / "compose" / "prometheus.yml"


def _services_with_obs_profile() -> dict[str, Any]:
    return _config(obs=True)["services"]


def test_prometheus_service_is_obs_profile_only() -> None:
    # Present when the profile is active...
    assert "prometheus" in _services_with_obs_profile()
    # ...and absent from the default (no-profile) model, so the turnkey graph is unchanged.
    assert "prometheus" not in _services()
    assert _services_with_obs_profile()["prometheus"]["profiles"] == ["obs"]


def test_prometheus_publishes_ui_but_not_the_scraped_ports() -> None:
    prom = _services_with_obs_profile()["prometheus"]
    published = _published_ports(prom)
    assert "9090" in published
    for aux in _AUX_PORTS.values():
        assert str(aux) not in published


def test_prometheus_static_config_targets_every_aux_port() -> None:
    # docker compose config validates the compose file, never the mounted prometheus.yml, so
    # parse the committed scrape config and assert its static targets match the aux-port contract.
    cfg = yaml.safe_load(_PROM_CONFIG.read_text())
    targets: set[str] = set()
    for job in cfg["scrape_configs"]:
        for static in job["static_configs"]:
            targets.update(static["targets"])
    assert targets == {f"{svc}:{port}" for svc, port in _AUX_PORTS.items()}


def test_prometheus_static_config_passes_promtool() -> None:
    # Bonus semantic check over the content assertions above: promtool is the only thing that
    # validates the Prometheus DSL itself. Skips cleanly when absent (it is not on every runner).
    if shutil.which("promtool") is None:
        pytest.skip("promtool not installed")
    res = subprocess.run(
        ["promtool", "check", "config", str(_PROM_CONFIG)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stdout + res.stderr


# --- Every image-declared VOLUME is mounted explicitly (ADR-0552) --------------------------

#: The complete rendered mount set of every service whose *image* declares a `VOLUME`, as
#: sorted (type, source, target) tuples. Full-set equality, not membership: a mount that
#: disappears, changes path, or displaces another reddens the case that names it. Sorted
#: rather than in declaration order, which is a cosmetic choice nothing should depend on.
#:
#: With nothing mounted at the image's `VOLUME` path, Compose allocates an anonymous volume
#: there on every `up`, and a `down` without `--volumes` orphans it rather than removing it —
#: so the stack silently restarts empty while `just compose-stop` and both operator guides
#: promise a plain teardown preserves state (#1911). Bind sources are repo-relative so the
#: expectation carries no checkout path.
#:
#: `prometheus` gets tmpfs rather than a named volume (see the case below). grafana is absent
#: on purpose: `grafana/grafana:13.0.3` declares no `VOLUME`, so it has nothing to cover.
_EXPECTED_MOUNTS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "postgres": (
        (
            "bind",
            "deploy/compose/bootstrap-migration-owner.sql",
            "/docker-entrypoint-initdb.d/010-migration-owner.sql",
        ),
        ("volume", "kdive-pgdata", "/var/lib/postgresql/data"),
    ),
    "minio": (("volume", "kdive-minio-data", "/data"),),
    "prometheus": (("bind", "deploy/compose/prometheus.yml", "/etc/prometheus/prometheus.yml"),),
}

#: Derived from the mount table above, so the two can never disagree.
_NAMED_DATA_VOLUMES = {
    source for mounts in _EXPECTED_MOUNTS.values() for kind, source, _ in mounts if kind == "volume"
}


def _rendered_mounts(service: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    rendered = []
    for mount in service.get("volumes") or ():
        source = mount["source"]
        if mount["type"] == "bind":
            source = Path(source).relative_to(_COMPOSE_FILE.parent).as_posix()
        rendered.append((mount["type"], source, mount["target"]))
    return tuple(sorted(rendered))


@pytest.mark.parametrize("service", sorted(_EXPECTED_MOUNTS))
def test_image_declared_volume_is_mounted_explicitly(service: str) -> None:
    # `prometheus` renders only under the obs profile; asking for it costs the other two nothing.
    assert _rendered_mounts(_services_with_obs_profile()[service]) == tuple(
        sorted(_EXPECTED_MOUNTS[service])
    )


@pytest.mark.parametrize("volume", sorted(_NAMED_DATA_VOLUMES))
def test_named_data_volume_is_declared_in_the_project_volumes_block(volume: str) -> None:
    # A mount naming a volume the top-level block never declares is an external-volume
    # reference to Compose, which fails the `up` outright. Assert the declaration and that
    # the resolved name is project-scoped, which is what makes `down --volumes` reach it.
    declared = _config(obs=True)["volumes"]
    assert volume in declared, sorted(declared)
    assert declared[volume]["name"].endswith(f"_{volume}")


def test_prometheus_tsdb_is_tmpfs_rather_than_a_named_volume() -> None:
    # ADR-0189 decided the demo TSDB is ephemeral; the anonymous volume it actually got was
    # orphaned, not dropped. tmpfs makes that decision true without adding a volume that
    # `just compose-down` cannot reach: that recipe runs a profile-less `down --volumes`,
    # which does not stop this profile-gated service, so a named volume here would survive
    # every destructive path except `scripts/live-stack/down.sh --wipe`.
    #
    # Every option is load-bearing, not decoration. The image runs as uid/gid 65534, and a
    # default-mode tmpfs (root-owned 0775) makes prometheus panic on its first write, so the
    # mode and ownership must be set; 0700 rather than a world-writable 1777 keeps a second
    # uid in the container from planting TSDB files. Without `size=` the mount defaults to
    # half of host RAM, which moves unbounded growth from disk to memory.
    #
    # The complementary half — that no named volume is mounted at /prometheus — is the
    # `prometheus` case of the mount table above. Asserting the *absence* of a top-level
    # `kdive-prom-data` declaration here would prove nothing: Compose prunes declared-but-
    # unreferenced volumes from the rendered model, so that check cannot fail.
    prometheus = _services_with_obs_profile()["prometheus"]
    assert prometheus["tmpfs"] == ["/prometheus:mode=0700,uid=65534,gid=65534,size=256m"]
