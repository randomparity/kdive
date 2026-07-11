# gdb backtrace and frame inspection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two read-only gdb-MI tools — `debug.backtrace` and `debug.read_frame` — that expose the stopped kernel's structured, redacted, bounded call stack over a live gdbstub `DebugSession`.

**Architecture:** Two new ops on the shared `GdbMiEngine` (so local-libvirt and remote-libvirt both gain them) issue `-stack-list-frames`, parsed by a new `stack_frames` helper into `GdbFrame`s, returned via a new `GdbBacktrace` port model and two MCP tools wired through the existing `run_engine_op` gate. See spec `docs/specs/2026-06-29-gdb-backtrace-frame-inspection.md` and ADR `docs/adr/0275-gdb-backtrace-frame-inspection.md`.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`, FastMCP, pygdbmi, pydantic.

## Global Constraints

- Python 3.14; manage with `uv`. Run guardrails as `just lint`, `just type`, `just test` (CI runs these recipes individually — all must be green before each commit).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` runs whole-tree (src + tests).
- Absolute imports only (no relative `..`). Google-style docstrings on non-trivial public APIs.
- Every tool returns a `ToolResponse`; a failure status carries an `error_category` and (by convention) a `data["code"]` discriminator. Pick the most specific existing `ErrorCategory`; never invent strings.
- All textual gdb/MI output passes the `Redactor` before it is returned or persisted.
- Doc prose rule: use "Milestone" not "Sprint"; avoid "critical", "robust", "comprehensive", "elegant". No `ADR-NNNN` strings in agent-facing tool/field descriptions (guard `tests/mcp/core/test_no_adr_leak.py`); cite ADRs only in module docstrings/comments.
- Conventional-commit messages, imperative ≤72-char subject, ending with the trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: `stack_frames` MI parser

**Files:**
- Modify: `src/kdive/providers/shared/debug_common/mi_protocol.py`
- Test: `tests/providers/local_libvirt/test_debug_gdbmi.py`

**Interfaces:**
- Consumes: `MiRecord`, `result_payload_dict`, `_dict_rows` (existing in `mi_protocol.py`).
- Produces: `stack_frames(records: list[MiRecord]) -> list[dict[str, Any]]` — the list of frame dicts from a `-stack-list-frames` result. Empty list when `stack` is missing, not a list, or empty (caller distinguishes nothing further; empty == no usable frames).

- [ ] **Step 1: Write the failing test**

Add to `tests/providers/local_libvirt/test_debug_gdbmi.py` (import `stack_frames` from `kdive.providers.shared.debug_common.gdbmi.mi_protocol` alongside the existing `evaluate_value` import):

```python
def test_stack_frames_extracts_frame_rows() -> None:
    records = [
        MiRecord(
            type="result",
            message="done",
            payload={
                "stack": [
                    {"frame": {"level": "0", "func": "panic", "addr": "0xffffffff81000000",
                               "file": "kernel/panic.c", "line": "42"}},
                    {"frame": {"level": "1", "func": "do_exit"}},
                ]
            },
        )
    ]
    rows = stack_frames(records)
    assert [row.get("func") for row in rows] == ["panic", "do_exit"]


def test_stack_frames_empty_for_missing_or_non_list_stack() -> None:
    assert stack_frames([MiRecord(type="result", message="done", payload={})]) == []
    assert stack_frames([MiRecord(type="result", message="done", payload={"stack": "oops"})]) == []
    assert stack_frames([MiRecord(type="result", message="done", payload={"stack": []})]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_debug_gdbmi.py::test_stack_frames_extracts_frame_rows -q`
Expected: FAIL (`ImportError` / `cannot import name 'stack_frames'`).

- [ ] **Step 3: Write minimal implementation**

In `src/kdive/providers/shared/debug_common/mi_protocol.py`, add after `breakpoint_rows` (uses the same `{"frame": {...}}` row shape as `breakpoint_rows` uses `{"bkpt": {...}}`):

```python
def stack_frames(records: list[MiRecord]) -> list[dict[str, Any]]:
    """The frame dicts from a ``-stack-list-frames`` result (``stack=[frame={...},...]``)."""
    rows: list[dict[str, Any]] = []
    for row in _dict_rows(result_payload_dict(records).get("stack")):
        entry = row.get("frame")
        if isinstance(entry, dict):
            rows.append(entry)
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_debug_gdbmi.py -k stack_frames -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/providers/shared/debug_common/mi_protocol.py tests/providers/local_libvirt/test_debug_gdbmi.py
git commit -m "feat(920): add stack_frames MI parser"  # + Co-Authored-By trailer
```

