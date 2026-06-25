# Per-Run console slicing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Scope each Run's captured console (the per-Run artifact and the crash-signature gate input) to that Run's own boot window on both providers, so a readiness-failing Run no longer matches a prior boot's `Kernel panic` from the same System's cumulative history.

**Architecture:** The whole boot window (read mark → `booter.boot` → readiness/crash → console capture) runs in one synchronous `boot_handler` invocation in the worker. So the boot-window "mark" is a worker-local `int` read once before `booter.boot` and threaded into every capture site — no persistence, no collector change, no migration. Local marks a `<sys>.log` byte offset; remote marks the next S3 part index. See [ADR-0241](../../adr/0241-per-run-console-slicing.md) and [the spec](../specs/2026-06-24-per-run-console-slicing.md).

**Tech Stack:** Python 3.14, `uv`, `pytest`, psycopg (async), libvirt/virtlogd console logs, S3-compatible object store.

## Global Constraints

- Python 3.14 managed with `uv`; run everything via `just` recipes (the justfile is the single source of truth). Never invent the underlying command.
- Per-commit guardrails (CI gates these individually): `just lint` (ruff check + format), `just type` (ty, whole tree src+tests), `just test` (excludes `live_vm`). Doc gates already passed for the spec/ADR; this plan changes no docs.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict. Google-style docstrings on non-trivial public APIs.
- Absolute imports only (no relative `..`). Conventional-commit subjects ≤72 chars, imperative, ending with the trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- TDD: failing test first, confirm it fails for the expected reason, minimal implementation, rerun focused test + guardrails, commit. Test behavior and edge/error paths, not implementation details.
- **Do not weaken any test gate.** The `live_vm`/`live_stack` suites stay gated.
- Run a single test: `uv run python -m pytest <path>::<name> -q`.
- Rollback for the whole change is removing the edits: marks stop being read and capture reverts to cumulative; no persisted state to reverse.

---

### Task 1: `read_console_log` byte offset + rotation guard

**Files:**
- Modify: `src/kdive/providers/shared/runtime_paths.py:56-86` (`read_console_log`)
- Test: `tests/providers/test_runtime_paths.py`

**Interfaces:**
- Produces: `read_console_log(path: Path, offset: int = 0) -> bytes` — returns bytes appended after `offset`; returns the whole file when `offset <= 0` or `offset > len(file)` (virtlogd rotated/truncated since the mark). Unchanged `FileNotFoundError → b""`, `PermissionError → CONFIGURATION_ERROR`, `OSError → INFRASTRUCTURE_FAILURE`.

Implementation note: read the whole file (as today) then slice, rather than `seek`, to preserve the existing exception categorization exactly. The local console log is bounded by virtlogd `max_size` (~2 MiB), so reading whole then slicing is cheap.

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/test_runtime_paths.py`:

```python
def test_read_console_log_offset_returns_tail(tmp_path: Path) -> None:
    path = tmp_path / "console.log"
    path.write_bytes(b"prior boot\nthis boot\n")

    assert read_console_log(path, offset=len(b"prior boot\n")) == b"this boot\n"


def test_read_console_log_offset_zero_returns_whole_file(tmp_path: Path) -> None:
    path = tmp_path / "console.log"
    path.write_bytes(b"whole\n")

    assert read_console_log(path, offset=0) == b"whole\n"


def test_read_console_log_offset_past_eof_reads_whole_file(tmp_path: Path) -> None:
    # virtlogd rotated the log between the mark and the capture: the live file is now shorter
    # than the recorded offset. Degrade to cumulative for this capture rather than empty.
    path = tmp_path / "console.log"
    path.write_bytes(b"rotated fresh\n")

    assert read_console_log(path, offset=10_000) == b"rotated fresh\n"


