# Remote console capture (event loop + would-block sentinel) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make remote-libvirt console capture persist console bytes (today every `…/console` artifact is 0 bytes), so boot failures like #587 are diagnosable.

**Architecture:** Two jointly-required fixes (live-experiment-verified — see [ADR-0182](../../adr/0182-remote-console-would-block-vs-eof.md)): (1) the reconciler registers and runs the libvirt event loop so a non-blocking console stream's buffer is actually filled; (2) the collector/stream split the overloaded empty-read sentinel so the pump backs off on a would-block read instead of dropping+reopening the stream.

**Tech Stack:** Python 3.14, libvirt-python, pytest. Code under `src/kdive/providers/remote_libvirt/console/` and `src/kdive/providers/infra/`; reconciler entrypoint `src/kdive/__main__.py`.

## Global Constraints

- Guardrails before every commit: `just lint`, `just type` (whole tree), and the focused tests. `ty` is whole-tree.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Absolute imports only.
- `libvirt-python` is an unstubbed C-extension; suppress `unresolved-import` with a scoped per-site ignore only if `ty` flags it (existing code already imports `libvirt` cleanly).
- The collector/stream/`ConsoleStream` protocol are remote-only; do not touch local-libvirt.
- Commit messages: Conventional Commits, ≤72-char imperative subject, end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

- `src/kdive/providers/remote_libvirt/console/collector.py` — `ConsoleStream.recv` protocol return type → `bytes | None`; `pump_once` handles `None` (would-block, keep stream) vs `b""` (EOF, drop).
- `src/kdive/providers/remote_libvirt/console/wiring.py` — `_RemoteConsoleStream.recv` returns `None` for the libvirt `-2` would-block.
- `src/kdive/providers/infra/libvirt_event_loop.py` (new) — `ensure_libvirt_event_loop()`: idempotent register + durable run-thread.
- `src/kdive/__main__.py` — call `ensure_libvirt_event_loop()` as the first action in `_run_reconciler`.
- Tests: `tests/providers/remote_libvirt/console/test_console_collector.py`, `.../test_console_wiring.py`, `tests/providers/infra/test_libvirt_event_loop.py` (new).

---

### Task 1: Collector would-block sentinel

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/console/collector.py` (the `ConsoleStream` protocol ~55-63 and `pump_once` ~162-185)
- Test: `tests/providers/remote_libvirt/console/test_console_collector.py`

**Interfaces:**
- Produces: `ConsoleStream.recv(self, nbytes: int) -> bytes | None` — `None` = would-block (no data this read), `b""` = clean EOF, non-empty bytes = data.
- `ConsoleCollector.pump_once(self) -> bool` — unchanged signature; returns `False` on would-block (keeps stream) and on EOF (drops stream), `True` when bytes buffered.

- [ ] **Step 1: Write the failing test** (append to `test_console_collector.py`, after the existing `FakeStream`):

```python
class FakeNonBlockingStream:
    """recv returns None for would-block, bytes for data, b"" for EOF (the real non-blocking shape)."""

    def __init__(self, script: list[bytes | None]) -> None:
        self._script = list(script)
        self.closed = False
        self.recvs = 0

    def recv(self, nbytes: int) -> bytes | None:
        self.recvs += 1
        return self._script.pop(0) if self._script else b""

    def close(self) -> None:
        self.closed = True


def test_would_block_keeps_stream_open_and_captures_later_data() -> None:
    # None = would-block (no data yet). The pump must NOT drop/reopen; later data on the SAME
    # stream is captured. Before the fix, the None read is treated as EOF and drops the stream.
    stream = FakeNonBlockingStream([None, b"booting\n", None, b"emergency\n"])
    opener = FakeOpenConsole([stream])
    store = FakePartStore()
    collector = _collector(opener, store, rotation_threshold=1024)
    assert collector.pump_once() is False  # would-block: no data, stream kept
    assert stream.closed is False
    assert opener.opens == 1
    assert collector.pump_once() is True   # "booting\n" on the same stream
    assert collector.pump_once() is False  # would-block again, still no drop
    assert stream.closed is False
    assert collector.pump_once() is True   # "emergency\n"
    assert opener.opens == 1               # never reopened
    collector.finalize()
    assert store.artifact == b"booting\nemergency\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/remote_libvirt/console/test_console_collector.py::test_would_block_keeps_stream_open_and_captures_later_data -q`
Expected: FAIL — the `None` read hits `if not chunk:` and drops the stream, so `stream.closed` is `True` / `opens == 2`.

- [ ] **Step 3: Update the protocol and `pump_once`**

In `collector.py`, change the `ConsoleStream` protocol docstring + signature:

```python
class ConsoleStream(Protocol):
    """The slice of a ``virDomainOpenConsole`` stream the collector reads.

    ``recv`` returns up to ``nbytes`` of console output, ``None`` when a non-blocking read
    would block (no data this instant — keep the stream), ``b""`` on a clean end-of-stream
    (the collector reconnects), and raises on a dropped stream. ``close`` releases the stream.
    """

    def recv(self, nbytes: int) -> bytes | None: ...
    def close(self) -> None: ...
```

