# gdb Write Watchpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add typed gdb write-watchpoint tools (`debug.set_watchpoint`, `debug.list_watchpoints`, `debug.clear_watchpoint`) over the shared `GdbMiEngine`, watching a bare C symbol or an explicit address for writes with a bounded size, without exposing arbitrary gdb expressions.

**Architecture:** Three read/mutating ops on the shared `GdbMiEngine` (`providers/shared/debug_common/gdbmi.py`) consumed by both local- and remote-libvirt; the watch expression `*(char(*)[N])0x<addr>` is constructed from a validated numeric address + bounded `byte_count ∈ {1,2,4,8}`, so no caller text reaches gdb. Three MCP tools wrap the ops via the existing `run_engine_op` (contributor RBAC, `live` session gate, per-session lock, off-loop).

**Tech Stack:** Python 3.14, `uv`/`ruff`/`ty`/`pytest`, FastMCP, pygdbmi (gdb/MI), pydantic.

## Global Constraints

- ADR: [0277](../../adr/0277-gdb-watchpoints.md). Spec: [2026-06-29-gdb-watchpoints](../../specs/2026-06-29-gdb-watchpoints.md).
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict whole-tree.
- Functions ≤100 lines, cyclomatic ≤8, ≤5 positional params; absolute imports only.
- Every tool returns a `ToolResponse`; failures carry the most specific `ErrorCategory` + `data["code"]`.
- Doc prose: no "critical/robust/comprehensive/elegant"; "Milestone" not "Sprint".
- Guardrails before every commit (these CI-hard-gate individually): `just lint`, `just type`, `just test` (or focused `uv run python -m pytest <path> -q`). Generated-doc gates: `just docs-check`, `just rbac-matrix` (regenerate), `just config-docs-check`.
- New tools marked `implemented` via the shared `_gdbmi_maturity()`; **not** added to `_LOCAL_PROVEN_DEBUG_TOOLS` (unit-tested only, like `resolve_symbol`).
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## File Structure

- `src/kdive/providers/ports/debug.py` — add `GdbWatchpointRef` model + 3 Protocol methods on `GdbMiEngine`.
- `src/kdive/providers/shared/debug_common/gdbmi.py` — add constants, refactor `_disassemble_start`→shared `_resolve_target`, add `set_watchpoint`/`list_watchpoints`/`clear_watchpoint` + helpers; fix the stale module docstring.
- `src/kdive/providers/fault_inject/debug/gdb.py` — synthetic set/list/clear.
- `src/kdive/mcp/tools/debug/ops.py` — 3 op closures + 3 `_register_*` + registration list + docstrings.
- `src/kdive/mcp/exposure.py` — 3 `_CONTRIBUTOR` scope entries.
- `src/kdive/mcp/tool_index.py` — 3 search-vocabulary entries.
- `tests/providers/local_libvirt/test_debug_gdbmi.py` — engine tests.
- `tests/providers/fault_inject/test_provider.py` — synthetic round-trip.
- `tests/mcp/debug/test_debug_ops.py` — op-map + tool tests.
- `tests/mcp/core/test_tool_docs.py` — 3 `_BEHAVIOR_TESTS_BY_TOOL` entries.
- Generated: `docs/guide/reference/debug.md`, `docs/guide/reference/index.md`, `docs/guide/safety-and-rbac.md` via `just docs` + `just rbac-matrix`.

---

### Task 1: Port model + Protocol methods

**Files:**
- Modify: `src/kdive/providers/ports/debug.py`

**Interfaces:**
- Produces: `GdbWatchpointRef(number: str, type: str|None, expr: str|None, addr: str|None, enabled: bool|None)`; `GdbMiEngine.set_watchpoint(attachment, *, symbol, address, byte_count) -> GdbWatchpointRef`, `.list_watchpoints(attachment) -> list[GdbWatchpointRef]`, `.clear_watchpoint(attachment, number) -> None`.

- [ ] **Step 1: Add the model** after `GdbBreakpointRef` (around line 64):

```python
class GdbWatchpointRef(ProviderModel):
    """One gdb/MI watchpoint reference."""

    number: str
    type: str | None = None
    expr: str | None = None
    addr: str | None = None
    enabled: bool | None = None
```

- [ ] **Step 2: Add Protocol methods** on `GdbMiEngine` (after `disassemble`, before the class ends):

