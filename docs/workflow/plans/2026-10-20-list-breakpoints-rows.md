# debug.list_breakpoints rows (#3242) — plan

Goal: implement [the spec](../specs/2026-10-20-list-breakpoints-rows-design.md) and
[ADR-0780](../../adr/0780-list-breakpoints-rows.md). Python 3.14, uv, just; FastMCP tools.

Global Constraints: no change to the provider port, the gdb/MI command layer or the fault-inject
engine; no dependency, schema or migration; ADR-0780 flips to `Accepted (<merge date>)` in Task 1.

Expected implementation size: 90–130 changed lines (S) — one op, one docstring, two summary
lines, one cell body, three cell tests, one MCP test and the regenerated reference/verbs rows.

## Task 1 — MCP op answers rows

Files: `src/kdive/mcp/tools/debug/operations/breakpoints.py`, `tests/mcp/debug/test_debug_ops.py`,
`docs/guide/toolsets/debug.md`, `src/kdive/mcp/resources/_content/toolsets-debug.md`, regenerated
`docs/guide/reference/debug.md` and `src/kdive/cli/commands/_generated_verbs.py`.
Interfaces: consumes `GdbMiEngine.list_breakpoints(attachment) -> list[GdbBreakpointRef]`.

Verification: focused-test — `test_list_breakpoints_lists_rows`, modelled on
`test_list_watchpoints_returns_listed`: a `_FakeMiController` `-break-list` body with a `bkpt`
row (`number` 1, `type` breakpoint, `addr` 0xffffffff81000010, `func` panic) and a `bkpt` row
(`number` 2, `type` hw watchpoint, `what` `*(char(*)[8])0x1000`); asserts
`resp.data == {"count": 2, "breakpoints": [<row 1 dict>, <row 2 dict>]}`. Red: `KeyError` /
inequality on `breakpoints`. Green:
`uv run pytest tests/mcp/debug/test_debug_ops.py -k list_breakpoints_lists_rows` → 1 passed.

Steps: write the test, see it fail; make `_list_breakpoints_op`

```python
def _list_breakpoints_op(session_id: str) -> _EngineOp:
    def op(engine: GdbMiEngine, attachment: GdbMiAttachment) -> ToolResponse:
        refs = engine.list_breakpoints(attachment)
        breakpoints: list[JsonValue] = [
            ref.model_dump(mode="json", exclude_none=True) for ref in refs
        ]
        return ToolResponse.success(
            session_id,
            "listed",
            suggested_next_actions=["debug.set_breakpoint", "debug.continue"],
            data={"count": len(breakpoints), "breakpoints": breakpoints},
        )

    return op
```

importing `JsonValue` from `kdive.serialization`; set the tool docstring to

```text
List breakpoints and watchpoints on a live DebugSession. Requires contributor.

data.breakpoints has one row per gdb breakpoint-table entry: number, type (breakpoint or
a watchpoint kind) and the location gdb reports, func and addr for a breakpoint or what
(the watched expression, named expr by the watchpoint tools) for a watchpoint. data.count
is the row count. A breakpoint gdb
resolved to several locations shows addr `<MULTIPLE>` and no func.
```

and both toolset summaries to "list each breakpoint and watchpoint with its number, kind and
location". Run `just docs`, `just cli-verbs`, then `just docs-check`, `just cli-verbs-check`,
`just resources-docs-check`, `just lint`, `just type` → all exit 0. Commit
`feat(debug): list breakpoint rows from debug.list_breakpoints`.

## Task 2 — live cell proves the rows

Files: `tests/integration/live_stack/debug_cells.py`, `tests/integration/live_stack/test_debug_cells.py`,
`docs/operating/runbooks/live-testing.md` (the "Two cells fail" bullet names only `read_frame`).
Interfaces: `Session.breakpoint(session_id, location) -> str`, `Session.exposed(**args)`,
`Truth.within(name, address) -> bool`, `hex_value(value) -> int`, all existing.

Verification: focused-test — in `test_debug_cells.py`, (a) `test_list_breakpoints_matches_the_installed_rows`:
`debug.set_breakpoint` answers numbers 1 then 2; `debug.list_breakpoints` answers two rows
(`vfs_read` at `_RIP`, `vfs_write` at 0xffffffff81200810) then the `vfs_write` row; result is
`{"counts": [2, 1], ...}`; (b) a row whose `addr` is 0xffffffff81200400 raises
`AssertionError` matching "not in vfs_read"; (c) a listing still holding number 1 after the clear
raises matching "listed". Drop the `list_breakpoints` case and its count iterator from
`test_a_known_gap_fails_after_the_observable_proof`. Red: (a) fails on the known-gap assertion.
Green: `uv run pytest tests/integration/live_stack/test_debug_cells.py` → all pass.

Steps: write the tests, see (a) fail; rewrite `list_breakpoints_body` to set both breakpoints,
list, clear the first, list, end the session, then call a module helper
`_require_breakpoints(s, env, {number: func})` asserting `count == len(expected)`, the listed
numbers equal the expected keys (message "listed …"), and per row `type == "breakpoint"`,
`func` equal, and `s.truth.within(func, hex_value(addr))` (message "breakpoint N is not in F").
Return `{"counts": [2, 1], "numbers_match": True, "addr_within_kallsyms": True}`. Run
`just lint`, `just type` → exit 0. Commit `test(debug): prove list_breakpoints rows live`.

## Task 3 — live proof (separately granted)

On the granted Fedora 44 x86_64 lab guest at the branch head, run the four
`debug.list_breakpoints` gdbstub functional cells through `examples/local-libvirt/demo-up.sh`
(default and `KDIVE_WORKER_DEATH_VERIFIER=docker`) as the runbook describes; expect `success`.
Clean up with `demo-down.sh --wipe --yes` and record the result under the runbook's rerun notes.
