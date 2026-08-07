"""Executable proof that the reference Compose data volumes survive a plain `down` (ADR-0552).

Drives the committed ``docker-compose.yml`` under a unique project name and free host ports,
starting only the three services whose images declare a ``VOLUME``. None of them has a
``build:`` or a ``depends_on``, so nothing is built and no other service starts.

The proof never invokes ``just compose-down`` or ``scripts/live-stack/down.sh``: both act on
the *default* project, which is the operator's own stack, and ``down.sh --wipe`` additionally
destroys every ``kdive-*`` libvirt domain. Every ``--volumes`` call here names this run's own
project.

Volume identity is read from each container's own mounts rather than from
``docker volume ls --filter label=com.docker.compose.project=...``. Compose does not label
daemon-created anonymous volumes with a project, so that filter returns empty on the broken
file as well as the fixed one and cannot detect the defect this proves absent.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import boto3
import psycopg
import pytest
from botocore.client import Config as BotoConfig

pytestmark = pytest.mark.live_stack

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _ROOT / "docker-compose.yml"

#: Service → (volume suffix, the container path it is mounted at). Mirrors the structural
#: expectation in ``test_compose_config.py``; this module proves the runtime consequence.
_DATA_MOUNTS = {
    "postgres": ("kdive-pgdata", "/var/lib/postgresql/data"),
    "minio": ("kdive-minio-data", "/data"),
}

#: prometheus is started too, but its image `VOLUME /prometheus` is covered by tmpfs rather
#: than a named volume (ADR-0189, ADR-0552), so it is proven by absence of any volume mount
#: at that path rather than by a persistence round trip.
_TMPFS_SERVICE = "prometheus"
_TMPFS_PATH = "/prometheus"

#: Every service the proof starts. None has a `build:` or a `depends_on`.
_SERVICES = (*sorted(_DATA_MOUNTS), _TMPFS_SERVICE)

_MARKER_TABLE = "kdive_volume_proof"
_MARKER_KEY = "marker"

#: The compose file's fixed local-development Postgres credentials.
_PG_CREDENTIALS = "kdive:kdive"  # pragma: allowlist secret — compose dev literal


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=30)
    except OSError, subprocess.SubprocessError:
        return False
    return probe.returncode == 0


def _require_runnable() -> None:
    """Skip unless opted in; once opted in, an absent daemon fails rather than skips."""
    if os.environ.get("KDIVE_RUN_COMPOSE_VOLUME_PROOF") != "1":
        pytest.skip("set KDIVE_RUN_COMPOSE_VOLUME_PROOF=1 for the isolated Compose volume proof")
    if _docker_compose_available():
        return
    # This test is the sole carrier of the runtime evidence, so it must not be able to report
    # a skip as proof on a runner that is meant to have Docker.
    if os.environ.get("KDIVE_REQUIRE_DOCKER") == "1":
        pytest.fail("KDIVE_REQUIRE_DOCKER=1 but the docker compose plugin is unavailable")
    pytest.skip("the docker compose plugin is required to drive the volume proof")


def _free_ports(count: int) -> list[int]:
    """Reserve distinct free ports, holding every socket until the last one is chosen.

    Closing each socket before binding the next lets the kernel hand the same port out twice,
    which would make two of this run's four published ports collide.
    """
    listeners = [socket.socket() for _ in range(count)]
    try:
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
        return [int(listener.getsockname()[1]) for listener in listeners]
    finally:
        for listener in listeners:
            listener.close()


def _run(argv: tuple[str, ...], env: dict[str, str], *, timeout: int = 180) -> str:
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
        env={**os.environ, **env},
    )
    return result.stdout


def _compose(env: dict[str, str], *args: str, timeout: int = 180) -> str:
    # `--profile obs` renders prometheus; without it compose drops the service silently.
    return _run(
        ("docker", "compose", "-f", str(_COMPOSE_FILE), "--profile", "obs", *args),
        env,
        timeout=timeout,
    )


def _container_id(env: dict[str, str], service: str) -> str:
    container = _compose(env, "ps", "--quiet", service, timeout=60).strip()
    assert container, f"{service} has no container"
    return container


def _container_mounts(env: dict[str, str], service: str) -> list[dict[str, str]]:
    raw = _run(
        ("docker", "inspect", "--format", "{{json .Mounts}}", _container_id(env, service)),
        env,
        timeout=60,
    )
    return list(json.loads(raw))


def _mounted_volume_name(env: dict[str, str], service: str) -> str:
    """Return the volume name the container actually has at the service's data path."""
    _volume, destination = _DATA_MOUNTS[service]
    for mount in _container_mounts(env, service):
        if mount["Destination"] == destination:
            assert mount["Type"] == "volume", (service, destination, mount["Type"])
            return str(mount["Name"])
    raise AssertionError(f"{service} has no mount at {destination}")


