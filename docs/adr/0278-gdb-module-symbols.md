# ADR-0278: typed gdb module-symbol loading for live gdbstub sessions (#923)

- Status: Accepted
- Date: 2026-06-29

## Context

The gdb-MI debug tier (ADR-0034, extended by ADR-0248 for `&<identifier>` symbol
resolution, ADR-0275 for stack walking, ADR-0276 for disassembly, and ADR-0277 for write
watchpoints) drives a live gdbstub `DebugSession` through a persistent
`gdb --interpreter=mi3` engine. The Run's `vmlinux` is loaded at attach, so every op works
over **core kernel** symbols — but the moment the code under investigation lives in a
loadable module, addresses resolve to bare hex and module-text backtrace frames are
anonymous, because gdb has no symbols for module code. ADR-0034 §7 explicitly dropped v1's
`load_module_symbols` from the M0 port and deferred it to "later introspection/postmortem
milestones"; #923 is that milestone, asking for: list loaded modules (name, base, whether
symbols are loaded), load symbols for one module through a structured tool, a
`configuration_error` when module debuginfo is missing, and a categorized failure on stale
module/address data rather than silently loading wrong symbols.

A QEMU gdbstub is kernel-agnostic — raw memory + registers, nothing Linux-aware. The attach
runs `gdb --nx` and loads only `vmlinux`, so there is no `lx-lsmod`/`lx-symbols` to lean on.
Enumerating loaded modules and their load addresses means reading the kernel's own
`struct module` list out of guest memory, and loading symbols means
`add-symbol-file <module.ko> <base>` with both a base address and the module's `.ko` DWARF.
The build already publishes the combined `kernel_ref` tar carrying DWARF-bearing `.ko`
files (`CONFIG_DEBUG_INFO=y`), so the debuginfo source is in hand; what is new is reading
the live module table and loading the symbols safely.

## Decision

Add two ops to the **shared** `GdbMiEngine` (so both local-libvirt and remote-libvirt gain
them) plus two MCP tools, lifting ADR-0034 §7's module-loading exclusion in the same narrow,
non-injectable way ADR-0248/0275/0276/0277 lifted the earlier exclusions: every gdb command
is *constructed* from validated, gated inputs — a gated identifier, a numeric pointer the
engine itself read, an engine-staged file path — never accepted as caller text.

1. **`GdbMiEngine.list_modules(attachment, *, max_modules=MAX_MODULES) -> list[GdbModule]`.**
   Walk the kernel `modules` `list_head` from `modules.next` until it returns to `&modules`,
   bounded to `max_modules` (512, `truncated` when longer — the ADR-0275/0276 response-bound
   convention). For each node recover `struct module *` via an internally-constructed
   `container_of` cast and read `mod->name` plus the module base. The base-address field
   moved across kernels (`mem[MOD_TEXT].base` on ≥6.4, `core_layout.base` before), so the
   engine probes which field exists **once** and reuses it; both probes failing raises
   `module_decode_failed`. `symbols_loaded` is reported from a per-attachment
   `loaded_modules` set the engine maintains (the engine is the source of truth for what it
   loaded this session) — gdb is not queried for it, satisfying "whether symbols are loaded
   when available". No expression token is caller input; the only varying token is a numeric
   pointer the engine just read, so the walk is non-injectable by construction.

2. **`GdbMiEngine.load_module_symbols(attachment, *, module, expected_base=None) ->
   GdbModule`.** Gate `module` to a module-name identifier (`bad_module_name`) before any
   MI. Then **re-read the module's current base, fresh** — the engine never loads at a
   passed-in address, only at the address it just read, so a silent wrong-address load is
   impossible by construction. A module absent from the live list is `module_not_loaded`
   (it was listed earlier and has since unloaded — stale); an `expected_base` that differs
   from the freshly-read base is `stale_module_address` (the module reloaded at a new
   address since the agent's `list_modules`, so refuse rather than load where the agent did
   not intend). Resolve the `.ko` via an injected `ModuleDebuginfoResolver` keyed on
   `attachment.run_id` (lazy, run-id-keyed fetch/extract of the combined `kernel_ref` tar →
   the `.ko` path); an absent or DWARF-less module is `no_module_debuginfo`
   (`CONFIGURATION_ERROR`) **with remediation**. Load via
   `-interpreter-exec console "add-symbol-file <ko> 0x<base>"` — `add-symbol-file` has no
   native MI verb, the path is engine-staged + MI-escaped (`_mi_path`), the base numeric, so
   the console command is non-injectable; a gdb `^error` is `add_symbol_failed`. On success
   record `module` in `loaded_modules`.

3. **`^error` classification.** A module-list read against a running target reuses the
   existing `_RUNNING_RE` reclassification (`inferior_running`, fixed with
   `debug.interrupt`) the stack/watchpoint ops already apply. These gdb messages and module
   names carry no secret, so matching the redacted text is sound (the ADR-0275/0276/0277
   property); records still pass the `Redactor` before response.

