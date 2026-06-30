# ADR-0276: typed gdb disassembly for live gdbstub sessions (#921)

- Status: Accepted
- Date: 2026-06-29

## Context

The gdb-MI debug tier (ADR-0034, extended by ADR-0248 for `&<identifier>` symbol resolution
and ADR-0275 for read-only stack walking) drives a live gdbstub `DebugSession` through a
persistent `gdb --interpreter=mi3` engine. An agent stopped at a breakpoint can already read
registers, read memory by address, resolve a symbol to its address, and walk the stack — but
it cannot see the **machine instructions** at a frame's address. To understand *what* the
kernel is executing (a faulting instruction, the prologue of a function, the site a backtrace
points at), the only path today is `read_memory` of the raw bytes plus an out-of-band
disassembler, or a heavier `drgn`/raw-gdb path that needs credentials a System may not have.

gdb already disassembles from the loaded `vmlinux`: `-data-disassemble -s START -e END -- 0`
returns `asm_insns=[{address,func-name,offset,inst},...]` for the address range. This is a
read-only command over an address range the caller does not express as a gdb expression, so —
like `-stack-list-frames` in ADR-0275 — exposing it does not reopen the expression-evaluation
surface ADR-0034 guarded. Issue #921 asks for typed, bounded, redacted disassembly around a
symbol or explicit address.

## Decision

Add one read-only op to the **shared** `GdbMiEngine` (so both local-libvirt and remote-libvirt
gain it) plus one MCP tool. No new gdb expression surface; no session state.