```python
    def set_watchpoint(
        self,
        attachment: GdbMiAttachment,
        *,
        symbol: str | None,
        address: int | None,
        byte_count: int,
    ) -> GdbWatchpointRef:
        """Set a hardware **write** watchpoint on a bare symbol or explicit address.

        Exactly one of ``symbol`` / ``address`` must be given; ``byte_count`` must be one of
        ``{1, 2, 4, 8}``. The watch expression is constructed from the resolved numeric address,
        never a caller expression.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` / ``bad_byte_count`` for an unsupported size,
                ``bad_target`` when not exactly one of symbol/address is given, ``bad_address`` for
                an out-of-range address, ``bad_symbol_name`` (via ``resolve_symbol``) for a
                non-identifier name (all before any MI command); ``DEBUG_ATTACH_FAILURE`` /
                ``inferior_running`` when the target is running, ``watchpoint_unsupported`` when the
                target refuses the watchpoint at set time, ``no_watchpoint_record`` for a malformed
                ``-break-watch`` result, or for other gdb/MI command failures;
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def list_watchpoints(self, attachment: GdbMiAttachment) -> list[GdbWatchpointRef]:
        """List watchpoints (only watchpoints, not breakpoints) through gdb/MI.

        Raises:
            CategorizedError: ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures or
                ``INFRASTRUCTURE_FAILURE`` for command timeouts.
        """
        ...

    def clear_watchpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        """Clear a watchpoint by number through gdb/MI.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` / ``bad_watchpoint_id`` for a non-numeric id,
                ``DEBUG_ATTACH_FAILURE`` for gdb/MI command failures, or ``INFRASTRUCTURE_FAILURE``
                for command timeouts.
        """
        ...
```

- [ ] **Step 3: Run type check** — `just type` → PASS (Protocol additions only; engine impl lands in Task 2). If `ty` flags the local-libvirt/fault-inject engines as not satisfying the Protocol, that is expected until Tasks 2–3 land; commit this task together with Task 2 if needed. Practically: defer the commit of Task 1 into Task 2's commit so the tree is never red.

---

### Task 2: Engine ops + shared target resolver

**Files:**
- Modify: `src/kdive/providers/shared/debug_common/gdbmi.py`
- Test: `tests/providers/local_libvirt/test_debug_gdbmi.py`

**Interfaces:**
- Consumes: `GdbWatchpointRef` (Task 1); existing `resolve_symbol`, `breakpoint_rows`, `result_payload_dict`, `_RUNNING_RE`, `_BREAK_ID_RE`, `_config_error`, `execute_mi_command`, `_redactor`.
- Produces: engine methods listed in Task 1; module constants `WATCH_BYTE_SIZES`, `DEFAULT_WATCH_BYTES`.

- [ ] **Step 1: Write the failing engine tests** — append to `tests/providers/local_libvirt/test_debug_gdbmi.py` (after the disassembly section, before `# --- error mapping`):

