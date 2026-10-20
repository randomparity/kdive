# debug.read_frame arguments and locals (#3243)

Decision: [ADR-0781](../../adr/0781-read-frame-variables.md).

## Problem

`debug.read_frame` answers only the selected frame's location. Its arguments and locals are
not returned, so the #2813 `debug.read_frame` functional cells fail after their location proof.

## Design

Engine (`gdbmi/commands/stack.py`, shared by local- and remote-libvirt): after the existing
`-stack-list-frames L L` (unchanged validation and `no_frame_at_level`), `-thread-list-ids`
yields `current-thread-id` (absent or non-numeric: `DEBUG_ATTACH_FAILURE` /
`no_current_thread`), then `-stack-list-variables --thread T --frame L --simple-values` runs
through `_stack_command` (so `inferior_running` and no-frame errors map as today). A missing or
non-list `variables` raises `DEBUG_ATTACH_FAILURE` / `no_frame_variables`; an empty list is
an honest empty answer. Rows with a string `name` become `GdbVariable(name, type, value)`;
`arg == "1"` goes to `args`, the rest to `locals`, in gdb's order.

Bounds: the whole `GdbFrameDetail` is redacted first, then cut to 64 variables (arguments first)
with each `type` and `value` cut to 256 characters; `truncated` is true when anything was cut.
Redacting before cutting means a secret split by the cut is still redacted.

Port (`providers/ports/debug.py`): `GdbVariable` and `GdbFrameDetail(GdbFrame)` with `args`,
`locals`, `truncated`; `read_frame` returns `GdbFrameDetail` and documents the two new codes.
Fault-inject: `panic` frame with `args=[fmt: const char * = 0xffffffff82000000]`, no locals.

MCP (`mcp/tools/debug/operations/stack.py`): `data={"level": L, "frame": <detail without
truncated, None fields omitted>, "truncated": bool}`; status, next actions, auth and audit
unchanged. Example:

    {"level": 0, "truncated": false, "frame": {"level": 0, "func": "vfs_read",
      "addr": "0xffffffff8143f2a4", "file": "fs/read_write.c", "line": 552,
      "args": [{"name": "file", "type": "struct file *", "value": "0xffff888004c1e300"},
               {"name": "count", "type": "size_t", "value": "16384"}],
      "locals": [{"name": "ret", "type": "ssize_t", "value": "<optimized out>"}]}}

The tool docstring states the fields and bounds; the generated reference and CLI verbs are
regenerated and both toolset summaries name arguments and locals.

Live cell `read_frame_body`: at the `vfs_read` stop, frame 0's argument names equal the fixture
v6.18 signature `(file, buf, count, pos)`, `locals` names `ret`, `file` reads as a kernel-half
pointer, `buf` as a nonzero user-half pointer, `count` as a positive integer. The known-gap
assertion goes; the runbook's gap line is replaced by this comparison.

## Failure model

1. Actors and deployments: an MCP client holding contributor on a live gdbstub DebugSession;
   local- and remote-libvirt engines, x86_64 cells; ppc64le shares the code path.
2. Invariants: the published `debug.read_frame` result (additive fields; `level` and the
   location unchanged); redaction of guest-derived text; no selected-frame change after the call.
3. Accepted: an `<optimized out>` variable is reported verbatim (the live cell then fails on it,
   honestly); struct/array variables carry no value (ADR-0781); 64/256 bounds cut long frames,
   flagged by `truncated`; three MI commands per call.
4. Covered elsewhere: remote carrier #3113; ppc64le live cells #2818; interrupt/resume #3244.

## Success

- S1 `read_frame` returns the frame's gdb-reported arguments and locals split by `arg` (C1).
- S2 the answer holds at most 64 variables with each type and value at most 256 characters,
  redacted before the cut, and says whether it was cut (C2).
- S3 the four `debug.read_frame` gdbstub functional cells record `success` on a live x86_64 run
  (C3; live-gated).
- S4 the tool reference and CLI verbs match the docstring.

## Validation

- S1 focused-test: `test_debug_gdbmi.py::test_read_frame_returns_args_and_locals`; red
  `AttributeError: 'GdbFrame' object has no attribute 'args'`.
- S2 focused-test: `test_read_frame_bounds_variables`, `test_read_frame_redacts_before_cut`.
- Error codes focused-test: `test_read_frame_raises_no_current_thread`,
  `test_read_frame_raises_no_frame_variables`.
- MCP shape focused-test: `tests/mcp/debug/test_debug_ops.py::test_read_frame_returns_read`.
- S3 focused-test: `tests/integration/live_stack/test_debug_cells.py` read-frame cases; live
  proof per the plan.
- S4 task-test-not-applicable for wording; `just docs-check` and `just cli-verbs-check` gate it.