1. **`GdbMiEngine.disassemble(attachment, *, symbol=None, address=None,
   instruction_count=64) -> GdbDisassembly`.** Validate, **before any MI command**:
   - `instruction_count` is an int in `1..MAX_DISASSEMBLE_INSTRUCTIONS` (default cap 256),
     else `CONFIGURATION_ERROR` / `code="bad_instruction_count"` — the same reject-over-cap
     shape `read_memory`/`backtrace` use.
   - **Exactly one** of `symbol` / `address` is given, else `CONFIGURATION_ERROR` /
     `code="bad_target"` (covers both-supplied and neither-supplied).
   - For `symbol`: resolve via the existing `resolve_symbol` (which gates the name to a bare C
     identifier — `CONFIGURATION_ERROR` / `bad_symbol_name` — and raises
     `DEBUG_ATTACH_FAILURE` for an unknown/addressless symbol). The disassembly command itself
     is then purely numeric, so it stays non-injectable.
   - For `address`: an int in the 64-bit range, else `CONFIGURATION_ERROR` /
     `code="bad_address"` (mirrors `read_memory`'s range check).

   Then **bound the response, not the gdb command** (the ADR-0275 trick): compute
   `end = start + instruction_count * MAX_INSTRUCTION_BYTES` (16 = x86-64's 15-byte maximum
   rounded up — a generous byte span that is guaranteed to cover at least `instruction_count`
   instructions of mapped code), issue `-data-disassemble -s 0x<start> -e 0x<end> -- 0`, parse
   the `asm_insns` rows, set `truncated = total > instruction_count`, and return the first
   `instruction_count` redacted `GdbInstruction`s. Slicing in Python is deterministic and
   avoids gdb's range/disassemble-mode quirks across versions.

2. **Forward window.** "Around" is implemented as a forward window starting at the
   symbol/address, not a centered one. Reliable *backward* disassembly of variable-length x86
   instructions is not possible without unwind heuristics that can desynchronize; a forward
   window from the target is deterministic and is what an agent inspecting a frame address or
   a function prologue needs.

3. **Mode 0 (flat instructions).** `-- 0` yields a flat `asm_insns` list of
   `{address, func-name, offset, inst}`. Each row maps to `GdbInstruction(address, inst,
   func_name, offset)`; `func-name`/`offset` carry the symbol context the acceptance criteria
   ask for. The source-mixed modes (`4`/`5`, nested `src_and_asm_line`) are rejected: they
   complicate bounding by instruction count and add a redaction path for file/line text with no
   acceptance-criterion gain.

4. **gdb `^error` classification.** A `_disassemble_command` wrapper inspects the redacted
   `^error` `msg` that `execute_mi_command` surfaces. gdb answers an unmapped or non-code range
   with `^error,"Cannot access memory at address 0x..."` / `"No function contains specified
   address."` rather than an empty `^done,asm_insns=[]`, so a `_NO_CODE_RE` match re-raises the
   caller's `no_instructions` code — the same code the empty/malformed-`asm_insns` path raises,
   so both response shapes converge (mirroring ADR-0275's `no_frames` convergence). Other gdb
   errors pass through unchanged. These gdb messages carry no secret, so matching the redacted
   text is sound.

5. **`debug.disassemble` MCP tool.** Read-only, `contributor` RBAC (every live-debug op is
   `contributor`), gated to a `live` `DebugSession` by the shared `run_engine_op` (per-session
   lock, attach-once, audit, off-loop). Params: `session_id`, optional `symbol`, optional
   `address`, `instruction_count` (default 64). `status="disassembled"`,
   `data={instruction_count, truncated, instructions:[{address,inst,func_name,offset},...]}`
   (None fields omitted), next actions `["debug.read_memory", "debug.read_registers"]`.

6. **Wiring.** Add `GdbInstruction(ProviderModel)` + `GdbDisassembly(ProviderModel)` and the
   Protocol method to `providers/ports/debug.py`; a `disassembly_rows` parser to
   `mi_protocol.py` (mirroring `stack_frames`); a synthetic conforming method to
   `FaultInjectDebugEngine`. Update the tier docstrings, `exposure.py` `_TOOL_SCOPES`
   (`_CONTRIBUTOR`), `tool_index.py` search vocabulary, and regenerate the tool reference
   (`just docs`). The new tool is marked `implemented`, matching the ADR-0248/0275 precedent
   (it rides the same already-live-proven attach transport and is unit-tested against the
   scripted controller); it is not added to `_LOCAL_PROVEN_DEBUG_TOOLS` until a live exercise
   lands.

No schema, migration, persistence, or destructive-op gate change; the MCP surface change is
additive.

## Consequences

- The instructions at any kernel address or symbol are reachable as structured, redacted,
  bounded data over the gdbstub with no raw gdb commands and no `drgn`/credential path — a
  natural companion to `backtrace`/`read_frame` (disassemble a frame's `addr`) and
  `resolve_symbol`.
- ADR-0034's expression-evaluation exclusion is untouched: `disassemble` takes a gated bare
  identifier or a numeric address, never a caller expression.
- Remote-libvirt inherits the op for free (shared engine), matching the rest of the tier.
- Failures are categorized with `data["code"]` discriminators (`bad_instruction_count`,
  `bad_target`, `bad_address`, `bad_symbol_name` via `resolve_symbol`, `no_instructions`) so an
  agent can branch on the cause.
- The new tool is covered by the `exposure.py` completeness guard, the `_BEHAVIOR_TESTS_BY_TOOL`
  coverage guard, the `tool_index` completeness guard, and the `just docs` generated-reference
  gate.

## Considered & rejected

- **A centered window around the target.** Rejected: reliable backward disassembly of
  variable-length x86 instructions requires heuristics that can desynchronize and produce
  garbage instructions before the target; a forward window is deterministic and covers the
  inspect-this-address use case.
- **Source-mixed disassembly modes (`-- 5`, `src_and_asm_line`).** Rejected: the nested
  per-source-line structure complicates count-bounding and adds a file/line redaction path; the
  flat mode-0 list already carries `func_name`/`offset` symbol context, satisfying "optional
  symbol/source context when available". Source-line enrichment is a possible future addition.
- **A caller-supplied byte range (`start`/`end`).** Rejected: exposes gdb's range/disassemble
  semantics and a second numeric contract to the agent; a single `instruction_count` cap plus a
  symbol/address selector covers the use cases and keeps the response bounded.
- **Bounding via a gdb instruction count (`-data-disassemble` has none).** gdb's
  `-data-disassemble` bounds by address range only; there is no instruction-count form, so the
  byte-span-plus-Python-slice approach (as ADR-0275 did for frames) is the deterministic path.
- **Passing the symbol straight into `-s` as an expression.** Rejected: resolving to a numeric
  address first via the already-gated `resolve_symbol` keeps the disassembly command
  non-injectable and reuses the unknown-symbol failure path, rather than letting a name reach
  gdb's linespec parser.
- **Returning raw opcode bytes (`-- 2`).** Rejected: `read_memory` already returns verbatim
  bytes under its cap; duplicating that here adds bytes to redact/bound for no new capability.
  The disassembly carries the rendered `inst` text instead.
- **Local-libvirt-only op.** Rejected: the engine is shared and remote-libvirt has the same
  gap; placing it on `GdbMiEngine` follows the tier's existing seam (as ADR-0248/0275 did).