---

### Task 2: `GdbBacktrace` port model (model only — NOT the Protocol methods yet)

**Files:**
- Modify: `src/kdive/providers/ports/debug.py`
- Test: covered transitively by Task 3 (a model has no behavior to unit-test alone).

**Ordering rationale (read this):** `DebugCapabilities.engine` is typed as the `GdbMiEngine`
**Protocol** (`src/kdive/providers/core/runtime.py:62`), and both the shared concrete engine
(local-libvirt composition) and `FaultInjectDebugEngine`
(`src/kdive/providers/fault_inject/composition.py:117`) are assigned to it. `ty` checks Protocol
conformance at those assignment sites. So if the two new methods are added to the Protocol
*before* every concrete implementer has them, `ty` goes red. Therefore: define only the
`GdbBacktrace` model here; implement the concrete engines (Task 3, Task 4); add the methods to
the Protocol **last** (Task 5), once every implementer already conforms.

**Interfaces:**
- Consumes: `GdbFrame`, `ProviderModel` (existing in `debug.py`).
- Produces: `GdbBacktrace(ProviderModel)` with `frames: list[GdbFrame]` and `truncated: bool`.

- [ ] **Step 1: Add the model**

In `src/kdive/providers/ports/debug.py`, after the `GdbStopRecord` class, add:

```python
class GdbBacktrace(ProviderModel):
    """A bounded, parsed gdb/MI stack backtrace."""

    frames: list[GdbFrame]
    truncated: bool = False
```

- [ ] **Step 2: Lint, type**

Run: `just lint && just type`
Expected: PASS (a new model only; no Protocol change yet, so no implementer is forced).

- [ ] **Step 3: Commit**

```bash
git add src/kdive/providers/ports/debug.py
git commit -m "feat(920): add GdbBacktrace port model"  # + trailer
```

---

### Task 3: `GdbMiEngine.backtrace` + `read_frame` implementation

**Files:**
- Modify: `src/kdive/providers/shared/debug_common/gdbmi.py`
- Test: `tests/providers/local_libvirt/test_debug_gdbmi.py`

**Interfaces:**
- Consumes: `stack_frames` (Task 1), `GdbBacktrace` (Task 2), existing `execute_mi_command`, `_frame_from`, `_redactor`, `_config_error`, `GdbFrame`, `CategorizedError`, `ErrorCategory`.
- Produces: concrete `backtrace(...) -> GdbBacktrace` and `read_frame(...) -> GdbFrame` on the shared concrete `GdbMiEngine` class; module constant `MAX_BACKTRACE_FRAMES = 64`.

**Ordering note:** the `GdbMiEngine` Protocol does **not** declare these methods yet (that lands
in Task 5). Adding them to the concrete class here is safe — a concrete class may carry methods
the Protocol doesn't list, and `ty` stays green because no Protocol-typed assignment requires
them yet. The engine tests call the concrete class directly (`_engine().backtrace(...)`), not
through the Protocol.

**Notes for the implementer:**
- `_frame_from(payload: dict) -> GdbFrame` already exists (lenient: maps each field with isinstance/`mi_int` guards). Reuse it.
- `execute_mi_command` raises `CategorizedError(DEBUG_ATTACH_FAILURE, details={"command":..., "payload": redact(result.payload)})` on a gdb `^error`. The error payload for the running case is `{"msg": "Cannot execute this command while the target is running."}`; `running` survives redaction (no secret in it).
- Redact each frame with `GdbFrame.model_validate(self._redactor().redact_value(frame.model_dump(mode="json")))`, matching `redact_stop`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/providers/local_libvirt/test_debug_gdbmi.py` a new section. Import `GdbBacktrace` from `kdive.providers.ports.debug` and `MAX_BACKTRACE_FRAMES` is already imported pattern — add `from kdive.providers.shared.debug_common.gdbmi import ... ` updates as needed (the module already exports via `__all__`; access `gdbmi.MAX_BACKTRACE_FRAMES`).

```python
def _stack_controller(frames: list[dict[str, object]], command: str = "-stack-list-frames") \
        -> _FakeMiController:
    return _FakeMiController(
        responses={command: [
            {"type": "result", "message": "done",
             "payload": {"stack": [{"frame": f} for f in frames]}}
        ]}
    )


