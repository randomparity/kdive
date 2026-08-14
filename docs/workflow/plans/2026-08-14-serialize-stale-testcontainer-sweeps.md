# Serialize stale testcontainer sweeps

**Goal:** Give concurrent xdist fixture processes one effective stale-container remover and silence
only a verified Docker concurrent-removal outcome.

**Architecture:** The existing best-effort sweep takes a fixed per-effective-user `flock` before it
constructs or enumerates the Docker client and holds it through removals. A narrowly classified
concurrent-removal 409 enters bounded exact-id absence polling; every other error keeps the current
warning path. No fixture call site or production module changes.

**Tech stack:** Python 3.14, `fcntl`, docker-py, pytest, Linux on `x86_64` and `ppc64le`.

## Global constraints

- Python 3.14 remains the runtime; use the standard library and existing `fcntl` support.
- The host is `x86_64`; the project targets `x86_64` and `ppc64le` Linux hosts.
- Change only shared test-backend cleanup and its focused tests.
- Preserve backend labels, liveness detection, live-container ownership, and fixture interfaces.
- Preserve warnings for unrelated Docker conflicts and failures.
- Add no dependency, production behavior, or Docker-wide cleanup surface.
- The full repository guardrail is `just ci`; CI gates its constituent recipes individually.

## Task 1: Serialize and verify stale-container removal

**Files:**

- Modify `tests/support/xdist_backend.py`: own the sweep lock, conflict classifier, bounded absence
  verifier, and best-effort sweep orchestration.
- Modify `tests/support/test_xdist_backend.py`: prove interprocess serialization, classifier edges,
  delayed absence, deadline, lock failure, and retained behavior.

**Interfaces:**

- Preserve `sweep_stale_backend_containers(client: Any | None = None) -> list[str]` for the Postgres
  and MinIO fixture callers.
- Add private constants `_SWEEP_LOCK_PATH`, `_REMOVAL_WAIT_S = 5.0`, and
  `_REMOVAL_POLL_S = 0.05`.
- Add `_sweep_locked() -> Iterator[bool]`, a dedicated safe opener that reports ordinary contention
  as `False` for the canonical `/tmp` lock.
- Add `_removal_is_already_in_progress(exc: Exception, container_id: str) -> bool`.
- Add `_wait_until_container_absent(client: Any, container_id: str, *, timeout_s: float = 5.0,
  clock: Callable[[], float] | None = None,
  sleep: Callable[[float], None] | None = None) -> bool`.
  Focused tests may call this private seam to inject deterministic time.

### Step 1: Write the failing regression tests

Extend the fake Docker client with `get(container_id)` behavior and construct `docker.errors.APIError`
instances whose response carries status 409 and whose `explanation` is controlled by the test.
Add these tests before implementation:

