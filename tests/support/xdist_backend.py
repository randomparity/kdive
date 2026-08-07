"""Cross-process coordination for one shared backend container per test run.

Under pytest-xdist each worker is a separate process, so a ``scope="session"``
container fixture would start one container per worker. This helper lets all of a
run's workers share a single container: the per-run temp root holds a
``fcntl.flock`` guard and a refcounted JSON state file, so the first worker starts
the container and the last to leave stops it by id. See ADR-0401.

The refcount only reaps when a run unwinds. A run killed outright — SIGKILL, OOM, a
cancelled CI or agent run — never decrements, so its container used to survive forever
(#1910). Every holder therefore also keeps a **shared** ``flock`` on a per-run
``kdive-<name>.alive`` file for its whole reference lifetime, and every container is
labelled with that file's path. The kernel drops the lock however the process died, so a
later run can tell a crashed run's container (lock free) from a concurrently-running
suite's (lock held) and reap only the former. See ADR-0551.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
import warnings
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

_POOL_MAX_SIZE = 10  # kdive.db.pool.create_pool default
_HEADROOM = 2
_CONNECTIONS_FLOOR = 500

# The `org.testcontainers` namespace is reserved by testcontainers' own `create_labels`,
# which raises on any label under it, so these live in a private `kdive.` namespace.
BACKEND_LABEL = "kdive.test-backend"
LIVENESS_LABEL = "kdive.test-backend-liveness"


def xdist_worker_id() -> str:
    """The xdist worker id (``gw0`` …) or ``master`` under a non-xdist run."""
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


def xdist_worker_count() -> int:
    """Number of xdist workers this run scheduled (1 when not under xdist)."""
    raw = os.environ.get("PYTEST_XDIST_WORKER_COUNT", "").strip()
    return int(raw) if raw else 1


def postgres_max_connections() -> int:
    """``max_connections`` sized for every worker's pool, with a fixed floor."""
    return max(_CONNECTIONS_FLOOR, xdist_worker_count() * _POOL_MAX_SIZE * _HEADROOM)


def worker_namespace_token() -> str:
    """A fresh globally-unique token for one worker's database/bucket name."""
    return uuid.uuid4().hex[:12]


def per_run_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The per-run temp root shared across this run's workers.

    Under xdist a worker's basetemp is ``…/pytest-N/popen-gwK``, so ``.parent`` is
    the run-shared ``…/pytest-N``. Under a non-xdist run ``getbasetemp()`` is already
    the per-run ``…/pytest-N`` and ``.parent`` would be the *persistent* per-user
    root, so use ``getbasetemp()`` itself.
    """
    base = Path(tmp_path_factory.getbasetemp())
    return base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base


def with_database_name(url: str, dbname: str) -> str:
    """Return ``url`` with its path component replaced by ``/dbname``."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


def docker_available() -> bool:
    """True if the Docker daemon answers a ping. Used to decide whether a container
    failure means 'Docker is down' (skip) or a real error (propagate)."""
    try:
        from testcontainers.core.docker_client import DockerClient

        DockerClient().client.ping()
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        return False
    return True


def skip_without_docker() -> None:
    """Skip the calling test when Docker is unusable, unless ``KDIVE_REQUIRE_DOCKER=1``
    (then the test runs and is allowed to fail loudly on a broken runner)."""
    if os.environ.get("KDIVE_REQUIRE_DOCKER") == "1":
        return
    if not docker_available():
        pytest.skip("Docker unavailable")


def _liveness_path(root: Path, name: str) -> Path:
    """The file whose held ``flock`` proves the run owning ``name``'s container is alive."""
    return root / f"kdive-{name}.alive"


def backend_container_labels(root: Path, name: str) -> dict[str, str]:
    """Labels every backend container must carry so a later run can judge it (ADR-0551).

    The liveness path is absolute because the run that reads it back off the container is
    a different process in an unrelated working directory.
    """
    return {BACKEND_LABEL: name, LIVENESS_LABEL: str(_liveness_path(root, name).resolve())}