def test_backtrace_returns_structured_frames(tmp_path: Path) -> None:
    controller = _stack_controller([
        {"level": "0", "func": "panic", "addr": "0xffffffff81000000",
         "file": "kernel/panic.c", "line": "42"},
        {"level": "1", "func": "do_exit", "addr": "0xffffffff81001000"},
    ])
    bt = _engine().backtrace(_attachment(controller, tmp_path), max_frames=64)
    assert bt.truncated is False
    assert [f.level for f in bt.frames] == [0, 1]
    assert bt.frames[0].func == "panic"
    assert bt.frames[0].file == "kernel/panic.c"
    assert bt.frames[0].line == 42
    assert "-stack-list-frames" in controller.written


def test_backtrace_truncates_to_max_frames(tmp_path: Path) -> None:
    frames = [{"level": str(i), "func": f"f{i}"} for i in range(5)]
    controller = _stack_controller(frames)
    bt = _engine().backtrace(_attachment(controller, tmp_path), max_frames=3)
    assert bt.truncated is True
    assert [f.level for f in bt.frames] == [0, 1, 2]


def test_backtrace_rejects_bad_max_frames_before_command(tmp_path: Path) -> None:
    controller = _FakeMiController()
    for bad in (0, gdbmi.MAX_BACKTRACE_FRAMES + 1):
        with pytest.raises(CategorizedError) as exc:
            _engine().backtrace(_attachment(controller, tmp_path), max_frames=bad)
        assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert exc.value.details["code"] == "bad_frame_count"
    assert controller.written == []


def test_backtrace_raises_no_frames_on_empty_stack(tmp_path: Path) -> None:
    controller = _stack_controller([])
    with pytest.raises(CategorizedError) as exc:
        _engine().backtrace(_attachment(controller, tmp_path), max_frames=64)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "no_frames"


def test_backtrace_raises_no_frames_on_malformed_stack(tmp_path: Path) -> None:
    controller = _FakeMiController(responses={
        "-stack-list-frames": [
            {"type": "result", "message": "done", "payload": {"stack": "garbage"}}
        ]
    })
    with pytest.raises(CategorizedError) as exc:
        _engine().backtrace(_attachment(controller, tmp_path), max_frames=64)
    assert exc.value.details["code"] == "no_frames"


def test_backtrace_classifies_running_inferior(tmp_path: Path) -> None:
    controller = _FakeMiController(responses={
        "-stack-list-frames": [
            {"type": "result", "message": "error",
             "payload": {"msg": "Cannot execute this command while the target is running."}}
        ]
    })
    with pytest.raises(CategorizedError) as exc:
        _engine().backtrace(_attachment(controller, tmp_path), max_frames=64)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "inferior_running"


def test_backtrace_passes_through_other_gdb_errors(tmp_path: Path) -> None:
    controller = _FakeMiController(responses={
        "-stack-list-frames": [
            {"type": "result", "message": "error", "payload": {"msg": "No stack."}}
        ]
    })
    with pytest.raises(CategorizedError) as exc:
        _engine().backtrace(_attachment(controller, tmp_path), max_frames=64)
    # Not reclassified: a non-running gdb error keeps the generic command-failure shape.
    assert exc.value.details["command"] == "-stack-list-frames"
    assert "code" not in exc.value.details or exc.value.details.get("code") != "inferior_running"


def test_backtrace_redacts_registered_secret_in_func(tmp_path: Path) -> None:
    secret = "topsecretfunc"  # pragma: allowlist secret - fake test value
    controller = _stack_controller([{"level": "0", "func": secret}])
    engine = _engine(Redactor(secret_values=[secret], registry=SecretRegistry()))
    bt = engine.backtrace(_attachment(controller, tmp_path), max_frames=64)
    assert bt.frames[0].func is not None
    assert secret not in bt.frames[0].func


def test_read_frame_returns_single_frame(tmp_path: Path) -> None:
    controller = _stack_controller(
        [{"level": "2", "func": "schedule", "addr": "0xffffffff8100a000"}],
        command="-stack-list-frames 2 2",
    )
    frame = _engine().read_frame(_attachment(controller, tmp_path), level=2)
    assert frame.level == 2
    assert frame.func == "schedule"
    assert "-stack-list-frames 2 2" in controller.written