```python
def test_concurrent_sweeps_have_one_effective_remover(tmp_path: Path) -> None:
    # Launch two Python subprocesses using the same monkeypatched sweep-lock path and shared marker
    # files. Process A blocks inside remove after process B announces it is about to sweep. Process B
    # must return without enumerating Docker while A holds the lock. Assert no second-enumeration
    # marker, no warnings, and one removal marker.


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
    container = _FakeDockerContainer("cid-full", _labels(tmp_path), error=_api_error(explanation))
    with pytest.warns(UserWarning, match="cid-full"):
        assert xdist_backend.sweep_stale_backend_containers(_FakeDockerClient(container)) == []


def test_concurrent_removal_waits_for_delayed_absence(tmp_path: Path) -> None:
    import docker.errors

    container = _FakeDockerContainer("cid-full", _labels(tmp_path))
    client = _FakeDockerClient(
        container,
        get_results=[container, container, docker.errors.NotFound("gone")],
    )
    clock = _FakeClock()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert xdist_backend._wait_until_container_absent(
            client, "cid-full", clock=clock, sleep=clock.sleep
        )
    assert client.get_ids == ["cid-full", "cid-full", "cid-full"]


def test_sweep_is_silent_after_exact_concurrent_removal_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _FakeDockerContainer(
        "cid-full",
        _labels(tmp_path),
        error=_api_error("removal of container cid-full is already in progress"),
    )
    client = _FakeDockerClient(
        container,
        get_results=[container, container, docker.errors.NotFound("gone")],
    )
    monkeypatch.setattr(xdist_backend, "_REMOVAL_POLL_S", 0.0)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert xdist_backend.sweep_stale_backend_containers(client) == []
    assert client.get_ids == ["cid-full", "cid-full", "cid-full"]


def test_concurrent_removal_warns_at_the_deadline(tmp_path: Path) -> None:
    # Keep get("cid-full") present while injected sleep advances monotonic time to five seconds.
    # Monkeypatch xdist_backend.time.monotonic and xdist_backend.time.sleep inside a narrow
    # monkeypatch.context(), call the public sweep, and assert it warns once without appending the id.


def test_verification_lookup_failure_warns_and_keeps_sweeping(tmp_path: Path) -> None:
    import docker.errors

    first = _FakeDockerContainer(
        "cid-first",
        _labels(tmp_path / "first"),
        error=_api_error("removal of container cid-first is already in progress"),
    )
    second = _FakeDockerContainer("cid-second", _labels(tmp_path / "second"))
    client = _FakeDockerClient(first, second, get_results=[docker.errors.APIError("lookup failed")])
    with pytest.warns(UserWarning, match="cid-first"):
        assert xdist_backend.sweep_stale_backend_containers(client) == ["cid-second"]
    assert first.removed_with is None
    assert second.removed_with == {"force": True, "v": True}


def test_sweep_lock_failure_warns_without_enumerating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    monkeypatch.setattr(xdist_backend, "_sweep_locked", _raising_lock)
    with pytest.warns(UserWarning, match="sweep skipped"):
        assert xdist_backend.sweep_stale_backend_containers(client) == []
    assert client.filters is None


def test_sweep_takes_the_lock_before_constructing_the_default_client(
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


def test_sweep_lock_refuses_a_symlink_without_touching_its_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.write_text("unchanged")
    lock_path = tmp_path / "sweep.lock"
    lock_path.symlink_to(target)
    monkeypatch.setattr(xdist_backend, "_SWEEP_LOCK_PATH", lock_path)
    with pytest.warns(UserWarning, match="sweep skipped"):
        assert xdist_backend.sweep_stale_backend_containers(_FakeDockerClient()) == []
    assert target.read_text() == "unchanged"


def test_sweep_lock_refuses_a_non_regular_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "sweep.lock"
    lock_path.mkdir()
    monkeypatch.setattr(xdist_backend, "_SWEEP_LOCK_PATH", lock_path)
    with pytest.warns(UserWarning, match="sweep skipped"):
        assert xdist_backend.sweep_stale_backend_containers(_FakeDockerClient()) == []


def test_sweep_lock_refuses_a_wrong_owner_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = tmp_path / "sweep.lock"
    lock_path.touch(mode=0o600)
    monkeypatch.setattr(xdist_backend, "_SWEEP_LOCK_PATH", lock_path)
    real_euid = os.geteuid()
    monkeypatch.setattr(xdist_backend.os, "geteuid", lambda: real_euid + 1)
    with pytest.warns(UserWarning, match="sweep skipped"):
        assert xdist_backend.sweep_stale_backend_containers(_FakeDockerClient()) == []
```

The subprocess body must import `tests.support.xdist_backend`, replace `_SWEEP_LOCK_PATH` with the
same `tmp_path` file in both processes, use only marker files for coordination, and carry a
ten-second `communicate` timeout with `finally` cleanup. It must exercise the public sweep, not only
`_locked`.

Run:

```sh
uv run python -m pytest tests/support/test_xdist_backend.py -q
```

Expected: the existing tests pass and every new behavioral group fails: interprocess ownership,
exact-conflict recovery, unrelated conflicts, lookup/deadline failure, lock-before-client ordering,
and unsafe lock paths. Record each group before implementation. Temporarily replace the expected
lock or conflict behavior with the old behavior and confirm the relevant test reddens; restore the
test before implementation.

### Step 2: Implement the minimal locked sweep

In `tests/support/xdist_backend.py`, add `time` and define the canonical lock independently of
environment-selected temp roots:

