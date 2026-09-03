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
import re
import stat
import time
import uuid
import warnings
from collections.abc import Callable, Iterator, Mapping, Sequence
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
_SWEEP_LOCK_PATH = Path(f"/tmp/kdive-test-backend-sweep-{os.geteuid()}.lock")
_REMOVAL_WAIT_S = 5.0
_REMOVAL_POLL_S = 0.05
_REMOVAL_IN_PROGRESS = re.compile(
    r"removal of container (?P<container_id>\S+?)\s+is already in progress"
)


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


def _probe_docker() -> bool:
    """Ping the Docker daemon once. Every failure mode means "not usable"."""
    try:
        from testcontainers.core.docker_client import DockerClient

        DockerClient().client.ping()
    except Exception:  # noqa: BLE001 - any failure means "not usable"
        return False
    return True


# The session verdict: None until the first probe, then that probe's answer forever.
# Reset only by a test that fabricates a verdict, and only via `monkeypatch` so the real
# one comes back (ADR-0580).
_docker_verdict: bool | None = None


def docker_available() -> bool:
    """Whether this process's Docker daemon answered, probed once and latched (ADR-0580).

    Decides whether a container failure means "Docker is down" (skip) or a real error
    (propagate). The answer is a property of the *session*, not of the instant a given test
    happens to ask: re-pinging per test made a slow ping under suite load indistinguishable
    from an absent daemon, so one test would drop out of a green run and the skip count
    varied between runs on an unchanged tree (#2074).

    Latching cuts both ways on purpose. A daemon that answered at session start keeps
    answering as far as this process is concerned, so a later outage surfaces as a test
    failure rather than a skip; a daemon that was down stays down, so the gated tests skip
    as a set instead of some of them.
    """
    global _docker_verdict
    if _docker_verdict is None:
        _docker_verdict = _probe_docker()
    return _docker_verdict


def skip_without_docker() -> None:
    """Skip the calling test when Docker is unusable, unless ``KDIVE_REQUIRE_DOCKER=1``
    (then the test runs and is allowed to fail loudly on a broken runner).

    The env override answers before the probe and leaves the session verdict undecided, so
    setting it never commits this process to an answer it did not measure.
    """
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

    **Only a missing file answers "dead".** The caller reaps on a False, so every other
    outcome — unreadable, wrong file type, lock held — answers "live". That asymmetry is
    the whole safety property: failing to reap costs one stale container until the next
    run, while reaping wrongly destroys a running suite's backend mid-test.

    A missing file means the per-run temp root was rotated away, which pytest does only to
    a finished run's root.

    One open, not a stat-then-open. The path is read back off a container label, so it is
    neither necessarily one this code wrote nor stable between two calls:

    - ``Path.is_file()`` would answer False for a file this process merely cannot *reach*
      — ``/tmp/pytest-of-<user>`` is mode 0700, so another user's live run is exactly that
      case, and a stat-based guard would read it as reapable and destroy it.
    - Two resolutions of the same path can differ. Checking one and opening the other
      leaves a window for a symlink swap.

    ``O_NONBLOCK`` keeps the open from blocking on a FIFO, and ``fstat`` interrogates the
    object actually opened rather than whatever the name resolves to next.
    """
    try:
        fd = os.open(liveness_path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # unreadable is never permission to destroy
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return True  # not a lock we took, and so not ours to reap either
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        return True
    finally:
        os.close(fd)
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


@contextmanager
def _sweep_locked() -> Iterator[bool]:
    """Safely try to own the process-external stale-backend sweep."""
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(_SWEEP_LOCK_PATH, flags, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid():
            raise OSError("stale-backend sweep lock must be a regular file owned by this user")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _removal_is_already_in_progress(exc: Exception, container_id: str) -> bool:
    """Whether ``exc`` is Docker's concurrent-removal conflict for ``container_id``."""
    import docker.errors

    if not isinstance(exc, docker.errors.APIError) or exc.status_code != 409:
        return False
    match = _REMOVAL_IN_PROGRESS.search(exc.explanation or "")
    return match is not None and match.group("container_id") == container_id


def _wait_until_container_absent(
    client: Any,
    container_id: str,
    *,
    timeout_s: float | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> bool:
    """Poll one container id until absent or the monotonic deadline expires."""
    import docker.errors

    timeout_s = _REMOVAL_WAIT_S if timeout_s is None else timeout_s
    clock = time.monotonic if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    deadline = clock() + timeout_s
    while True:
        try:
            client.containers.get(container_id)
        except docker.errors.NotFound:
            return True
        except Exception:  # noqa: BLE001 - lookup failure preserves the removal warning
            return False
        now = clock()
        if now >= deadline:
            return False
        sleep(min(_REMOVAL_POLL_S, deadline - now))


def _reap_stale_candidates(client: Any, candidates: Sequence[Any]) -> list[str]:
    """Remove stale candidates while preserving per-container best-effort behavior."""
    import docker.errors

    reaped: list[str] = []
    for container in candidates:
        try:
            if not is_stale_backend_container(container.labels):
                continue
            container.remove(force=True, v=True)
        except docker.errors.NotFound:
            continue
        except Exception as exc:  # noqa: BLE001 - keep sweeping the rest
            if _removal_is_already_in_progress(exc, container.id) and _wait_until_container_absent(
                client, container.id
            ):
                continue
            warnings.warn(
                f"shared_container: could not reap stale container {container.id}: {exc}",
                stacklevel=2,
            )
            continue
        reaped.append(container.id)
    return reaped


def sweep_stale_backend_containers(client: Any | None = None) -> list[str]:
    """Remove this repo's backend containers whose owning run is gone; return their ids.

    Best-effort by construction: this runs on the way to starting a container, and a
    Docker hiccup while reaping someone else's leak must not fail the caller's run.
    """
    try:
        with _sweep_locked() as acquired:
            if not acquired:
                return []
            if client is None:
                from testcontainers.core.docker_client import DockerClient

                client = DockerClient().client
            # docker-py inspects every listed id before returning, and re-raises `NotFound`
            # when a container is removed in between unless told not to. That raise lands
            # outside `_reap_stale_candidates`' per-container handler, so a *foreign*
            # container vanishing mid-enumeration would take the whole sweep down to zero
            # ids and leave this run's own leaks behind. The flag is docker-py's documented
            # remedy for exactly this race.
            candidates = client.containers.list(
                all=True, filters={"label": BACKEND_LABEL}, ignore_removed=True
            )
            return _reap_stale_candidates(client, candidates)
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot sweep now"
        warnings.warn(f"shared_container: stale-backend sweep skipped: {exc}", stacklevel=2)
        return []


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
    ``__enter__`` fails **and** this session's latched Docker verdict is "down" (and not
    ``require_docker``) does this skip. A failure while Docker is up (disk full, write
    error) propagates, and any failure in the consuming ``with`` body propagates too (the
    ``yield`` is outside the skip-catch). This is the tricky usage contract of
    ``shared_container``, kept here once rather than re-implemented by each fixture.

    The verdict is the session's, not a fresh ping (ADR-0580), so an outage that begins
    after Docker has already answered reddens the run instead of quietly shrinking it.
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
