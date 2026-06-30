# ADR-0275: typed gdb backtrace and frame inspection over the gdbstub (#920)

- Status: Accepted
- Date: 2026-06-29

## Context

The gdb-MI debug tier (ADR-0034, extended by ADR-0248) drives a live gdbstub `DebugSession`
through a persistent `gdb --interpreter=mi3` engine. ADR-0034 deliberately scoped the engine
to a narrow surface — breakpoints, `read_registers`, `read_memory` (4096-byte cap), `continue`,
`interrupt` — and the module docstring lists "general expression evaluation, module loading,
**stack walking**, and watchpoints" as explicitly outside the contract
(`providers/shared/debug_common/gdbmi.py:5-8`). ADR-0248 narrowed the expression-evaluation
exclusion to allow exactly `&<identifier>` for symbol resolution.

When the kernel stops at a breakpoint, the `*stopped` record carries only the **innermost**
frame (`GdbStopRecord.frame`). There is no way to see frames `#1..#N` — the call chain that
tells an agent *how* the kernel reached the stop. The only alternatives are walking saved frame
pointers by hand out of raw memory, or a heavier `drgn`/raw-gdb path that needs credentials a
System may not have. Issue #920 asks for typed backtrace and single-frame inspection.

gdb already holds the call stack at a stop: `-stack-list-frames` returns
`stack=[frame={level,addr,func,file,line},...]` and `-stack-list-frames N N` returns one frame.
These are read-only introspection commands that take no caller-supplied expression, so exposing
them does not reopen the expression-evaluation surface ADR-0034 guarded.

## Decision

Lift ADR-0034's "stack walking is out of contract" exclusion with two read-only ops on the
**shared** `GdbMiEngine`, so both local-libvirt and remote-libvirt gain them, plus two MCP
tools. No `-stack-select-frame` session state, no frame-local values.

1. **`GdbMiEngine.backtrace(attachment, *, max_frames=MAX_BACKTRACE_FRAMES) -> GdbBacktrace`.**
   Validate `max_frames` is an int in `1..MAX_BACKTRACE_FRAMES` (default 64), raising
   `CONFIGURATION_ERROR` / `code="bad_frame_count"` **before** any MI command — the same
   reject-over-cap shape `read_memory` uses. Issue `-stack-list-frames` **unbounded**: gdb
   returns the whole stack and the cap bounds the *response*, not the gdb command, which avoids
   the version-dependent `^error` some gdbs raise when the high level exceeds stack depth. Parse
   the `stack` rows. Empty / missing / non-list `stack` (gdb omitted frame data, or malformed
   output) raises `DEBUG_ATTACH_FAILURE` / `code="no_frames"`. Otherwise build redacted
   `GdbFrame`s, set `truncated = total > max_frames`, and return the first `max_frames`.

2. **`GdbMiEngine.read_frame(attachment, *, level) -> GdbFrame`.** Validate `level` is a
   **non-negative** int, raising `CONFIGURATION_ERROR` / `code="bad_frame_level"` before any MI
   command. The bound is deliberately *not* `MAX_BACKTRACE_FRAMES`: that cap bounds the
   `backtrace` response, whereas `read_frame` selects a single frame and a deep kernel stack can
   hold a valid frame past 64 (`backtrace` flags this with `truncated=true`). Issue
   `-stack-list-frames level level` and let gdb decide whether the level exists. No frame at that
   level (empty/missing/malformed) raises `DEBUG_ATTACH_FAILURE` / `code="no_frame_at_level"`
   (carrying `level`) — an out-of-range level is a "no frame here" result, not a config error.
   Otherwise return the single redacted frame.

3. **Running-inferior classification.** `-stack-list-frames` against a running target returns a
   gdb `^error` that `execute_mi_command` already maps to `DEBUG_ATTACH_FAILURE` (payload
   redacted). A `_stack_command` wrapper inspects the redacted error `msg`; a `running` match
   re-raises `DEBUG_ATTACH_FAILURE` / `code="inferior_running"`. Other gdb errors pass through
   unchanged. The running error message carries no secret, so matching the redacted text is
   sound.