def _assert_tmpfs_leaves_no_volume(env: dict[str, str]) -> None:
    """A tmpfs mount is container-internal, so it appears in no `docker inspect` mount entry.

    That is exactly what makes it the right cover for prometheus: the anonymous volume it used
    to get was orphaned by every plain `down`, and a *named* volume would outlive
    `just compose-down`, which runs a profile-less `down --volumes` that never stops this
    profile-gated service.
    """
    stray = [
        mount
        for mount in _container_mounts(env, _TMPFS_SERVICE)
        if mount["Destination"] == _TMPFS_PATH
    ]
    assert stray == [], stray


def _existing_volumes(env: dict[str, str]) -> set[str]:
    return set(_run(("docker", "volume", "ls", "--quiet"), env, timeout=60).split())


def _connect_when_ready(dsn: str, *, deadline_s: float = 60.0) -> psycopg.Connection[tuple[str]]:
    """Cross the official Postgres image's temporary init-server restart."""
    deadline = time.monotonic() + deadline_s
    while True:
        try:
            return psycopg.connect(dsn, autocommit=True)
        except psycopg.OperationalError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def _s3_when_ready(endpoint: str, *, deadline_s: float = 60.0):  # noqa: ANN202 - botocore client
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",  # pragma: allowlist secret — compose dev literal
        region_name="us-east-1",
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 1}),
    )
    deadline = time.monotonic() + deadline_s
    while True:
        try:
            client.list_buckets()
            return client
        except Exception:  # noqa: BLE001 - any startup error is retried until the deadline
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


@contextmanager
def _isolated_stack() -> Iterator[tuple[dict[str, str], str, str, str]]:
    token = uuid.uuid4().hex[:12]
    project = f"kdive-volume-proof-{token}"
    postgres_port, minio_port, console_port, prometheus_port = _free_ports(4)
    env = {
        "COMPOSE_PROJECT_NAME": project,
        # Every published host port is overridden, and to loopback: the defaults
        # (5432/9000/9001/9090 on 0.0.0.0) would collide with an operator's running stack and
        # would put this run's fixed-credential backends on every host interface.
        "KDIVE_POSTGRES_PORT": f"127.0.0.1:{postgres_port}",
        "KDIVE_MINIO_PORT": f"127.0.0.1:{minio_port}",
        "KDIVE_MINIO_CONSOLE_PORT": f"127.0.0.1:{console_port}",
        "KDIVE_PROMETHEUS_PORT": f"127.0.0.1:{prometheus_port}",
    }
    dsn = f"postgresql://{_PG_CREDENTIALS}@127.0.0.1:{postgres_port}/kdive"
    endpoint = f"http://127.0.0.1:{minio_port}"
    try:
        yield env, project, dsn, endpoint
    finally:
        _teardown(env, project)


def _teardown(env: dict[str, str], project: str) -> None:
    errors: list[str] = []
    try:
        _compose(env, "down", "--volumes", "--remove-orphans", timeout=180)
    except Exception as exc:  # noqa: BLE001 - every absence probe must still run
        errors.append(f"down: {exc}")
    remaining = _existing_volumes(env)
    stranded = sorted(name for name in remaining if name.startswith(f"{project}_"))
    if stranded:
        errors.append(f"volumes remain: {stranded}")
    for kind, argv in (
        ("containers", ("docker", "ps", "--all", "--quiet", "--filter", f"name={project}")),
        ("networks", ("docker", "network", "ls", "--quiet", "--filter", f"name={project}")),
    ):
        left = _run(argv, env, timeout=60).strip()
        if left:
            errors.append(f"{kind} remain: {left}")
    if errors:
        raise AssertionError(f"isolated Compose volume proof cleanup failed: {'; '.join(errors)}")


