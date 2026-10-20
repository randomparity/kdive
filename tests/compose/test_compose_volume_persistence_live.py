"""Executable proof that the reference Compose data volumes survive a plain `down` (ADR-0552).

Drives the committed ``docker-compose.yml`` under a unique project name and free host ports,
starting only three services. SeaweedFS is built once from the production Compose source
before timed starts; each start disables rebuilding. No other service starts.

The proof never invokes ``just compose-down`` or ``scripts/live-stack/stack-down.sh``: both act on
the *default* project, which is the operator's own stack, and ``stack-down.sh --wipe`` additionally
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
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import boto3
import psycopg
import pytest
from botocore import UNSIGNED
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

pytestmark = pytest.mark.live_stack

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _ROOT / "docker-compose.yml"

#: Service → (volume suffix, the container path it is mounted at). Mirrors the structural
#: expectation in ``test_compose_config.py``; this module proves the runtime consequence.
_DATA_MOUNTS = {
    "postgres": ("kdive-pgdata", "/var/lib/postgresql/data"),
    "seaweedfs": ("kdive-seaweedfs-data", "/data"),
}

#: prometheus is started too, but its image `VOLUME /prometheus` is covered by tmpfs rather
#: than a named volume (ADR-0189, ADR-0552), so it is proven by absence of any volume mount
#: at that path rather than by a persistence round trip.
_TMPFS_SERVICE = "prometheus"
_TMPFS_PATH = "/prometheus"

#: Explicit service selection excludes the app graph; SeaweedFS has its own preparation.
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


class _ProofDeadline(BaseException):
    """A phase stop must escape the readiness loops that retry Exception."""


def _run(argv: tuple[str, ...], env: dict[str, str], *, timeout: int = 180) -> str:
    remaining = float(env.get("_KDIVE_PROOF_DEADLINE", "inf")) - time.monotonic()
    if remaining <= 0:
        raise _ProofDeadline("Compose proof phase deadline expired")
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=min(timeout, remaining),
        check=True,
        env={
            key: value
            for key, value in {**os.environ, **env}.items()
            if key not in {"_KDIVE_PROOF_DEADLINE", "_KDIVE_PROOF_IMAGE_ID"}
        },
    )
    return result.stdout


def _compose(env: dict[str, str], *args: str, timeout: int = 180) -> str:
    # `--profile obs` renders prometheus; without it compose drops the service silently.
    started = time.monotonic()
    progress = ("--progress", "plain") if args and args[0] == "build" else ()
    try:
        return _run(
            ("docker", "compose", "-f", str(_COMPOSE_FILE), "--profile", "obs", *progress, *args),
            env,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"compose {args[0]} failed: {exc}; stdout={getattr(exc, 'stdout', None)!r}; "
            f"stderr={getattr(exc, 'stderr', None)!r}",
            flush=True,
        )
        raise
    finally:
        if args and args[0] in {"build", "up", "down"}:
            # Visible on successful CI runs through --capture=tee-sys, including cleanup.
            print(f"compose {' '.join(args)}: {time.monotonic() - started:.3f} seconds", flush=True)


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


def _volumes_held_by_project(env: dict[str, str]) -> set[str]:
    """Every volume any started service is attached to, from the containers themselves.

    Not a `docker volume ls` filter: Compose gives daemon-created anonymous volumes no project
    label, so a filtered query cannot see the defect. Not a host-wide `volume ls` delta either
    — this repo's disposable-Postgres fixtures create anonymous volumes continuously, so a
    global before/after would redden on another process's work.
    """
    return {
        mount["Name"]
        for service in _SERVICES
        for mount in _container_mounts(env, service)
        if mount["Type"] == "volume"
    }


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
        aws_access_key_id="kdive",
        aws_secret_access_key="kdive-demo-secret",  # pragma: allowlist secret — compose dev literal
        region_name="us-east-1",
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 1}),
    )
    deadline = time.monotonic() + deadline_s
    while True:
        try:
            client.list_buckets()
            anonymous = boto3.client(
                "s3",
                endpoint_url=endpoint,
                region_name="us-east-1",
                config=BotoConfig(signature_version=UNSIGNED),
            )
            with pytest.raises(ClientError) as denied:
                anonymous.list_buckets()
            assert denied.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403
            return client
        except Exception:  # noqa: BLE001 - any startup error is retried until the deadline
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


def _s3_bucket_when_visible(client, bucket: str, *, deadline_s: float = 60.0) -> int:  # noqa: ANN202 - botocore client
    """Wait until a newly created bucket is observable through its S3 head operation."""
    deadline = time.monotonic() + deadline_s
    while True:
        try:
            status = client.head_bucket(Bucket=bucket)["ResponseMetadata"]["HTTPStatusCode"]
            if status == 200:
                return status
        except Exception:  # noqa: BLE001 - the local S3 service can lag after a volume reset
            pass
        if time.monotonic() >= deadline:
            raise AssertionError(f"S3 bucket {bucket!r} did not become visible before the deadline")
        time.sleep(0.2)


def test_bucket_visibility_waits_for_the_created_bucket(monkeypatch) -> None:
    class _Client:
        responses = iter(
            (
                ClientError({"Error": {"Code": "NoSuchBucket"}}, "HeadBucket"),
                {"ResponseMetadata": {"HTTPStatusCode": 200}},
            )
        )

        def head_bucket(self, *, Bucket: str):  # noqa: N803 - S3 operation keyword
            response = next(self.responses)
            if isinstance(response, Exception):
                raise response
            return response

    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    assert _s3_bucket_when_visible(_Client(), "fresh-bucket") == 200

    assert sleeps == [0.2]


def test_bucket_visibility_fails_at_its_deadline() -> None:
    class _Client:
        def head_bucket(self, *, Bucket: str):  # noqa: N803 - S3 operation keyword
            raise ClientError({"Error": {"Code": "NoSuchBucket"}}, "HeadBucket")

    with pytest.raises(AssertionError, match="did not become visible"):
        _s3_bucket_when_visible(_Client(), "fresh-bucket", deadline_s=0)


@contextmanager
def _isolated_stack() -> Iterator[tuple[dict[str, str], str, str, str]]:
    if sys.platform != "linux" or threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Compose proof requires the Linux main thread for phase deadlines")
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    if previous_timer != (0, 0):
        raise RuntimeError("Compose proof requires an idle SIGALRM timer")
    previous_handler = signal.getsignal(signal.SIGALRM)
    token = uuid.uuid4().hex
    project = f"kdive-volume-proof-{token}"
    postgres_port, seaweedfs_port, prometheus_port = _free_ports(3)
    env = {
        "COMPOSE_PROJECT_NAME": project,
        "KDIVE_SEAWEEDFS_IMAGE": f"kdive-volume-proof:{token}",
        # Every published host port is overridden to an isolated loopback address and port:
        # the defaults (5432/8333/9090 on 127.0.0.1, ADR-0554) would still collide with
        # an operator's running stack on the same host.
        "KDIVE_POSTGRES_PORT": f"127.0.0.1:{postgres_port}",
        "KDIVE_SEAWEEDFS_PORT": f"127.0.0.1:{seaweedfs_port}",
        "KDIVE_PROMETHEUS_PORT": f"127.0.0.1:{prometheus_port}",
    }
    dsn = f"postgresql://{_PG_CREDENTIALS}@127.0.0.1:{postgres_port}/kdive"
    endpoint = f"http://127.0.0.1:{seaweedfs_port}"

    def expire(_signum, _frame):
        raise _ProofDeadline("isolated Compose proof phase deadline expired")

    owned = False
    primary = None
    tag = env["KDIVE_SEAWEEDFS_IMAGE"]
    signal.signal(signal.SIGALRM, expire)
    try:
        env["_KDIVE_PROOF_DEADLINE"] = str(time.monotonic() + 2400)
        signal.setitimer(signal.ITIMER_REAL, 2400)
        assert not _run(
            ("docker", "image", "ls", "--quiet", "--no-trunc", tag), env, timeout=60
        ).strip(), f"invocation image tag already exists: {tag}"
        owned = True
        print(f"preparing production SeaweedFS source as {tag}", flush=True)
        _compose(env, "build", "seaweedfs", timeout=600)
        image_id = _run(
            ("docker", "image", "inspect", "--format", "{{.Id}}", tag), env, timeout=60
        ).strip()
        assert image_id, f"prepared image missing: {tag}"
        env["_KDIVE_PROOF_IMAGE_ID"] = image_id
        print(f"prepared {tag}: {image_id}", flush=True)
        yield env, project, dsn, endpoint
    except BaseException as exc:
        primary = exc
        if isinstance(exc, (subprocess.TimeoutExpired, _ProofDeadline)):
            exc.add_note(
                f"Docker CLI/phase stopped; daemon completion/cleanup uncertain; tag={tag}"
            )
        raise
    finally:
        try:
            signal.setitimer(signal.ITIMER_REAL, 0)
            env["_KDIVE_PROOF_DEADLINE"] = str(time.monotonic() + 600)
            signal.setitimer(signal.ITIMER_REAL, 600)
            if owned:
                _teardown(env, project)
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"cleanup incomplete for {tag}: {cleanup}")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _teardown(env: dict[str, str], project: str) -> None:
    errors: list[str] = []
    try:
        _compose(env, "down", "--volumes", "--remove-orphans", timeout=180)
    except Exception as exc:  # noqa: BLE001 - every absence probe must still run
        errors.append(f"down: {exc}")
    for kind, argv in (
        ("volumes", ("docker", "volume", "ls", "--quiet")),
        ("containers", ("docker", "ps", "--all", "--quiet", "--filter", f"name={project}")),
        ("networks", ("docker", "network", "ls", "--quiet", "--filter", f"name={project}")),
    ):
        try:
            left = _run(argv, env, timeout=60).strip()
            if kind == "volumes":
                left = " ".join(name for name in left.split() if name.startswith(f"{project}_"))
            if left:
                errors.append(f"{kind} remain: {left}")
        except Exception as exc:  # noqa: BLE001 - remaining cleanup probes must run
            errors.append(f"{kind}: {exc}")
    tag = env["KDIVE_SEAWEEDFS_IMAGE"]
    for action in ("remove", "verify"):
        try:
            present = _run(
                ("docker", "image", "ls", "--quiet", "--no-trunc", tag), env, timeout=60
            ).strip()
            if present and action == "remove":
                _run(("docker", "image", "rm", tag), env, timeout=60)
            elif present:
                errors.append(f"image tag remains: {tag}")
        except Exception as exc:  # noqa: BLE001 - verification follows removal failure
            errors.append(f"image {action} {tag}: {exc}")
    if errors:
        raise AssertionError(f"isolated Compose volume proof cleanup failed: {'; '.join(errors)}")


def _up(env: dict[str, str]) -> None:
    # Always the explicit service list: a bare `up -d` would start the whole default graph
    # and build the app image via `migrate`, which defeats the isolation this proof relies on.
    _compose(
        env, "up", "-d", "--no-build", "--wait", "--wait-timeout", "180", *_SERVICES, timeout=360
    )
    actual = _run(
        ("docker", "inspect", "--format", "{{.Image}}", _container_id(env, "seaweedfs")),
        env,
        timeout=60,
    ).strip()
    assert actual == env["_KDIVE_PROOF_IMAGE_ID"], (actual, env["_KDIVE_PROOF_IMAGE_ID"])


def test_plain_down_preserves_backend_state_and_down_volumes_resets_it() -> None:
    _require_runnable()
    with _isolated_stack() as (env, project, dsn, endpoint):
        expected_volumes = {
            service: f"{project}_{volume}" for service, (volume, _) in _DATA_MOUNTS.items()
        }
        bucket = project
        payload = project.encode()

        # --- arm 1: bring up, prove the mounts are the project's named volumes, write markers
        _up(env)
        # Criterion 3, read from the project's own containers rather than from a host-wide
        # `docker volume ls` delta: every volume any started service holds, at any path, is
        # one of the two expected names. An anonymous volume anywhere in the project reddens
        # it, and nothing another process does on this host can.
        assert _volumes_held_by_project(env) == set(expected_volumes.values())
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
        with pytest.raises(ClientError) as missing_bucket:
            s3.get_object(Bucket=bucket, Key=_MARKER_KEY)
        assert missing_bucket.value.response["Error"]["Code"] in {"NoSuchBucket", "NoSuchKey"}
        s3.create_bucket(Bucket=bucket)
        assert _s3_bucket_when_visible(s3, bucket) == 200


@pytest.fixture
def image_proof_boundary(monkeypatch):
    """A shared image ID has two tags; only the proof's name may be removed."""
    images = {"sibling:cache": "sha256:prepared"}
    calls = []
    timer = [0.0, 0.0]
    monkeypatch.setattr(sys.modules[__name__], "_free_ports", lambda _: [1, 2, 3])
    monkeypatch.setattr(signal, "getitimer", lambda _: tuple(timer))
    monkeypatch.setattr(
        signal,
        "setitimer",
        lambda _, seconds, interval=0: timer.__setitem__(slice(None), [seconds, interval]),
    )
    monkeypatch.setattr(signal, "getsignal", lambda _: signal.SIG_DFL)
    monkeypatch.setattr(signal, "signal", lambda *args: calls.append(("signal", *args)))

    def run(argv, env, *, timeout=180):
        calls.append(argv)
        tag = env.get("KDIVE_SEAWEEDFS_IMAGE", "unused")
        if argv[1:3] == ("image", "ls"):
            return images.get(argv[-1], "")
        if argv[1:3] == ("image", "rm"):
            images.pop(argv[-1])
        if argv[1:3] == ("image", "inspect"):
            return images[tag]
        if argv[1] == "inspect":
            return "sha256:prepared"
        return ""

    def compose(env, *args, timeout=180):
        calls.append(args)
        calls.append(("timeout", args[0], timeout))
        if args[0] == "build":
            images[env["KDIVE_SEAWEEDFS_IMAGE"]] = "sha256:prepared"
        return "container"

    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_run", run)
    monkeypatch.setattr(module, "_compose", compose)
    return images, calls, timer