```python
# --- watchpoints (ADR-0277) ----------------------------------------------------------------


def _watch_set_controller(command: str) -> _FakeMiController:
    return _FakeMiController(
        responses={
            command: [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"wpt": {"number": "2", "exp": "*(char(*)[8])0x1000"}},
                }
            ]
        }
    )


def test_set_watchpoint_symbol_resolves_then_watches(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-data-evaluate-expression &d_hash_shift": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"value": "0xffffffff81000000 <d_hash_shift>"},
                }
            ],
            "-break-watch *(char(*)[8])0xffffffff81000000": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"wpt": {"number": "3", "exp": "*(char(*)[8])0xffffffff81000000"}},
                }
            ],
        }
    )
    ref = _engine().set_watchpoint(
        _attachment(controller, tmp_path), symbol="d_hash_shift", address=None, byte_count=8
    )
    assert ref.number == "3"
    assert ref.expr == "*(char(*)[8])0xffffffff81000000"


def test_set_watchpoint_address_skips_symbol_resolution(tmp_path: Path) -> None:
    command = "-break-watch *(char(*)[4])0x1000"
    controller = _watch_set_controller(command)
    ref = _engine().set_watchpoint(
        _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=4
    )
    assert ref.number == "2"
    assert "-data-evaluate-expression &" not in " ".join(controller.written)
    assert command in controller.written


@pytest.mark.parametrize("bad", [0, 3, 16])
def test_set_watchpoint_rejects_bad_byte_count_before_command(bad: int, tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=bad
        )
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["code"] == "bad_byte_count"
    assert exc.value.details["supported"] == [1, 2, 4, 8]
    assert controller.written == []


@pytest.mark.parametrize(("symbol", "address"), [("d_hash_shift", 0x1000), (None, None)])
def test_set_watchpoint_rejects_bad_target(
    symbol: str | None, address: int | None, tmp_path: Path
) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=symbol, address=address, byte_count=8
        )
    assert exc.value.details["code"] == "bad_target"
    assert controller.written == []


@pytest.mark.parametrize("bad", [-1, 0x1_0000_0000_0000_0000])
def test_set_watchpoint_rejects_out_of_range_address(bad: int, tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=None, address=bad, byte_count=8
        )
    assert exc.value.details["code"] == "bad_address"
    assert controller.written == []


def test_set_watchpoint_unsupported_target_is_categorized(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-watch *(char(*)[8])0x1000": [
                {
                    "type": "result",
                    "message": "error",
                    "payload": {"msg": "Target does not support hardware watchpoints."},
                }
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=8
        )
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "watchpoint_unsupported"


def test_set_watchpoint_running_target_is_inferior_running(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-watch *(char(*)[8])0x1000": [
                {
                    "type": "result",
                    "message": "error",
                    "payload": {"msg": "Cannot insert watchpoints while the target is running."},
                }
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=8
        )
    assert exc.value.details["code"] == "inferior_running"


def test_set_watchpoint_malformed_result_is_categorized(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-watch *(char(*)[8])0x1000": [
                {"type": "result", "message": "done", "payload": {"no-wpt": {}}}
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=8
        )
    assert exc.value.category is ErrorCategory.DEBUG_ATTACH_FAILURE
    assert exc.value.details["code"] == "no_watchpoint_record"


def test_set_watchpoint_passes_through_unrelated_gdb_error(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-watch *(char(*)[8])0x1000": [
                {"type": "result", "message": "error", "payload": {"msg": "Some other failure"}}
            ]
        }
    )
    with pytest.raises(CategorizedError) as exc:
        _engine().set_watchpoint(
            _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=8
        )
    assert exc.value.details.get("code") not in {"watchpoint_unsupported", "inferior_running"}


def test_list_watchpoints_filters_watchpoint_rows(tmp_path: Path) -> None:
    controller = _FakeMiController(
        responses={
            "-break-list": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {
                        "BreakpointTable": {
                            "body": [
                                {"bkpt": {"number": "1", "type": "breakpoint", "func": "panic"}},
                                {
                                    "bkpt": {
                                        "number": "2",
                                        "type": "hw watchpoint",
                                        "what": "*(char(*)[8])0x1000",
                                        "enabled": "y",
                                    }
                                },
                            ]
                        }
                    },
                }
            ]
        }
    )
    refs = _engine().list_watchpoints(_attachment(controller, tmp_path))
    assert [r.number for r in refs] == ["2"]
    assert refs[0].expr == "*(char(*)[8])0x1000"
    assert refs[0].enabled is True


def test_clear_watchpoint_requires_numeric_id(tmp_path: Path) -> None:
    controller = _FakeMiController()
    with pytest.raises(CategorizedError) as exc:
        _engine().clear_watchpoint(_attachment(controller, tmp_path), "abc")
    assert exc.value.details["code"] == "bad_watchpoint_id"
    assert controller.written == []


def test_clear_watchpoint_deletes(tmp_path: Path) -> None:
    controller = _FakeMiController()
    _engine().clear_watchpoint(_attachment(controller, tmp_path), "2")
    assert "-break-delete 2" in controller.written


def test_set_watchpoint_redacts_registered_secret_in_expr(tmp_path: Path) -> None:
    secret = "topsecretexpr"  # pragma: allowlist secret - fake test value
    controller = _FakeMiController(
        responses={
            "-break-watch *(char(*)[8])0x1000": [
                {
                    "type": "result",
                    "message": "done",
                    "payload": {"wpt": {"number": "2", "exp": secret}},
                }
            ]
        }
    )
    engine = _engine(Redactor(secret_values=[secret], registry=SecretRegistry()))
    ref = engine.set_watchpoint(
        _attachment(controller, tmp_path), symbol=None, address=0x1000, byte_count=8
    )
    assert ref.expr is not None
    assert secret not in ref.expr
```

- [ ] **Step 2: Run tests to verify they fail** — `uv run python -m pytest tests/providers/local_libvirt/test_debug_gdbmi.py -k watchpoint -q`. Expected: FAIL (`AttributeError: 'GdbMiEngine' object has no attribute 'set_watchpoint'`).