def test_read_frame_reaches_past_backtrace_cap(tmp_path: Path) -> None:
    controller = _stack_controller(
        [{"level": "70", "func": "deep"}], command="-stack-list-frames 70 70"
    )
    frame = _engine().read_frame(_attachment(controller, tmp_path), level=70)
    assert frame.level == 70
    assert "-stack-list-frames 70 70" in controller.written


def test_read_frame_rejects_negative_level_before_command(tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().read_frame(_attachment(controller, tmp_path), level=-1)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_frame_level"
    assert controller.written == []


def test_read_frame_raises_no_frame_at_level(tmp_path: Path) -> None:
    controller = _stack_controller([], command="-stack-list-frames 9 9")
    with pytest.raises(CategorizedError) as exc:
        _engine().read_frame(_attachment(controller, tmp_path), level=9)
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "no_frame_at_level"
    assert exc.value.details["level"] == 9


def test_read_frame_classifies_running_inferior(tmp_path: Path) -> None:
    controller = _FakeMiController(responses={
        "-stack-list-frames 0 0": [
            {"type": "result", "message": "error",
             "payload": {"msg": "Selected thread is running."}}
        ]
    })
    with pytest.raises(CategorizedError) as exc:
        _engine().read_frame(_attachment(controller, tmp_path), level=0)
    assert exc.value.details["code"] == "inferior_running"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_debug_gdbmi.py -k "backtrace or read_frame" -q`
Expected: FAIL (`AttributeError: 'GdbMiEngine' object has no attribute 'backtrace'`).

- [ ] **Step 3: Write the implementation**

In `src/kdive/providers/shared/debug_common/gdbmi.py`:

1. Add the import of `GdbBacktrace` to the existing `from kdive.providers.ports.debug import (...)` block.
2. Add module constant near `MAX_MEMORY_READ_BYTES`:

```python
MAX_BACKTRACE_FRAMES = 64
# gdb errors with a message containing this token when a stack command hits a running target.
_RUNNING_RE = re.compile(r"running", re.IGNORECASE)
```

3. Add `stack_frames` to the `from kdive.providers.shared.debug_common.gdbmi.mi_protocol import (...)` block.
4. Add the methods (place after `resolve_symbol`, before the interactive-execution section):

```python
    # --- stack walking (ADR-0275) ---------------------------------------------------------

    def backtrace(
        self, attachment: GdbMiAttachment, *, max_frames: int = MAX_BACKTRACE_FRAMES
    ) -> GdbBacktrace:
        """Walk the stopped inferior's stack, bounded to ``max_frames`` (ADR-0275)."""
        if not isinstance(max_frames, int) or max_frames < 1 or max_frames > MAX_BACKTRACE_FRAMES:
            raise _config_error(
                f"max_frames must be between 1 and {MAX_BACKTRACE_FRAMES}",
                code="bad_frame_count",
                details={"max_frames": max_frames},
            )
        rows = stack_frames(self._stack_command(attachment, "-stack-list-frames"))
        if not rows:
            raise CategorizedError(
                "gdb/MI returned no stack frames",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "no_frames"},
            )
        parsed = [self._frame_from(row) for row in rows]
        truncated = len(parsed) > max_frames
        frames = [self._redact_frame(frame) for frame in parsed[:max_frames]]
        return GdbBacktrace(frames=frames, truncated=truncated)

    def read_frame(self, attachment: GdbMiAttachment, *, level: int) -> GdbFrame:
        """Inspect one selected stack frame by ``level`` (ADR-0275).

        ``level`` is gated only to a non-negative int; an out-of-range level is answered by gdb
        as ``no_frame_at_level``, not a config error, so ``read_frame`` can reach a frame past the
        ``backtrace`` response cap (a deep kernel stack).
        """
        if not isinstance(level, int) or level < 0:
            raise _config_error(
                f"frame level must be a non-negative integer, got {level!r}",
                code="bad_frame_level",
                details={"level": level},
            )
        rows = stack_frames(self._stack_command(attachment, f"-stack-list-frames {level} {level}"))
        if not rows:
            raise CategorizedError(
                "gdb/MI returned no frame at the requested level",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "no_frame_at_level", "level": level},
            )
        return self._redact_frame(self._frame_from(rows[0]))

    def _stack_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]:
        """Run a stack MI command, reclassifying a running-target gdb error to ``inferior_running``."""
        try:
            return self.execute_mi_command(attachment, command)
        except CategorizedError as exc:
            if exc.category is ErrorCategory.DEBUG_ATTACH_FAILURE:
                payload = exc.details.get("payload")
                msg = payload.get("msg") if isinstance(payload, dict) else None
                if isinstance(msg, str) and _RUNNING_RE.search(msg):
                    raise CategorizedError(
                        "gdb/MI cannot walk the stack while the inferior is running",
                        category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                        details={"code": "inferior_running", "command": command},
                    ) from exc
            raise

    def _redact_frame(self, frame: GdbFrame) -> GdbFrame:
        return GdbFrame.model_validate(self._redactor().redact_value(frame.model_dump(mode="json")))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_debug_gdbmi.py -k "backtrace or read_frame" -q`
Expected: PASS (all new tests).

- [ ] **Step 5: Lint, type, full engine test, commit**

```bash
just lint && just type
uv run python -m pytest tests/providers/local_libvirt/test_debug_gdbmi.py -q
git add src/kdive/providers/shared/debug_common/gdbmi.py tests/providers/local_libvirt/test_debug_gdbmi.py
git commit -m "feat(920): add backtrace and read_frame engine ops"  # + trailer
```

---

### Task 4: `FaultInjectDebugEngine` conformance

**Files:**
- Modify: `src/kdive/providers/fault_inject/debug/gdb.py`
- Test: `tests/providers/fault_inject/test_provider.py` (already instantiates `FaultInjectDebugEngine` — add explicit assertions in Step 1b).

**Ordering note:** the Protocol still does not declare the methods at this point (that lands in
Task 5). Adding the concrete methods to `FaultInjectDebugEngine` here is additive and `ty` stays
green. After this task BOTH Protocol implementers (shared concrete + fault-inject) carry the
methods, which is the precondition Task 5 needs before it adds the methods to the Protocol.

**Interfaces:**
- Consumes: `GdbFrame`, `GdbBacktrace` (add to the existing `from kdive.providers.ports.debug import (...)`).
- Produces: `backtrace(self, attachment, *, max_frames=64) -> GdbBacktrace` and `read_frame(self, attachment, *, level) -> GdbFrame` on `FaultInjectDebugEngine`.

- [ ] **Step 1: Add the methods**

In `src/kdive/providers/fault_inject/debug/gdb.py`, add `GdbBacktrace` to the port import, then add to `FaultInjectDebugEngine` (after `interrupt`):

```python
    def backtrace(
        self, attachment: GdbMiAttachment, *, max_frames: int = 64
    ) -> GdbBacktrace:
        del attachment, max_frames
        return GdbBacktrace(
            frames=[
                GdbFrame(level=0, func="panic", addr="0xffffffff81000000",
                         file="kernel/panic.c", line=1),
                GdbFrame(level=1, func="do_exit", addr="0xffffffff81001000"),
            ],
            truncated=False,
        )

    def read_frame(self, attachment: GdbMiAttachment, *, level: int) -> GdbFrame:
        del attachment
        return GdbFrame(level=level, func="panic", addr="0xffffffff81000000",
                        file="kernel/panic.c", line=1)
