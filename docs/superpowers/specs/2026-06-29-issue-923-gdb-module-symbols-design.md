# Issue #923 — gdb module-symbol loading for live kernel sessions

- **Issue:** #923 (Add gdb module-symbol loading for live kernel sessions)
- **ADR:** [ADR-0278](../../adr/0278-gdb-module-symbols.md)
- **Status:** Draft for review
- **Date:** 2026-06-29

## Problem

A `live` gdbstub `DebugSession` drives a persistent `gdb --interpreter=mi3` engine
(ADR-0034) against a booted kernel. The Run's `vmlinux` is loaded at attach, so the
agent can break, read registers/memory, resolve a symbol, walk the stack (ADR-0275),
disassemble (ADR-0276), and set write watchpoints (ADR-0277) — but only over **core
kernel** symbols. The moment the interesting code is in a loadable module
(`ext4`, a NIC driver, a freshly built test module), every address resolves to a raw
hex number and every backtrace frame in module text is anonymous, because gdb has no
symbols for module code.

ADR-0034 §7 deliberately dropped v1's `load_module_symbols` from the M0 port and
deferred it to "later introspection/postmortem milestones". #923 is that milestone.

A QEMU gdbstub is kernel-agnostic: it exposes raw memory + registers, nothing about
Linux modules. The attach loads only `vmlinux` (`gdb --nx`, no kernel helper scripts),
so there is no `lx-lsmod`/`lx-symbols` to lean on. Enumerating loaded modules and
their load addresses therefore means reading the kernel's own `struct module` list out
of guest memory, and loading symbols means `add-symbol-file <module.ko> <base>` with
both a base address and the module's `.ko` DWARF.

## Acceptance criteria (from the issue)

1. A contributor can list loaded modules with name, base address, and whether symbols
   are loaded when available.
2. A contributor can request symbol loading for one module through a structured tool,
   not raw gdb command text.
3. Missing module debuginfo returns `configuration_error` with remediation.
4. Stale module/address data returns a categorized failure instead of silently loading
   wrong symbols.
5. Tests cover module listing, successful symbol load, missing debuginfo, stale
   address, and malformed MI output.

## Decision summary

Add **two ops to the shared `GdbMiEngine`** plus **two `contributor` MCP tools**, in
the same narrow, non-injectable way ADR-0248/0275/0276/0277 lifted earlier ADR-0034
exclusions: every gdb command is *constructed* from validated, gated inputs (a gated
identifier, a numeric address, an engine-staged file path), never accepted as caller
text. Remote-libvirt inherits both ops for free (shared engine). No schema, migration,
RBAC, or destructive-op-gate change.

### What ships where

| Layer | Change |
|-------|--------|
| `providers/ports/debug.py` | `GdbModule(ProviderModel)` record (`name`, `base_address`, `symbols_loaded`, `identity_verified: bool \| None` — `None`/omitted for list rows, set by load); two Protocol methods on `GdbMiEngine`; `GdbMiAttachment` gains `run_id: str` and `loaded_modules: set[str]` |
| `providers/shared/debug_common/gdbmi.py` | `list_modules` + `load_module_symbols`; injected `module_debuginfo_resolver` seam (default raises `MISSING_DEPENDENCY`, like the live attach); the module-list walk + base-field probe |
| `providers/shared/debug_common/debuginfo.py` | `ModuleDebuginfoResolver`: lazy, run-id-keyed fetch/extract of the combined `kernel_ref` tar → `ModuleDebuginfo{path, srcversion?, build_id?}` (`.ko` path + identity from `.modinfo`/`.note.gnu.build-id`; live/IO seams injected, mirroring `DebuginfoResolver`) |
| `providers/fault_inject/debug/gdb.py` | synthetic conforming `list_modules`/`load_module_symbols` |
| `mcp/tools/debug/ops.py` | two op factories + two tool registrations |
| `mcp/exposure.py`, `tool_index`, generated docs | `_CONTRIBUTOR` scopes, search vocab, `just docs` |
| `docs/adr/0278-gdb-module-symbols.md` + README index | new ADR amending ADR-0034 §7 |

### Op 1 — `debug.list_modules(session_id)`

`GdbMiEngine.list_modules(attachment, *, max_modules=MAX_MODULES) -> list[GdbModule]`.

