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
- Add `_removal_is_already_in_progress(exc: Exception, container_id: str) -> bool`.
- Add `_wait_until_container_absent(client: Any, container_id: str, *, timeout_s: float = 5.0,
  clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> bool`.
  Focused tests may call this private seam to inject deterministic time.

### Step 1: Write the failing regression tests

Extend the fake Docker client with `get(container_id)` behavior and construct `docker.errors.APIError`
instances whose response carries status 409 and whose `explanation` is controlled by the test.
Add these tests before implementation:

```python
def test_concurrent_sweeps_have_one_effective_remover(tmp_path: Path) -> None:
    # Launch two Python subprocesses using the same monkeypatched sweep-lock path and shared marker
    # files. Process A blocks inside remove after process B announces it is about to sweep. While A
    # holds the lock it records failure if B has enumerated. After A marks the id removed and exits,
    # B enumerates an empty candidate list. Assert no violation marker and one removal marker.


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
    client = _FakeDockerClient(..., get_results=[container, container, docker.errors.NotFound(...)])
    clock = _FakeClock()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert xdist_backend._wait_until_container_absent(
            client, "cid-full", clock=clock, sleep=clock.sleep
        )
    assert client.get_ids == ["cid-full", "cid-full", "cid-full"]


def test_concurrent_removal_warns_at_the_deadline(tmp_path: Path) -> None:
    # Keep get("cid-full") present while injected sleep advances monotonic time to five seconds.
    # Assert the public sweep warns once and does not append the id to its result.


def test_sweep_lock_failure_warns_without_enumerating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeDockerClient()
    monkeypatch.setattr(xdist_backend, "_locked", _raising_lock)
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
    monkeypatch.setattr(xdist_backend, "_locked", _raising_lock)
    with pytest.warns(UserWarning, match="sweep skipped"):
        assert xdist_backend.sweep_stale_backend_containers() == []
    assert constructed is False
```

The subprocess body must import `tests.support.xdist_backend`, replace `_SWEEP_LOCK_PATH` with the
same `tmp_path` file in both processes, use only marker files for coordination, and carry a
ten-second `communicate` timeout with `finally` cleanup. It must exercise the public sweep, not only
`_locked`.

Run:

```sh
uv run python -m pytest \
  tests/support/test_xdist_backend.py \
  -k 'concurrent_sweeps or concurrent_removal or unrelated_409 or sweep_lock_failure' -q
```

Expected: the new tests fail because sweep enumeration is unlocked and the conflict helper does not
exist. Temporarily replace the expected lock or conflict behavior with the old behavior and confirm
the relevant test reddens; restore the test before implementation.

### Step 2: Implement the minimal locked sweep

In `tests/support/xdist_backend.py`, add `time` and define the canonical lock independently of
environment-selected temp roots:

```python
_SWEEP_LOCK_PATH = Path(f"/tmp/kdive-test-backend-sweep-{os.geteuid()}.lock")
_REMOVAL_WAIT_S = 5.0
_REMOVAL_POLL_S = 0.05
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
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    import docker.errors

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

Take `_locked(_SWEEP_LOCK_PATH)` around client construction, enumeration, and the complete candidate
loop. Catch lock/client/enumeration setup failures outside that context, emit the existing
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
- Only exact concurrent-removal 409 plus verified exact-id absence is silent.
- Unrelated conflicts, lookup failure, deadline expiry, and lock failure remain warnings.
- Default Docker-client construction occurs only after the sweep lock is held.
- The public fixture interface and existing Docker-backed stale/live proof are unchanged.
- Focused tests, `just lint`, `just type`, and `just ci` pass without untracked files.

## Rollback and cleanup

Reverting the implementation commit restores the prior unlocked best-effort sweep. Subprocess tests
terminate both children in `finally` and use pytest-owned temporary marker and lock paths. The empty
canonical `/tmp` lock file is harmless and may persist; no test deletes a lock file it did not
create.