```

Also add `GdbFrame` to the import if not present (the module currently imports `GdbBreakpointRef`, `GdbMiAttachment`, `GdbStopRecord`).

- [ ] **Step 1b: Add explicit assertions**

In `tests/providers/fault_inject/test_provider.py`, near the existing `FaultInjectDebugEngine()`
exercises, add a synthetic-frame test:

```python
def test_fault_inject_engine_backtrace_and_read_frame() -> None:
    from pathlib import Path

    from kdive.providers.ports.debug import GdbMiAttachment
    from kdive.providers.fault_inject.debug.gdb import _SyntheticGdbController

    engine = FaultInjectDebugEngine()
    attachment = GdbMiAttachment(
        controller=_SyntheticGdbController(), rsp_host="127.0.0.1", rsp_port=1234,
        transcript_path=Path("/tmp/fi-transcript.jsonl"),
    )
    bt = engine.backtrace(attachment, max_frames=64)
    assert bt.truncated is False
    assert [f.level for f in bt.frames] == [0, 1]
    assert engine.read_frame(attachment, level=3).level == 3
```

(Adjust imports to match the file's existing style; reuse any attachment helper already present.)

- [ ] **Step 2: Type-check + tests**

Run: `just lint && just type && uv run python -m pytest tests/providers/fault_inject/ -q`
Expected: PASS. `ty` is green because the Protocol does not yet require the methods (added in
Task 5); the concrete class simply carries them.

- [ ] **Step 3: Commit**

```bash
git add src/kdive/providers/fault_inject/debug/gdb.py tests/providers/fault_inject/test_provider.py
git commit -m "feat(920): add synthetic backtrace/read_frame to fault-inject engine"  # + trailer
```

---

### Task 5: declare Protocol methods, then add op factories + registrations

**Files:**
- Modify: `src/kdive/providers/ports/debug.py` (Protocol methods — Step 0)
- Modify: `src/kdive/mcp/tools/debug/ops.py`
- Test: `tests/mcp/debug/test_debug_ops.py`

**Interfaces:**
- Consumes: `run_engine_op`, `_EngineOp`, `ToolResponse`, `GdbMiEngine` (the port Protocol), `GdbMiAttachment`, `_docmeta`, `JsonValue`, the concrete engine ops from Task 3/4.
- Produces: the `GdbMiEngine` **Protocol** now declares `backtrace`/`read_frame`; `_backtrace_op(session_id, max_frames) -> _EngineOp`, `_read_frame_op(session_id, level) -> _EngineOp`; tools `debug.backtrace`/`debug.read_frame` added to `_register_debug_ops`; the `_op_for` test map gets `"backtrace"`/`"read_frame"` entries.

- [ ] **Step 0: Add the two methods to the `GdbMiEngine` Protocol**

This is now safe — both concrete implementers (Task 3 shared engine, Task 4 fault-inject) already
carry the methods, so adding them to the Protocol keeps `ty` green at every Protocol-typed
assignment site. In `src/kdive/providers/ports/debug.py`, in the `GdbMiEngine` Protocol, add:

```python
    def backtrace(self, attachment: GdbMiAttachment, *, max_frames: int) -> GdbBacktrace:
        """Walk the stopped inferior's stack through gdb/MI, bounded to ``max_frames``.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` / ``bad_frame_count`` for an out-of-range
                ``max_frames`` (raised before any MI command); ``DEBUG_ATTACH_FAILURE`` /
                ``inferior_running`` when the target is running, ``no_frames`` when gdb returns
                no usable frame data, or for other gdb/MI command failures;
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def read_frame(self, attachment: GdbMiAttachment, *, level: int) -> GdbFrame:
        """Inspect one selected stack frame by ``level`` through gdb/MI.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` / ``bad_frame_level`` for a negative or
                non-integer ``level`` (raised before any MI command); ``DEBUG_ATTACH_FAILURE`` /
                ``inferior_running`` when the target is running, ``no_frame_at_level`` when no
                frame exists at ``level``, or for other gdb/MI command failures;
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...
```