- [ ] **Step 3: Add imports + constants** in `gdbmi.py`. Add `GdbWatchpointRef` to the `kdive.providers.ports.debug` import block. After the `MAX_INSTRUCTION_BYTES` constant block add:

```python
# x86-64 hardware data-watchpoint widths: one debug register covers one of these. A
# non-power-of-two or larger region forces gdb to chain registers or fall back to a software
# watchpoint that single-steps the inferior — unusable over a kernel gdbstub (ADR-0277).
WATCH_BYTE_SIZES = (1, 2, 4, 8)
# Default watched width: one 64-bit word (covers a kernel pointer/long/counter).
DEFAULT_WATCH_BYTES = 8
```

After `_NO_MEMORY_RE` add:

```python
# gdb's ^error when the target/stub refuses a hardware watchpoint at *set* time. Anchored to
# capability-refusal phrasing so a running-target ("...while the target is running.") or
# insert-time ("Could not insert hardware watchpoints...") message is classified elsewhere, not
# swallowed here.
_NO_WATCHPOINT_RE = re.compile(
    r"does not support\b.*watchpoint|cannot set hardware watchpoint", re.IGNORECASE
)
```

- [ ] **Step 4: Refactor `_disassemble_start` into the shared `_resolve_target`.** Rename the method `_disassemble_start` to `_resolve_target` (body unchanged) and update its one call site inside `disassemble` from `self._disassemble_start(attachment, symbol=symbol, address=address)` to `self._resolve_target(attachment, symbol=symbol, address=address)`. (Disassemble tests must stay green — same codes.)

- [ ] **Step 5: Add the engine methods** in the disassembly section (after `_redact_instruction`), a new `# --- watchpoints (ADR-0277)` block:

```python
    def set_watchpoint(
        self,
        attachment: GdbMiAttachment,
        *,
        symbol: str | None = None,
        address: int | None = None,
        byte_count: int = DEFAULT_WATCH_BYTES,
    ) -> GdbWatchpointRef:
        """Set a hardware **write** watchpoint on a symbol/address window (ADR-0277).

        Validates the size and target before any MI command, constructs the numeric write-watch
        expression ``*(char(*)[N])0x<addr>`` (no caller text), issues ``-break-watch``, and parses
        the ``wpt`` result into a redacted ref.
        """
        if not isinstance(byte_count, int) or byte_count not in WATCH_BYTE_SIZES:
            raise _config_error(
                f"byte_count must be one of {list(WATCH_BYTE_SIZES)}",
                code="bad_byte_count",
                details={"byte_count": byte_count, "supported": list(WATCH_BYTE_SIZES)},
            )
        start = self._resolve_target(attachment, symbol=symbol, address=address)
        expression = f"*(char(*)[{byte_count}])0x{start:x}"
        records = self._watchpoint_command(attachment, f"-break-watch {expression}")
        return self._watchpoint_ref(records)

    def list_watchpoints(self, attachment: GdbMiAttachment) -> list[GdbWatchpointRef]:
        """List watchpoints only (filtering out breakpoints) from ``-break-list`` (ADR-0277)."""
        return [
            self._watchpoint_ref_from(entry)
            for entry in breakpoint_rows(self.execute_mi_command(attachment, "-break-list"))
            if _is_watchpoint_row(entry)
        ]

    def clear_watchpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        """Delete a watchpoint by ``number`` via ``-break-delete`` (ADR-0277)."""
        if not _BREAK_ID_RE.match(number):
            raise _config_error(
                f"watchpoint id must be numeric, got {number!r}",
                code="bad_watchpoint_id",
                details={"number": number},
            )
        self.execute_mi_command(attachment, f"-break-delete {number}")

    def _watchpoint_command(self, attachment: GdbMiAttachment, command: str) -> list[MiRecord]:
        """Issue a watch command, classifying running-target then unsupported gdb ``^error``s.

        Running-target is checked first so a message that also names a watchpoint is not
        misclassified as ``watchpoint_unsupported``; other gdb errors pass through.
        """
        try:
            return self.execute_mi_command(attachment, command)
        except CategorizedError as exc:
            if exc.category is ErrorCategory.DEBUG_ATTACH_FAILURE:
                payload = exc.details.get("payload")
                msg = payload.get("msg") if isinstance(payload, dict) else None
                if isinstance(msg, str):
                    if _RUNNING_RE.search(msg):
                        raise CategorizedError(
                            "gdb/MI cannot set the watchpoint while the inferior is running",
                            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                            details={"code": "inferior_running", "command": command},
                        ) from exc
                    if _NO_WATCHPOINT_RE.search(msg):
                        raise CategorizedError(
                            "gdb/MI target cannot support the requested watchpoint",
                            category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                            details={"code": "watchpoint_unsupported", "command": command},
                        ) from exc
            raise

    def _watchpoint_ref(self, records: list[MiRecord]) -> GdbWatchpointRef:
        entry = result_payload_dict(records).get("wpt")
        if not isinstance(entry, dict):
            raise CategorizedError(
                "gdb/MI -break-watch returned no watchpoint record",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "no_watchpoint_record"},
            )
        return self._watchpoint_ref_from(entry)

    def _watchpoint_ref_from(self, entry: dict[str, Any]) -> GdbWatchpointRef:
        expression = entry.get("exp") if isinstance(entry.get("exp"), str) else None
        if expression is None and isinstance(entry.get("what"), str):
            expression = entry.get("what")
        enabled_raw = entry.get("enabled")
        enabled = enabled_raw == "y" if isinstance(enabled_raw, str) else None
        return GdbWatchpointRef.model_validate(
            self._redactor().redact_value(
                {
                    "number": str(entry.get("number")),
                    "type": entry.get("type") if isinstance(entry.get("type"), str) else None,
                    "expr": expression,
                    "addr": entry.get("addr") if isinstance(entry.get("addr"), str) else None,
                    "enabled": enabled,
                }
            )
        )
```

