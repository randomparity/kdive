"""Cross-process coordination for one shared backend container per test run.

Under pytest-xdist each worker is a separate process, so a ``scope="session"``
container fixture would start one container per worker. This helper lets all of a
run's workers share a single container: the per-run temp root holds a
``fcntl.flock`` guard and a refcounted JSON state file, so the first worker starts
the container and the last to leave stops it by id. See ADR-0401.
"""

from __future__ import annotations

import fcntl
import json
import os
import uuid
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

_POOL_MAX_SIZE = 10  # kdive.db.pool.create_pool default
_HEADROOM = 2
_CONNECTIONS_FLOOR = 500


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


@contextmanager
def shared_container_or_skip(
    root: Path,
    name: str,
    *,
    start: Callable[[], tuple[str, str]],
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
    start: Callable[[], tuple[str, str]],
    stop: Callable[[str], None],
) -> Iterator[str]:
    """Yield one shared container's server URL, coordinated across xdist workers.

    ``start()`` returns ``(server_url, container_id)``; ``stop(container_id)`` stops
    it. Exactly one container is alive at a time: the first holder starts it, later
    holders reuse the URL, and the holder that releases last stops it.
    """
    lock_path = root / f"kdive-{name}.lock"
    state_path = root / f"kdive-{name}.json"

    with _locked(lock_path):
        state = _read_state(state_path)
        if state is None:
            server_url, cid = start()
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
        # No `return` in this finally: it would swallow a body exception. Guard with an
        # `if state is not None` block instead so any in-flight exception propagates.
        with _locked(lock_path):
            state = _read_state(state_path)
            if state is not None:
                state["refcount"] -= 1
                if state["refcount"] <= 0:
                    # Best-effort stop: teardown must never raise — a raise here would
                    # wedge the run and (via a caller's finally: manager.__exit__)
                    # could mask an in-flight body exception. Warn instead of
                    # swallowing silently; always unlink so the next run starts clean
                    # (a failed stop leaks one container, the ADR-0401 residual).
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