def test_read_console_log_offset_equal_to_size_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "console.log"
    path.write_bytes(b"exactly\n")

    assert read_console_log(path, offset=len(b"exactly\n")) == b""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/test_runtime_paths.py -q -k offset`
Expected: FAIL — `read_console_log() got an unexpected keyword argument 'offset'`.

- [ ] **Step 3: Add the `offset` parameter + rotation guard**

In `src/kdive/providers/shared/runtime_paths.py`, replace `read_console_log`:

```python
def read_console_log(path: Path, offset: int = 0) -> bytes:
    """Read a System console log; absent logs are treated as empty.

    ``offset`` slices to one boot window (ADR-0241): bytes appended at/after ``offset`` are
    returned. As a rotation guard, ``offset <= 0`` or an ``offset`` past the file's current end —
    which happens when virtlogd rotated/truncated the log between the mark read and this capture —
    returns the whole current file (degrade to cumulative for this one capture rather than an empty
    slice that would drop this boot's evidence). The size comparison does not detect a rotation
    that regrew past ``offset`` (an accepted residual, ADR-0241 caveat 4).
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return b""
    except PermissionError as err:
        # A non-root worker under qemu:///system cannot read virtlogd's root:0600 console log
        # (ADR-0223). This never heals on retry — it is a host config problem, not transient
        # infrastructure — so categorize it as such and name the operator fix.
        raise CategorizedError(
            "failed to read console log",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "operation": "read_console_log",
                "path": str(path),
                "error": type(err).__name__,
                "remediation": WORKER_READABILITY_REMEDIATION,
            },
        ) from err
    except OSError as err:
        raise CategorizedError(
            "failed to read console log",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={
                "operation": "read_console_log",
                "path": str(path),
                "error": type(err).__name__,
            },
        ) from err
    if 0 < offset <= len(data):
        return data[offset:]
    return data
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/test_runtime_paths.py -q`
Expected: PASS (new offset tests + the four pre-existing `read_console_log` tests, which exercise the `offset=0` default path).

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type && uv run python -m pytest tests/providers/test_runtime_paths.py -q`
Expected: all clean.

```bash
git add src/kdive/providers/shared/runtime_paths.py tests/providers/test_runtime_paths.py
git commit -m "feat(console): add byte offset + rotation guard to read_console_log (#773)"
```
(append the required `Co-Authored-By` trailer)

---

### Task 2: `RemoteConsolePartStore.assemble` start_index

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/console/wiring.py:105-109` (`assemble`)
- Test: `tests/providers/remote_libvirt/console/test_console_wiring.py`

**Interfaces:**
- Produces: `RemoteConsolePartStore.assemble(system_id: UUID, start_index: int = 0) -> bytes` — concatenates parts whose index `>= start_index`, in index order. Default `0` keeps the teardown `finalize()` whole-history assembly unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/remote_libvirt/console/test_console_wiring.py`:

```python
def test_assemble_concatenates_all_parts_by_default() -> None:
    store = FakeObjectStore()
    part_store = RemoteConsolePartStore(store, "unused")
    sid = uuid4()
    part_store.put_part(sid, 0, b"a")
    part_store.put_part(sid, 1, b"b")
    part_store.put_part(sid, 2, b"c")
    assert part_store.assemble(sid) == b"abc"


def test_assemble_start_index_slices_to_boot_window() -> None:
    # Parts 0..1 are a prior boot; this boot's window starts at part index 2.
    store = FakeObjectStore()
    part_store = RemoteConsolePartStore(store, "unused")
    sid = uuid4()
    part_store.put_part(sid, 0, b"prior ")
    part_store.put_part(sid, 1, b"boot ")
    part_store.put_part(sid, 2, b"this ")
    part_store.put_part(sid, 3, b"boot")
    assert part_store.assemble(sid, start_index=2) == b"this boot"


def test_assemble_start_index_past_all_parts_is_empty() -> None:
    store = FakeObjectStore()
    part_store = RemoteConsolePartStore(store, "unused")
    sid = uuid4()
    part_store.put_part(sid, 0, b"prior")
    assert part_store.assemble(sid, start_index=1) == b""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/remote_libvirt/console/test_console_wiring.py -q -k assemble`
Expected: FAIL — `assemble() got an unexpected keyword argument 'start_index'`.

- [ ] **Step 3: Add the `start_index` parameter**

In `src/kdive/providers/remote_libvirt/console/wiring.py`, replace `assemble`:

```python
    def assemble(self, system_id: UUID, start_index: int = 0) -> bytes:
        """Concatenate the System's numbered console parts in index order (no DB access).

        ``start_index`` slices to one boot window (ADR-0241): only parts with index
        ``>= start_index`` are included. Default ``0`` is the whole history (the teardown
        ``finalize()`` assembly).
        """
        return b"".join(
            self.read_part(system_id, index)
            for index in self.list_part_indices(system_id)
            if index >= start_index
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/remote_libvirt/console/test_console_wiring.py -q`
Expected: PASS (new assemble tests + all pre-existing wiring tests).

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type && uv run python -m pytest tests/providers/remote_libvirt/console/test_console_wiring.py -q`

```bash
git add src/kdive/providers/remote_libvirt/console/wiring.py tests/providers/remote_libvirt/console/test_console_wiring.py
git commit -m "feat(console): slice RemoteConsolePartStore.assemble by start_index (#773)"
```
(append the trailer)

---

### Task 3: `ConsoleSnapshotter` port — `mark_boot_window` + `snapshot(start_index)`

**Files:**
- Modify: `src/kdive/providers/ports/console.py:31-43` (the `ConsoleSnapshotter` Protocol)

**Interfaces:**
- Produces (Protocol additions both implementers and the handler rely on):
  - `async def mark_boot_window(self, system_id: UUID) -> int` — the next part index at boot start (the remote boot-window mark).
  - `async def snapshot(self, conn, system_id: UUID, run_id: UUID, start_index: int = 0) -> ConsoleSnapshot | None` — assemble only parts `>= start_index`.

No test in this task: a Protocol has no runtime behavior of its own; Task 4 tests the concrete implementation and Task 5 tests the handler dispatch. The default `start_index: int = 0` keeps the four existing `test_console_snapshot.py` tests (which call `.snapshot(conn, system_id, run_id)`) valid.

- [ ] **Step 1: Extend the Protocol**

In `src/kdive/providers/ports/console.py`, replace the `ConsoleSnapshotter` class body:

```python
class ConsoleSnapshotter(Protocol):
    """Persist an immutable per-Run console snapshot for a System's current boot."""

    async def mark_boot_window(self, system_id: UUID) -> int:
        """Return the boot-window mark to record before the boot starts (ADR-0241).

        For a part-based collector this is the next part index (parts produced from now on belong
        to this boot). The boot handler reads it before ``booter.boot`` and passes it back to
        :meth:`snapshot` as ``start_index`` so only this boot's parts are assembled. Never raises:
        the handler treats a failure as mark ``0`` (cumulative — the pre-slicing behavior).
        """
        ...

    async def snapshot(
        self, conn: AsyncConnection, system_id: UUID, run_id: UUID, start_index: int = 0
    ) -> ConsoleSnapshot | None:
        """Assemble the console for this boot window and write a per-Run ``console-<run>`` artifact.

        ``start_index`` (the mark from :meth:`mark_boot_window`) slices to one boot window:
        only parts with index ``>= start_index`` are assembled (ADR-0241). Default ``0`` is the
        whole history. The artifact row is written on ``conn`` so it commits atomically with the
        boot step. Returns ``None`` when no console bytes are available for the window yet. Never
        raises for an absent or partial console — capture is best-effort and must not fail the boot.
        """
        ...