Run `just type` — expected PASS (every implementer already conforms).

- [ ] **Step 1: Write the failing handler tests**

Add to `tests/mcp/debug/test_debug_ops.py`. First extend `_op_for`'s factory map with:
```python
        "backtrace": debug_ops._backtrace_op,
        "read_frame": debug_ops._read_frame_op,
```
Then add tests (a `live` session is seeded by the existing `_seed_live_session`):

```python
def test_backtrace_returns_walked(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController({
                "-stack-list-frames": [
                    {"type": "result", "message": "done", "payload": {"stack": [
                        {"frame": {"level": "0", "func": "panic", "addr": "0xffffffff81000000",
                                   "file": "kernel/panic.c", "line": "42"}},
                        {"frame": {"level": "1", "func": "do_exit"}},
                    ]}}
                ]
            })
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool, _ctx(), session_id, runtime,
                _op_for("backtrace", runtime, session_id, max_frames=64),
            )
        assert resp.status == "walked"
        assert resp.data["frame_count"] == 2
        assert resp.data["truncated"] is False
        assert resp.data["frames"][0]["func"] == "panic"
        assert resp.data["frames"][0]["line"] == 42
        assert "debug.read_frame" in resp.suggested_next_actions

    asyncio.run(_run())


def test_backtrace_running_inferior_is_categorized(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController({
                "-stack-list-frames": [
                    {"type": "result", "message": "error",
                     "payload": {"msg": "Cannot execute this command while the target is running."}}
                ]
            })
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool, _ctx(), session_id, runtime,
                _op_for("backtrace", runtime, session_id, max_frames=64),
            )
        assert resp.status == "error"
        assert resp.error_category == "debug_attach_failure"
        assert resp.data["code"] == "inferior_running"

    asyncio.run(_run())


def test_read_frame_returns_read(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController({
                "-stack-list-frames 2 2": [
                    {"type": "result", "message": "done", "payload": {"stack": [
                        {"frame": {"level": "2", "func": "schedule", "addr": "0xffffffff8100a000"}}
                    ]}}
                ]
            })
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool, _ctx(), session_id, runtime,
                _op_for("read_frame", runtime, session_id, level=2),
            )
        assert resp.status == "read"
        assert resp.data["level"] == 2
        assert resp.data["frame"]["func"] == "schedule"

    asyncio.run(_run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/debug/test_debug_ops.py -k "backtrace or read_frame" -q`