@contextmanager
def _liveness_held(root: Path, name: str) -> Iterator[None]:
    """Hold a shared lock on the run's liveness file for the caller's whole reference.

    Shared, not exclusive: every xdist worker holding a reference takes one concurrently,
    so the lock is free only once the last of them has let go — or died.
    """
    with open(_liveness_path(root, name), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _run_is_live(liveness_path: Path) -> bool:
    """True while any process still holds the shared lock on ``liveness_path``.

    An absent file means the per-run temp root was rotated away, which pytest does only
    to a finished run's root. Any other error answers "live": the caller reaps on a False,
    so an unreadable lock must never be read as permission to destroy a container.
    """
    if not liveness_path.exists():
        return False
    try:
        with open(liveness_path) as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return True
    return False


def is_stale_backend_container(labels: Mapping[str, str]) -> bool:
    """True when ``labels`` name a backend container whose owning run is gone.

    A container carrying no liveness label is not provably ours — someone else's, or one
    started before ADR-0551 — and is never reported stale.
    """
    recorded = labels.get(LIVENESS_LABEL)
    if not recorded:
        return False
    return not _run_is_live(Path(recorded))


def sweep_stale_backend_containers(client: Any | None = None) -> list[str]:
    """Remove this repo's backend containers whose owning run is gone; return their ids.

    Best-effort by construction: this runs on the way to starting a container, and a
    Docker hiccup while reaping someone else's leak must not fail the caller's run.
    """
    import docker.errors

    try:
        if client is None:
            from testcontainers.core.docker_client import DockerClient

            client = DockerClient().client
        candidates = client.containers.list(all=True, filters={"label": BACKEND_LABEL})
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot sweep now"
        warnings.warn(f"shared_container: stale-backend sweep skipped: {exc}", stacklevel=2)
        return []

    reaped: list[str] = []
    for container in candidates:
        # Reading `.labels` is inside the guard too: docker-py raises on a container whose
        # inspect payload lacks `Config`, and one such container must not abort the sweep
        # and take the caller's run down with it.
        try:
            if not is_stale_backend_container(container.labels):
                continue
            # `v=True` takes the anonymous volume with it. On this path nothing else ever
            # will: `docker volume prune` skips a volume an existing container holds.
            container.remove(force=True, v=True)
        except docker.errors.NotFound:
            continue  # a concurrently sweeping run got there first — not a failure
        except Exception as exc:  # noqa: BLE001 - keep sweeping the rest
            warnings.warn(
                f"shared_container: could not reap stale container {container.id}: {exc}",
                stacklevel=2,
            )
            continue
        reaped.append(container.id)
    return reaped


@contextmanager
def shared_container_or_skip(
    root: Path,
    name: str,
    *,
    start: Callable[[Mapping[str, str]], tuple[str, str]],
    stop: Callable[[str], None],
    require_docker: bool,
) -> Iterator[str]:
    """Yield the shared server URL, turning a genuine Docker-down failure into a skip.

    Drives :func:`shared_container` but scopes the skip to *acquisition*: only when
    ``__enter__`` fails **and** Docker is actually down (and not ``require_docker``)
    does this skip. A failure while Docker is up (disk full, write error) propagates,
    and any failure in the consuming ``with`` body propagates too (the ``yield`` is
    outside the skip-catch). This is the tricky usage contract of ``shared_container``,
    kept here once rather than re-implemented by each fixture.
    """
    manager = shared_container(root, name, start=start, stop=stop)
    try:
        server_url = manager.__enter__()  # runs the whole flock+read+start+write body
    except Exception as exc:
        if require_docker or docker_available():
            raise
        pytest.skip(f"Docker unavailable for testcontainers: {exc}")
    try:
        yield server_url
    finally:
        manager.__exit__(None, None, None)  # refcount decrement / stop-by-id


@contextmanager
def shared_container(
    root: Path,
    name: str,
    *,
    start: Callable[[Mapping[str, str]], tuple[str, str]],
    stop: Callable[[str], None],
) -> Iterator[str]:
    """Yield one shared container's server URL, coordinated across xdist workers.

    ``start(labels)`` returns ``(server_url, container_id)`` and must stamp ``labels``
    onto the container it creates, so a later run can reap it if this one is killed
    (ADR-0551); ``stop(container_id)`` stops it. Exactly one container is alive at a
    time: the first holder starts it, later holders reuse the URL, and the holder that
    releases last stops it.
    """
    lock_path = root / f"kdive-{name}.lock"
    state_path = root / f"kdive-{name}.json"

    # Held for this whole reference, and taken before any container exists, so a
    # container carrying our liveness label always has a live lock behind it.
    with _liveness_held(root, name):
        with _locked(lock_path):
            state = _read_state(state_path)
            if state is None:
                server_url, cid = start(backend_container_labels(root, name))
                state = {"url": server_url, "container_id": cid, "refcount": 1}
                try:
                    _write_state(state_path, state)
                except Exception:
                    # The container is started but its id was never recorded, so no later
                    # release can reap it — stop it now rather than leak it, then re-raise
                    # the real (non-Docker) write failure so it is not masked as a skip.
                    with suppress(Exception):
                        stop(cid)
                    raise
            else:
                state["refcount"] += 1
                server_url = str(state["url"])
                _write_state(state_path, state)

        try:
            yield server_url
        finally:
            # No `return` in this finally: it would swallow a body exception. Guard with
            # an `if state is not None` block instead so any in-flight exception
            # propagates.
            with _locked(lock_path):
                state = _read_state(state_path)
                if state is not None:
                    state["refcount"] -= 1
                    if state["refcount"] <= 0:
                        # Best-effort stop: teardown must never raise — a raise here would
                        # wedge the run and (via a caller's finally: manager.__exit__)
                        # could mask an in-flight body exception. Warn instead of
                        # swallowing silently; always unlink so the next run starts clean
                        # (a failed stop leaks one container; the next run's sweep reaps
                        # it, ADR-0551).
                        try:
                            stop(state["container_id"])
                        except Exception as exc:  # noqa: BLE001
                            warnings.warn(
                                f"shared_container: stop({state['container_id']}) failed: {exc}",
                                stacklevel=2,
                            )
                        finally:
                            state_path.unlink(missing_ok=True)
                    else:
                        _write_state(state_path, state)


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read_state(state_path: Path) -> dict | None:
    try:
        return json.loads(state_path.read_text())
    except FileNotFoundError:
        return None  # legitimately absent → start fresh
    except json.JSONDecodeError:
        # Present but unparseable. Our own writes are atomic (os.replace), so this is
        # never a mid-write of ours — it means external tampering/truncation. Warn
        # (don't silently mask) but still treat as absent so a stray file cannot wedge
        # the whole suite; a genuine live container coexisting with a corrupt file is
        # not producible by this code under the per-run root.
        warnings.warn(
            f"shared_container: corrupt state file {state_path}, starting fresh",
            stacklevel=2,
        )
        return None


def _write_state(state_path: Path, state: dict) -> None:
    # Atomic: write to a temp file in the same dir, then os.replace (never a partial
    # read by another worker under the flock).
    tmp = state_path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, state_path)