```python
_SWEEP_LOCK_PATH = Path(f"/tmp/kdive-test-backend-sweep-{os.geteuid()}.lock")
_REMOVAL_WAIT_S = 5.0
_REMOVAL_POLL_S = 0.05
```

Add a dedicated context manager; do not reuse `_locked`, whose per-run paths do not face a
world-writable predictable-name boundary:

```python
@contextmanager
def _sweep_locked() -> Iterator[bool]:
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
```

Implement the classifier as an exact status-and-explanation comparison. Short ids and substring
matches are deliberately rejected:

```python
def _removal_is_already_in_progress(exc: Exception, container_id: str) -> bool:
    import docker.errors

    return (
        isinstance(exc, docker.errors.APIError)
        and exc.status_code == 409
        and exc.explanation
        == f"removal of container {container_id} is already in progress"
    )
```

Implement bounded polling. NotFound alone returns `True`; another lookup exception or deadline
returns `False`:

```python
def _wait_until_container_absent(
    client: Any,
    container_id: str,
    *,
    timeout_s: float = _REMOVAL_WAIT_S,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> bool:
    import docker.errors

    clock = time.monotonic if clock is None else clock
    sleep = time.sleep if sleep is None else sleep
    deadline = clock() + timeout_s
    while True:
        try:
            client.containers.get(container_id)
        except docker.errors.NotFound:
            return True
        except Exception:  # noqa: BLE001 - lookup failure preserves the warning
            return False
        now = clock()
        if now >= deadline:
            return False
        sleep(min(_REMOVAL_POLL_S, deadline - now))
```

Take `_sweep_locked()` around client construction, enumeration, and the complete candidate loop.
When it yields `False`, return `[]` silently without constructing Docker. Catch other
lock/client/enumeration setup failures outside that context, emit the existing
`stale-backend sweep skipped` warning, and return `[]`; never enumerate unlocked. In the candidate
exception handler, suppress only when `_removal_is_already_in_progress` and
`_wait_until_container_absent` both return true. Preserve NotFound and every existing warning path.
Keep functions below 100 lines by extracting the already-locked candidate loop if needed.

Run the focused command from Step 1. Expected: all selected tests pass with no warnings outside the
tests that explicitly assert them.

### Step 3: Verify retained cleanup behavior

Run:

```sh
uv run python -m pytest tests/support/test_xdist_backend.py -q
```

Expected: the full support module passes, including the Docker-backed stale/live proof when Docker
is available; it skips only under the repository's existing Docker-unavailable rule.

Then run:

```sh
just lint
just type
```

Expected: both commands exit zero with no warnings. Review the diff for the 100-line function,
complexity-eight, and 100-character line limits. Commit the implementation and tests as one logical
fix with `fix(tests): serialize stale backend sweeps`.

### Step 4: Run the repository gate

Run bare:

```sh
just ci
```

Expected: every constituent recipe and the xdist suite pass with no stale-sweep warning. Run
`git status --porcelain` afterward; expected output is empty. If Docker is unavailable, the focused
real-container test may follow its existing skip contract, but the CI-required Docker path must run
in GitHub CI before merge handoff.

## Acceptance criteria

- Two public sweeps in separate processes cannot enumerate the shared candidate set concurrently.
- A contending sweep skips without warning or Docker construction instead of waiting on the owner.
- Only exact concurrent-removal 409 plus verified exact-id absence is silent.
- Unrelated conflicts, lookup failure, deadline expiry, and lock failure remain warnings.
- Default Docker-client construction occurs only after the sweep lock is held.
- A hostile symlink, wrong-owner inode, or non-regular lock path is not followed, truncated, or
  removed, and the sweep does not continue unlocked.
- The public fixture interface and existing Docker-backed stale/live proof are unchanged.
- Focused tests, `just lint`, `just type`, and `just ci` pass without untracked files.

## Rollback and cleanup

Reverting the implementation commit restores the prior unlocked best-effort sweep. Subprocess tests
terminate both children in `finally` and use pytest-owned temporary marker and lock paths. The empty
canonical `/tmp` lock file is harmless and may persist; no test deletes a lock file it did not
create.