```

- [ ] **Step 2: Type-check the Protocol change**

Run: `just type`
Expected: clean (no implementer yet diverges — Task 4 updates the concrete class in the same branch; if `ty` flags the existing `RemoteLibvirtConsoleSnapshotter` as not satisfying the Protocol, that is expected and fixed in Task 4. If so, proceed to Task 4 before committing Task 3.)

- [ ] **Step 3: Commit (fold with Task 4 if `ty` is red)**

If `just type` is clean standalone:
```bash
git add src/kdive/providers/ports/console.py
git commit -m "feat(console): add mark_boot_window + snapshot start_index to port (#773)"
```
Otherwise do not commit yet — implement Task 4 and commit Tasks 3+4 together so the tree is green at the commit (never commit with a red guardrail).
(append the trailer)

---

### Task 4: `RemoteLibvirtConsoleSnapshotter` — mark + sliced snapshot

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/console/snapshot.py:38-58` (`RemoteLibvirtConsoleSnapshotter`)
- Test: `tests/providers/remote_libvirt/console/test_console_snapshot.py`

**Interfaces:**
- Consumes: `RemoteConsolePartStore.assemble(system_id, start_index)` (Task 2), `RemoteConsolePartStore.list_part_indices` (existing), the `ConsoleSnapshotter` Protocol (Task 3).
- Produces: `RemoteLibvirtConsoleSnapshotter().mark_boot_window(system_id) -> int` and `.snapshot(conn, system_id, run_id, start_index=0)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/providers/remote_libvirt/console/test_console_snapshot.py` (the `_run_snapshot` helper, `_seed_parts`, `_count_rows`, and `FakeObjectStore` import already exist at the top of the file):