- **Enumeration.** Read the `modules` `list_head` and walk `node = modules.next` until it
  returns to `&modules`, bounded to `max_modules`. For each node, recover
  `struct module *` via an internally-constructed `container_of` cast expression, then read
  `mod->name` and the module's base address. The `list` member offset is itself derived from
  the loaded DWARF (`&((struct module *)0)->list`), not hard-coded, so the cast is
  `(struct module *)((char *)<node> - <list-offset>)` with both `<node>` and `<list-offset>`
  numeric values the engine read from gdb. **No part of any expression is caller input** — the
  only varying tokens are numbers the engine itself read. This is the same non-injectable
  construction principle as the watch expression in ADR-0277.
- **`modules` symbol assumption.** The walk assumes `-data-evaluate-expression modules`
  resolves the kernel module-list head. `modules` is a file-scoped static; if the loaded
  DWARF makes it ambiguous or it does not resolve, the head read fails and the op raises
  `module_decode_failed` (`DEBUG_ATTACH_FAILURE`) rather than walking a wrong list — a named,
  fail-closed assumption, not a silent one.
- **Version tolerance.** The base-address field moved across kernel versions
  (`mem[MOD_TEXT].base` on ≥6.4, `core_layout.base` before that). Probe which field
  exists **once** (on the first decodable module) via a trial `-data-evaluate-expression`;
  reuse the winning field for every row. If both probes fail (no usable base field), raise
  `module_decode_failed` (`DEBUG_ATTACH_FAILURE`) rather than guessing.
- **Per-row decode failure.** A single garbage row (unreadable `name`/base on one node) does
  **not** abort the whole list: the row is skipped and counted, and the response carries a
  `decode_errors` count (mirroring the drgn `helper_modules` partial-decode contract). Only a
  failure to read the list head or the one-time base-field probe is fatal
  (`module_decode_failed`); a walk where *every* row fails to decode is also fatal.
- **Bound + truncation.** Stop at `max_modules` (512) and set `truncated=True` if the
  list is longer, exactly like `backtrace`/`disassemble`.
- **`symbols_loaded`.** Reported from `attachment.loaded_modules` (the per-attachment set
  this engine maintains), satisfying criterion 1's "when available". gdb is not queried
  for this; the engine is the source of truth for what *it* loaded this session.
- **Tool output:** `status="listed"`, `data={count, truncated, decode_errors, modules:[{name,
  base_address, symbols_loaded}]}`, next `["debug.load_module_symbols", "debug.backtrace"]`.

### Op 2 — `debug.load_module_symbols(session_id, module, expected_base?)`

`GdbMiEngine.load_module_symbols(attachment, *, module, expected_base=None) -> GdbModule`.

1. **Gate `module`** to a module-name identifier (`^[A-Za-z0-9_]+$`) **before any MI** →
   `bad_module_name` (`CONFIGURATION_ERROR`).
2. **Idempotency.** If `module` is already in `attachment.loaded_modules` (and step 3's
   staleness checks still pass), return `status="loaded"` **without** re-issuing
   `add-symbol-file`. A second `add-symbol-file` of the same objfile would add a duplicate
   symbol table (confirm is off at attach), producing ambiguous symbols; the loaded-set is
   the guard, not just a reporting field.
3. **Re-read the live base, fresh.** Re-walk the module list and find `module`'s current
   base. This is what makes a silent wrong-**address** load impossible: the engine never
   trusts a passed-in address for the load; it always loads at the address it just read. The
   re-walk uses the **same enumeration as `list_modules`** (so the same `max_modules` bound
   applies — a module enumerated beyond the bound is reported `module_not_loaded`; >512
   loaded modules is rare and signalled by `list_modules` `truncated`, see Non-goals).
   - Module not present in the live list → `module_not_loaded` (`DEBUG_ATTACH_FAILURE`):
     it was listed earlier but has since unloaded — stale.
   - `expected_base` supplied **and ≠** current base → `stale_module_address`
     (`DEBUG_ATTACH_FAILURE`): the module reloaded at a new address since the agent's
     `list_modules`; refuse rather than load at an address the agent did not intend
     (criterion 4). `expected_base` is **optional**: when omitted the load proceeds at the
     freshly-read base (no stale view to compare against), so an agent that wants the
     criterion-4 stale-address guard must thread back the base it saw — the tool docstring
     and `list_modules` next-actions steer it to do so.
