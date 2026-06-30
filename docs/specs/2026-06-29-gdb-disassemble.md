# Spec: typed gdb disassembly for live gdbstub sessions (#921)

- Status: Draft
- Date: 2026-06-29
- ADR: [0276](../adr/0276-gdb-disassemble.md)

## Problem

The gdb-MI debug tier (ADR-0034, extended by ADR-0248 and ADR-0275) lets an agent set
breakpoints, `continue` to a stop, read registers/memory by address, resolve a symbol, and walk
the stopped stack over a live gdbstub `DebugSession`. But there is no way to see the **machine
instructions** at an address: an agent that wants to read the faulting instruction, a function
prologue, or the code a backtrace frame points at must `read_memory` the raw bytes and
disassemble them out-of-band, or open a heavier `drgn`/raw-gdb path needing credentials a
System may not have.

Issue #921 asks for typed disassembly so an agent can request a bounded instruction window
around a symbol or explicit address and receive structured, redacted output — without issuing
raw gdb commands or evaluating arbitrary expressions.

## Goals

- A contributor can request disassembly from a live gdbstub `DebugSession` around either a bare
  C **symbol** or an explicit **address** and receive structured instructions (`address`,
  `inst`, optional `func_name`, optional `offset`) with textual fields redacted and the list
  bounded to `instruction_count` with a `truncated` signal.
- The engine validates `instruction_count` against a fixed cap and refuses an out-of-range or
  unbounded request **before** issuing any gdb command.
- Exactly one of `symbol` / `address` is required; supplying both or neither is a categorized
  configuration error.
- The tool returns categorized failures, distinguished by a `data["code"]` discriminator, for a
  bad bound, a bad target, an unknown symbol, an invalid/non-code address range, and malformed
  gdb output.
- No raw expression evaluation is introduced; the disassembly command is purely numeric and the
  surface stays as narrow as the rest of the tier.

## Non-goals

- A centered window (backward disassembly of variable-length x86 instructions is unreliable;
  see ADR-0276 "Considered & rejected"). "Around" is a forward window from the target.
- Source-line–interleaved disassembly (`src_and_asm_line`); the flat mode-0 list carries
  `func_name`/`offset` symbol context. Possible future enrichment.
- A caller-supplied raw byte range, raw opcode bytes (`read_memory` already returns verbatim
  bytes), or any new gdb session state.

## Success criteria

Each maps to an acceptance-criteria checkbox on #921 and to a test.

1. **Symbol disassembly** — `disassemble(symbol="schedule", instruction_count=N)` resolves the
   symbol to its address, issues `-data-disassemble -s 0x<addr> -e 0x<addr+N*16> -- 0`, and
   returns up to `N` structured instructions. Tool returns `status="disassembled"`,
   `data={instruction_count, truncated, instructions:[...]}`.
2. **Address disassembly** — `disassemble(address=0x..., instruction_count=N)` skips symbol
   resolution and disassembles from that address.
3. **Invalid bounds refused before any command** — `instruction_count` ∉ `1..256` →
   `CONFIGURATION_ERROR` / `code="bad_instruction_count"`, no MI command written. Both-or-neither
   of symbol/address → `CONFIGURATION_ERROR` / `code="bad_target"`. Out-of-range address →
   `CONFIGURATION_ERROR` / `code="bad_address"`.
4. **Unknown symbol** — `resolve_symbol` of an unknown name yields a gdb `^error` mapped to
   `DEBUG_ATTACH_FAILURE`; a non-identifier name → `CONFIGURATION_ERROR` / `bad_symbol_name`.
5. **Invalid / non-code range** — gdb `^error,"Cannot access memory at address 0x..."` /
   `"No function contains specified address."` → `DEBUG_ATTACH_FAILURE` / `code="no_instructions"`.
6. **Malformed MI output** — empty / missing / non-list `asm_insns` →
   `DEBUG_ATTACH_FAILURE` / `code="no_instructions"`.
7. **Truncation** — more than `instruction_count` instructions in the byte span →
   `truncated=true`, list sliced to `instruction_count`.
8. **Redaction** — a registered secret appearing in an `inst`/`func_name` field is masked before
   the instruction is returned.

## Design

See [ADR-0276](../adr/0276-gdb-disassemble.md) for the decision and rejected alternatives. In
brief:

- **Port models** (`providers/ports/debug.py`): `GdbInstruction(address, inst, func_name,
  offset)` and `GdbDisassembly(instructions, truncated)`; a `disassemble(...)` Protocol method on
  `GdbMiEngine`.
- **Parser** (`mi_protocol.py`): `disassembly_rows(records)` returns the `asm_insns` dict rows
  (mirrors `stack_frames`), tolerating a missing/non-list payload.
- **Engine** (`providers/shared/debug_common/gdbmi.py`): `disassemble(...)` validates bounds and
  target up front, resolves a symbol via `resolve_symbol`, bounds the *response* by slicing the
  unbounded-range result, and redacts each instruction. A `_disassemble_command` wrapper maps the
  unmapped-range gdb `^error` to `no_instructions`.
- **Tool** (`mcp/tools/debug/ops.py`): `_disassemble_op` + `_register_debug_disassemble`,
  read-only, contributor, via `run_engine_op`.
- **Fault-inject** (`providers/fault_inject/debug/gdb.py`): a synthetic `disassemble` returning
  plausible instructions.
- **Guards/docs**: exposure scope, `tool_index` vocabulary, `_BEHAVIOR_TESTS_BY_TOOL`, and the
  regenerated `just docs` reference.

No schema, migration, RBAC, persistence, config, or destructive-op gate change.

## Testing

- **Engine** (`tests/providers/local_libvirt/test_debug_gdbmi.py`): symbol-path resolution +
  command shape, address-path, truncation, `bad_instruction_count` (parametrized 0 and cap+1)
  with no command written, `bad_target` (both/neither), `bad_address`, `no_instructions` on
  empty/malformed `asm_insns`, `no_instructions` on the unmapped-range `^error`, pass-through of
  an unrelated gdb error, and secret redaction in an instruction field. Plus a
  `disassembly_rows` parser test for the flat and malformed shapes.
- **Tool** (`tests/mcp/debug/test_debug_ops.py`): happy path returns `status="disassembled"`
  with the structured payload and the next-action pointers; a `no_instructions` failure is
  surfaced with `error_category="debug_attach_failure"` and `data["code"]`.
- **Guards** (`tests/mcp/core/test_tool_docs.py`, `tests/mcp/test_tool_index.py`): the new tool
  is registered, scoped, vocabulary-indexed, behavior-mapped, and present in the generated
  reference.
