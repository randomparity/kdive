# ADR-0248: symbol resolution over the gdbstub transport (#805)

- Status: Accepted
- Date: 2026-06-25

## Context

The gdb-MI debug tier (ADR-0034) loads the Run's DWARF `vmlinux` into gdb at attach
(`-file-exec-and-symbols`, `providers/shared/debug_common/gdbmi.py:167`), so the live engine
holds the kernel symbol table. But ADR-0034 deliberately scoped the engine to seven ops over
**registers and raw memory by integer address** and declared symbol/expression resolution
"outside this engine's contract" (`gdbmi.py:5-6`). `debug.read_memory` takes an integer
address only; there is no `resolve_symbol`.

So confirming a named global like `d_hash_shift` over the gdbstub transport forces either an
out-of-band `vmlinux` download + `nm`/`System.map` math, or a `drgn-live` session — the only
symbol-aware path, which needs an `ssh_credential_ref` the System may not have. The Part 4
black-box review (`BLACK_BOX_REVIEW.md`, 2026-06-25) flagged this as defect **D4b** (REAL):
gdb already holds the symbol table, yet the only way to a named address is heavy and
out-of-band. The error-ergonomics half (**D4a**) is a separate issue.

ADR-0034's narrow surface was a safety choice: gating the breakpoint *location* to a bare C
identifier (`_SYMBOL_NAME_RE`, `gdbmi.py:94`) keeps `-break-insert` an
address-of-a-name and therefore non-injectable. Any symbol op must preserve that property
rather than open a general expression evaluator. `-break-insert` itself does not solve D4b: it
is gated to a code location, so it cannot resolve a **data** global like `d_hash_shift`.

## Decision

Extend ADR-0034's engine contract with one read-only symbol-resolution op on the **shared**
`GdbMiEngine` (`providers/shared/debug_common/gdbmi.py`), so both local-libvirt and
remote-libvirt gain it, plus one MCP tool.

1. **`GdbMiEngine.resolve_symbol(attachment, name) -> int`.** Gate `name` to
   `_SYMBOL_NAME_RE` (bare C identifier) and raise `CONFIGURATION_ERROR` /
   `code="bad_symbol_name"` **before** issuing any MI command. Otherwise issue
   `-data-evaluate-expression &<name>` — the only expression form ever sent is
   `&<identifier>` (address-of-a-name), so the surface stays non-injectable, the same property
   the breakpoint-location gate already guarantees. `&name` resolves both data globals and
   functions, unlike the code-only `-break-insert`. Parse the first `0x[0-9a-fA-F]+` token from
   the returned `value` (skipping any leading C type-cast like `(int *)`); return it as an
   `int`. A gdb `^error` (e.g. unknown symbol) surfaces as `DEBUG_ATTACH_FAILURE` through the
   existing `execute_mi_command` mapping — identical to `set_breakpoint`'s contract for a bad
   symbol today. A present-but-unparseable `value` raises `DEBUG_ATTACH_FAILURE` /
   `code="bad_symbol_value"` with the value **redacted** in details.

2. **`debug.resolve_symbol(session_id, name)` MCP tool.** Read-only, `contributor` RBAC
   (every live-debug op is `contributor`), gated to a `live` DebugSession by the shared
   `run_engine_op` path (per-session lock, attach-once, audit). Success:
   `status="resolved"`, `data={"symbol": name, "address": "0x..."}`,
   `suggested_next_actions=["debug.read_memory", "debug.read_registers"]` so the
   address→value chain is discoverable. The engine now exposes eight ops; the tier docstrings,
   `exposure.py` map (`_CONTRIBUTOR`), and the generated tool reference are updated to match.

3. **Value reads compose, not a new op.** The agent obtains a symbol's value by chaining the
   existing `debug.read_memory(address, byte_count)` — reusing its 4096-byte cap and verbatim
   bytes contract — rather than a bespoke resolve-and-read op.

No schema, migration, persistence, or gate change; the MCP surface change is additive.

## Consequences

- A named global's address is reachable over gdbstub with no `vmlinux` download and no
  `drgn-live`/`ssh_credential_ref`. Value confirmation is a documented two-call chain
  (`resolve_symbol` → `read_memory`).
- ADR-0034's "expression evaluation is out of contract" is narrowed, not reversed: the engine
  now evaluates exactly one gated form, `&<identifier>`. The module docstring is updated to say
  so, so the contract statement does not drift from behavior.
- Remote-libvirt inherits the op for free (shared engine), matching how the tier's other ops
  already work.
- Unknown-symbol error text is unchanged (generic `DEBUG_ATTACH_FAILURE`); improving it is
  D4a, deliberately out of scope.
- The new tool is covered by the existing `test_exposure_map_covers_every_registered_tool`
  guard and the `just docs` / `just resources-docs` generated-reference gates.

## Considered & rejected

- **A one-shot `debug.read_symbol(name, byte_count)`** (resolve then read in one call).
  Rejected for now: `resolve_symbol` + the existing `read_memory` already composes to a value,
  so a combined op duplicates `read_memory`'s cap/redaction handling and adds a second public
  contract for no new capability. It can be added later if the two-call chain proves painful in
  practice (the issue lists it as an *optional* extra, not the acceptance bar).
- **Surface the already-resolved `addr` from `_set_breakpoint_op`** (the issue's "cheapest
  partial win"). Rejected: `-break-insert` is gated to a **code** location, so it cannot
  resolve a data global like `d_hash_shift` — it does not meet the acceptance criterion.
- **A general `-data-evaluate-expression <expr>` op.** Rejected: an arbitrary-expression
  surface is an injection/side-effect risk and reverses ADR-0034's deliberate scoping. The
  `&<identifier>` gate keeps the capability while preserving non-injectability.
- **`-symbol-info-variables`/`-symbol-info-functions` instead of `&name`.** Rejected: those
  return symtab/declaration metadata, not a single runtime address, so the parse is heavier and
  the data-vs-code distinction leaks into the caller. `&name` yields the address directly for
  both.
- **A local-libvirt-only op.** Rejected: the engine is shared and remote-libvirt has the same
  gap; putting it on `GdbMiEngine` follows the tier's existing seam.
- **Returning the value inline / type-aware rendering.** Rejected: that re-imports the
  type-evaluation surface ADR-0034 excluded; raw bytes via `read_memory` keep the value path
  uniform with the rest of the tier.