In `pump_once`, replace the recv-handling block (the `chunk = stream.recv(...)` ... `self._buffer.extend(chunk)` region) so `None` is distinguished from `b""`:

```python
            try:
                chunk = stream.recv(self._read_chunk)
            except Exception:  # noqa: BLE001 - any stream error is a drop; reconnect next pump
                _log.info("console stream for %s dropped; will reconnect", self._system_id)
                self._drop_stream()
                return False
            if chunk is None:
                # Would-block on the non-blocking stream: no data this instant. Keep the stream
                # open; the hosting loop backs off and pumps again (ADR-0182).
                return False
            if not chunk:
                self._drop_stream()
                return False
            self._buffer.extend(chunk)
            self._maybe_rotate()
            return True
```

- [ ] **Step 4: Run the test to verify it passes (and the existing suite)**

Run: `uv run python -m pytest tests/providers/remote_libvirt/console/test_console_collector.py -q`
Expected: PASS — the new test plus all existing tests (notably `test_empty_console_bytes_finalize_to_empty_artifact` and `test_reconnect_on_stream_drop`, which keep `b""`=EOF behavior).

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type`
```bash
git add src/kdive/providers/remote_libvirt/console/collector.py tests/providers/remote_libvirt/console/test_console_collector.py
git commit -m "fix(console): treat would-block as no-data, not end-of-stream (#594)"
```

---

### Task 2: `_RemoteConsoleStream.recv` returns None for would-block

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/console/wiring.py` (`_RemoteConsoleStream.recv` ~159-165)
- Test: `tests/providers/remote_libvirt/console/test_console_wiring.py`

**Interfaces:**
- Consumes: the `ConsoleStream.recv -> bytes | None` contract from Task 1.
- Produces: `_RemoteConsoleStream.recv(self, nbytes) -> bytes | None` — `-2 → None`, `-1`/`None` → raises `ConnectionError`, bytes (incl. `b""`) → returned.

- [ ] **Step 1: Write the failing test** (append to `test_console_wiring.py`):

```python
from kdive.providers.remote_libvirt.console.wiring import _RemoteConsoleStream


class _FakeLibvirtStream:
    def __init__(self, value: object) -> None:
        self._value = value

    def recv(self, nbytes: int) -> object:
        return self._value


def _wrapped(value: object) -> _RemoteConsoleStream:
    return _RemoteConsoleStream(conn=object(), stream=_FakeLibvirtStream(value), closer=lambda: None)


def test_recv_maps_would_block_to_none() -> None:
    assert _wrapped(-2).recv(8192) is None


def test_recv_returns_bytes_and_eof() -> None:
    assert _wrapped(b"data").recv(8192) == b"data"
    assert _wrapped(b"").recv(8192) == b""


def test_recv_raises_on_error_sentinels() -> None:
    import pytest

    for bad in (-1, None):
        with pytest.raises(ConnectionError):
            _wrapped(bad).recv(8192)
```

Note: `_RemoteConsoleStream.__init__` is positional `(self, conn, stream, closer)`; pass by keyword as above only if the signature accepts it, otherwise positional `_RemoteConsoleStream(object(), _FakeLibvirtStream(value), lambda: None)`. Check the constructor and match it.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/remote_libvirt/console/test_console_wiring.py -k recv -q`
Expected: FAIL — `test_recv_maps_would_block_to_none` fails because `-2` currently returns `b""`.

- [ ] **Step 3: Update `_RemoteConsoleStream.recv`**

```python
    def recv(self, nbytes: int) -> bytes | None:
        got = self._stream.recv(nbytes)
        if got is None or got == -1:
            raise ConnectionError("console stream recv failed")
        if got == -2:  # would-block on the non-blocking stream: no data this read (ADR-0182)
            return None
        return got
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python -m pytest tests/providers/remote_libvirt/console/test_console_wiring.py -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type`
```bash
git add src/kdive/providers/remote_libvirt/console/wiring.py tests/providers/remote_libvirt/console/test_console_wiring.py
git commit -m "fix(console): map libvirt -2 would-block to None on remote stream (#594)"
```

---

### Task 3: libvirt event loop registration in the reconciler

**Files:**
- Create: `src/kdive/providers/infra/libvirt_event_loop.py`
- Modify: `src/kdive/__main__.py` (`_run_reconciler` ~478 — first action)
- Test: `tests/providers/infra/test_libvirt_event_loop.py` (new)

**Interfaces:**
- Produces: `ensure_libvirt_event_loop(*, register=libvirt.virEventRegisterDefaultImpl, run=libvirt.virEventRunDefaultImpl, spawn=<thread starter>) -> None` — idempotent: registers the default impl once and starts one durable daemon run-thread; repeated calls are no-ops. Seams injected for tests.

- [ ] **Step 1: Write the failing test** (`tests/providers/infra/test_libvirt_event_loop.py`):

```python
"""Tests for the idempotent libvirt event-loop registration (ADR-0182)."""

from __future__ import annotations

import kdive.providers.infra.libvirt_event_loop as mod


