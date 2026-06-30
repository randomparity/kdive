# Spec: typed gdb write watchpoints for live gdbstub sessions (#922)

- Status: Draft
- Date: 2026-06-29
- ADR: [0277](../adr/0277-gdb-watchpoints.md)

## Problem

The gdb-MI debug tier (ADR-0034, extended by ADR-0248, ADR-0275, and ADR-0276) lets an agent set
breakpoints, `continue` to a stop, read registers/memory, resolve a symbol, walk the stopped
stack, and disassemble over a live gdbstub `DebugSession`. But there is no way to answer "what
code writes this address?" A data watchpoint is the standard tool: the kernel runs, and gdb
stops the moment the watched bytes change.

gdb sets a hardware data watchpoint with `-break-watch EXPR`. A raw watch expression would
reopen the arbitrary-expression surface ADR-0034 excluded ("general expression evaluation … and
watchpoints remain outside this engine's contract"). Issue #922 asks for typed, bounded
watchpoints on a bare C **symbol** or an explicit **address** for **writes**, plus list and
clear — without exposing arbitrary gdb expression execution.

## Goals

- A contributor can set a **write** watchpoint on a live gdbstub `DebugSession` around either a
  bare C **symbol** (resolved to its address via the existing gated `resolve_symbol`) or an
  explicit **address**, for a bounded **size**, and receive a structured `GdbWatchpointRef`
  (`number`, `expr`, …).
- A contributor can list watchpoints (only watchpoints, not breakpoints) and clear one by number
  through structured tool responses.
- The engine validates the size and target **before** issuing any gdb command and refuses an
  out-of-range size, a bad target, or an invalid address as a categorized configuration error.
- The implementation introduces no raw expression evaluation: the watch command is constructed
  from a validated numeric address plus a bounded size, never a caller expression.
- When the target cannot support the requested watchpoint, the response says so with a distinct
  `data["code"]`.

## Non-goals

- Read (`-r`) or access (`-a`) watchpoints. The issue asks for writes; the MI default
  (`-break-watch` with no flag) is a write watchpoint. A `kind` flag is a clean additive future
  change (see ADR-0277 "Considered & rejected").
- Deriving the watched size from the symbol's DWARF type (`&name` does not cheaply yield the
  type, the same limitation ADR-0276 noted); an explicit bounded `byte_count` is used instead.
- Any new gdb session state, schema, migration, RBAC, or destructive-op-gate change.

## Accepted behavior (not failures)

- **Data vs. code symbol / address** — `set_watchpoint(symbol="d_hash_shift")` or a data
  `address` watches those bytes for writes, which is the primary use (watch a variable). Watching
  a code address is also accepted (gdb sets it); it is the caller's choice.
- **Unaligned address** — an `address` that is not naturally aligned for the size is passed to
  gdb as given; gdb sets the hardware watchpoint it can. Alignment is the caller's responsibility
  (use an address from a symbol/frame).

## Failure modes / limitations

A kernel data watchpoint over the gdbstub is necessarily a **hardware** (debug-register)
watchpoint: a software watchpoint single-steps the inferior, which is infeasible for a running
kernel. That constraint has three consequences the engine cannot fully detect at `set` time, so
these tools are **not** added to the live-proof set until a live exercise lands (matching the
ADR-0248/0276 precedent), and the `set` result is best-effort:

- **The stub may accept the watchpoint but never trap.** QEMU's gdbstub does not reliably trap on
  hardware debug-register events — the documented reason `set_breakpoint` uses a *software*
  breakpoint and avoids `-break-insert -h` (#711, see the `set_breakpoint` comment in
  `gdbmi.py`). The same risk applies to watchpoints. When a watchpoint is set but never traps, the
  failure is **silent at `set` time**: gdb returns `^done,wpt=…`. The observable signal is the
  next `debug.continue` returning `timed_out=True` with no stop — not a `set`-time error. The
  `set_watchpoint` tool docstring names this, and `debug.continue` is a `suggested_next_action`.
- **Debug registers are scarce (4 on x86-64).** Setting more watchpoints than free debug registers
  (counting any hardware breakpoints) typically returns `^done,wpt=…` at `set` time and only fails
  at **insertion** on the next `continue` ("Could not insert hardware watchpoints: …"). So
  exhaustion, like the non-trap case, surfaces on `continue`, not `set`. `list_watchpoints` is the
  affordance for an agent to see how many watchpoints are already armed before adding more.
