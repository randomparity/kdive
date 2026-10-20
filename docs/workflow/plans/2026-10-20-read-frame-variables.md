# debug.read_frame arguments and locals (#3243) — plan

Goal: implement [the spec](../specs/2026-10-20-read-frame-variables-design.md) and
[ADR-0781](../../adr/0781-read-frame-variables.md). The shared gdb/MI engine reads the selected
frame's variables with one extra pair of MI commands, the port gains two records, and the MCP op
publishes them. Python 3.14, uv, just, pydantic, FastMCP, pygdbmi.

Global Constraints: no dependency, schema or migration; `debug.backtrace`, stop records and
`GdbFrame` keep their shape; ADR-0781 flips to `Accepted (<merge date>)` in Task 1; bounds are
`MAX_FRAME_VARIABLES = 64` and `MAX_VARIABLE_TEXT = 256`.

Expected implementation size: 200–260 changed lines (M) — engine ~60, port ~25, fake ~5, op and
docstring ~15, engine/MCP/fake tests ~90, cell body and tests ~50, docs and regenerated rows ~15.

## Task 1 — engine, port, fake and MCP op return the variables

Files: `src/kdive/providers/ports/debug.py`,
`src/kdive/providers/shared/debug_common/gdbmi/core/mi_protocol.py`,
`src/kdive/providers/shared/debug_common/gdbmi/commands/stack.py`,
`src/kdive/providers/fault_inject/debug/gdb.py`, `src/kdive/mcp/tools/debug/operations/stack.py`,
tests `tests/providers/local_libvirt/test_debug_gdbmi.py`, `tests/mcp/debug/test_debug_ops.py`,
`tests/providers/fault_inject/test_provider.py`; docs `docs/guide/toolsets/debug.md`,
`src/kdive/mcp/resources/_content/toolsets-debug.md`, regenerated
`docs/guide/reference/debug.md`, `src/kdive/cli/commands/_generated_verbs.py`; ADR status.

Interfaces: produces `GdbVariable`, `GdbFrameDetail`, `GdbMiEngine.read_frame(attachment, *,
level: int) -> GdbFrameDetail`, `frame_variables(records) -> list[dict[str, Any]] | None`.
Consumes existing `GdbMiStackCommands._stack_command`, `_frame_from`, `_redactor`,
`execute_mi_command`, `result_payload_dict`, `_dict_rows`, `mi_int`.

Verification:
- read variables — focused-test `test_read_frame_returns_args_and_locals`: scripted
  `-stack-list-frames 2 2`, `-thread-list-ids` (`current-thread-id` "3"), and
  `-stack-list-variables --thread 3 --frame 2 --simple-values` with rows `{name: "fd", arg: "1",
  type: "int", value: "7"}`, `{name: "b", type: "struct big"}`; asserts `args` names `["fd"]`,
  `locals` `[GdbVariable(name="b", type="struct big")]`, `truncated is False`, and the variables
  command in `controller.written`. Red: `AttributeError` on `args`.
- bounds — `test_read_frame_bounds_variables`: 70 locals and one argument whose value is
  300 × "x"; asserts 1 + 63 kept, the value is 256 long, `truncated is True`.
- redact before cut — `test_read_frame_redacts_before_cut`: an argument value
  `"A" * 250 + secret` with the secret registered; asserts `secret[:6]` not in the value.
- codes — `test_read_frame_raises_no_current_thread` (`-thread-list-ids` answers `^done` with no
  payload) and `test_read_frame_raises_no_frame_variables` (variables command answers `^done`
  with no payload): `DEBUG_ATTACH_FAILURE` with codes `no_current_thread` / `no_frame_variables`.
- existing `test_read_frame_returns_single_frame` and `test_read_frame_reaches_past_backtrace_cap`
  script the two new commands (empty `variables=[]`) and stay green.
- MCP shape — `test_read_frame_returns_read` scripts the two new commands with one argument and
  asserts `resp.data["frame"]["args"] == [{"name": "fd", "type": "int", "value": "7"}]`,
  `resp.data["frame"]["locals"] == []`, `resp.data["truncated"] is False`, no `truncated` key in
  the frame. Red: `KeyError: 'args'`.
- fake — `test_debug_engine_backtrace_and_read_frame` asserts
  `[v.name for v in engine.read_frame(attachment, level=3).args] == ["fmt"]`.