def test_prepares_once_before_yield_and_cleans_exact_tag(image_proof_boundary, monkeypatch):
    images, calls, timer = image_proof_boundary
    monkeypatch.setenv("KDIVE_SEAWEEDFS_IMAGE", "sibling:cache")
    with _isolated_stack() as (env, *_):
        tag = env["KDIVE_SEAWEEDFS_IMAGE"]
        assert tag != "sibling:cache"
        assert env["_KDIVE_PROOF_IMAGE_ID"] == "sha256:prepared"
        assert sum(call[0] == "build" for call in calls) == 1
        assert ("timeout", "build", 600) in calls
        _up(env)
        assert any("--no-build" in call and "180" in call for call in calls)
        assert ("timeout", "up", 360) in calls
    assert images == {"sibling:cache": "sha256:prepared"}
    assert timer == [0, 0]
    assert ("docker", "image", "rm", tag) in calls


@pytest.mark.parametrize("failure", ["proof", "build", "cleanup", "cleanup_only"])
def test_primary_failure_survives_owned_cleanup(image_proof_boundary, monkeypatch, failure):
    images, calls, timer = image_proof_boundary
    module = sys.modules[__name__]
    original = module._compose

    def compose(env, *args, timeout=180):
        result = original(env, *args, timeout=timeout)
        if args[0] == failure:
            raise ValueError(failure)
        if failure.startswith("cleanup") and args[0] == "down":
            raise ValueError("cleanup")
        return result

    monkeypatch.setattr(module, "_compose", compose)
    with (
        pytest.raises(
            AssertionError if failure == "cleanup_only" else ValueError,
            match="proof" if failure == "cleanup" else failure.replace("_only", ""),
        ) as error,
        _isolated_stack(),
    ):
        if failure != "cleanup_only":
            raise ValueError("proof")
    assert images == {"sibling:cache": "sha256:prepared"}
    assert timer == [0, 0]
    if failure == "cleanup":
        assert "cleanup" in error.value.__notes__[0]