4. **Resolve the `.ko`** via the injected `ModuleDebuginfoResolver` keyed on
   `attachment.run_id`: lazily fetch the combined `kernel_ref` tar (only on first load),
   extract `lib/modules/<ver>/`, and locate `<module>.ko` (matching `-`/`_` name
   variants). The resolver returns `ModuleDebuginfo{path, srcversion?, build_id?}` — the
   artifact `.ko`'s identity read from its `.modinfo` (`srcversion=`) and `.note.gnu.build-id`
   ELF note (parsed directly, no new dependency; both are emitted by `modpost`/the linker
   independently of the in-memory `struct module` config). Absent (or DWARF-less) →
   `no_module_debuginfo` (`CONFIGURATION_ERROR`) **with remediation** naming that the Run
   must be built with `CONFIG_DEBUG_INFO=y` and the module present (criterion 3).
5. **Verify binary identity (criterion 4, binary dimension).** Read the *running* module's
   identity from `struct module` — try `mod->srcversion` first (exists with
   `CONFIG_MODVERSIONS`), then `mod->build_id` (exists with `CONFIG_STACKTRACE_BUILD_ID`); a
   gdb `^error` on a missing field is caught, not fatal. Compare against the artifact `.ko`'s
   matching identity from step 4:
   - Both sides expose the **same identity kind** and the values **differ** →
     `module_binary_mismatch` (`DEBUG_ATTACH_FAILURE`), refuse **before** `add-symbol-file`:
     the artifact `.ko` is not the binary running at that address (guest-side or rebuilt
     module), so loading would silently misattribute symbols.
   - Values **match** → proceed with `identity_verified=true`.
   - Neither in-memory field exists, or the `.ko` lacks the matching one → cannot compare;
     proceed with `identity_verified=false`. This is **disclosed in the response, not silent**
     (the kernel was built without `MODVERSIONS`/`STACKTRACE_BUILD_ID`); the agent sees the
     load was not identity-checked rather than assuming it was.
6. **Load.** Issue `-interpreter-exec console "add-symbol-file <ko> 0x<base>"`. The path
   is engine-staged and MI-escaped (`_mi_path`), the base is numeric — non-injectable.
   A gdb `^error` → `add_symbol_failed` (`DEBUG_ATTACH_FAILURE`). On success record
   `module` in `attachment.loaded_modules`.
7. **Tool output:** `status="loaded"`, `data={module, base_address, symbols_loaded:true,
   identity_verified}`, next `["debug.backtrace", "debug.disassemble", "debug.list_modules"]`.

**Scope of criterion 4.** The checks above close both staleness dimensions. *Enumeration/address*
staleness: the engine never loads at a passed-in or previously-seen address, only at one it
re-reads, and refuses on `module_not_loaded` / `stale_module_address`. *Binary identity*: when
the kernel exposes a module identity (`srcversion`/`build_id`) the engine refuses a mismatched
`.ko` with `module_binary_mismatch`; when the kernel exposes none, it loads but reports
`identity_verified=false` so the unverifiable case is disclosed, never silent. A silent wrong
load — wrong address or wrong binary — is therefore impossible whenever the kernel gives the
engine the data to detect it, and transparent when it does not.

`-interpreter-exec console` is a new pattern for this engine (no prior console-exec use);
`add-symbol-file` has no native MI verb, so the console form is required. It is confined
to this one engine-constructed command.

### Error taxonomy

| code | category | when |
|------|----------|------|
| `bad_module_name` | `CONFIGURATION_ERROR` | non-identifier `module` (pre-MI) |
| `no_module_debuginfo` | `CONFIGURATION_ERROR` | `.ko` absent / no DWARF (with remediation) |
| `inferior_running` | `DEBUG_ATTACH_FAILURE` | module-list read against a running target |
| `module_decode_failed` | `DEBUG_ATTACH_FAILURE` | base-field probes fail / malformed MI eval value |
| `module_not_loaded` | `DEBUG_ATTACH_FAILURE` | requested module not in the live list (stale) |
| `stale_module_address` | `DEBUG_ATTACH_FAILURE` | `expected_base` ≠ current base (stale) |
| `module_binary_mismatch` | `DEBUG_ATTACH_FAILURE` | running module's `srcversion`/`build_id` ≠ artifact `.ko`'s |
| `add_symbol_failed` | `DEBUG_ATTACH_FAILURE` | gdb `^error` from `add-symbol-file` |