def _reset() -> None:
    mod._STATE.registered = False  # test-only reset of the module guard


def test_registers_and_spawns_once_idempotent() -> None:
    _reset()
    registers: list[int] = []
    spawns: list[object] = []

    def register() -> None:
        registers.append(1)

    def spawn(target) -> None:  # noqa: ANN001 - test seam
        spawns.append(target)

    mod.ensure_libvirt_event_loop(register=register, run=lambda: None, spawn=spawn)
    mod.ensure_libvirt_event_loop(register=register, run=lambda: None, spawn=spawn)

    assert registers == [1]          # registered exactly once
    assert len(spawns) == 1          # run-thread started exactly once
    _reset()


def test_run_thread_retries_on_error_until_stopped() -> None:
    _reset()
    calls = {"n": 0}

    def run() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient libvirt event error")
        raise mod._StopRunLoop  # test-only sentinel to end the loop deterministically

    # Drive the loop body directly (no real thread/sleep): first call raises and is swallowed,
    # second ends the loop.
    mod._run_loop_body(run, sleep=lambda _s: None)
    assert calls["n"] == 2
    _reset()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/infra/test_libvirt_event_loop.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the helper**

```python
"""Process-wide libvirt event loop for the reconciler (ADR-0182).

A libvirt non-blocking stream's incoming buffer is filled by libvirt's event loop. The
remote console collector polls such a stream, so the reconciler must register the default
event-loop implementation and run it for the process lifetime, or every console capture
would-blocks forever and persists 0 bytes. Registration is idempotent; the run-thread is
durable (a transient error is logged and retried, not fatal) and observable.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import libvirt

_log = logging.getLogger(__name__)

_RETRY_BACKOFF_S = 1.0


class _StopRunLoop(Exception):
    """Test-only sentinel to end the run loop deterministically."""


@dataclass
class _State:
    registered: bool = False


_STATE = _State()
_LOCK = threading.Lock()


def _run_loop_body(run: Callable[[], None], *, sleep: Callable[[float], None]) -> None:
    """Run libvirt's event loop forever; log+retry a transient error instead of dying."""
    while True:
        try:
            run()
        except _StopRunLoop:
            return
        except Exception:  # noqa: BLE001 - a dead loop silently stops all console capture
            _log.warning("libvirt event loop iteration failed; retrying", exc_info=True)
            sleep(_RETRY_BACKOFF_S)


def _default_spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="libvirt-event-loop", daemon=True).start()


def ensure_libvirt_event_loop(
    *,
    register: Callable[[], None] = libvirt.virEventRegisterDefaultImpl,
    run: Callable[[], None] = libvirt.virEventRunDefaultImpl,
    spawn: Callable[[Callable[[], None]], None] = _default_spawn,
) -> None:
    """Register libvirt's default event loop and start its run-thread, once per process.

    Must be called before any libvirt connection whose stream events matter is opened
    (libvirt services only connections opened after registration). Idempotent.
    """
    with _LOCK:
        if _STATE.registered:
            return
        register()
        _STATE.registered = True
    spawn(lambda: _run_loop_body(run, sleep=time.sleep))
    _log.info("libvirt event loop registered and running")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python -m pytest tests/providers/infra/test_libvirt_event_loop.py -q`
Expected: PASS.

- [ ] **Step 5: Wire into the reconciler entrypoint**

In `src/kdive/__main__.py`, add to `_run_reconciler` as its **first** statement (before `stop = _install_stop()` / any provider composition):

```python
async def _run_reconciler(secret_registry: SecretRegistry, telemetry: Telemetry) -> None:
    from kdive.providers.infra.libvirt_event_loop import ensure_libvirt_event_loop

    ensure_libvirt_event_loop()  # before any libvirt connection (ADR-0182)
    ...
```

(Keep the existing local imports; place this import alongside them at the top of the function.)

- [ ] **Step 6: Guardrails + commit**

Run: `just lint && just type`
```bash
git add src/kdive/providers/infra/libvirt_event_loop.py tests/providers/infra/test_libvirt_event_loop.py src/kdive/__main__.py
git commit -m "fix(console): run a libvirt event loop in the reconciler (#594)"
```

---

## Self-Review

- **Spec coverage:** Task 3 = event-loop fix (spec Approach A + ordering/durability constraints); Tasks 1-2 = sentinel split (spec Approach B, changes 2-4). Spec change 1 (`ensure_libvirt_event_loop`) = Task 3. All covered.
- **Type consistency:** `ConsoleStream.recv -> bytes | None` (Task 1) matches `_RemoteConsoleStream.recv -> bytes | None` (Task 2). `ensure_libvirt_event_loop` keyword seams match the test.
- **Live gate (not automatable in CI):** after merge, an operator confirms a remote System's `…/console` is non-empty after a real boot (the experiment already showed the fix shape turns 0 → ~21 KB). Record in the PR body as the required live verification.
- **Note:** confirm `_RemoteConsoleStream.__init__` parameter names before finalizing the Task 2 test (use positional args if it is not keyword-friendly).