@pytest.mark.parametrize(
    ("phase", "expected", "message"),
    [
        ("work", _ProofDeadline, "phase"),
        ("cleanup", _ProofDeadline, "cleanup"),
        ("both", ValueError, "proof"),
        ("transition", ValueError, "proof"),
    ],
)
def test_deadline_stops_phase_and_restores_timer(
    image_proof_boundary, monkeypatch, phase, expected, message
):
    images, calls, timer = image_proof_boundary
    primary = ValueError("proof")
    if phase in {"cleanup", "both"}:

        def stop(*args):
            raise _ProofDeadline("cleanup")

        monkeypatch.setattr(sys.modules[__name__], "_teardown", stop)
    if phase == "transition":
        original_set = signal.setitimer

        def expire_on_cancel(which, seconds, interval=0):
            if seconds == 0 and timer[0] == 2400:
                original_set(which, 0)
                handler = next(call[2] for call in calls if call[0] == "signal")
                handler(signal.SIGALRM, None)
            return original_set(which, seconds, interval)

        monkeypatch.setattr(signal, "setitimer", expire_on_cancel)
    with pytest.raises(expected, match=message) as error, _isolated_stack():
        if phase in {"both", "transition"}:
            raise primary
        if phase == "work":
            handler = next(call[2] for call in calls if call[0] == "signal")
            handler(signal.SIGALRM, None)
    if phase in {"both", "transition"}:
        assert error.value is primary
        assert "cleanup" in primary.__notes__[0]
    assert timer == [0, 0]
    assert calls[-1] == ("signal", signal.SIGALRM, signal.SIG_DFL)
    if phase == "work":
        assert images == {"sibling:cache": "sha256:prepared"}