And add the module-level row predicate near `_NO_WATCHPOINT_RE`'s usage (module function, after the class or with the other helpers at file end):

```python
def _is_watchpoint_row(entry: dict[str, Any]) -> bool:
    kind = entry.get("type")
    return isinstance(kind, str) and "watchpoint" in kind.lower()
```

- [ ] **Step 6: Fix the stale module docstring.** In the module docstring change the line listing what stays out of contract to: `general expression evaluation and module loading remain outside this engine's contract.` (stack walking, disassembly, and now watchpoints are in-contract).

- [ ] **Step 7: Run the watchpoint + disassemble tests** — `uv run python -m pytest tests/providers/local_libvirt/test_debug_gdbmi.py -k "watchpoint or disassemble" -q`. Expected: PASS.

- [ ] **Step 8: Guardrails + commit** — `just lint && just type`, then:

```bash
git add src/kdive/providers/ports/debug.py src/kdive/providers/shared/debug_common/gdbmi.py tests/providers/local_libvirt/test_debug_gdbmi.py
git commit -m "feat(922): shared GdbMiEngine watchpoint set/list/clear"
```

---

### Task 3: Fault-inject synthetic engine

**Files:**
- Modify: `src/kdive/providers/fault_inject/debug/gdb.py`
- Test: `tests/providers/fault_inject/test_provider.py`

**Interfaces:**
- Consumes: `GdbWatchpointRef` (Task 1); the engine method signatures (Task 2).
- Produces: `FaultInjectDebugEngine.set_watchpoint/.list_watchpoints/.clear_watchpoint` satisfying the Protocol.

- [ ] **Step 1: Write the failing synthetic test** — append to `tests/providers/fault_inject/test_provider.py`:

```python
def test_debug_engine_watchpoints_round_trip(tmp_path: Path) -> None:
    engine = FaultInjectDebugEngine()
    attachment = fault_inject_attach_seam(
        host="127.0.0.1", port=1234, run_id="r", transcript_path=tmp_path / "t.jsonl"
    )
    ref = engine.set_watchpoint(attachment, symbol=None, address=0x1000, byte_count=8)
    listed = engine.list_watchpoints(attachment)
    assert [w.number for w in listed] == [ref.number]
    engine.clear_watchpoint(attachment, ref.number)
    assert engine.list_watchpoints(attachment) == []
```

(Confirm `FaultInjectDebugEngine` and `fault_inject_attach_seam` are already imported in this test module; if not, add them.)

- [ ] **Step 2: Run to verify it fails** — `uv run python -m pytest tests/providers/fault_inject/test_provider.py -k watchpoint -q`. Expected: FAIL (`AttributeError`).

- [ ] **Step 3: Implement** in `FaultInjectDebugEngine`. Add `GdbWatchpointRef` to the `kdive.providers.ports.debug` import; add `self._watchpoints: dict[Path, dict[str, GdbWatchpointRef]] = {}` to `__init__` (the `self._next`/`self._lock` are shared); add the methods:

