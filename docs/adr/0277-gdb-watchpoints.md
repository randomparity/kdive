# ADR-0277: typed gdb write watchpoints for live gdbstub sessions (#922)

- Status: Accepted
- Date: 2026-06-29

## Context

The gdb-MI debug tier (ADR-0034, extended by ADR-0248 for `&<identifier>` symbol resolution,
ADR-0275 for stack walking, and ADR-0276 for disassembly) drives a live gdbstub `DebugSession`
through a persistent `gdb --interpreter=mi3` engine. An agent stopped at a breakpoint can set
breakpoints, read registers and memory, resolve a symbol, walk the stack, and disassemble — but
it cannot ask "what code writes this address?" A data watchpoint is the standard tool for that:
the kernel runs, and gdb stops the moment the watched bytes change.

gdb sets a hardware data watchpoint with `-break-watch EXPR`, which watches the lvalue named by
`EXPR`. The interesting and dangerous part is `EXPR`: a raw watch expression would reopen the
arbitrary-expression surface ADR-0034 deliberately excluded ("general expression evaluation …
and watchpoints remain outside this engine's contract"). Issue #922 asks for typed, bounded
watchpoints on a **bare symbol or an explicit address** for **writes**, without exposing
arbitrary gdb expression execution, plus list and clear.

## Decision

Add three ops to the **shared** `GdbMiEngine` (so both local-libvirt and remote-libvirt gain
them) plus three MCP tools. No new gdb expression surface; the watch expression is *constructed*
from a validated numeric address and a bounded size, never accepted from the caller. This lifts
the ADR-0034 "watchpoints remain outside" exclusion in the same narrow, non-injectable way
ADR-0248 lifted the "no expression evaluation" exclusion: a gated name or a numeric address, no
caller expression.

1. **`GdbMiEngine.set_watchpoint(attachment, *, symbol=None, address=None, byte_count=8) ->
   GdbWatchpointRef`.** Validate, **before any MI command**:
   - `byte_count` is one of `WATCH_BYTE_SIZES = {1, 2, 4, 8}`, else `CONFIGURATION_ERROR` /
     `code="bad_byte_count"` (with `supported`). These are exactly the x86-64 hardware data
     watchpoint widths — a single debug-register watchpoint covers one of them. A non-power-of-two
     or larger region would force gdb to split into several hardware watchpoints or fall back to a
     **software** watchpoint, which single-steps the inferior — infeasible over a kernel gdbstub —
     so bounding to these four keeps every watchpoint a single, predictable hardware watchpoint.
   - **Exactly one** of `symbol` / `address` is given (reusing the disassemble target resolver,
     ADR-0276): a non-identifier `symbol` raises `bad_symbol_name`, neither/both raises
     `bad_target`, an out-of-range `address` raises `bad_address`, and a symbol resolves via the
     already-gated `resolve_symbol` to a numeric address. The watch command is then purely
     numeric.

   Construct the **write** watch expression `*(char(*)[N])0x<addr>` (pointer-to-array-of-`N`-char,
   dereferenced — an `N`-byte lvalue at the address; no spaces, so MI tokenizes it as one
   argument), issue `-break-watch <expr>` (MI default is a write watchpoint — not `-a`/`-r`), and
   parse the `wpt={number,exp}` result into a redacted `GdbWatchpointRef`. The address and size
   are the only inputs, so the expression is non-injectable by construction.

2. **Write watchpoints only.** The issue asks to "watch … for writes". The MI default
   (`-break-watch` with no flag) is a write watchpoint, the highest-value variant (catch the
   writer). Read (`-r`) and access (`-a`) watchpoints are a possible additive future flag; adding
   them now would widen the surface past the acceptance criteria.

3. **"Target cannot support" classification.** A `_watchpoint_command` wrapper inspects the
   redacted `^error` `msg`. gdb reports an unsupported or exhausted watchpoint with a message
   naming the watchpoint (e.g. "Target does not support hardware watchpoints.", "Could not insert
   hardware watchpoints: …"); a `_WATCHPOINT_UNSUPPORTED_RE` match re-raises as
   `DEBUG_ATTACH_FAILURE` / `code="watchpoint_unsupported"` so an agent can branch on it. Other
   gdb errors pass through unchanged. These gdb messages carry no secret, so matching the redacted
   text is sound (the same property ADR-0275/0276 rely on).

4. **`GdbMiEngine.list_watchpoints(attachment) -> list[GdbWatchpointRef]`.** Issue `-break-list`
   and keep only rows whose `type` names a watchpoint (`"hw watchpoint"`, `"read watchpoint"`,
   `"acc watchpoint"` all contain `"watchpoint"`); breakpoints are excluded. Each row's `what`
   carries the watched expression, normalized to `GdbWatchpointRef.expr`. The shared
   `breakpoint_rows` parser already yields these rows (gdb lists watchpoints in the same
   `BreakpointTable`), so no new parser is needed.

5. **`GdbMiEngine.clear_watchpoint(attachment, number) -> None`.** Gate `number` to a bare
   integer (`code="bad_watchpoint_id"`) and issue `-break-delete <number>` (gdb deletes a
   watchpoint by the same numbering as a breakpoint). No list round-trip to confirm the number is
   a watchpoint: deleting by a wrong number is the caller's concern, identical to
   `clear_breakpoint`.

6. **Three MCP tools.** `debug.set_watchpoint` (mutating), `debug.list_watchpoints` (read-only),
   `debug.clear_watchpoint` (mutating), each `contributor` RBAC (every live-debug op is
   `contributor`), gated to a `live` `DebugSession` by the shared `run_engine_op` (per-session
   lock, attach-once, audit, off-loop). Params: `set_watchpoint(session_id, symbol?, address?,
   byte_count=8)`, `list_watchpoints(session_id)`, `clear_watchpoint(session_id, number)`.
   `set` → `status="watching"`, `data={number, expr, byte_count}`, next
   `["debug.continue", "debug.list_watchpoints"]`; `list` → `status="listed"`,
   `data={count, watchpoints:[{number,type,expr,addr,enabled},…]}`, next
   `["debug.set_watchpoint", "debug.continue"]`; `clear` → `status="cleared"`, next
   `["debug.list_watchpoints"]`.

7. **Wiring.** Add `GdbWatchpointRef(ProviderModel)` and the three Protocol methods to
   `providers/ports/debug.py`; add a synthetic conforming implementation to
   `FaultInjectDebugEngine`; factor the disassemble target resolver into a shared
   `_resolve_target` (used by both `disassemble` and `set_watchpoint`); update the tier
   docstrings (including the shared-engine "watchpoints remain outside" line, now lifted),
   `exposure.py` `_TOOL_SCOPES` (`_CONTRIBUTOR`), `tool_index.py` search vocabulary, and
   regenerate the tool reference (`just docs`). The new tools are marked `implemented`, matching
   the ADR-0248/0276 precedent (they ride the same already-live-proven attach transport and are
   unit-tested against the scripted controller); they are **not** added to
   `_LOCAL_PROVEN_DEBUG_TOOLS` until a live exercise lands.

No schema, migration, persistence, or destructive-op-gate change; the MCP surface change is
additive.

## Consequences

- An agent can ask "what writes this address/symbol?" as structured, redacted, typed data over
  the gdbstub with no raw gdb expression and no `drgn`/credential path — a natural companion to
  `set_breakpoint`/`continue` (set a watchpoint, continue, stop on the write) and
  `resolve_symbol`/`disassemble` (disassemble the writer's frame).
- ADR-0034's expression-evaluation exclusion stays intact: `set_watchpoint` takes a gated bare
  identifier or a numeric address plus a bounded size, never a caller expression.
- Remote-libvirt inherits the ops for free (shared engine), matching the rest of the tier.
- Failures are categorized with `data["code"]` discriminators (`bad_byte_count`, `bad_target`,
  `bad_address`, `bad_symbol_name` via `resolve_symbol`, `bad_watchpoint_id`,
  `watchpoint_unsupported`) so an agent can branch on the cause.
- The new tools are covered by the `exposure.py` completeness guard, the
  `_BEHAVIOR_TESTS_BY_TOOL` coverage guard, the `tool_index` completeness guard, and the
  `just docs` generated-reference gate.

## Considered & rejected

- **A caller-supplied watch expression.** Rejected: it reopens exactly the arbitrary-expression
  surface ADR-0034 excluded. Constructing `*(char(*)[N])0x<addr>` from a validated address and a
  bounded size keeps the command non-injectable while still watching the requested bytes.
- **Read (`-r`) and access (`-a`) watchpoints now.** Rejected for this issue: it asks for write
  watchpoints; the MI default is a write watchpoint, and write ("who changed this?") is the
  primary use. A `kind` flag adding read/access is a clean additive follow-up.
- **An arbitrary `byte_count`.** Rejected: x86-64 hardware data watchpoints cover 1/2/4/8 bytes;
  a non-power-of-two or larger region makes gdb either chain several hardware watchpoints or fall
  back to a software watchpoint that single-steps the inferior — unusable over a kernel gdbstub.
  Bounding to `{1,2,4,8}` keeps each watchpoint a single hardware watchpoint with predictable
  semantics; a genuinely unsupported target is still surfaced as `watchpoint_unsupported`.
- **Deriving the size from the symbol's type.** Rejected: `&name` does not cheaply yield the
  symbol's type (the same limitation ADR-0276 noted), and an explicit bounded `byte_count` is
  simpler and keeps address and symbol targets uniform.
- **Verifying the number is a watchpoint before clearing.** Rejected: gdb's `-break-delete`
  deletes by number; an extra `-break-list` round-trip to confirm the kind adds latency for no
  safety, exactly as `clear_breakpoint` deletes without confirming.
- **A local-libvirt-only op.** Rejected: the engine is shared and remote-libvirt has the same
  gap; placing the ops on `GdbMiEngine` follows the tier's existing seam (as ADR-0248/0275/0276
  did).