Expected: FAIL (`AttributeError: module ... has no attribute '_backtrace_op'`).

- [ ] **Step 3: Write the op factories**

In `src/kdive/mcp/tools/debug/ops.py`, after `_interrupt_op`, add the following. Note: if `ty`
rejects assigning `frame.model_dump(...)` (`dict[str, Any]`) into `list[JsonValue]`, wrap the
comprehension in `cast("list[JsonValue]", [...])` (import `cast` from `typing`) — the dumped
frame is a flat dict of `str`/`int`/`None` and is a valid `JsonValue` at runtime; the cast just
satisfies the checker. Confirm against how neighboring handlers return nested structures.

```python
def _backtrace_op(session_id: str, max_frames: int) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        result = engine.backtrace(attachment, max_frames=max_frames)
        frames: list[JsonValue] = [
            frame.model_dump(mode="json", exclude_none=True) for frame in result.frames
        ]
        return ToolResponse.success(
            session_id,
            "walked",
            suggested_next_actions=["debug.read_frame", "debug.read_registers"],
            data={"frame_count": len(frames), "truncated": result.truncated, "frames": frames},
        )

    return op


def _read_frame_op(session_id: str, level: int) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        frame = engine.read_frame(attachment, level=level)
        return ToolResponse.success(
            session_id,
            "read",
            suggested_next_actions=["debug.read_registers", "debug.read_memory"],
            data={"level": level, "frame": frame.model_dump(mode="json", exclude_none=True)},
        )

    return op
```

- [ ] **Step 4: Register the two tools**

In `_register_debug_ops`, append:
```python
    _register_debug_backtrace(app, pool, runtime)
    _register_debug_read_frame(app, pool, runtime)
```
Update its docstring "eight gdb-MI `debug.*` tools" → "ten gdb-MI `debug.*` tools". Then add the two registrar functions (mirroring `_register_debug_read_registers`, both `read_only`, `meta=_gdbmi_maturity()`):

```python
def _register_debug_backtrace(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(name="debug.backtrace", annotations=_docmeta.read_only(), meta=_gdbmi_maturity())
    async def debug_backtrace(
        session_id: Annotated[
            str, Field(description="The live DebugSession to walk the stopped stack on.")
        ],
        max_frames: Annotated[
            int,
            Field(description="Maximum frames to return (1-64); the backtrace is truncated past it."),
        ] = 64,
    ) -> ToolResponse:
        """Walk the stopped kernel's stack on a live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool, current_context(), session_id, runtime,
            _backtrace_op(session_id, max_frames),
        )


def _register_debug_read_frame(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(name="debug.read_frame", annotations=_docmeta.read_only(), meta=_gdbmi_maturity())
    async def debug_read_frame(
        session_id: Annotated[
            str, Field(description="The live DebugSession to inspect a frame on.")
        ],
        level: Annotated[
            int,
            Field(description="Stack frame index to inspect (0 is the innermost frame)."),
        ],
    ) -> ToolResponse:
        """Inspect one selected stack frame on a live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool, current_context(), session_id, runtime,
            _read_frame_op(session_id, level),
        )
```

Also update the module docstring's tool list (line ~1) to include `.backtrace`/`.read_frame`, and extend the `_gdbmi_maturity` docstring to note backtrace/read_frame ride the same proven attach transport (ADR-0275) and are omitted from the live-proof set until a live exercise lands (mirroring the `resolve_symbol` note).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/debug/test_debug_ops.py -k "backtrace or read_frame" -q`
Expected: PASS.