```python
    def set_watchpoint(
        self,
        attachment: GdbMiAttachment,
        *,
        symbol: str | None = None,
        address: int | None = None,
        byte_count: int = 8,
    ) -> GdbWatchpointRef:
        del symbol
        with self._lock:
            number = str(self._next)
            self._next += 1
            target = address if address is not None else 0xFFFFFFFF81000000
            ref = GdbWatchpointRef(
                number=number,
                type="hw watchpoint",
                expr=f"*(char(*)[{byte_count}])0x{target:x}",
                enabled=True,
            )
            self._watchpoints.setdefault(attachment.transcript_path, {})[number] = ref
            return ref

    def list_watchpoints(self, attachment: GdbMiAttachment) -> list[GdbWatchpointRef]:
        with self._lock:
            return list(self._watchpoints.get(attachment.transcript_path, {}).values())

    def clear_watchpoint(self, attachment: GdbMiAttachment, number: str) -> None:
        with self._lock:
            bucket = self._watchpoints.get(attachment.transcript_path)
            if bucket is None:
                return
            bucket.pop(number, None)
            if not bucket:
                self._watchpoints.pop(attachment.transcript_path, None)
```

- [ ] **Step 4: Run to verify it passes** — same pytest command. Expected: PASS.

- [ ] **Step 5: Guardrails + commit** — `just lint && just type`, then:

```bash
git add src/kdive/providers/fault_inject/debug/gdb.py tests/providers/fault_inject/test_provider.py
git commit -m "feat(922): fault-inject synthetic watchpoint engine"
```

---

### Task 4: MCP tools

**Files:**
- Modify: `src/kdive/mcp/tools/debug/ops.py`
- Test: `tests/mcp/debug/test_debug_ops.py`

**Interfaces:**
- Consumes: engine methods (Task 2); existing `run_engine_op`, `_docmeta.mutating/read_only`, `_gdbmi_maturity`, `current_context`, `ToolResponse`.
- Produces: tools `debug.set_watchpoint`, `debug.list_watchpoints`, `debug.clear_watchpoint`; op factories `_set_watchpoint_op`, `_list_watchpoints_op`, `_clear_watchpoint_op`.

- [ ] **Step 1: Write the failing op-map + tool tests** — in `tests/mcp/debug/test_debug_ops.py` add three keys to `_op_for`'s factory dict (`"set_watchpoint": debug_ops._set_watchpoint_op, "list_watchpoints": debug_ops._list_watchpoints_op, "clear_watchpoint": debug_ops._clear_watchpoint_op`) and append:

```python
def test_set_watchpoint_returns_watching(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-break-watch *(char(*)[8])0x1000": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {"wpt": {"number": "2", "exp": "*(char(*)[8])0x1000"}},
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for(
                    "set_watchpoint", runtime, session_id, symbol=None, address=0x1000, byte_count=8
                ),
            )
        assert resp.status == "watching"
        assert resp.data["number"] == "2"
        assert resp.data["byte_count"] == 8
        assert "debug.continue" in resp.suggested_next_actions

    asyncio.run(_run())


def test_list_watchpoints_returns_listed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-break-list": [
                        {
                            "type": "result",
                            "message": "done",
                            "payload": {
                                "BreakpointTable": {
                                    "body": [
                                        {
                                            "bkpt": {
                                                "number": "2",
                                                "type": "hw watchpoint",
                                                "what": "*(char(*)[8])0x1000",
                                            }
                                        }
                                    ]
                                }
                            },
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool, _ctx(), session_id, runtime, _op_for("list_watchpoints", runtime, session_id)
            )
        assert resp.status == "listed"
        assert resp.data["count"] == 1

    asyncio.run(_run())


def test_clear_watchpoint_returns_cleared(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController({})
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for("clear_watchpoint", runtime, session_id, number="2"),
            )
        assert resp.status == "cleared"

    asyncio.run(_run())


def test_set_watchpoint_unsupported_is_categorized(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            session_id = await _seed_live_session(pool, state=DebugSessionState.LIVE)
            controller = _FakeMiController(
                {
                    "-break-watch *(char(*)[8])0x1000": [
                        {
                            "type": "result",
                            "message": "error",
                            "payload": {"msg": "Target does not support hardware watchpoints."},
                        }
                    ]
                }
            )
            runtime = _runtime(_CountingAttach(controller))
            resp = await run_engine_op(
                pool,
                _ctx(),
                session_id,
                runtime,
                _op_for(
                    "set_watchpoint", runtime, session_id, symbol=None, address=0x1000, byte_count=8
                ),
            )
        assert resp.status == "error"
        assert resp.error_category == "debug_attach_failure"
        assert resp.data["code"] == "watchpoint_unsupported"

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify they fail** — `uv run python -m pytest tests/mcp/debug/test_debug_ops.py -k watchpoint -q`. Expected: FAIL (`AttributeError: module ... has no attribute '_set_watchpoint_op'`).

- [ ] **Step 3: Implement the op factories** in `ops.py` (after `_disassemble_op`):

```python
def _set_watchpoint_op(
    session_id: str, symbol: str | None, address: int | None, byte_count: int
) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        ref = engine.set_watchpoint(
            attachment, symbol=symbol, address=address, byte_count=byte_count
        )
        data: dict[str, JsonValue] = {"number": ref.number, "byte_count": byte_count}
        if ref.expr is not None:
            data["expr"] = ref.expr
        return ToolResponse.success(
            session_id,
            "watching",
            suggested_next_actions=["debug.continue", "debug.list_watchpoints"],
            data=data,
        )

    return op


