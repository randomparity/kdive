from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
import uuid
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import pytest
from _pytest.outcomes import Skipped

from tests.support import xdist_backend


@pytest.fixture(autouse=True)
def _private_sweep_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test in this module its own sweep lock.

    ``_SWEEP_LOCK_PATH`` is host-global per uid, deliberately: it serializes real sweeps
    against each other so two suites cannot reap the same container at once. That makes it
    shared state between *unrelated* runs, and ``sweep_stale_backend_containers`` returns
    ``[]`` when it cannot take it. So a second kdive suite running as the same user turned
    the assertions here into a function of what else was on the machine — ``in`` assertions
    failed, and ``not in`` assertions passed **vacuously**, because ``[]`` satisfies those
    too. Both directions were wrong, and the silent one is worse.

    Serialization is not what these tests measure, and the tests that do exercise the lock
    set the path themselves, so this only overrides the default. Tests keep their own
    ``monkeypatch.setattr`` where they need a specific path; a later set wins.
    """
    monkeypatch.setattr(xdist_backend, "_SWEEP_LOCK_PATH", tmp_path / "sweep-lock")


# Written out rather than imported from the module under test, so the assertions that pin
# the real label still fail if someone changes it. `private_backend_label` derives this
# run's key from it, so the two can never drift into different namespaces.
_REPO_WIDE_BACKEND_LABEL = "kdive.test-backend"

# Stamped on every container the real-daemon tests below start, and filtered on by
# nothing: purely so a container orphaned under a per-invocation key stays findable.
# See `_run_labelled_container`.
_TEST_SCRATCH_LABEL = "kdive.test-scratch"


@pytest.fixture(scope="session", autouse=True)
def _reap_orphaned_scratch_containers() -> None:
    """Collect scratch containers a previous run of this module was killed before removing.

    ``private_backend_label`` puts this module's real containers beyond the reach of every
    sweep on the host, which also puts them beyond ADR-0551's crash recovery: before it, a
    run killed between the start and the ``finally`` left containers the *next* run's sweep
    collected, and each one pins an anonymous volume. This restores that collection on the
    same next-run principle, keyed to the one label every scratch container carries.

    ``status=exited`` is what makes it safe with other suites in flight, structurally
    rather than by warning: a live test holds a container that is *running* for its whole
    ``sleep 300``, so it can never be a candidate. ``ignore_removed=True`` for the reason
    the sweep uses it — docker-py inspects every listed id, and a container removed in
    between would otherwise raise (`xdist_backend.py`).

    Best-effort, like the sweep it mirrors. A daemon that will not answer is the ordinary
    Docker-less runner and says nothing; a removal that fails once the daemon *has*
    answered is an anomaly and warns.
    """
    try:
        import docker

        client = docker.from_env()
    except Exception:
        return
    with suppress(Exception):
        _reap_scratch_orphans(client)


def _reap_scratch_orphans(client: Any) -> list[str]:
    """Remove every *exited* scratch container and its anonymous volume; return their ids."""
    import docker.errors

    reaped: list[str] = []
    orphans = client.containers.list(
        all=True,
        filters={"label": _TEST_SCRATCH_LABEL, "status": "exited"},
        ignore_removed=True,
    )
    for orphan in orphans:
        try:
            orphan.remove(force=True, v=True)
        except docker.errors.NotFound:
            continue  # a concurrent run's reaper got there first
        except Exception as exc:
            warnings.warn(f"scratch reap: could not remove orphan {orphan.id}: {exc}", stacklevel=2)
            continue
        reaped.append(orphan.id)
    return reaped


@pytest.fixture
def private_backend_label(monkeypatch: pytest.MonkeyPatch) -> str:
    """Scope one test's *real* containers to a label key no other suite can enumerate.

    Only for the tests that start containers on a real daemon. ``BACKEND_LABEL`` is
    repo-wide on purpose — a sweep must be able to reap a container a *crashed* run left
    behind, and it can only find one by a key every run agrees on (ADR-0551). The cost is
    that a container carrying it and holding no liveness lock is reapable by every process
    on the host, and every suite sweeps on its way to starting a backend
    (``tests/db/conftest.py``, ``tests/store/conftest.py``).

    A test that plants a deliberately stranded container is therefore racing every other
    suite for the right to reap it, and loses often enough to matter under
    worktree-per-agent parallelism: the sibling's ``remove`` wins, ours gets ``NotFound``,
    the id never reaches our returned list, and ``assert stranded.id in reaped`` goes red
    against unmodified code (#2219). The same applies once a live container's lock is
    released to model a SIGKILL.

    Scoping the key per test invocation removes the contention in both directions: no
    sibling can enumerate what this test planted, and this test's sweeps stop reaping
    strays that belong to other suites. ``backend_container_labels`` and the sweep's
    filter both read ``BACKEND_LABEL`` at call time, so one patch moves the whole
    round trip and producer and consumer cannot disagree.

    Deliberately **not** a fix in ``xdist_backend`` itself. Making the enumeration key
    per-run or per-checkout would contradict ADR-0551's decision and strand a crashed
    run's container forever whenever its worktree is deleted — which, under
    worktree-per-agent, is the normal case.
    """
    label = f"{_REPO_WIDE_BACKEND_LABEL}-{uuid.uuid4().hex}"
    monkeypatch.setattr(xdist_backend, "BACKEND_LABEL", label)
    return label


class _CountingProbe:
    """A stand-in for the Docker ping that records how often it was asked.

    Answers are consumed in order and the last one repeats, so ``_CountingProbe(True,
    False)`` models a daemon that answers at session start and stops answering afterwards
    — the #2074 condition. A call count of one is the assertion that matters.
    """

    def __init__(self, *answers: bool) -> None:
        self._answers = answers
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self._answers[min(self.calls - 1, len(self._answers) - 1)]


@pytest.fixture
def docker_unlatched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the session Docker verdict for one test and put the real one back after.

    ``monkeypatch`` restores whatever the module held, not ``None``: a fabricated verdict
    left behind would be trusted by every later Docker-gated test in this worker, and an
    unset latch would make the next one re-probe under load, which is the behaviour
    ADR-0580 removes.
    """
    monkeypatch.setattr(xdist_backend, "_docker_verdict", None)


def test_docker_available_probes_once_and_reuses_the_verdict(
    docker_unlatched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _CountingProbe(True, False)
    monkeypatch.setattr(xdist_backend, "_probe_docker", probe)

    first = xdist_backend.docker_available()
    second = xdist_backend.docker_available()

    # A second probe would have answered False; the latched verdict does not.
    assert (first, second) == (True, True)
    assert probe.calls == 1


def test_docker_available_latches_an_unavailable_verdict_too(
    docker_unlatched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon that comes up mid-run does not un-skip the tests already skipped."""
    probe = _CountingProbe(False, True)
    monkeypatch.setattr(xdist_backend, "_probe_docker", probe)

    first = xdist_backend.docker_available()
    second = xdist_backend.docker_available()

    assert (first, second) == (False, False)
    assert probe.calls == 1


def test_skip_without_docker_stops_skipping_once_the_daemon_has_answered(
    docker_unlatched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #2074 property: a run that had Docker cannot lose a test to a slow ping.

    The second call is caught rather than allowed to propagate — a regression here would
    otherwise skip *this* test, which is exactly the silent-coverage-loss shape under test.
    """
    monkeypatch.delenv("KDIVE_REQUIRE_DOCKER", raising=False)
    probe = _CountingProbe(True, False)
    monkeypatch.setattr(xdist_backend, "_probe_docker", probe)

    xdist_backend.skip_without_docker()
    try:
        xdist_backend.skip_without_docker()
    except Skipped as exc:
        pytest.fail(f"a latched-available verdict must never skip a later test: {exc}")

    assert probe.calls == 1


def test_skip_without_docker_keeps_skipping_on_a_latched_unavailable_verdict(
    docker_unlatched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KDIVE_REQUIRE_DOCKER", raising=False)
    probe = _CountingProbe(False, True)
    monkeypatch.setattr(xdist_backend, "_probe_docker", probe)

    for _ in range(2):
        with pytest.raises(Skipped, match="Docker unavailable"):
            xdist_backend.skip_without_docker()

    assert probe.calls == 1


def test_require_docker_answers_before_the_probe_and_leaves_the_latch_unset(
    docker_unlatched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``KDIVE_REQUIRE_DOCKER=1`` still short-circuits, and does not decide the session."""
    probe = _CountingProbe(False)
    monkeypatch.setattr(xdist_backend, "_probe_docker", probe)
    monkeypatch.setenv("KDIVE_REQUIRE_DOCKER", "1")

    xdist_backend.skip_without_docker()
    assert probe.calls == 0
    assert xdist_backend._docker_verdict is None

    # Unset, the very next call probes for real — proving the count above was a
    # short-circuit and not a latch this test had already taken.
    monkeypatch.delenv("KDIVE_REQUIRE_DOCKER")
    with pytest.raises(Skipped, match="Docker unavailable"):
        xdist_backend.skip_without_docker()
    assert probe.calls == 1


def test_acquisition_failure_raises_once_the_daemon_has_answered(
    docker_unlatched: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A daemon that dies mid-run reddens the run instead of shrinking it (ADR-0580).

    The ``Skipped`` catch is load-bearing: a regression converts the acquisition failure
    back into a skip, which would skip *this* test and leave the suite green — the same
    silent-coverage-loss shape the test exists to forbid.
    """
    probe = _CountingProbe(True, False)
    monkeypatch.setattr(xdist_backend, "_probe_docker", probe)
    assert xdist_backend.docker_available() is True  # the session-start verdict

    def _start(_labels: Mapping[str, str]) -> tuple[str, str]:
        raise RuntimeError("daemon went away")

    try:
        with (
            pytest.raises(RuntimeError, match="daemon went away"),
            xdist_backend.shared_container_or_skip(
                tmp_path, "pg", start=_start, stop=lambda _cid: None, require_docker=False
            ),
        ):
            pass  # pragma: no cover - acquisition raises before the body runs
    except Skipped as exc:
        pytest.fail(f"a latched-available verdict must raise, not skip: {exc}")

    assert probe.calls == 1


def test_acquisition_failure_still_skips_when_the_daemon_never_answered(
    docker_unlatched: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The skip a laptop without Docker relies on is unchanged."""
    probe = _CountingProbe(False)
    monkeypatch.setattr(xdist_backend, "_probe_docker", probe)

    def _start(_labels: Mapping[str, str]) -> tuple[str, str]:
        raise RuntimeError("cannot connect to the docker daemon")

    with (
        pytest.raises(Skipped, match="Docker unavailable for testcontainers"),
        xdist_backend.shared_container_or_skip(
            tmp_path, "pg", start=_start, stop=lambda _cid: None, require_docker=False
        ),
    ):
        pass  # pragma: no cover - acquisition skips before the body runs

    assert probe.calls == 1


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
    assert liveness.is_absolute() and liveness.parent == tmp_path.resolve()


@contextmanager
def _fail_rather_than_hang(seconds: int = 5) -> Iterator[None]:
    """Turn a blocked call into a failure, so a regression reddens instead of hanging.

    A test that hangs burns the whole CI job timeout and reports nothing useful. SIGALRM
    interrupts the blocking syscall, and PEP 475 only retries on EINTR when the handler
    returns normally — raising here propagates out of the call instead.
    """

    def _blocked(_signum: int, _frame: object) -> None:
        raise AssertionError(f"call blocked for more than {seconds}s")

    previous = signal.signal(signal.SIGALRM, _blocked)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_a_liveness_path_that_is_not_a_regular_file_is_left_alone(tmp_path: Path) -> None:
    """A FIFO must neither hang the sweep nor be read as permission to reap.

    The path arrives from a container label, not from this process, so it can be any file
    type. Opening a FIFO without `O_NONBLOCK` blocks until someone opens the write end,
    which is why this is wrapped: dropping that flag must fail here, not stall CI.
    """
    os.mkfifo(tmp_path / "kdive-pg.alive")
    with _fail_rather_than_hang():
        assert xdist_backend.is_stale_backend_container(_labels(tmp_path)) is False


def test_a_liveness_lock_this_process_cannot_reach_is_left_alone(tmp_path: Path) -> None:
    """The multi-user case, and the one a stat-based guard gets backwards.

    pytest's per-run root is `/tmp/pytest-of-<user>`, mode 0700. Two users sharing one
    Docker daemon means every container the other user's LIVE run owns has a liveness path
    this process cannot stat. `Path.is_file()` reports False for exactly that, which would
    make a live suite's backend look reapable and destroy it mid-run.
    """
    unreachable = tmp_path / "other-users-run"
    unreachable.mkdir()
    (unreachable / "kdive-pg.alive").touch()
    unreachable.chmod(0o000)
    try:
        assert xdist_backend.is_stale_backend_container(_labels(unreachable)) is False
    finally:
        unreachable.chmod(0o700)  # so tmp_path cleanup can remove it


def test_an_unreadable_liveness_file_is_left_alone(tmp_path: Path) -> None:
    # Same asymmetry at file level: only a *missing* file is evidence the run is gone.
    lock = tmp_path / "kdive-pg.alive"
    lock.touch()
    lock.chmod(0o000)
    assert xdist_backend.is_stale_backend_container(_labels(tmp_path)) is False


def test_the_liveness_lock_is_already_held_when_start_runs(tmp_path: Path) -> None:
    """The ordering the whole safety case rests on (ADR-0551): lock first, container second.

    Were the lock taken after `start` returned, a container would exist for a window with
    a free lock behind it, and any concurrent run's sweep would read it as crash debris
    and force-remove it. Nothing else in this file catches that: every other liveness
    assertion observes state from inside the `with`, by which time the lock is held either
    way, so moving `_liveness_held` after `start()` leaves them all green.

    Asserting *inside* `start` is what pins the order — that is the exact instant the
    container would come into existence.
    """
    observed: list[bool] = []

    def start(labels: Mapping[str, str]) -> tuple[str, str]:
        observed.append(xdist_backend.is_stale_backend_container(labels))
        return "postgresql://u:p@host:5432/test", "cid-ordering"

    with xdist_backend.shared_container(tmp_path, "pg", start=start, stop=lambda _cid: None):
        pass

    assert observed == [False], "start() ran before its container's liveness lock was held"


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
    def __init__(self, *containers: Any, get_results: Sequence[Any] = ()) -> None:
        self._containers = list(containers)
        self._get_results = list(get_results)
        self.get_ids: list[str] = []
        self.filters: dict[str, str] | None = None
        self.ignore_removed: bool = False
        self.containers = self

    # Named `list` to match docker-py's `client.containers.list`. The return annotation
    # spells `Sequence` because inside this class body `list` is now this method.
    def list(  # noqa: A002
        self, all: bool, filters: dict[str, str], ignore_removed: bool = False
    ) -> Sequence[_FakeDockerContainer]:
        self.filters = filters
        self.ignore_removed = ignore_removed
        return [*self._containers]

    def get(self, container_id: str) -> Any:
        self.get_ids.append(container_id)
        result = self._get_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _api_error(explanation: str, *, status_code: int = 409) -> Exception:
    import docker.errors
    import requests

    response = requests.Response()
    response.status_code = status_code
    response.url = "http://docker.test/containers/cid"
    response.reason = "Conflict"
    return docker.errors.APIError("remove failed", response=response, explanation=explanation)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _wait_for_path(path: Path, *processes: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        for process in processes:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    f"subprocess exited before {path.name}: stdout={stdout!r}, stderr={stderr!r}"
                )
        time.sleep(0.01)
    assert path.exists(), f"timed out waiting for {path}"


def test_concurrent_sweeps_have_one_effective_remover(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import sys
        import time
        import warnings
        from pathlib import Path

        sys.path.insert(0, sys.argv[1])
        from tests.support import xdist_backend

        root = Path(sys.argv[2])
        role = sys.argv[3]
        xdist_backend._SWEEP_LOCK_PATH = root / "sweep.lock"

        class Container:
            id = "cid-shared"
            labels = {
                xdist_backend.BACKEND_LABEL: "pg",
                xdist_backend.LIVENESS_LABEL: str(root / "missing.alive"),
            }

            def remove(self, **kwargs):
                (root / "owner-holding").touch()
                deadline = time.monotonic() + 10
                while not (root / "contender-done").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                if not (root / "contender-done").exists():
                    raise RuntimeError("contender did not finish while owner held the lock")
                if (root / "contender-enumerated").exists():
                    (root / "violation").touch()
                (root / "removed").touch()

        class Containers:
            def list(self, **kwargs):
                if role == "contender":
                    (root / "contender-enumerated").touch()
                return [] if (root / "removed").exists() else [Container()]

        class Client:
            containers = Containers()

        if role == "contender":
            (root / "contender-started").touch()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            xdist_backend.sweep_stale_backend_containers(Client())
        (root / f"{role}-done").touch()
        """
    )
    args = [sys.executable, "-c", script, str(_REPO_ROOT), str(tmp_path)]
    owner = subprocess.Popen(
        [*args, "owner"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    contender: subprocess.Popen[str] | None = None
    try:
        _wait_for_path(tmp_path / "owner-holding", owner)
        contender = subprocess.Popen(
            [*args, "contender"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        _wait_for_path(tmp_path / "contender-done", owner, contender)
        owner_stdout, owner_stderr = owner.communicate(timeout=10)
        contender_stdout, contender_stderr = contender.communicate(timeout=10)
        assert owner.returncode == 0, (owner_stdout, owner_stderr)
        assert contender.returncode == 0, (contender_stdout, contender_stderr)
    finally:
        for process in (owner, contender):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    assert (tmp_path / "removed").exists()
    assert not (tmp_path / "contender-enumerated").exists()
    assert not (tmp_path / "violation").exists()


@pytest.mark.parametrize(
    "explanation",
    [
        "removal of container other-id is already in progress",
        "removal of container cid is already in progress",
        "removal of container cid-full-extra is already in progress",
        "another Docker conflict",
    ],
)
def test_unrelated_409_explanations_warn(explanation: str, tmp_path: Path) -> None:
    import docker.errors

    container = _FakeDockerContainer("cid-full", _labels(tmp_path), error=_api_error(explanation))
    client = _FakeDockerClient(container, get_results=[docker.errors.NotFound("gone")])
    with pytest.warns(UserWarning, match="cid-full"):
        assert xdist_backend.sweep_stale_backend_containers(client) == []


@pytest.mark.parametrize("status_code", [400, 500])
def test_concurrent_removal_phrase_with_non_conflict_status_warns(
    status_code: int, tmp_path: Path
) -> None:
    import docker.errors

    error = _api_error(
        "removal of container cid-full is already in progress", status_code=status_code
    )
    container = _FakeDockerContainer("cid-full", _labels(tmp_path), error=error)
    client = _FakeDockerClient(container, get_results=[docker.errors.NotFound("gone")])
    with pytest.warns(UserWarning, match="cid-full"):
        assert xdist_backend.sweep_stale_backend_containers(client) == []


def test_concurrent_removal_classifier_allows_surrounding_api_prose() -> None:
    error = _api_error('Conflict ("removal of container cid-full is already in progress")')
    assert xdist_backend._removal_is_already_in_progress(error, "cid-full") is True


def test_concurrent_removal_waits_for_delayed_absence(tmp_path: Path) -> None:
    import docker.errors

    container = _FakeDockerContainer("cid-full", _labels(tmp_path))
    client = _FakeDockerClient(
        container,
        get_results=[container, container, docker.errors.NotFound("gone")],
    )
    clock = _FakeClock()
    assert xdist_backend._wait_until_container_absent(
        client, "cid-full", clock=clock, sleep=clock.sleep
    )
    assert client.get_ids == ["cid-full", "cid-full", "cid-full"]


def test_sweep_is_silent_after_exact_concurrent_removal_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import docker.errors

    container = _FakeDockerContainer(
        "cid-full",
        _labels(tmp_path),
        error=_api_error("removal of container cid-full is already in progress"),
    )
    client = _FakeDockerClient(
        container,
        get_results=[container, container, docker.errors.NotFound("gone")],
    )
    monkeypatch.setattr(xdist_backend, "_SWEEP_LOCK_PATH", tmp_path / "sweep.lock")
    monkeypatch.setattr(xdist_backend, "_REMOVAL_POLL_S", 0.0)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert xdist_backend.sweep_stale_backend_containers(client) == []
    assert client.get_ids == ["cid-full", "cid-full", "cid-full"]


def test_concurrent_removal_warns_at_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container = _FakeDockerContainer(
        "cid-full",
        _labels(tmp_path),
        error=_api_error("removal of container cid-full is already in progress"),
    )
    client = _FakeDockerClient(container, get_results=[container] * 4)
    clock = _FakeClock()
    with monkeypatch.context() as patch:
        patch.setattr(xdist_backend.time, "monotonic", clock)
        patch.setattr(xdist_backend.time, "sleep", clock.sleep)
        patch.setattr(xdist_backend, "_REMOVAL_WAIT_S", 0.1)
        patch.setattr(xdist_backend, "_REMOVAL_POLL_S", 0.05)
        with pytest.warns(UserWarning, match="cid-full"):
            assert xdist_backend.sweep_stale_backend_containers(client) == []


def test_verification_lookup_failure_warns_and_keeps_sweeping(tmp_path: Path) -> None:
    first = _FakeDockerContainer(
        "cid-first",
        _labels(tmp_path / "first"),
        error=_api_error("removal of container cid-first is already in progress"),
    )
    second = _FakeDockerContainer("cid-second", _labels(tmp_path / "second"))
    client = _FakeDockerClient(first, second, get_results=[RuntimeError("lookup failed")])
    with pytest.warns(UserWarning, match="cid-first"):
        assert xdist_backend.sweep_stale_backend_containers(client) == ["cid-second"]
    assert second.removed_with == {"force": True, "v": True}


class _EnumerationRaceClient(_FakeDockerClient):
    """A client whose enumeration trips over someone else's container disappearing.

    Mirrors docker-py's `ContainerCollection.list`, which inspects every listed id and
    re-raises `NotFound` from that per-id lookup unless `ignore_removed=True`.
    """

    def list(  # noqa: A002
        self, all: bool, filters: dict[str, str], ignore_removed: bool = False
    ) -> Sequence[_FakeDockerContainer]:
        import docker.errors

        if not ignore_removed:
            raise docker.errors.NotFound("foreign container removed mid-enumeration")
        return super().list(all=all, filters=filters, ignore_removed=ignore_removed)


def test_sweep_survives_a_foreign_container_removed_during_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container this run does not own vanishing mid-list must not zero the sweep.

    The removal raises from docker-py's per-id inspect, which is above
    `_reap_stale_candidates`' per-container handler, so without the tolerance flag the
    outer catch swallows it and returns no ids -- abandoning this run's own stale
    containers because an unrelated one disappeared. Any `just ci` sharing the host with
    another run can hit it.
    """
    container = _FakeDockerContainer("cid-full", _labels(tmp_path))
    client = _EnumerationRaceClient(container, get_results=[container])
    monkeypatch.setattr(xdist_backend, "_SWEEP_LOCK_PATH", tmp_path / "sweep.lock")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert xdist_backend.sweep_stale_backend_containers(client) == ["cid-full"]
    assert client.ignore_removed is True


@contextmanager
def _raising_lock() -> Iterator[None]:
    raise OSError("cannot open sweep lock")
    yield


def test_sweep_lock_failure_warns_without_enumerating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    monkeypatch.setattr(xdist_backend, "_sweep_locked", _raising_lock)
    with pytest.warns(UserWarning, match="sweep skipped"):
        assert xdist_backend.sweep_stale_backend_containers(client) == []
    assert client.filters is None


def test_sweep_takes_lock_before_constructing_default_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from testcontainers.core import docker_client

    constructed = False

    def observable_factory() -> None:
        nonlocal constructed
        constructed = True

    monkeypatch.setattr(docker_client, "DockerClient", observable_factory)
    monkeypatch.setattr(xdist_backend, "_sweep_locked", _raising_lock)
    with pytest.warns(UserWarning, match="sweep skipped"):
        assert xdist_backend.sweep_stale_backend_containers() == []
    assert constructed is False


def test_sweep_lock_refuses_symlink_without_touching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.write_text("unchanged")
    lock_path = tmp_path / "sweep.lock"
    lock_path.symlink_to(target)
    monkeypatch.setattr(xdist_backend, "_SWEEP_LOCK_PATH", lock_path)
    with pytest.warns(UserWarning, match="sweep skipped"):
        assert xdist_backend.sweep_stale_backend_containers(_FakeDockerClient()) == []
    assert target.read_text() == "unchanged"


def test_sweep_lock_refuses_non_regular_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "sweep.lock"
    lock_path.mkdir()
    monkeypatch.setattr(xdist_backend, "_SWEEP_LOCK_PATH", lock_path)
    with pytest.warns(UserWarning, match="sweep skipped"):
        assert xdist_backend.sweep_stale_backend_containers(_FakeDockerClient()) == []


def test_sweep_lock_refuses_wrong_owner_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "sweep.lock"
    lock_path.touch(mode=0o600)
    real_euid = os.geteuid()
    monkeypatch.setattr(xdist_backend, "_SWEEP_LOCK_PATH", lock_path)
    monkeypatch.setattr(xdist_backend.os, "geteuid", lambda: real_euid + 1)
    with pytest.warns(UserWarning, match="sweep skipped"):
        assert xdist_backend.sweep_stale_backend_containers(_FakeDockerClient()) == []


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
    assert client.filters == {"label": _REPO_WIDE_BACKEND_LABEL}


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


def test_the_scratch_reap_takes_only_exited_containers_and_their_volumes() -> None:
    """The scratch reaper's whole safety argument is its filter (#2219).

    `_run_labelled_container` puts these containers beyond every sweep on the host, so this
    reaper is the only thing that collects one a killed run left behind. It runs at session
    start on a host where other suites are mid-test, and the single thing between it and
    their in-flight fixtures is `status=exited`: a live test holds a container that is
    *running* for its whole `sleep 300`.

    So assert the filter, not just the outcome. A reaper that removed the right container
    while enumerating the wrong set would satisfy any test that only looked at what came
    back — and the wrong set here is other agents' running backends.
    """
    orphan = _FakeDockerContainer("cid-orphan", {_TEST_SCRATCH_LABEL: "1"})
    client = _FakeDockerClient(orphan)

    assert _reap_scratch_orphans(client) == ["cid-orphan"]
    assert client.filters == {"label": "kdive.test-scratch", "status": "exited"}
    # Same reason the sweep passes it: docker-py inspects every listed id, and a container
    # another run removed in between would otherwise take the whole reap down.
    assert client.ignore_removed is True
    # `v=True` takes the anonymous volume the postgres image declares. Without it the
    # volume outlives the container and nothing ever reclaims it (#1910).
    assert orphan.removed_with == {"force": True, "v": True}


def test_the_scratch_reap_is_silent_when_another_run_got_there_first() -> None:
    import docker.errors

    gone = _FakeDockerContainer(
        "cid-gone", {_TEST_SCRATCH_LABEL: "1"}, error=docker.errors.NotFound("already reaped")
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning here fails the test
        assert _reap_scratch_orphans(_FakeDockerClient(gone)) == []


def _run_labelled_container(client: Any, labels: Mapping[str, str], started: list[str]) -> Any:
    """Start one real container carrying ``labels``, plus a fixed recovery handle.

    The image is the db fixtures' own digest-pinned constant, so this reuses the reference
    ``scripts/pull-test-images.sh`` already pre-pulls — a guard keeps those two in step —
    and cannot drift if `postgres:17` is re-tagged upstream. The entrypoint is overridden
    so it starts instantly instead of running initdb.

    Created and started separately, so ``started`` records the id *before* anything can
    fail: ``containers.run`` does the same two steps but raises out of the second without
    ever returning the object, which would leave the caller's teardown with nothing to
    remove.

    ``_TEST_SCRATCH_LABEL`` is the price of ``private_backend_label``. A container these
    tests start under a per-invocation key is invisible to every sweep on the host —
    which is the point while the test runs, but means a run killed outright leaks a
    container *and its anonymous volume* that no sweep will ever reap, where the repo-wide
    key would have had the next run collect it. These containers are not
    testcontainers-managed either, so the ``org.testcontainers`` cleanup in AGENTS.md does
    not see them. One fixed key nothing filters on costs no isolation and makes that
    residual collectable: ``_reap_orphaned_scratch_containers`` does it automatically, and
    AGENTS.md carries the manual equivalent.
    """
    from tests.db import conftest as db_conftest

    container = client.containers.create(
        db_conftest._POSTGRES_IMAGE,
        entrypoint=["sleep", "300"],
        labels={**labels, _TEST_SCRATCH_LABEL: "1"},
    )
    started.append(container.id)
    container.start()
    return container


def _remove_quietly(client: Any, *container_ids: str) -> None:
    """Best-effort teardown for containers a real-daemon test started."""
    import docker.errors

    for container_id in container_ids:
        with suppress(docker.errors.NotFound):
            client.containers.get(container_id).remove(force=True, v=True)


def test_sweep_reaps_a_real_stranded_container_but_spares_a_live_one(
    tmp_path: Path, private_backend_label: str
) -> None:
    """The whole fix, against a real daemon (#1910).

    The fakes above cannot catch a mismatch between the label the sweep filters on and
    the label a container actually carries, nor a `remove` call the daemon rejects — both
    would leave every other test in this file green while the leak continued. Scoping the
    key per invocation keeps that round trip intact: `backend_container_labels` stamps
    whatever key the sweep then filters on, so producer and consumer still have to agree.

    Two fixtures keep this independent of whatever else is running on the host, and both
    are needed: ``_private_sweep_lock`` so a sibling suite holding the host-global lock
    cannot turn our sweeps into ``[]``, and ``private_backend_label`` so a sibling's sweep
    cannot reap the stranded container we plant before our own sweep gets to it (#2219).
    See their docstrings.
    """
    xdist_backend.skip_without_docker()
    import docker
    import docker.errors

    client = docker.from_env()
    stranded_root = tmp_path / "stranded"
    stranded_root.mkdir()
    started: list[str] = []
    try:
        # The lock is taken before the container exists, the order `shared_container`
        # itself holds to, so there is never an instant where a container carries the
        # liveness label with nothing holding the lock behind it (ADR-0551).
        with xdist_backend._liveness_held(tmp_path, "pg"):
            container = _run_labelled_container(
                client, xdist_backend.backend_container_labels(tmp_path, "pg"), started
            )
            # A second container whose run is already gone: its liveness file is never
            # locked, so it is stale from the start. Sweeping both in ONE call is what
            # keeps the spare assertion honest — an empty result would satisfy `not in`
            # on its own, so the reap of this one is the evidence that the sweep actually
            # ran and still spared the live one.
            stranded = _run_labelled_container(
                client, xdist_backend.backend_container_labels(stranded_root, "pg"), started
            )

            # A concurrently-running suite holds its lock, so a sweep from any other run
            # must leave its backend alone. This is the property that makes the sweep
            # safe on a shared host.
            reaped = xdist_backend.sweep_stale_backend_containers()
            assert container.id not in reaped
            assert stranded.id in reaped
            container.reload()
            assert container.status == "running"

        # The lock is now free, exactly as it would be had this run been SIGKILLed.
        assert container.id in xdist_backend.sweep_stale_backend_containers()
        with pytest.raises(docker.errors.NotFound):
            client.containers.get(container.id)
    finally:
        _remove_quietly(client, *started)


def test_a_concurrent_suites_sweep_cannot_reach_this_runs_containers(
    tmp_path: Path, private_backend_label: str
) -> None:
    """A sibling suite on the same host cannot enumerate this run's containers (#2219).

    This is the property that makes the test above deterministic, asserted directly
    against a real daemon rather than left to whatever else happens to be running.

    ``control`` carries the repo-wide key a sibling suite really filters on and holds its
    liveness lock for its whole life, so every sweep on the host spares it — the same
    protection ``tests/db/test_postgres_url_fixture.py`` and
    ``tests/store/test_minio_store_fixture.py`` already rely on. It is the anti-vacuity
    control: without it, ``subject.id not in visible`` would pass just as happily against
    an enumeration that returned nothing at all, which is the failure mode the
    ``_private_sweep_lock`` docstring was written about.

    ``subject`` is stranded on purpose — its liveness file is never locked, so it is stale
    to any sweep that can see it. Under the repo-wide key that made it a free-for-all;
    under this run's key nobody else can list it, and only our own sweep reaps it.

    The enumeration below is read-only. Running a *real* unscoped sweep here would prove
    the same point by destroying whatever other suites had stranded on the host, which is
    the behaviour this test exists to keep out of the suite.
    """
    xdist_backend.skip_without_docker()
    import docker
    import docker.errors

    client = docker.from_env()
    control_root = tmp_path / "control"
    subject_root = tmp_path / "subject"
    control_root.mkdir()
    subject_root.mkdir()
    control_labels = {
        _REPO_WIDE_BACKEND_LABEL: "pg",
        xdist_backend.LIVENESS_LABEL: str(
            xdist_backend._liveness_path(control_root, "pg").resolve()
        ),
    }

    started: list[str] = []
    with xdist_backend._liveness_held(control_root, "pg"):
        try:
            control = _run_labelled_container(client, control_labels, started)
            subject = _run_labelled_container(
                client, xdist_backend.backend_container_labels(subject_root, "pg"), started
            )

            control.reload()
            subject.reload()
            # Read back off the running containers: `labels=` reaching the daemon is what
            # everything below depends on, and a mock cannot show it.
            assert control.labels[_REPO_WIDE_BACKEND_LABEL] == "pg"
            assert subject.labels[private_backend_label] == "pg"
            assert _REPO_WIDE_BACKEND_LABEL not in subject.labels

            # Exactly the enumeration `sweep_stale_backend_containers` performs in a
            # sibling suite that never patched anything — `ignore_removed=True` included.
            # This filter is the repo-wide key, so on a shared host the list holds other
            # suites' live backends; without the flag, one of them being removed between
            # the list and docker-py's per-id inspect fails this test with a Docker
            # exception. That is the race `xdist_backend.py` documents at its own call.
            visible = {
                found.id
                for found in client.containers.list(
                    all=True,
                    filters={"label": _REPO_WIDE_BACKEND_LABEL},
                    ignore_removed=True,
                )
            }
            assert control.id in visible, "the sibling's enumeration returned nothing"
            assert subject.id not in visible

            # ...and this run's own sweep still reaps its own stranded container.
            #
            # Only `subject` is asserted on. `control` is spared here for two independent
            # reasons — it is outside the private key's enumeration *and* its liveness
            # lock is held — so no assertion about it could tell the two apart or fail.
            # That the sweep spares a live container it can genuinely see is proven in
            # `test_sweep_reaps_a_real_stranded_container_but_spares_a_live_one`.
            reaped = xdist_backend.sweep_stale_backend_containers()
            assert subject.id in reaped
            with pytest.raises(docker.errors.NotFound):
                client.containers.get(subject.id)
        finally:
            _remove_quietly(client, *started)
