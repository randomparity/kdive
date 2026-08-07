from __future__ import annotations

import signal
import subprocess
import sys
import textwrap
import warnings
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from tests.support import xdist_backend


def test_worker_id_defaults_to_master(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert xdist_backend.xdist_worker_id() == "master"
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw7")
    assert xdist_backend.xdist_worker_id() == "gw7"


def test_worker_count_defaults_to_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_XDIST_WORKER_COUNT", raising=False)
    assert xdist_backend.xdist_worker_count() == 1
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "18")
    assert xdist_backend.xdist_worker_count() == 18


def test_max_connections_floor_and_scaling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "4")
    assert xdist_backend.postgres_max_connections() == 500  # floor
    monkeypatch.setenv("PYTEST_XDIST_WORKER_COUNT", "64")
    assert xdist_backend.postgres_max_connections() == 1280  # 64 * 20


def test_with_database_name_replaces_path() -> None:
    url = "postgresql://u:p@host:5432/test"
    assert xdist_backend.with_database_name(url, "kdive_test_gw0_abc") == (
        "postgresql://u:p@host:5432/kdive_test_gw0_abc"
    )


def test_namespace_token_is_unique_and_short() -> None:
    a, b = xdist_backend.worker_namespace_token(), xdist_backend.worker_namespace_token()
    assert a != b and len(a) == 12 and a.isalnum()


class _FakeContainer:
    starts = 0
    stops: list[str] = []
    labels: list[dict[str, str]] = []

    @classmethod
    def start(cls, labels: Mapping[str, str]) -> tuple[str, str]:
        cls.starts += 1
        cls.labels.append(dict(labels))
        return "postgresql://u:p@host:5432/test", f"cid-{cls.starts}"

    @classmethod
    def stop(cls, cid: str) -> None:
        cls.stops.append(cid)


def _acquire(root: Path):
    return xdist_backend.shared_container(
        root, "pg", start=_FakeContainer.start, stop=_FakeContainer.stop
    )


def test_single_start_across_concurrent_holders(tmp_path: Path) -> None:
    _FakeContainer.starts = 0
    _FakeContainer.stops = []
    with _acquire(tmp_path) as url_a:
        with _acquire(tmp_path) as url_b:
            assert url_a == url_b
            assert _FakeContainer.starts == 1  # one real start for two holders
            assert _FakeContainer.stops == []  # not stopped while a holder is active
        assert _FakeContainer.stops == []  # inner release did not stop it
    assert _FakeContainer.stops == ["cid-1"]  # last release stopped exactly once


def test_finish_early_then_reacquire_restarts(tmp_path: Path) -> None:
    _FakeContainer.starts = 0
    _FakeContainer.stops = []
    with _acquire(tmp_path):
        pass  # sole holder finishes -> container stopped, state cleared
    assert _FakeContainer.stops == ["cid-1"]
    with _acquire(tmp_path):
        assert _FakeContainer.starts == 2  # a later holder lazily starts a fresh one
    assert _FakeContainer.stops == ["cid-1", "cid-2"]


def test_corrupt_state_file_warns_and_starts_fresh(tmp_path: Path) -> None:
    _FakeContainer.starts = 0
    _FakeContainer.stops = []
    (tmp_path / "kdive-pg.json").write_text("{ partial")  # externally-corrupted file
    # must not raise JSONDecodeError; warns (does not silently mask) then starts fresh
    with pytest.warns(UserWarning, match="corrupt"), _acquire(tmp_path) as url:
        assert url.endswith("/test")
        assert _FakeContainer.starts == 1


def test_start_then_write_failure_stops_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeContainer.starts = 0
    _FakeContainer.stops = []

    def _boom_write(_path: Path, _state: dict) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(xdist_backend, "_write_state", _boom_write)
    # start() succeeds but recording state fails: the started container must be stopped
    # (not leaked) and the real write error must propagate (not be swallowed).
    with pytest.raises(OSError, match="disk full"), _acquire(tmp_path):
        pass
    assert _FakeContainer.starts == 1
    assert _FakeContainer.stops == ["cid-1"]  # stopped despite never being recorded


def test_stop_failure_warns_but_does_not_raise_and_unlinks(tmp_path: Path) -> None:
    def boom(_cid: str) -> None:
        raise RuntimeError("already removed")

    # teardown must be best-effort: warn, not raise (so it can never mask a body error
    # or wedge the run), and always unlink so the next run starts clean.
    with (
        pytest.warns(UserWarning, match="stop"),
        xdist_backend.shared_container(tmp_path, "pg", start=_FakeContainer.start, stop=boom),
    ):
        pass  # sole holder; refcount hits 0 and stop() raises internally
    assert not (tmp_path / "kdive-pg.json").exists()


# --- crash reaper: liveness lock, staleness predicate, sweep (ADR-0551, #1910) ---------