def _list_watchpoints_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        refs = engine.list_watchpoints(attachment)
        watchpoints: list[JsonValue] = [
            ref.model_dump(mode="json", exclude_none=True) for ref in refs
        ]
        return ToolResponse.success(
            session_id,
            "listed",
            suggested_next_actions=["debug.set_watchpoint", "debug.continue"],
            data={"count": len(watchpoints), "watchpoints": watchpoints},
        )

    return op


def _clear_watchpoint_op(session_id: str, number: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        engine.clear_watchpoint(attachment, number)
        return ToolResponse.success(
            session_id, "cleared", suggested_next_actions=["debug.list_watchpoints"]
        )

    return op
```

- [ ] **Step 4: Register the tools.** Add to `_register_debug_ops` the three calls and bump the docstring count ("eleven" → "fourteen"); add three `_register_*` functions:

```python
def _register_debug_set_watchpoint(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.set_watchpoint",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_set_watchpoint(
        session_id: Annotated[str, Field(description="The live DebugSession to set a watchpoint on.")],
        symbol: Annotated[
            str | None,
            Field(description="Bare C symbol to watch for writes (or use address)."),
        ] = None,
        address: Annotated[
            int | None,
            Field(description="Start address (integer) to watch for writes (or use symbol)."),
        ] = None,
        byte_count: Annotated[
            int,
            Field(description="Bytes to watch; one of 1, 2, 4, or 8 (one hardware watchpoint)."),
        ] = 8,
    ) -> ToolResponse:
        """Set a hardware write watchpoint on a symbol/address for a live DebugSession.

        Watchpoints are hardware (debug-register) watchpoints: the stub may accept one yet never
        trap, surfacing as a debug.continue timeout rather than an error. Requires contributor.
        """
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _set_watchpoint_op(session_id, symbol, address, byte_count),
        )


