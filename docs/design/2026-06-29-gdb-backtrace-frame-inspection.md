# Spec: typed gdb backtrace and frame inspection (#920)

- Status: Draft
- Date: 2026-06-29
- ADR: [0275](../adr/0275-gdb-backtrace-frame-inspection.md)

## Problem

The gdb-MI debug tier (ADR-0034, extended by ADR-0248) lets an agent set breakpoints,
`continue` to a stop, and read registers/memory by integer address over a live gdbstub
`DebugSession`. But when the kernel is stopped, there is no way to ask *where* it is: the
engine deliberately excluded stack walking (`providers/shared/debug_common/gdbmi.py:5-8`,
"stack walking ... remain outside this engine's contract"). An agent that wants the call
chain at a breakpoint must read the saved frame pointer and walk it by hand from raw memory,
or open a heavier `drgn`/raw-gdb path. The `*stopped` record already carries the *innermost*
frame (`GdbStopRecord.frame`), but nothing exposes frames `#1..#N`.

Issue #920 asks for typed backtrace and single-frame inspection so an agent can see the
structured call chain — frame index, function, address, file, line — without issuing raw gdb
commands or evaluating arbitrary expressions.

## Goals

- A contributor can request a backtrace from a stopped live gdbstub `DebugSession` and receive
  structured frames (`level`, `func`, `addr`, `file`, `line`) with textual fields redacted and
  the list bounded to a fixed cap with a `truncated` signal.
- A contributor can inspect one selected frame by index and receive that single structured,
  redacted frame.
- Both tools return categorized failures, distinguished by a `data["code"]` discriminator,
  when the inferior is running or gdb returns no usable frame data (omitted or malformed).
- No raw expression evaluation is introduced; the surface stays as narrow as the rest of the
  tier.

## Non-goals

- Frame-local variables or argument **values** (an expression-evaluation surface; out of scope,
  see ADR-0275 "Considered & rejected").
- Selecting a current frame as gdb session state across calls (`-stack-select-frame`); each op
  is stateless over the shared attach.
- A free-form rendered `bt` text blob beyond the structured (already-redacted) frames.

## Design

Two read-only ops on the **shared** `GdbMiEngine`
(`providers/shared/debug_common/gdbmi.py`), so local-libvirt and remote-libvirt both gain
them, plus two MCP tools wired through the existing `run_engine_op` gate.

### Engine

- **`backtrace(attachment, *, max_frames=MAX_BACKTRACE_FRAMES) -> GdbBacktrace`.** Validate
  `max_frames` is an int in `1..MAX_BACKTRACE_FRAMES` (default 64), raising
  `CONFIGURATION_ERROR` / `code="bad_frame_count"` **before** any MI command (mirroring
  `read_memory`'s cap rejection). Issue `-stack-list-frames` (unbounded — gdb returns the full
  stack; the cap bounds the *response*, not the command, avoiding the "high level beyond depth"
  `^error` ambiguity). Parse the `stack=[frame={...},...]` payload. If gdb returns no usable
  frames (empty, missing `stack`, or non-list — "omitted or malformed frame data"), raise
  `DEBUG_ATTACH_FAILURE` / `code="no_frames"`. Otherwise measure before slicing: compute
  `total = len(parsed)`, build redacted `GdbFrame`s, set `truncated = total > max_frames`, and
  return `parsed[:max_frames]`.

- **`read_frame(attachment, *, level) -> GdbFrame`.** Validate `level` is a **non-negative**
  int, raising `CONFIGURATION_ERROR` / `code="bad_frame_level"` before any MI command. The
  bound is intentionally *not* coupled to `MAX_BACKTRACE_FRAMES`: that cap bounds the backtrace
  *response* size, whereas `read_frame` selects one frame and a deep kernel stack may have a
  valid frame past 64 (`backtrace` signals this with `truncated=true`). Issue
  `-stack-list-frames level level` (inclusive single-frame window) and let gdb decide whether
  the level exists. No frame at that level (empty/missing/malformed `stack`) raises
  `DEBUG_ATTACH_FAILURE` / `code="no_frame_at_level"` (carrying `level`) — so an
  out-of-range level is a "no frame here" answer, not a `bad_frame_level` config error.
  Otherwise return the redacted single frame.