- [ ] **Step 6: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/providers/ports/debug.py src/kdive/mcp/tools/debug/ops.py tests/mcp/debug/test_debug_ops.py
git commit -m "feat(920): declare Protocol methods and add backtrace/read_frame tools"  # + trailer
```

---

### Task 6: Exposure RBAC + search vocabulary + behavior-test map

**Files:**
- Modify: `src/kdive/mcp/exposure.py`
- Modify: `src/kdive/mcp/tool_index.py`
- Modify: `tests/mcp/core/test_tool_docs.py`

**Interfaces:**
- Consumes: `_CONTRIBUTOR`, `_TOOL_SCOPES` (exposure), `_TOOL_KEYWORDS` map (tool_index), `_BEHAVIOR_TESTS_BY_TOOL` (test_tool_docs).
- Produces: the two tools registered in each map so the completeness/coverage guard tests pass.

- [ ] **Step 1: Add RBAC scopes**

In `src/kdive/mcp/exposure.py` `_TOOL_SCOPES`, alongside the other `debug.*` `_CONTRIBUTOR` entries (~line 121-132):
```python
    "debug.backtrace": _CONTRIBUTOR,
    "debug.read_frame": _CONTRIBUTOR,
```

- [ ] **Step 2: Add search vocabulary**

In `src/kdive/mcp/tool_index.py` (~line 116, the debug plane block):
```python
    "debug.backtrace": frozenset({"backtrace", "stack", "frames", "call", "trace", "unwind", "debug"}),
    "debug.read_frame": frozenset({"frame", "stack", "inspect", "select", "backtrace", "debug"}),
```

- [ ] **Step 3: Add behavior-test map entries**

In `tests/mcp/core/test_tool_docs.py` `_BEHAVIOR_TESTS_BY_TOOL` (alphabetical among the `debug.*` keys):
```python
    "debug.backtrace": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.read_frame": ("tests/mcp/debug/test_debug_ops.py",),
```

- [ ] **Step 4: Run the guard tests**

Run: `uv run python -m pytest tests/mcp/core/test_tool_docs.py tests/mcp/core/test_exposure.py tests/mcp/tools/test_gateway_search.py -q`
Expected: PASS (completeness guard `CLASSIFIED|PUBLIC == registry`, behavior-test coverage guard, search index guard all green).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/mcp/exposure.py src/kdive/mcp/tool_index.py tests/mcp/core/test_tool_docs.py
git commit -m "feat(920): wire backtrace/read_frame into exposure, search, doc guards"  # + trailer
```

---

### Task 7: Regenerate the tool reference + full-suite gate

**Files:**
- Modify (generated): `docs/guide/reference/debug.md`, `docs/guide/reference/index.md` (whatever `just docs` regenerates).

- [ ] **Step 1: Regenerate the reference**

Run: `just docs`
This rewrites the agent-facing tool reference from the live registry.

- [ ] **Step 2: Verify the generated-doc gate is green**

Run: `just docs-check`
Expected: PASS (committed reference matches a fresh generation).

- [ ] **Step 3: Run the full local suite**

Run: `just lint && just type && just test`
Expected: all green (architecture, exposure, doc-generation, and no-ADR-leak guards included). The new tool descriptions must contain no `ADR-NNNN` string (guard `test_no_adr_leak`).

- [ ] **Step 4: Commit**

```bash
git add docs/guide/reference/
git commit -m "docs(920): regenerate tool reference for backtrace/read_frame"  # + trailer
```

---

## Self-Review

**Spec coverage:** backtrace structured frames (Task 3/5), single-frame inspect (Task 3/5), running-inferior + no_frames + no_frame_at_level categorized failures (Task 3), truncation (Task 3), malformed MI (Task 3), bounded+redacted (Task 3), read_frame reaches past cap (Task 3), RBAC/search/doc wiring (Task 6/7) — all covered.

**Placeholder scan:** every code step shows full code; no TBD/TODO.

**Type consistency:** `GdbBacktrace{frames, truncated}` defined in Task 2, consumed in Task 3 (`.frames`, `.truncated`) and Task 5 (`result.frames`, `result.truncated`). `backtrace(*, max_frames)`/`read_frame(*, level)` signatures identical across Task 2 Protocol, Task 3 impl, Task 4 fault-inject, Task 5 ops. `data["code"]` values (`bad_frame_count`, `bad_frame_level`, `no_frames`, `no_frame_at_level`, `inferior_running`) consistent between spec, engine impl, and tests.