4. **`debug.backtrace` / `debug.read_frame` MCP tools.** Read-only, `contributor` RBAC (every
   live-debug op is `contributor`), gated to a `live` `DebugSession` by the shared
   `run_engine_op` (per-session lock, attach-once, audit, off-loop). `debug.backtrace`:
   `status="walked"`, `data={frame_count, truncated, frames:[{level,func,addr,file,line},...]}`
   (None fields omitted), next actions `["debug.read_frame", "debug.read_registers"]`.
   `debug.read_frame`: `status="read"`, `data={level, frame:{...}}`, next actions
   `["debug.read_registers", "debug.read_memory"]`.

5. **Wiring.** Add `GdbBacktrace(ProviderModel)` and the two Protocol methods to
   `providers/ports/debug.py`; a `stack_frames` parser to `mi_protocol.py` (mirroring
   `breakpoint_rows`); synthetic conforming methods to `FaultInjectDebugEngine`. Update the tier
   docstrings, `exposure.py` `_TOOL_SCOPES` (`_CONTRIBUTOR`), `tool_index.py` search vocabulary,
   and regenerate the tool reference (`just docs`). The two new tools are marked `implemented`,
   matching the ADR-0248 `resolve_symbol` precedent (they ride the same already-live-proven
   attach transport and are unit-tested against the scripted controller); they are not added to
   the `_LOCAL_PROVEN_DEBUG_TOOLS` live-proof set until a live exercise lands.

No schema, migration, persistence, or destructive-op gate change; the MCP surface change is
additive.

## Consequences

- The stopped-kernel call chain is reachable as structured, redacted, bounded data over the
  gdbstub with no raw gdb commands, no frame-pointer math, and no `drgn`/credential path.
- ADR-0034's "stack walking is out of contract" is reversed for these two read-only commands;
  the module docstring is updated so the stated contract does not drift from behavior. The
  expression-evaluation exclusion (ADR-0248's `&<identifier>` aside) is untouched — neither op
  takes a caller expression.
- Remote-libvirt inherits both ops for free (shared engine), matching the rest of the tier.
- Failures are categorized with `data["code"]` discriminators (`inferior_running`, `no_frames`,
  `no_frame_at_level`, `bad_frame_count`, `bad_frame_level`) so an agent can branch on the cause.
- The new tools are covered by the `exposure.py` completeness guard, the
  `_BEHAVIOR_TESTS_BY_TOOL` coverage guard, and the `just docs` generated-reference gate.

## Considered & rejected

- **Frame-local variables / argument values** (`-stack-list-locals`, `-stack-list-arguments`
  with values). Rejected: returning evaluated locals/args reopens exactly the
  expression-evaluation surface ADR-0034 excluded and carries a per-value redaction burden for
  no acceptance-criterion gain. Argument *names only* (`-stack-list-arguments 0`) is a possible
  future enrichment but is not required and is left out to keep the surface minimal.
- **A free-form rendered `bt` text blob.** Rejected: the structured frames already carry the
  redacted textual fields (`func`, `file`) bounded by `max_frames`; a second rendered string
  duplicates that data and adds a redaction/cap path for no new capability. "Bounded redacted
  text" is satisfied by the bounded, redacted structured frames.
- **Bounding via `-stack-list-frames 0 max_frames`.** Rejected: some gdb versions raise
  `^error,"No frame at level N."` when the high bound exceeds stack depth, which would turn a
  short stack into a spurious failure. Issuing the command unbounded and slicing in Python is
  deterministic and version-independent.
- **`-stack-info-depth` + a second bounded list call** to compute `truncated`. Rejected: two MI
  round-trips where slicing the single unbounded result already yields the count and the
  truncation flag.
- **`-stack-select-frame N` then `-stack-info-frame`** for `read_frame`. Rejected: mutates gdb
  session state (the "current frame") across otherwise-stateless ops; `-stack-list-frames N N`
  reads one frame without side effects.
- **A general `-stack-list-frames` passthrough with caller-controlled low/high bounds.**
  Rejected: exposes gdb's level-beyond-depth error semantics to the agent and adds a second
  numeric contract; a single `max_frames` cap plus a `level` selector covers the use cases.
- **Local-libvirt-only ops.** Rejected: the engine is shared and remote-libvirt has the same
  gap; placing them on `GdbMiEngine` follows the tier's existing seam (as ADR-0248 did).