def _register_debug_list_watchpoints(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.list_watchpoints",
        annotations=_docmeta.read_only(),
        meta=_gdbmi_maturity(),
    )
    async def debug_list_watchpoints(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose watchpoints to list.")
        ],
    ) -> ToolResponse:
        """List all watchpoints on a live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool, current_context(), session_id, runtime, _list_watchpoints_op(session_id)
        )


def _register_debug_clear_watchpoint(
    app: FastMCP, pool: AsyncConnectionPool, runtime: DebugEngineRuntime | DebugRuntimeResolver
) -> None:
    @app.tool(
        name="debug.clear_watchpoint",
        annotations=_docmeta.mutating(),
        meta=_gdbmi_maturity(),
    )
    async def debug_clear_watchpoint(
        session_id: Annotated[
            str, Field(description="The live DebugSession whose watchpoint to clear.")
        ],
        number: Annotated[
            str, Field(description="Watchpoint number to clear (from debug.list_watchpoints).")
        ],
    ) -> ToolResponse:
        """Clear a watchpoint by number on a live DebugSession. Requires contributor."""
        return await run_engine_op(
            pool,
            current_context(),
            session_id,
            runtime,
            _clear_watchpoint_op(session_id, number),
        )
```

Update the module docstring's tool list (add `.set_watchpoint/.list_watchpoints/.clear_watchpoint` and ADR-0277) and the `_gdbmi_maturity` docstring (note watchpoints are unit-tested only, not in `_LOCAL_PROVEN_DEBUG_TOOLS`, like `resolve_symbol`).

- [ ] **Step 5: Run to verify it passes** — `uv run python -m pytest tests/mcp/debug/test_debug_ops.py -k watchpoint -q`. Expected: PASS.

- [ ] **Step 6: Guardrails + commit** — `just lint && just type`, then:

```bash
git add src/kdive/mcp/tools/debug/ops.py tests/mcp/debug/test_debug_ops.py
git commit -m "feat(922): register debug watchpoint MCP tools"
```

---

### Task 5: Wiring guards + generated docs

**Files:**
- Modify: `src/kdive/mcp/exposure.py`, `src/kdive/mcp/tool_index.py`, `tests/mcp/core/test_tool_docs.py`
- Generated: `docs/guide/reference/debug.md`, `docs/guide/reference/index.md`, `docs/guide/safety-and-rbac.md`

**Interfaces:**
- Consumes: registered tool names (Task 4).

- [ ] **Step 1: Exposure scope** — in `src/kdive/mcp/exposure.py` `_TOOL_SCOPES`, after `"debug.disassemble": _CONTRIBUTOR,` add:

```python
    "debug.set_watchpoint": _CONTRIBUTOR,
    "debug.list_watchpoints": _CONTRIBUTOR,
    "debug.clear_watchpoint": _CONTRIBUTOR,
```

- [ ] **Step 2: Search vocabulary** — in `src/kdive/mcp/tool_index.py`, after the `debug.disassemble` entry add:

```python
    "debug.set_watchpoint": frozenset({"watchpoint", "watch", "write", "monitor", "data", "debug"}),
    "debug.list_watchpoints": frozenset({"watchpoints", "list", "watch", "debug"}),
    "debug.clear_watchpoint": frozenset({"watchpoint", "clear", "remove", "delete", "debug"}),
```

- [ ] **Step 3: Behavior-test map** — in `tests/mcp/core/test_tool_docs.py` `_BEHAVIOR_TESTS_BY_TOOL`, add (keep alphabetical within the debug block):

```python
    "debug.set_watchpoint": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.list_watchpoints": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.clear_watchpoint": ("tests/mcp/debug/test_debug_ops.py",),
```

Do **not** add the new tools to `_LOCAL_PROVEN_DEBUG_TOOLS` (unit-tested only).

- [ ] **Step 4: Regenerate docs** — `just docs` and `just rbac-matrix` (both mutating). Then verify: `just docs-check && just config-docs-check`. Expected: in sync.

- [ ] **Step 5: Run the full guard suites** — `uv run python -m pytest tests/mcp/core/test_tool_docs.py tests/mcp/test_tool_index.py -q`. Expected: PASS (registration/scope/vocab/behavior-map/reference completeness guards all green).

- [ ] **Step 6: Guardrails + commit** — `just lint && just type`, then:

```bash
git add src/kdive/mcp/exposure.py src/kdive/mcp/tool_index.py tests/mcp/core/test_tool_docs.py docs/guide/
git commit -m "feat(922): wire watchpoint scope, vocab, docs"
```

---

### Task 6: Full-suite verification

- [ ] **Step 1: Run the whole gate** — `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, test). Expected: PASS. Fix any failure before proceeding; architecture/doc-generation tests can live outside the touched dirs.

## Self-Review

**Spec coverage:** Symbol watch (T2/T4), address watch (T2/T4), bad byte_count/target/address (T2), arbitrary-expression reject via resolve_symbol (T2), watchpoint_unsupported + inferior_running + malformed + pass-through (T2/T4), list filtering (T2/T4), clear gate+delete (T2), redaction (T2), fault-inject round-trip (T3), guards/docs (T5), full gate (T6). All eight success criteria mapped.

**Placeholder scan:** none — every code/test step shows full content.

**Type consistency:** `GdbWatchpointRef(number, type, expr, addr, enabled)` consistent across ports/engine/fault-inject/ops; `set_watchpoint(*, symbol, address, byte_count)` signature identical in Protocol, engine, fault-inject, and the `_set_watchpoint_op` call; `WATCH_BYTE_SIZES`/`DEFAULT_WATCH_BYTES` defined in Task 2 and referenced consistently.

## Execution Handoff

Tasks are tightly coupled (shared engine + ports + ops, same files, strict ordering), so per the work-issue workflow this plan is executed **inline** in the current session via superpowers:test-driven-development, committing one task at a time.