- **`watchpoint_unsupported` covers only the set-time refusal.** Criterion 5 detects the case where
  gdb refuses `-break-watch` up front with a watchpoint-naming `^error` (e.g. "Target does not
  support hardware watchpoints."). A stub that accepts-but-never-traps, or fails at insert time,
  is **not** a `set`-time error and surfaces as the `continue` timeout above. This honestly scopes
  the #922 acceptance criterion "clearly reports when the target cannot support the requested
  watchpoint": the engine reports the refusal it can see and routes the silent cases to the
  `continue`-timeout signal rather than claiming a guarantee it cannot keep.

## Success criteria

Each maps to an acceptance-criteria checkbox on #922 and to a test.

1. **Symbol watchpoint** — `set_watchpoint(symbol="d_hash_shift", byte_count=N)` resolves the
   symbol to its address, issues `-break-watch *(char(*)[N])0x<addr>`, and returns a
   `GdbWatchpointRef` parsed from the `wpt` result. Tool returns `status="watching"`,
   `data={number, expr, byte_count}`.
2. **Address watchpoint** — `set_watchpoint(address=0x..., byte_count=N)` skips symbol
   resolution and watches from that address (no `-data-evaluate-expression` written).
3. **Invalid bounds refused before any command** — `byte_count` ∉ `{1,2,4,8}` →
   `CONFIGURATION_ERROR` / `code="bad_byte_count"` (with `supported`), no MI command written.
   Both-or-neither of symbol/address → `CONFIGURATION_ERROR` / `code="bad_target"`. Out-of-range
   address → `CONFIGURATION_ERROR` / `code="bad_address"`. (Target validation is shared with
   `disassemble`.)
4. **Arbitrary expression rejected** — a non-identifier `symbol` →
   `CONFIGURATION_ERROR` / `code="bad_symbol_name"` (via `resolve_symbol`), raised before any
   watch command; the constructed expression is purely numeric, so no caller text reaches gdb.
5. **Set-time unsupported target reported** — when gdb answers `-break-watch` with an `^error`
   whose redacted msg names a watchpoint (e.g. "Target does not support hardware watchpoints."),
   the engine raises `DEBUG_ATTACH_FAILURE` / `code="watchpoint_unsupported"`; an unrelated gdb
   error passes through unchanged. This covers only the set-time refusal — an accept-but-never-trap
   stub or insert-time debug-register exhaustion surfaces as a `debug.continue` timeout, not a
   `set`-time error (see "Failure modes / limitations").
6. **List** — `list_watchpoints` issues `-break-list` and returns only the rows whose `type`
   names a watchpoint, each as a `GdbWatchpointRef` (`number`, `type`, `expr` from `what`,
   `addr`, `enabled`); a breakpoint row in the same table is excluded. Tool returns
   `status="listed"`, `data={count, watchpoints:[...]}`.
7. **Clear** — `clear_watchpoint(number)` gates `number` to a bare integer
   (`code="bad_watchpoint_id"`, no command written on a bad id) and issues `-break-delete
   <number>`. Tool returns `status="cleared"`.
8. **Redaction** — a registered secret appearing in a watchpoint's `expr` field is masked before
   the ref is returned.

## Design

See [ADR-0277](../adr/0277-gdb-watchpoints.md) for the decision and rejected alternatives. In
brief:

- **Port model** (`providers/ports/debug.py`): `GdbWatchpointRef(number, type, expr, addr,
  enabled)`; `set_watchpoint`/`list_watchpoints`/`clear_watchpoint` Protocol methods on
  `GdbMiEngine`.
- **Engine** (`providers/shared/debug_common/gdbmi.py`): `set_watchpoint(...)` validates
  `byte_count` against `WATCH_BYTE_SIZES`, resolves the target via a shared `_resolve_target`
  (factored out of `disassemble`'s `_disassemble_start`), constructs the numeric write-watch
  expression, and parses the redacted `wpt` ref; a `_watchpoint_command` wrapper maps a
  watchpoint-naming `^error` to `watchpoint_unsupported`. `list_watchpoints(...)` filters
  `breakpoint_rows` to watchpoint-typed rows. `clear_watchpoint(...)` gates the id and deletes.
- **Tool** (`mcp/tools/debug/ops.py`): `_set_watchpoint_op` / `_list_watchpoints_op` /
  `_clear_watchpoint_op` plus their `_register_*`, via `run_engine_op` (contributor, `live`
  session); set/clear `mutating`, list `read_only`.
- **Fault-inject** (`providers/fault_inject/debug/gdb.py`): synthetic set/list/clear tracking
  watchpoints in-memory per attachment (mirrors the breakpoint synthetic).
- **Guards/docs**: exposure scope, `tool_index` vocabulary, `_BEHAVIOR_TESTS_BY_TOOL`, and the
  regenerated `just docs` reference.

No schema, migration, RBAC, persistence, config, or destructive-op gate change. The shared-engine
docstring's "watchpoints remain outside this engine's contract" line is updated, as this ADR
lifts that exclusion in the same narrow, non-injectable way ADR-0248 lifted "no expression
evaluation".

## Testing

- **Engine** (`tests/providers/local_libvirt/test_debug_gdbmi.py`): symbol-path resolution +
  command shape, address-path (no symbol resolution written), `bad_byte_count` (parametrized
  e.g. 0, 3, 16) with no command written, `bad_target` (both/neither), `bad_address`,
  `watchpoint_unsupported` on a watchpoint-naming `^error`, pass-through of an unrelated gdb
  error, `list_watchpoints` filtering watchpoints from a realistic mixed `-break-list` table —
  a `bkpt` body row whose `type="breakpoint"` alongside one whose `type="hw watchpoint"` with a
  populated `what`, asserting only the watchpoint is returned and its `expr` maps from `what`
  (pinning the assumed watchpoint-row wire shape) — `clear` numeric-id gate + `-break-delete`
  shape, and secret redaction in the `expr` field.
- **Tool** (`tests/mcp/debug/test_debug_ops.py`): `set_watchpoint` happy path returns
  `status="watching"` with `data={number, expr, byte_count}` and the next-action pointers;
  `list_watchpoints` returns `status="listed"` with the structured payload; `clear_watchpoint`
  returns `status="cleared"`; an unsupported-target failure is surfaced with
  `error_category="debug_attach_failure"` and `data["code"]="watchpoint_unsupported"`.
- **Fault-inject** (`tests/providers/fault_inject/test_provider.py`): a set → list → clear
  round-trip over the synthetic engine.
- **Guards** (`tests/mcp/core/test_tool_docs.py`, `tests/mcp/test_tool_index.py`): the three new
  tools are registered, scoped, vocabulary-indexed, behavior-mapped, and present in the generated
  reference.