`inferior_running` matches the existing `_RUNNING_RE` reclassification the stack/watchpoint
ops use. These gdb messages and module names carry no secret, so matching/returning the
redacted text is sound (the property ADR-0275/0276/0277 rely on); records still pass the
`Redactor` before response.

## Testing (criterion 5)

Unit tests drive the engine against the scripted fake `MiController` and a fake
`ModuleDebuginfoResolver`, plus tool-level tests via `run_engine_op` on a seeded `live`
session (the established `tests/mcp/debug/test_debug_ops.py` pattern):

- **list** — scripted module-list walk over two modules (one with the `mem[MOD_TEXT]`
  layout, asserting the probe path), `symbols_loaded` reflects the loaded-set, `truncated`
  on an over-`max_modules` walk.
- **successful load** — fake resolver returns a `.ko` path + matching `srcversion`; assert
  `add-symbol-file` issued at the freshly-read base, `identity_verified=true`, and the module
  joins the loaded-set (so a follow-up `list_modules` shows `symbols_loaded=true`).
- **missing debuginfo** — resolver raises `no_module_debuginfo`; assert
  `configuration_error` + remediation in the envelope.
- **stale address** — `expected_base` differs from the scripted current base →
  `stale_module_address`; and a module absent from the list → `module_not_loaded`. Assert
  **no** `add-symbol-file` command was issued in either case.
- **binary mismatch** — scripted `mod->srcversion` differs from the fake resolver's `.ko`
  `srcversion` → `module_binary_mismatch`, and assert **no** `add-symbol-file` was issued.
- **identity unavailable** — `mod->srcversion`/`mod->build_id` both gdb-`^error` (configs
  absent) → load proceeds with `identity_verified=false`. Cover the `build_id`-fallback match
  path too (srcversion missing, build_id matches).
- **idempotent re-load** — a second `load_module_symbols` for an already-loaded module returns
  `loaded` and issues **no** second `add-symbol-file`.
- **partial decode** — a walk with one garbage row returns the decodable rows plus
  `decode_errors=1`; an all-rows-fail walk and a head/probe failure → `module_decode_failed`.
- **malformed MI** — module-list walk returns an unparseable/garbage evaluate payload (and
  the running-target `^error`) → `module_decode_failed` / `inferior_running`.

The two tools are marked `implemented` (ADR-0248/0276/0277 precedent: they ride the same
already-live-proven attach transport and are unit-tested against the scripted controller)
but are **not** added to `_LOCAL_PROVEN_DEBUG_TOOLS` until a live KVM exercise lands.

## Non-goals / accepted costs

- **No `lx-lsmod`/`lx-symbols` / build-plane change.** Adopting the kernel's gdb scripts
  would require a new published artifact + gdb auto-load enablement — a build/attach-plane
  change, out of this debug-tier-only scope. (Considered & rejected in the ADR.)
- **No per-section symbol placement.** `add-symbol-file <ko> <text-base>` places `.text`
  at the module base; precise per-section (`.data`/`.bss`) addresses are a possible
  follow-up. `.text` symbolization (backtrace/disassemble of module code) is the target.
- **Identity check degrades to disclosure, not refusal.** When the kernel exposes neither
  `mod->srcversion` (no `CONFIG_MODVERSIONS`) nor `mod->build_id` (no
  `CONFIG_STACKTRACE_BUILD_ID`), the engine cannot compare and loads with
  `identity_verified=false` rather than refusing — refusing would make the feature unusable on
  such kernels. The unverifiable case is disclosed in the response, never silent.
- **`list_modules` is O(modules) MI round-trips** (a Python-side list walk), bounded by
  `MAX_MODULES`. Acceptable: module lists are small-to-moderate and the op is synchronous
  and bounded, like the other read ops.
- **The struct-walk is kernel-version-coupled.** Mitigated by probing the base field; the
  live correctness is deferred (tools not in the live-proof set), matching the sibling
  precedent.
- No schema, migration, persistence, RBAC, or destructive-op-gate change.