- docs — task-test-not-applicable for the summary wording; `just docs-check`,
  `just cli-verbs-check`, `just resources-docs-check` gate the generated output.

Green: `uv run pytest tests/providers/local_libvirt/test_debug_gdbmi.py -k read_frame
tests/mcp/debug/test_debug_ops.py -k read_frame tests/providers/fault_inject/test_provider.py
tests/providers/test_ports_contracts.py` → all pass.

Steps (write the tests first and see them fail):

1. Port, after `GdbFrame`:

```python
class GdbVariable(ProviderModel):
    """One frame argument or local from gdb/MI ``-stack-list-variables --simple-values``."""

    name: str
    type: str | None = None
    value: str | None = None


class GdbFrameDetail(GdbFrame):
    """A selected frame with its arguments and locals, bounded (ADR-0781)."""

    args: list[GdbVariable] = Field(default_factory=list)
    locals: list[GdbVariable] = Field(default_factory=list)
    truncated: bool = False
```

import `Field` from pydantic; change the Protocol's `read_frame` to return `GdbFrameDetail` and
add to its Raises: "``no_current_thread`` when gdb reports no current thread,
``no_frame_variables`` when gdb returns no variable list".

2. `mi_protocol.py`, after `stack_frames`:

```python
def frame_variables(records: list[MiRecord]) -> list[dict[str, Any]] | None:
    """The variable dicts from a ``-stack-list-variables`` result, or None when gdb sent none.

    gdb/MI emits ``variables=[{name,arg,type,value},...]``; ``arg="1"`` marks an argument and
    ``--simple-values`` omits ``value`` for a struct, union or array.
    """
    variables = result_payload_dict(records).get("variables")
    return _dict_rows(variables) if isinstance(variables, list) else None
```

3. `stack.py`: add `MAX_FRAME_VARIABLES = 64`, `MAX_VARIABLE_TEXT = 256`; add
`def _current_thread(self, attachment: GdbMiAttachment) -> str: ...` to `_StackHost`; import
`GdbFrameDetail`, `GdbVariable`, `frame_variables`, `result_payload_dict`. Replace the tail of
`read_frame` after `if not rows: raise no_frame` with:

```python
        thread = self._current_thread(attachment)
        records = self._stack_command(
            attachment,
            f"-stack-list-variables --thread {thread} --frame {level} --simple-values",
            missing=no_frame,
        )
        variables = frame_variables(records)
        if variables is None:
            raise CategorizedError(
                "gdb/MI returned no variables for the frame",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "no_frame_variables", "level": level},
            )
        named = [row for row in variables if isinstance(row.get("name"), str)]
        detail = GdbFrameDetail(
            **self._frame_from(rows[0]).model_dump(),
            args=[_variable(row) for row in named if row.get("arg") == "1"],
            locals=[_variable(row) for row in named if row.get("arg") != "1"],
        )
        redacted = self._redactor().redact_value(detail.model_dump(mode="json"))
        return _bounded(GdbFrameDetail.model_validate(redacted))

    def _current_thread(self: _StackHost, attachment: GdbMiAttachment) -> str:
        records = self.execute_mi_command(attachment, "-thread-list-ids")
        thread = result_payload_dict(records).get("current-thread-id")
        if not isinstance(thread, str) or not thread.isdigit():
            raise CategorizedError(
                "gdb/MI reported no current thread",
                category=ErrorCategory.DEBUG_ATTACH_FAILURE,
                details={"code": "no_current_thread"},
            )
        return thread
```

and module functions:

```python
def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _variable(row: dict[str, Any]) -> GdbVariable:
    return GdbVariable(name=row["name"], type=_text(row.get("type")), value=_text(row.get("value")))


def _cut(text: str | None) -> str | None:
    return text[:MAX_VARIABLE_TEXT] if text is not None else None


def _bounded(detail: GdbFrameDetail) -> GdbFrameDetail:
    """Keep the first 64 variables, arguments first, each type and value cut to 256 chars."""
    args = detail.args[:MAX_FRAME_VARIABLES]
    kept = [*args, *detail.locals[: MAX_FRAME_VARIABLES - len(args)]]
    cut = [v.model_copy(update={"type": _cut(v.type), "value": _cut(v.value)}) for v in kept]
    truncated = cut != [*detail.args, *detail.locals]
    return detail.model_copy(
        update={"args": cut[: len(args)], "locals": cut[len(args) :], "truncated": truncated}
    )
```

