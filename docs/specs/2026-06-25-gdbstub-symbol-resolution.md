# Spec — symbol resolution over the gdbstub transport (#805, BB-P4 D4b)

- **Status:** Draft
- **Date:** 2026-06-25
- **Issue:** [#805](https://github.com/randomparity/kdive/issues/805) (epic #800)
- **ADR:** [ADR-0248](../adr/0248-gdbstub-symbol-resolution.md)

## Problem

On a `local-libvirt` (or remote-libvirt) box, the gdb-MI debug tier already loads the Run's
DWARF `vmlinux` into gdb at attach (`-file-exec-and-symbols <vmlinux>`,
`providers/shared/debug_common/gdbmi.py:167`), so the symbol table is present in the live
engine. Yet the exposed `debug.*` ops only read **registers and raw memory by integer
address** — there is no way to turn a name like `d_hash_shift` into its address. The engine
docstring explicitly puts symbol/expression resolution "outside this engine's contract"
(`gdbmi.py:5-6`).

Consequently, confirming a named kernel global over the gdbstub transport requires one of:

1. downloading the `vmlinux`/debuginfo object and doing `nm`/`System.map` address math
   out-of-band, or
2. a `drgn-live` session — the only symbol-aware path — which needs an `ssh_credential_ref`
   the System may not carry.

Both are heavy for "what address is `d_hash_shift`?" when gdb already holds the answer.

This is the substantive half of black-box review defect **D4**. The error-ergonomics half
(**D4a** — improving the message when the symbol-less path is hit) is tracked separately and
is out of scope here.

## Goal / acceptance

A named kernel global's address (and, by composition, its value) is obtainable over the
gdbstub transport **without** downloading `vmlinux` and shelling out to `nm`.

Concretely: a new read-only `debug.resolve_symbol(session_id, name)` op returns the address
gdb resolves for `name`, gated to a bare C identifier, reusing the existing live-session
gate, per-session lock, attach-once, redaction, and audit machinery. The agent reads the
value (if wanted) by chaining the existing `debug.read_memory(address, byte_count)`.

## Non-goals

- **Arbitrary expression evaluation.** The name is gated to a bare C identifier and the only
  MI expression issued is `&<name>` (address-of-a-name). This is *not* a general
  `-data-evaluate-expression` surface; gating keeps it non-injectable, the same property the
  breakpoint-location gate already relies on (`_SYMBOL_NAME_RE`, `gdbmi.py:94`).
- **A one-shot resolve-and-read op** (`read_symbol(name, byte_count)`). Address +
  `read_memory` already composes to deliver a value; see ADR "Considered & rejected".
- **Improved no-symbol error ergonomics** (D4a) — separate issue.
- **Watchpoints, stack walking, type-aware value rendering** — unchanged, still out of
  contract.

## Design summary

One new engine method on the **shared** `GdbMiEngine` (so remote-libvirt benefits too) and one
new MCP tool. See [ADR-0248](../adr/0248-gdbstub-symbol-resolution.md) for the decision and
rejected alternatives.

### Engine: `GdbMiEngine.resolve_symbol(attachment, name) -> int`

1. Reject `name` that is not a bare C identifier (`_SYMBOL_NAME_RE`) with
   `CONFIGURATION_ERROR` / `code="bad_symbol_name"`, **before** any MI command runs (mirrors
   the breakpoint-location and read-range gates).
2. Issue `-data-evaluate-expression &<name>`. gdb returns the pointer value rendered as e.g.
   `(int *) 0xffffffff82a1b3c0 <d_hash_shift>` for a data global or
   `(void (*)(...)) 0xffffffff81... <panic>` for a function — `&name` works for both data and
   code symbols, unlike `-break-insert` (a code location only).
3. Parse the first `0x[0-9a-fA-F]+` token out of the `value` field. The address is the only
   `0x` token in the rendering; a leading C type-cast prefix (`(int *)`) is skipped by
   searching rather than anchoring.
4. A `^error` (e.g. unknown symbol) surfaces as `DEBUG_ATTACH_FAILURE` via the existing
   `execute_mi_command` mapping — the same contract `set_breakpoint` has today for a bad
   symbol (`test_run_maps_mi_error_to_debug_attach_failure`). A present-but-unparseable
   `value` raises `DEBUG_ATTACH_FAILURE` / `code="bad_symbol_value"` with the **redacted**
   value echoed.
5. Returns the resolved address as an `int`.

### MCP op: `debug.resolve_symbol(session_id, name)`

- Read-only (`_docmeta.read_only()`), `contributor` RBAC (same as every live-debug op), gated
  to a `live` DebugSession by the shared `run_engine_op` path.
- Success envelope: `status="resolved"`, `data={"symbol": name, "address": "0x..."}`,
  `suggested_next_actions=["debug.read_memory", "debug.read_registers"]` so the
  address→value chain is discoverable.
- Wired into `exposure.py` (`_CONTRIBUTOR`), the generated tool reference
  (`docs/guide/reference/debug.md` via `just docs`), and the packaged doc-resource snapshot
  (`just resources-docs`).

## Edge cases & failure modes

| Input / condition | Result |
|---|---|
| `name` not a bare C identifier (`d_hash_shift; x`, empty, `0bad`) | `CONFIGURATION_ERROR` / `bad_symbol_name`, no MI command issued |
| Unknown symbol (gdb `^error "No symbol ..."`) | `DEBUG_ATTACH_FAILURE` (existing `execute_mi_command` mapping), gdb payload redacted |
| `value` present but no `0x` token | `DEBUG_ATTACH_FAILURE` / `bad_symbol_value`, value redacted in details |
| `value` missing from payload | `DEBUG_ATTACH_FAILURE` / `bad_symbol_value` |
| Symbol resolves to `0x0` (weak/absent) | returns `0x0` — a valid resolved address, not an error |
| Type-cast prefix `(int *) 0x...` | first `0x` token parsed as the address |
| Session not `live` / cross-project / non-contributor | unchanged gate codes (`not_live`, `unknown_session`, `AuthorizationError`) |
| Server restarted (engine gone) | `no_live_session` from the registry, unchanged |

## Test plan (TDD, no gdb)

Engine tests (`tests/providers/local_libvirt/test_debug_gdbmi.py`) drive the scripted
`_FakeMiController`:

- resolves a data-global address from `(int *) 0x... <name>` rendering
- resolves a function address from a function-pointer rendering
- parses past a type-cast prefix; accepts `0x0`
- rejects a non-identifier name with `bad_symbol_name` and issues **no** MI command
- maps a gdb `^error` to `DEBUG_ATTACH_FAILURE`
- raises `bad_symbol_value` on a missing/unparseable `value`, with the value redacted

MCP-op tests (`tests/mcp/debug/test_debug_ops.py`) drive `run_engine_op` against a seeded
`live` DebugSession + fake attach seam:

- happy path: `status="resolved"`, `data["address"]`, `data["symbol"]`,
  `debug.read_memory` in `suggested_next_actions`
- bad name surfaces as a `configuration_error` envelope without a transport command

Exposure/doc guardrails: `test_exposure_map_covers_every_registered_tool` (already enforces
the map entry); `just docs` + `just resources-docs` regenerate the committed references.

## Rollback

Pure addition — no schema, migration, or persistence change. Reverting the commits removes the
tool, the engine method, the exposure entry, and the regenerated doc rows; nothing else
depends on it.