_REPO_ROOT = Path(xdist_backend.__file__).resolve().parents[2]


def _labels(root: Path, name: str = "pg") -> dict[str, str]:
    return xdist_backend.backend_container_labels(root, name)


def test_labels_name_the_backend_and_point_at_its_liveness_lock(tmp_path: Path) -> None:
    labels = _labels(tmp_path, "minio")
    assert labels["kdive.test-backend"] == "minio"
    # An absolute path, so a sweeping run in any cwd can resolve it.
    liveness = Path(labels["kdive.test-backend-liveness"])
    assert liveness.is_absolute() and liveness.parent == tmp_path


def test_start_is_handed_the_labels_it_must_stamp(tmp_path: Path) -> None:
    _FakeContainer.starts, _FakeContainer.stops, _FakeContainer.labels = 0, [], []
    with _acquire(tmp_path):
        pass
    # The container cannot be swept later unless start() actually receives these.
    assert _FakeContainer.labels == [_labels(tmp_path)]


def test_a_live_holder_makes_its_container_not_stale(tmp_path: Path) -> None:
    _FakeContainer.starts, _FakeContainer.stops, _FakeContainer.labels = 0, [], []
    labels = _labels(tmp_path)
    with _acquire(tmp_path):
        # A concurrent suite's container must survive another run's sweep.
        assert xdist_backend.is_stale_backend_container(labels) is False
    # …and become reapable only once every holder has let go.
    assert xdist_backend.is_stale_backend_container(labels) is True


def test_a_second_holder_keeps_the_container_live(tmp_path: Path) -> None:
    _FakeContainer.starts, _FakeContainer.stops, _FakeContainer.labels = 0, [], []
    labels = _labels(tmp_path)
    with _acquire(tmp_path):
        with _acquire(tmp_path):
            assert xdist_backend.is_stale_backend_container(labels) is False
        # One xdist worker finishing must not expose the container to the sweep while
        # another still holds it — this is why the lock is shared, not exclusive.
        assert xdist_backend.is_stale_backend_container(labels) is False
    assert xdist_backend.is_stale_backend_container(labels) is True


def test_a_sigkilled_run_leaves_its_container_stale(tmp_path: Path) -> None:
    """The failure mode #1910 is about: no finalizer runs, yet the container is reapable.

    A `pytest_sessionfinish` or `atexit` backstop cannot pass this test, which is why the
    liveness lock is the predicate — the kernel drops it however the process died.
    """
    labels = _labels(tmp_path)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(_REPO_ROOT)!r})
                from pathlib import Path
                from tests.support import xdist_backend

                with xdist_backend.shared_container(
                    Path({str(tmp_path)!r}),
                    "pg",
                    start=lambda labels: ("postgresql://u:p@h:5432/t", "cid-crashed"),
                    stop=lambda cid: None,
                ):
                    print("holding", flush=True)
                    time.sleep(300)
            """),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "holding"
        assert xdist_backend.is_stale_backend_container(labels) is False  # still running
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        child.kill()
        if child.stdout is not None:
            child.stdout.close()
        child.wait(timeout=10)
    assert xdist_backend.is_stale_backend_container(labels) is True


def test_a_removed_run_root_reads_as_stale(tmp_path: Path) -> None:
    # pytest rotates old per-run roots away; a container whose lock file is gone with it
    # belongs to a finished run.
    labels = _labels(tmp_path / "rotated-away")
    assert xdist_backend.is_stale_backend_container(labels) is True


def test_a_container_without_the_liveness_label_is_never_stale() -> None:
    # Someone else's container, or one from before ADR-0551. The sweep refuses to reap
    # what it cannot prove it owns.
    assert xdist_backend.is_stale_backend_container({}) is False
    assert xdist_backend.is_stale_backend_container({"kdive.test-backend": "pg"}) is False


class _FakeDockerContainer:
    def __init__(self, cid: str, labels: dict[str, str], error: Exception | None = None) -> None:
        self.id = cid
        self.labels = labels
        self._error = error
        self.removed_with: dict[str, Any] | None = None

    def remove(self, **kwargs: Any) -> None:
        if self._error is not None:
            raise self._error
        self.removed_with = kwargs


class _UninspectableContainer:
    """docker-py raises from `.labels` when a container's inspect payload has no Config."""

    id = "cid-uninspectable"

    @property
    def labels(self) -> dict[str, str]:
        raise RuntimeError("no Config in inspect payload")


class _FakeDockerClient:
    # `Any` because the sweep also has to cope with a container it cannot inspect, which
    # is a different shape by construction.
    def __init__(self, *containers: Any) -> None:
        self._containers = list(containers)
        self.filters: dict[str, str] | None = None
        self.containers = self

    # Named `list` to match docker-py's `client.containers.list`. The return annotation
    # spells `Sequence` because inside this class body `list` is now this method.
    def list(self, all: bool, filters: dict[str, str]) -> Sequence[_FakeDockerContainer]:  # noqa: A002
        self.filters = filters
        return [*self._containers]