def _up(env: dict[str, str]) -> None:
    # Always the explicit service list: a bare `up -d` would start the whole default graph
    # and build the app image via `migrate`, which defeats the isolation this proof relies on.
    _compose(env, "up", "-d", "--wait", "--wait-timeout", "180", *_SERVICES, timeout=360)


def test_plain_down_preserves_backend_state_and_down_volumes_resets_it() -> None:
    _require_runnable()
    with _isolated_stack() as (env, project, dsn, endpoint):
        expected_volumes = {
            service: f"{project}_{volume}" for service, (volume, _) in _DATA_MOUNTS.items()
        }
        bucket = project
        payload = project.encode()

        # --- arm 1: bring up, prove the mounts are the project's named volumes, write markers
        before = _existing_volumes(env)
        _up(env)
        # Criterion 3 directly: the `up` created exactly the two expected volumes and no
        # anonymous extra. A set difference, so an unnamed 64-hex volume shows up here even
        # though Compose gives it no project label to filter on.
        assert _existing_volumes(env) - before == set(expected_volumes.values())
        mounted = {service: _mounted_volume_name(env, service) for service in _DATA_MOUNTS}
        # A 64-hex name here is the #1911 defect: an anonymous volume Compose will orphan.
        assert mounted == expected_volumes
        _assert_tmpfs_leaves_no_volume(env)

        with _connect_when_ready(dsn) as conn:
            conn.execute(f"CREATE TABLE {_MARKER_TABLE} (marker text primary key)")  # noqa: S608
            conn.execute(f"INSERT INTO {_MARKER_TABLE} VALUES (%s)", (project,))  # noqa: S608
        s3 = _s3_when_ready(endpoint)
        s3.create_bucket(Bucket=bucket)
        s3.put_object(Bucket=bucket, Key=_MARKER_KEY, Body=payload)

        # --- arm 2: a plain `down` keeps every named volume. Nothing is orphaned: arm 1
        # already proved each data path holds the project's named volume rather than an
        # anonymous one, and that the tmpfs path holds no volume at all.
        _compose(env, "down", timeout=180)
        surviving = _existing_volumes(env)
        assert set(expected_volumes.values()) <= surviving, sorted(expected_volumes.values())

        # --- arm 3: the markers are still there after a fresh `up` — the criterion #1911 fails
        _up(env)
        assert {
            service: _mounted_volume_name(env, service) for service in _DATA_MOUNTS
        } == expected_volumes
        with _connect_when_ready(dsn) as conn:
            rows = conn.execute(f"SELECT marker FROM {_MARKER_TABLE}").fetchall()  # noqa: S608
        assert rows == [(project,)]
        s3 = _s3_when_ready(endpoint)
        assert s3.get_object(Bucket=bucket, Key=_MARKER_KEY)["Body"].read() == payload

        # --- arm 4: `down --volumes` still resets, proven positively rather than by absence
        _compose(env, "down", "--volumes", timeout=180)
        assert not set(expected_volumes.values()) & _existing_volumes(env)

        _up(env)
        with _connect_when_ready(dsn) as conn:
            present = conn.execute(
                "SELECT count(*) FROM pg_tables WHERE tablename = %s", (_MARKER_TABLE,)
            ).fetchone()
            assert present == (0,)
            # The connection still works, so the empty result above is a wiped volume rather
            # than a probe that silently failed.
            conn.execute(f"CREATE TABLE {_MARKER_TABLE} (marker text primary key)")  # noqa: S608
            conn.execute(f"INSERT INTO {_MARKER_TABLE} VALUES ('after-wipe')")  # noqa: S608
            assert conn.execute(f"SELECT marker FROM {_MARKER_TABLE}").fetchall() == [  # noqa: S608
                ("after-wipe",)
            ]
        s3 = _s3_when_ready(endpoint)
        buckets = {entry["Name"] for entry in s3.list_buckets()["Buckets"]}
        assert bucket not in buckets
        s3.create_bucket(Bucket=bucket)
        assert bucket in {entry["Name"] for entry in s3.list_buckets()["Buckets"]}