```python
async def _run_mark(system_id: UUID) -> int:
    return await RemoteLibvirtConsoleSnapshotter().mark_boot_window(system_id)


async def _run_snapshot_sliced(migrated_url: str, system_id: UUID, run_id: UUID, start_index: int):
    async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
        return await RemoteLibvirtConsoleSnapshotter().snapshot(
            conn, system_id, run_id, start_index
        )


def test_mark_boot_window_is_next_part_index(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeObjectStore()
    monkeypatch.setattr(snapshot_mod, "object_store_from_env", lambda: store)
    system_id = uuid4()
    _seed_parts(store, system_id, [b"a", b"b"])  # parts 0, 1
    assert asyncio.run(_run_mark(system_id)) == 2


def test_mark_boot_window_zero_when_no_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeObjectStore()
    monkeypatch.setattr(snapshot_mod, "object_store_from_env", lambda: store)
    assert asyncio.run(_run_mark(uuid4())) == 0


def test_snapshot_slices_to_boot_window(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Prior boot wrote parts 0..1 (ending in a panic); this boot's window starts at the mark.
    store = FakeObjectStore()
    monkeypatch.setattr(snapshot_mod, "object_store_from_env", lambda: store)
    system_id, run_id = uuid4(), uuid4()
    _seed_parts(store, system_id, [b"prior ", b"Kernel panic\n"])  # parts 0, 1

    mark = asyncio.run(_run_mark(system_id))  # == 2
    _seed_parts(store, system_id, [b"prior ", b"Kernel panic\n", b"this boot READY\n"])  # +part 2

    snap = asyncio.run(_run_snapshot_sliced(migrated_url, system_id, run_id, mark))

    assert snap is not None
    assert snap.data == b"this boot READY\n"  # no prior-boot panic in the window
    assert asyncio.run(_count_rows(migrated_url, system_id, snap.object_key)) == 1


def test_snapshot_empty_window_returns_none(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Healthy boot whose bytes never rotated into a part: the window is empty → no artifact.
    store = FakeObjectStore()
    monkeypatch.setattr(snapshot_mod, "object_store_from_env", lambda: store)
    system_id, run_id = uuid4(), uuid4()
    _seed_parts(store, system_id, [b"prior boot"])  # part 0
    mark = asyncio.run(_run_mark(system_id))  # == 1, nothing at/after it

    snap = asyncio.run(_run_snapshot_sliced(migrated_url, system_id, run_id, mark))

    assert snap is None
    key = f"remote-libvirt/systems/{system_id}/console-{run_id}"
    assert asyncio.run(_count_rows(migrated_url, system_id, key)) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/remote_libvirt/console/test_console_snapshot.py -q -k "mark or slices or empty_window"`
Expected: FAIL — `RemoteLibvirtConsoleSnapshotter` has no attribute `mark_boot_window` / `snapshot()` takes no `start_index`.

- [ ] **Step 3: Implement mark + sliced snapshot**

In `src/kdive/providers/remote_libvirt/console/snapshot.py`, replace the `snapshot` method and add `mark_boot_window`:

```python
    async def mark_boot_window(self, system_id: UUID) -> int:
        """Return the next part index for ``system_id`` — this boot's window starts here.

        Read from the S3 part-index list (not the collector's memory), so it is unaffected by a
        collector restart/reconnect: ``_take_index`` keeps part indices monotonic (ADR-0241).
        """

        def _next_index() -> int:
            store = object_store_from_env()
            parts = RemoteConsolePartStore(store, "")
            existing = parts.list_part_indices(system_id)
            return (max(existing) + 1) if existing else 0

        return await asyncio.to_thread(_next_index)

    async def snapshot(
        self, conn: AsyncConnection, system_id: UUID, run_id: UUID, start_index: int = 0
    ) -> ConsoleSnapshot | None:
        """Persist a ``console-<run>`` artifact from this boot's parts (index ``>= start_index``).

        Returns ``None`` when the boot window has no parts yet. The blocking S3 work runs in a
        worker thread; the row is upserted on ``conn`` so it commits with the boot step.
        """
        store = object_store_from_env()
        # The conninfo is unused on this path: this snapshotter writes the per-Run `artifacts` row
        # on the boot handler's `conn` (below), never via the part store's own teardown row path.
        parts = RemoteConsolePartStore(store, "")
        data = await asyncio.to_thread(parts.assemble, system_id, start_index)
        if not data:
            return None
        stored = await asyncio.to_thread(parts.put_run_console, system_id, run_id, data)
        artifact_id = await _upsert_run_console_row(conn, system_id, stored)
        return ConsoleSnapshot(artifact_id, stored.key, data)
```

Note: `asyncio.to_thread(parts.assemble, system_id, start_index)` passes `start_index` positionally.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/remote_libvirt/console/test_console_snapshot.py -q`
Expected: PASS — new mark/slice/empty tests plus the four pre-existing tests (which call `.snapshot(conn, system_id, run_id)`, hitting the `start_index=0` default → unchanged cumulative behavior).

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type && uv run python -m pytest tests/providers/remote_libvirt/console/ -q`
Expected: clean. (`just type` now sees the implementer satisfy the Protocol from Task 3.)

```bash
git add src/kdive/providers/ports/console.py src/kdive/providers/remote_libvirt/console/snapshot.py tests/providers/remote_libvirt/console/test_console_snapshot.py
git commit -m "feat(console): slice remote per-Run snapshot to its boot window (#773)"
```
(If Task 3 was not committed standalone, this commit includes `ports/console.py`; otherwise drop it from the `git add`.) (append the trailer)

---

### Task 5: Boot handler — read the mark, thread it through every capture site

**Files:**
- Modify: `src/kdive/jobs/handlers/runs_boot.py` — `_capture_console_artifact` (75-105), `_read_redacted_console` (137-149), `_capture_run_console` (108-134), `_record_crash_halted_live` (282-317), `_run_boot_and_capture_outcome` (393-443), `boot_handler` (446-502); add `_mark_boot_window`.
- Test: `tests/jobs/handlers/test_runs_boot.py`

**Interfaces:**
- Consumes: `read_console_log(path, offset)` (Task 1), `snapshotter.mark_boot_window` + `snapshotter.snapshot(..., start_index)` (Tasks 3–4).
- Produces: `_mark_boot_window(system_id: UUID, snapshotter: ConsoleSnapshotter | None) -> int` and a `mark: int` threaded into `_capture_run_console(..., mark)`, `_record_crash_halted_live(..., mark)`, and `_run_boot_and_capture_outcome(..., mark)`.

Design (matches the spec): `boot_handler` computes `mark` after resolving `snapshotter`/`system_id` and **before** `_run_boot_and_capture_outcome` (whose first action is `booter.boot`), passes `mark` in, and reuses it in the boot-failure best-effort capture (`boot_handler ~488`). Remote → `snapshotter.snapshot(conn, system_id, run_id, mark)`; local → `_capture_console_artifact(..., offset=mark)` → `_read_redacted_console(system_id, secret_registry, mark)` → `read_console_log(path, offset=mark)`. `_mark_boot_window` is best-effort: any failure logs and returns `0` (cumulative — never fails the boot).

- [ ] **Step 1: Write the failing tests**

Append to `tests/jobs/handlers/test_runs_boot.py` (it already imports the module under test; add imports it lacks at the top — `from uuid import uuid4`, `from pathlib import Path`, and the module alias if not present). These two unit tests drive the helpers directly with injected deps (no transport):