@pytest.mark.parametrize("refusal", ["collision", "probe", "timer", "thread"])
def test_refuses_before_mutating_owned_resources(image_proof_boundary, monkeypatch, refusal):
    images, calls, timer = image_proof_boundary
    module = sys.modules[__name__]
    if refusal in {"collision", "probe"}:

        def run(*args, **kwargs):
            if refusal == "probe":
                raise OSError("probe failed")
            return "sha256:foreign"

        monkeypatch.setattr(module, "_run", run)
    elif refusal == "timer":
        timer[:] = [12, 0]
    else:
        monkeypatch.setattr(threading, "current_thread", lambda: object())
    with pytest.raises((AssertionError, OSError, RuntimeError)), _isolated_stack():
        pytest.fail("refused context yielded")
    assert not any(call[0] in {"build", "down", "docker"} for call in calls)
    assert images == {"sibling:cache": "sha256:prepared"}


def test_started_image_must_match_preparation(image_proof_boundary, monkeypatch):
    with _isolated_stack() as (env, *_):
        env["_KDIVE_PROOF_IMAGE_ID"] = "sha256:different"
        with pytest.raises(AssertionError):
            _up(env)


def test_subprocess_deadline_clips_timeout_and_hides_private_fields(monkeypatch):
    observed = {}
    monkeypatch.setattr(time, "monotonic", lambda: 10)

    def run(argv, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="ok")

    monkeypatch.setattr(subprocess, "run", run)
    env = {"_KDIVE_PROOF_DEADLINE": "15", "_KDIVE_PROOF_IMAGE_ID": "private"}
    assert _run(("unused",), env, timeout=600) == "ok"
    assert observed["timeout"] == 5
    assert not set(env) & observed["env"].keys()
    env["_KDIVE_PROOF_DEADLINE"] = "10"
    with pytest.raises(_ProofDeadline):
        _run(("unused",), env)