def test_sweep_reaps_only_the_stale_labeled_container_and_its_volume(tmp_path: Path) -> None:
    _FakeContainer.starts, _FakeContainer.stops, _FakeContainer.labels = 0, [], []
    dead = _FakeDockerContainer("cid-dead", _labels(tmp_path / "finished-run"))
    foreign = _FakeDockerContainer("cid-foreign", {"org.testcontainers": "true"})
    with _acquire(tmp_path):
        live = _FakeDockerContainer("cid-live", _labels(tmp_path))
        client = _FakeDockerClient(dead, live, foreign)
        reaped = xdist_backend.sweep_stale_backend_containers(client)

    assert reaped == ["cid-dead"]
    # `v=True` takes the anonymous volume with it — the 7.5 GB `docker volume prune`
    # cannot reclaim while the container holds it (#1910).
    assert dead.removed_with == {"force": True, "v": True}
    assert live.removed_with is None and foreign.removed_with is None
    # Enumeration is scoped to this repo's own containers, not every container on the host.
    assert client.filters == {"label": "kdive.test-backend"}


def test_sweep_is_silent_when_another_run_reaped_the_same_container(tmp_path: Path) -> None:
    import docker.errors

    gone = _FakeDockerContainer(
        "cid-gone", _labels(tmp_path), error=docker.errors.NotFound("already reaped")
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning here fails the test
        assert xdist_backend.sweep_stale_backend_containers(_FakeDockerClient(gone)) == []


def test_sweep_warns_and_keeps_going_when_a_removal_fails(tmp_path: Path) -> None:
    doomed = _FakeDockerContainer(
        "cid-doomed", _labels(tmp_path / "a"), error=RuntimeError("daemon busy")
    )
    other = _FakeDockerContainer("cid-other", _labels(tmp_path / "b"))
    with pytest.warns(UserWarning, match="cid-doomed"):
        reaped = xdist_backend.sweep_stale_backend_containers(_FakeDockerClient(doomed, other))
    # One failure must not abandon the rest of the sweep.
    assert reaped == ["cid-other"]


def test_sweep_survives_a_container_it_cannot_inspect(tmp_path: Path) -> None:
    # The sweep runs on the way to starting a backend, so anything it raises fails the
    # caller's whole suite. One unreadable container must not cost the rest of the sweep.
    reapable = _FakeDockerContainer("cid-reapable", _labels(tmp_path / "gone"))
    client = _FakeDockerClient(_UninspectableContainer(), reapable)
    with pytest.warns(UserWarning, match="cid-uninspectable"):
        assert xdist_backend.sweep_stale_backend_containers(client) == ["cid-reapable"]


def test_sweep_warns_rather_than_failing_the_run_when_docker_is_unusable() -> None:
    class _BrokenClient:
        @property
        def containers(self) -> Any:
            raise RuntimeError("docker daemon gone")

    with pytest.warns(UserWarning, match="sweep"):
        assert xdist_backend.sweep_stale_backend_containers(_BrokenClient()) == []


def test_sweep_reaps_a_real_stranded_container_but_spares_a_live_one(tmp_path: Path) -> None:
    """The whole fix, against a real daemon (#1910).

    The fakes above cannot catch a mismatch between the label the sweep filters on and
    the label a container actually carries, nor a `remove` call the daemon rejects — both
    would leave every other test in this file green while the leak continued.

    `postgres:17` is the image the db fixtures already use, so this pulls nothing extra;
    the entrypoint is overridden so it starts instantly instead of running initdb.
    """
    xdist_backend.skip_without_docker()
    import docker
    import docker.errors

    client = docker.from_env()
    labels = xdist_backend.backend_container_labels(tmp_path, "pg")
    container = client.containers.run(
        "postgres:17", entrypoint=["sleep", "300"], detach=True, labels=labels
    )
    try:
        with xdist_backend._liveness_held(tmp_path, "pg"):
            # A concurrently-running suite holds its lock, so a sweep from any other run
            # must leave its backend alone. This is the property that makes the sweep
            # safe on a shared host.
            assert container.id not in xdist_backend.sweep_stale_backend_containers()
            container.reload()
            assert container.status == "running"

        # The lock is now free, exactly as it would be had this run been SIGKILLed.
        assert container.id in xdist_backend.sweep_stale_backend_containers()
        with pytest.raises(docker.errors.NotFound):
            client.containers.get(container.id)
    finally:
        with suppress(docker.errors.NotFound):
            client.containers.get(container.id).remove(force=True, v=True)