4. **Two MCP tools.** `debug.list_modules` (read-only), `debug.load_module_symbols`
   (mutating — it changes the engine's symbol state), each `contributor` RBAC, gated to a
   `live` `DebugSession` by the shared `run_engine_op` (per-session lock, attach-once, audit,
   off-loop). `list` → `status="listed"`, `data={count, truncated, modules:[{name,
   base_address, symbols_loaded}]}`, next `["debug.load_module_symbols", "debug.backtrace"]`;
   `load` → `status="loaded"`, `data={module, base_address, symbols_loaded:true}`, next
   `["debug.backtrace", "debug.disassemble", "debug.list_modules"]`.

5. **Wiring.** Add `GdbModule(ProviderModel)` and the two Protocol methods to
   `providers/ports/debug.py`; extend `GdbMiAttachment` with `run_id: str` and
   `loaded_modules: set[str]`; add a synthetic conforming implementation to
   `FaultInjectDebugEngine`; add `ModuleDebuginfoResolver` beside `DebuginfoResolver` in
   `debug_common/debuginfo.py` (IO seams injected, unit-tested with fakes); update the tier
   docstrings (including the shared-engine "module loading remains outside" line, now lifted),
   `exposure.py` `_TOOL_SCOPES` (`_CONTRIBUTOR`), `tool_index.py` search vocabulary, and
   regenerate the tool reference (`just docs`). The new tools are marked `implemented`,
   matching the ADR-0248/0276/0277 precedent (they ride the same already-live-proven attach
   transport and are unit-tested against the scripted controller); they are **not** added to
   `_LOCAL_PROVEN_DEBUG_TOOLS` until a live exercise lands.

No schema, migration, persistence, RBAC, or destructive-op-gate change; the MCP surface
change is additive.

## Consequences

- An agent debugging a live kernel can list loaded modules and load a module's symbols as
  structured, redacted data over the gdbstub with no raw gdb command text and no
  `drgn`/credential path — so `backtrace`, `disassemble`, `resolve_symbol`, and breakpoints
  all gain module-code visibility after one `load_module_symbols`.
- ADR-0034's expression-evaluation exclusion stays intact: the module-list walk and the
  watch/add-symbol commands are constructed from gated identifiers, engine-read numeric
  pointers, and engine-staged paths, never a caller expression.
- A silent wrong-address load is impossible: `load_module_symbols` always loads at the base
  it re-reads at call time, and surfaces `module_not_loaded` / `stale_module_address` when
  the agent's view is stale.
- Remote-libvirt inherits both ops for free (shared engine), matching the rest of the tier.
- Failures are categorized with `data["code"]` discriminators (`bad_module_name`,
  `no_module_debuginfo`, `inferior_running`, `module_decode_failed`, `module_not_loaded`,
  `stale_module_address`, `add_symbol_failed`) so an agent can branch on the cause.
- The new tools are covered by the `exposure.py` completeness guard, the
  `_BEHAVIOR_TESTS_BY_TOOL` coverage guard, the `tool_index` completeness guard, and the
  `just docs` generated-reference gate.
- `list_modules` is O(modules) MI round-trips (a bounded Python-side list walk), and the
  struct-walk is kernel-version-coupled (mitigated by the one-time base-field probe). Both
  costs are accepted: the op is synchronous and bounded like the other read ops, and live
  correctness is deferred with the tools held out of the live-proof set (the
  ADR-0248/0276/0277 precedent).

## Considered & rejected

- **Adopt the kernel's gdb helper scripts (`lx-lsmod`/`lx-symbols`).** The most
  version-correct path (the scripts ship with the kernel, compute per-section addresses, and
  read the live list each call). Rejected for this issue: it requires a **build-plane**
  change — `CONFIG_GDB_SCRIPTS=y`, publishing `scripts/gdb` as a new artifact, staging it at
  attach, and enabling gdb **auto-load** (dropping the `--nx` posture) — plus a new
  auto-load security surface, all outside the debug-tier-only scope the sibling ADRs
  (0275/0276/0277) established. A future ADR may revisit it for per-section precision.
- **Use `lx-lsmod`/`lx-symbols` via `-interpreter-exec` but defer publishing the scripts.**
  Rejected: the feature would not work live until a separate issue publishes the scripts —
  a phantom feature that ships code that cannot run.
- **A caller-supplied module base or load command.** Rejected: trusting a caller base is
  exactly the silent-wrong-load the issue forbids, and a caller command reopens the
  arbitrary-gdb surface ADR-0034 excluded. The engine re-reads the base and constructs the
  command itself.
- **Stage every module's debuginfo at attach.** Rejected: it fetches/extracts the whole
  combined tar for every debug session even when no module symbols are loaded. The injected
  `ModuleDebuginfoResolver` fetches lazily on first `load_module_symbols`, keyed and cached
  on `run_id`.
- **Query gdb for `symbols_loaded` (e.g. parse `info files`).** Rejected: the engine is the
  authority for what it `add-symbol-file`'d this session; a per-attachment `loaded_modules`
  set is simpler and deterministic than parsing objfile listings, and "when available" in
  the criterion scopes the signal to what the engine knows.
- **Per-section symbol placement (`.data`/`.bss` addresses).** Rejected for this issue:
  `add-symbol-file <ko> <text-base>` symbolizes module `.text` (the backtrace/disassemble
  target). Precise per-section placement is a clean additive follow-up.
- **A local-libvirt-only op.** Rejected: the engine is shared and remote-libvirt has the
  same gap; placing the ops on `GdbMiEngine` follows the tier's existing seam (as
  ADR-0248/0275/0276/0277 did).