- **gdb `^error` classification.** A `_stack_command` wrapper inspects the (redacted) `^error`
  `msg` that `execute_mi_command` surfaces. A running target (`"...while the target is
  running."`, `"Selected thread is running."`) becomes `code="inferior_running"`. Real gdb
  reports an out-of-range level or an unwindable target with `^error,"No frame at level N."` /
  `"No stack."` (not an empty `^done`), so a `no (stack|frame)` match becomes the caller's
  missing-data code (`no_frames` for backtrace, `no_frame_at_level` for read_frame) — the same
  code the empty-`^done` path raises, so both gdb response shapes yield the documented code. Any
  other gdb error passes through unchanged.

- **`GdbBacktrace(ProviderModel)`** in `providers/ports/debug.py`: `frames: list[GdbFrame]`,
  `truncated: bool`. The two methods are added to the `GdbMiEngine` Protocol; the
  `FaultInjectDebugEngine` gains synthetic conforming implementations.

- **Parser.** `stack_frames(records) -> list[dict]` in `mi_protocol.py`, mirroring the existing
  `breakpoint_rows` (each `stack` row is `{"frame": {...}}`).

### MCP tools

- **`debug.backtrace(session_id, max_frames=64)`** — read-only, `contributor`. Success:
  `status="walked"`, `data={"frame_count": N, "truncated": bool, "frames": [{level, func,
  addr, file, line}, ...]}` (None fields omitted per frame),
  `suggested_next_actions=["debug.read_frame", "debug.read_registers"]`.

- **`debug.read_frame(session_id, level)`** — read-only, `contributor`. Success:
  `status="read"`, `data={"level": L, "frame": {...}}`,
  `suggested_next_actions=["debug.read_registers", "debug.read_memory"]`.

Both go through `run_engine_op`: UUID parse, project + `contributor` gate, `live`-state gate,
per-session lock, attach-once, blocking work off-loop. Engine `CategorizedError`s map to the
failure envelope (`data["code"]` preserved) by the existing `_op_failure`.

The engine grows to ten ops; the tier docstrings, `exposure.py` `_TOOL_SCOPES`
(`_CONTRIBUTOR`), `tool_index.py` search vocabulary, and the generated tool reference
(`just docs`) are updated to match.

## Acceptance criteria → verification

| Criterion | Test |
| --- | --- |
| Backtrace returns structured frames | engine: normal multi-frame `-stack-list-frames` → `GdbFrame` list; handler: `data["frames"]` carries level/func/addr/file/line |
| Frames carry index/func/addr/file/line | engine parse test asserts all five fields |
| Inspect one selected frame | engine: `read_frame(level=2)` → single frame; handler: `data["level"]`/`data["frame"]` |
| Categorized failure when running | engine: `^error` "...target is running." → `DEBUG_ATTACH_FAILURE` `code="inferior_running"`; handler: `error_category` + `data["code"]` |
| Categorized failure when frames omitted/malformed | engine: empty/missing/non-list `stack` → `no_frames`; out-of-range level → `no_frame_at_level` (not `bad_frame_level`) |
| `read_frame` reaches frames past the backtrace cap | engine: `read_frame(level=70)` issues `-stack-list-frames 70 70` (not rejected pre-command); negative level → `bad_frame_level` |
| Truncated backtrace | engine: stack deeper than `max_frames` → `truncated=true`, `len(frames)==max_frames` |
| Malformed MI output | engine: `^done` with garbage `stack` → `no_frames` `DEBUG_ATTACH_FAILURE` |
| Bounded + redacted | engine: registered secret in `func` masked; `max_frames` over-cap rejected pre-command |

## Rollback

Pure additive MCP surface over the shared engine; no schema, migration, persistence, RBAC
gate, or config change. Reverting the change removes the two tools and engine ops with no
state to undo.