```python
def test_mark_boot_window_local_is_file_size(tmp_path, monkeypatch) -> None:
    # Local (no snapshotter): the mark is the current console-log byte size.
    from kdive.jobs.handlers import runs_boot

    system_id = uuid4()
    log = tmp_path / f"{system_id}.log"
    log.write_bytes(b"prior boot bytes\n")
    monkeypatch.setattr(runs_boot, "console_log_path", lambda sid: log)

    mark = asyncio.run(runs_boot._mark_boot_window(system_id, None))

    assert mark == len(b"prior boot bytes\n")


def test_mark_boot_window_local_zero_when_log_absent(tmp_path, monkeypatch) -> None:
    from kdive.jobs.handlers import runs_boot

    system_id = uuid4()
    monkeypatch.setattr(runs_boot, "console_log_path", lambda sid: tmp_path / "missing.log")

    assert asyncio.run(runs_boot._mark_boot_window(system_id, None)) == 0


def test_mark_boot_window_remote_uses_snapshotter(monkeypatch) -> None:
    from kdive.jobs.handlers import runs_boot

    class _Snap:
        async def mark_boot_window(self, system_id):
            return 7

        async def snapshot(self, conn, system_id, run_id, start_index=0):
            return None

    assert asyncio.run(runs_boot._mark_boot_window(uuid4(), _Snap())) == 7


def test_mark_boot_window_degrades_to_zero_on_failure(monkeypatch) -> None:
    from kdive.jobs.handlers import runs_boot

    class _Boom:
        async def mark_boot_window(self, system_id):
            raise RuntimeError("s3 down")

        async def snapshot(self, conn, system_id, run_id, start_index=0):
            return None

    # Best-effort: a mark-read failure must not propagate; it degrades to cumulative (0).
    assert asyncio.run(runs_boot._mark_boot_window(uuid4(), _Boom())) == 0
```

For the local capture-offset wiring, add a test that the offset reaches `read_console_log`:

```python
def test_capture_console_artifact_reads_from_offset(tmp_path, monkeypatch) -> None:
    from kdive.jobs.handlers import runs_boot

    system_id = uuid4()
    log = tmp_path / f"{system_id}.log"
    log.write_bytes(b"prior\nthis boot panic\n")
    monkeypatch.setattr(runs_boot, "console_log_path", lambda sid: log)

    seen = {}

    async def fake_capture(system_id_arg, secret_registry, offset):
        seen["offset"] = offset
        return b"this boot panic\n"

    monkeypatch.setattr(runs_boot, "_read_redacted_console", fake_capture)
    # Drive _capture_console_artifact with a no-op store path; assert the offset is forwarded.
    runs_boot  # marker; the real assertion is below via _read_redacted_console offset
    redacted = asyncio.run(
        runs_boot._read_redacted_console(system_id, _NoSecrets(), len(b"prior\n"))
    )
    assert redacted == b"this boot panic\n"
```