4. Fake `read_frame` returns `GdbFrameDetail(level=level, func="panic",
addr="0xffffffff81000000", file="kernel/panic.c", line=1, args=[GdbVariable(name="fmt",
type="const char *", value="0xffffffff82000000")])`.

5. MCP op data: `{"level": level, "frame": frame.model_dump(mode="json", exclude_none=True,
exclude={"truncated"}), "truncated": frame.truncated}`. Docstring, after the halt paragraph:

```text
data.frame holds the frame's location plus args and locals, each a name, gdb's type and,
for a scalar or pointer, gdb's printed value (a struct or array shows its type only). At
most 64 variables return and each type and value is cut at 256 characters; data.truncated
says whether anything was cut.
```

6. Toolset summaries (both files): "`debug.read_frame` — read one stack frame with its
arguments and locals." ADR-0781 Status `Accepted (<merge date>)`. Run `just docs`,
`just cli-verbs`, then `just docs-check`, `just cli-verbs-check`, `just resources-docs-check`,
`just lint`, `just type` → exit 0. Commit `feat(debug): return frame arguments and locals`.

## Task 2 — live cell compares the variables

Files: `tests/integration/live_stack/debug_cells.py`,
`tests/integration/live_stack/test_debug_cells.py`, `docs/operating/runbooks/live-testing.md`
(the read-frame gap bullet becomes the comparison; history entries untouched).
Interfaces: existing `Session.stop_in_vfs_read`, `Session.exposed`, `KERNEL_BASE`, `BREAK_AT`.

Verification: focused-test in `test_debug_cells.py` — (a) `test_read_frame_compares_vfs_read_args`
with frame args `file=0xffff888004c1e300`, `buf=0x55d0c0de1000 "x"`, `count=16384`, `pos=0x0` and
local `ret`: result `{"frame0": "vfs_read", "args": ["file", "buf", "count", "pos"],
"count": 16384}`; (b) parametrized failures: names out of order → "arguments are";
`buf` `<optimized out>` → "buf reads"; `buf` a kernel address → "buf is not a user pointer";
no `ret` local → "ret". Each ends the session. Replace
`test_read_frame_known_gap_fails_after_the_observable_proof`. Red: (a) fails on the old body.
Green: `uv run pytest tests/integration/live_stack/test_debug_cells.py` → all pass.

Steps: add `_VFS_READ_ARGS = ("file", "buf", "count", "pos")  # fs/read_write.c, v6.18` and

```python
def _number(value: object, name: str) -> int:
    token = str(value).split(" ", 1)[0]
    assert re.fullmatch(r"0x[0-9a-f]+|[0-9]+", token), f"vfs_read's {name} reads {value!r}"
    return int(token, 0)
```

then the body after the existing location assertion:

```python
    args = {str(v.get("name")): v.get("value") for v in frame.get("args", [])}
    assert tuple(args) == _VFS_READ_ARGS, f"vfs_read's arguments are {tuple(args)}"
    assert "ret" in [v.get("name") for v in frame.get("locals", [])], "no ret local"
    assert _number(args["file"], "file") >= KERNEL_BASE, "file is not a kernel pointer"
    assert 0 < _number(args["buf"], "buf") < KERNEL_BASE, "buf is not a user pointer"
    count = _number(args["count"], "count")
    assert count > 0, "count is not positive"
    return {"frame0": BREAK_AT, "args": list(_VFS_READ_ARGS), "count": count}
```

with the docstring "Frame 0 is ``vfs_read`` at the stop, with its parameters and ``ret``."
Run `just lint`, `just type` → exit 0. Commit `test(debug): compare read_frame variables live`.

## Task 3 — live proof (separately granted)

On the granted Fedora 44 x86_64 lab guest (rolled back to its expanded-rootfs snapshot before and
after; server `TMPDIR` on a 1200 MB quota tmpfs), at the branch head, run the four
`debug.read_frame` gdbstub functional cells (`default` and `recovery`, each `direct` and
`gateway`) through `examples/local-libvirt/demo-up.sh`, as the #3242 rerun did; expect
`success`. Clean up with `demo-down.sh --wipe --yes`; record the result under the runbook's
rerun notes.