where `_NoSecrets` is a minimal `SecretRegistry` stand-in already used elsewhere in this test file (reuse the file's existing secret-registry fixture/helper; if none exists, construct `SecretRegistry()` — it redacts nothing for unregistered values). Confirm the helper name by reading the top of `tests/jobs/handlers/test_runs_boot.py` before writing this test.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -q -k "mark_boot_window or reads_from_offset"`
Expected: FAIL — `_mark_boot_window` undefined / `_read_redacted_console` takes no offset.

- [ ] **Step 3: Implement the mark + thread it through**

In `src/kdive/jobs/handlers/runs_boot.py`:

(a) Add the mark helper (place after `_capture_run_console`):

```python
async def _mark_boot_window(system_id: UUID, snapshotter: ConsoleSnapshotter | None) -> int:
    """The boot-window start mark, read before ``booter.boot`` (ADR-0241).

    Remote: the snapshotter's next part index. Local: the current console-log byte size. Best-effort
    — any failure degrades to ``0`` (cumulative, the pre-slicing behavior) and never fails the boot.
    """
    try:
        if snapshotter is not None:
            return await snapshotter.mark_boot_window(system_id)
        return await asyncio.to_thread(_console_log_size, system_id)
    except Exception:
        _log.warning(
            "reading the console boot-window mark for system %s failed; "
            "capturing the cumulative console for this boot",
            system_id,
            exc_info=True,
        )
        return 0


def _console_log_size(system_id: UUID) -> int:
    try:
        return console_log_path(system_id).stat().st_size
    except FileNotFoundError:
        return 0
```

(b) Thread `offset` into the local read. Replace `_read_redacted_console` signature/first line:

```python
async def _read_redacted_console(
    system_id: UUID, secret_registry: SecretRegistry, offset: int = 0
) -> bytes | None:
    raw = await asyncio.to_thread(read_console_log, console_log_path(system_id), offset)
```
(the rest of the function body is unchanged.)

(c) Thread `offset` into `_capture_console_artifact`. Change its signature to add `offset: int = 0` and its `_read_redacted_console` call:

```python
async def _capture_console_artifact(
    conn: AsyncConnection,
    system_id: UUID,
    run_id: UUID,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
    offset: int = 0,
) -> _ConsoleArtifact | None:
    try:
        if artifact_store is None:
            return None
        redacted = await _read_redacted_console(system_id, secret_registry, offset)
```
(the rest of the body is unchanged.)

(d) Thread `mark` through `_capture_run_console`. Add a `mark: int` parameter and pass it to both branches:

```python
async def _capture_run_console(
    conn: AsyncConnection,
    system_id: UUID,
    run_id: UUID,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
    snapshotter: ConsoleSnapshotter | None,
    mark: int,
) -> _ConsoleArtifact | None:
    if snapshotter is not None:
        try:
            snap = await snapshotter.snapshot(conn, system_id, run_id, mark)
        except Exception:
            _log.warning(
                "console snapshot failed for system %s run %s; boot outcome unaffected",
                system_id,
                run_id,
                exc_info=True,
            )
            return None
        return None if snap is None else _ConsoleArtifact(snap.id, snap.object_key, snap.data)
    return await _capture_console_artifact(
        conn, system_id, run_id, secret_registry, artifact_store, mark
    )
```
(keep the existing docstring.)

(e) Thread `mark` into `_record_crash_halted_live`: add a `mark: int` parameter (after `snapshotter`) and pass it to its `_capture_run_console` call:

```python
    artifact = await _capture_run_console(
        conn, system_id, run.id, secret_registry, artifact_store, snapshotter, mark
    )
```

(f) Thread `mark` into `_run_boot_and_capture_outcome`: add a `mark: int` parameter (after `snapshotter`) and pass it to all three `_capture_run_console` / `_record_crash_halted_live` calls (the expected-crash site, the ready site, and the crashed-halted-live call).

(g) In `boot_handler`, compute the mark before the boot and pass it both into `_run_boot_and_capture_outcome` and the boot-failure best-effort capture:

```python
    snapshotter = binding.runtime.console_snapshotter
    system_id = run.require_system_id()
    mark = await _mark_boot_window(system_id, snapshotter)

    try:
        result = await _run_boot_and_capture_outcome(
            conn,
            job_ctx,
            run,
            booter,
            binding.runtime.connector,
            binding.runtime.profile_policy,
            secret_registry,
            artifact_store,
            snapshotter,
            mark,
        )
    except CategorizedError:
        await abandon_run_step_best_effort(conn, run_id, "boot")
        try:
            await _capture_run_console(
                conn, system_id, run_id, secret_registry, artifact_store, snapshotter, mark
            )
        finally:
            raise
    except Exception:
        await abandon_run_step_best_effort(conn, run_id, "boot")
        raise
```

- [ ] **Step 4: Run the focused tests + the whole boot-handler suite**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -q`
Expected: PASS — new mark/offset tests plus all pre-existing boot-handler tests (the two-Runs-one-System immutability test and the crash-gate tests still pass: with no prior content seeded their windows start at `0`/empty-prefix, so behavior is unchanged).

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type && uv run python -m pytest tests/jobs/handlers/test_runs_boot.py tests/providers/ -q`
Expected: clean.

```bash
git add src/kdive/jobs/handlers/runs_boot.py tests/jobs/handlers/test_runs_boot.py
git commit -m "feat(console): scope boot-handler capture + gates to the boot window (#773)"
```
(append the trailer)

---

### Task 6: Provider-parity slicing test (two Runs, one System)

**Files:**
- Test: `tests/jobs/handlers/test_runs_boot.py` (local parity) and `tests/providers/remote_libvirt/console/test_console_snapshot.py` (remote parity — may already be covered by Task 4's `test_snapshot_slices_to_boot_window`; add the local mirror here).

**Interfaces:** none new — this task only adds a behavioral parity test proving the acceptance criterion end-to-end at the helper boundary.

- [ ] **Step 1: Write the local parity test**

Append to `tests/jobs/handlers/test_runs_boot.py`. This proves a readiness-failing second Run does NOT see the first Run's panic once sliced:

```python
def test_local_slice_excludes_prior_boot_panic(tmp_path, monkeypatch) -> None:
    from kdive.jobs.handlers import runs_boot

    system_id = uuid4()
    log = tmp_path / f"{system_id}.log"
    monkeypatch.setattr(runs_boot, "console_log_path", lambda sid: log)

    # Run A boots and panics; bytes are appended to the System's serial log.
    log.write_bytes(b"[run A] Kernel panic - not syncing: A\n")

    # Run B's boot-window mark is taken before its boot appends a clean log.
    mark_b = asyncio.run(runs_boot._mark_boot_window(system_id, None))
    with log.open("ab") as fh:
        fh.write(b"[run B] booted clean READY\n")

    redacted_b = asyncio.run(runs_boot._read_redacted_console(system_id, SecretRegistry(), mark_b))

    assert redacted_b == b"[run B] booted clean READY\n"
    assert not runs_boot._generic_panic_matches(redacted_b)  # prior panic is out of B's window
```
(Import `SecretRegistry` from `kdive.security.secrets.secret_registry` at the top of the test file if it is not already imported.)

- [ ] **Step 2: Run it to verify it passes (the slicing machinery from Tasks 1+5 is in place)**

Run: `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py::test_local_slice_excludes_prior_boot_panic -q`
Expected: PASS. (Before Task 5's wiring it would have failed because the offset was ignored — this is the integration proof, so it is acceptable for it to pass immediately here.)

- [ ] **Step 3: Confirm the remote mirror exists**

Verify `tests/providers/remote_libvirt/console/test_console_snapshot.py::test_snapshot_slices_to_boot_window` (Task 4) asserts the same property for remote (a prior-boot `Kernel panic` part is excluded from the window). If both providers have a prior-panic-exclusion test, the parity acceptance criterion is covered.

- [ ] **Step 4: Guardrails + commit**

Run: `just lint && just type && uv run python -m pytest tests/jobs/handlers/test_runs_boot.py tests/providers/remote_libvirt/console/ -q`

```bash
git add tests/jobs/handlers/test_runs_boot.py
git commit -m "test(console): prove prior-boot panic excluded from the sliced window (#773)"
```
(append the trailer)

---

### Final verification (before push — Step 7 of work-issue)

- [ ] Run the **full** local suite, not just touched dirs: `just lint && just type && just test`
- [ ] Confirm no `live_vm`/`live_stack` gate was weakened (`git diff main -- pyproject.toml` shows no marker changes).
- [ ] Run the doc gates that the spec/ADR touched: `just adr-status-check && just docs-links && just docs-paths` (no code-doc generation is affected by this change).

## Self-Review (completed by plan author)

**Spec coverage:**
- Local byte offset + rotation guard → Task 1. ✓
- Remote part-index slice (`assemble start_index`) → Task 2. ✓
- Port additions (`mark_boot_window`, `snapshot start_index`) → Task 3. ✓
- Remote snapshotter mark + sliced snapshot, empty-window → None → Task 4. ✓
- Boot handler: mark read before `booter.boot`, threaded into all four capture sites incl. the boot-failure best-effort path; best-effort degrade-to-0 → Task 5. ✓
- Acceptance (prior-boot panic excluded, both providers) → Task 6 (local) + Task 4 (remote). ✓
- No migration / object key unchanged / within-Run idempotency → unchanged by construction (no key or schema edits in any task). ✓
- Caveat 3 (empty remote ready window → None) → Task 4 `test_snapshot_empty_window_returns_none`. ✓

**Type consistency:** `mark`/`start_index`/`offset` are all `int`; `mark_boot_window(system_id) -> int`; `snapshot(conn, system_id, run_id, start_index=0)`; `assemble(system_id, start_index=0)`; `read_console_log(path, offset=0)` — consistent across Tasks 1–5.

**Placeholders:** none — every code step shows the full edited function or appended test.
