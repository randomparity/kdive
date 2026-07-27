# ADR 0463 — Consolidate the debug step variants into `debug.advance`

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1584
- **Epic:** #1576
- **Implements:** [ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §3's retired-name
  search vocabulary, through the `RETIRED_TOOL_NAMES` mechanism
  [ADR-0458](0458-fold-postmortem-triage-into-postmortem-crash.md) §2 landed.
- **Amends:** [ADR-0379](0379-gdb-source-and-instruction-stepping.md), which added the four
  stepping tools this change folds into one. The capabilities themselves are unchanged; only
  the tool surface is.

## Context

`debug.step`, `debug.next`, `debug.step_instruction`, and `debug.finish` were four tools over
one shape. Every precondition epic #1576 requirement 5 sets for a parameter consolidation
matched:

- **authorization** — all four `_CONTRIBUTOR` in `exposure.py`, all four gated by the same
  `run_engine_op_with_resolver` session gate (contributor + project + `live`);
- **annotations** — all four `_docmeta.mutating()` with the shared `_gdbmi_maturity()` meta;
- **execution class** — all four synchronous, none job-returning;
- **result shape** — all four `ToolResponse.success(session_id, "stopped", data=_stop_data(...))`.

The parameters matched too: each wrapper took exactly `session_id: str, timeout_sec: float = 0.0`,
and the four backing `GdbMiEngine` methods share one signature,
`(attachment, *, timeout_sec: float) -> GdbStopRecord`. ADR-0379 had already routed all four
through the verb-generic `ExecutionControl.resume`, so the only real difference left was the
gdb-MI command string and the prose guidance about when to pick each one.

`debug.continue` and `debug.interrupt` are *not* folded in. `interrupt` has no `timeout_sec`
parameter and returns `GdbStopRecord | None`, a different result contract; `continue` resumes
indefinitely rather than to a bounded stop. Neither is one more value of "how far to advance".

## Decision

### 1. One tool, `debug.advance(session_id, mode, timeout_sec)`

`mode` is a closed enum — `into`, `over`, `instruction`, `out` — mapped by `_ADVANCE_CALLS` to
`engine.step`, `engine.next`, `engine.step_instruction`, and `engine.finish` respectively. All
four old wrappers and all four names are removed in this change; there is no alias and no
deprecation period. The registry drops from 133 tools to 130.

The mode names describe the *unit of advance* rather than restating the gdb command names. `into`
and `over` say what distinguishes them (whether calls are entered), which the old `step`/`next`
pair did not; `out` is the plain-language reading of `finish`.

Params are flat top-level arguments, not a `request` wrapper, per
[ADR-0372](0372-flat-params-for-mutation-tools.md). `mode` is declared as a `Literal`, so the
schema carries an inline `enum` — that is what makes the four values searchable text for
`tools.search` (epic requirement 4) and what makes the generated `kdivectl` verb emit an argparse
`choices=` instead of a free-form string.

No per-mode parameters exist, so the schema needs no conditional or discriminated branch. What
*is* per-mode is the guidance: `instruction` is the fallback where the code has no debug symbols,
and `out` needs a frame that can return. Both live in the wrapper docstring and the `mode` `Field`
description, since those are the agent-facing contract (epic requirement 7).

### 2. Authorization, annotations, and gating are uniform across modes

There is no branch-dependent authorization to declare or test: `_TOOL_SCOPES` carries one
`debug.advance` → contributor entry and every mode runs the identical session gate. Requirement 5's
"branch-dependent authorization must be explicit and tested" clause is satisfied vacuously, which
is itself the reason this consolidation is allowed.

### 3. The mode is folded into the audit transition, not just the args

The Debug-plane audit descriptor derives `transition` from the tool name
(`tool.removeprefix("debug.")`). Left alone, four transitions — `step`, `next`,
`step_instruction`, `finish` — would have collapsed into a single `advance`, weakening audit
attribution in exactly the way epic requirement 10 forbids.

`_op_audit` therefore takes an optional positional-only `transition` override, and `debug.advance`
passes `f"advance:{mode}"`:

| call | audit `tool` | audit `transition` |
| --- | --- | --- |
| `debug.advance(mode="into")` | `debug.advance` | `advance:into` |
| `debug.advance(mode="over")` | `debug.advance` | `advance:over` |
| `debug.advance(mode="instruction")` | `debug.advance` | `advance:instruction` |
| `debug.advance(mode="out")` | `debug.advance` | `advance:out` |

One audit row per distinct operation, as before. The override is positional-only so it can never
collide with an audited op parameter in `**args`. `mode` is *also* recorded in `args` for
`args_digest` correlation; the transition is what a query filters on. All four transitions are
pinned by a parametrised test that drives the registered wrapper end-to-end.

`_AUDITED_OPS` is swept in the same change: the four old names are removed and `debug.advance`
added. That set is a silent failure mode — a stale name there stops audit rows with no error at
all — so an equality assertion pins the whole set rather than membership of the new name.

### 4. One converged `suggested_next_actions` list for every mode

`finish` used a three-entry breadcrumb list (`read_registers`, `backtrace`, `continue`) while the
other three shared a four-entry list that also offered `debug.step`. The merged tool converges on
one list for all four modes:

```
["debug.read_registers", "debug.backtrace", "debug.advance", "debug.continue"]
```

Converging rather than branching on mode, because the entry `finish` omitted was the stepping tool
itself, and after an `out` the target is stopped in the caller frame — as steppable as any other
stop, so offering `debug.advance` there is correct rather than merely harmless. Branching an
advisory field on `mode` would reintroduce, inside one tool, the per-variant divergence this
consolidation exists to remove. The list is asserted exactly (not just non-empty) for every mode.

This list is also the change's one *blocking* sweep site: `visible_next_actions` raises
`ValueError` on an unregistered tool name, so leaving `debug.step` in it would have made every
`debug.advance` response raise.

### 5. Retired names as `tools.search` vocabulary

Four rows are added to `RETIRED_TOOL_NAMES`, all pointing at the same replacement, which the
existing inversion into `_RETIRED_BY_REPLACEMENT` already groups:

```python
"debug.finish": "debug.advance",
"debug.next": "debug.advance",
"debug.step": "debug.advance",
"debug.step_instruction": "debug.advance",
```

The mechanism itself is unchanged. This namespace needs the vocabulary more than most: the
surviving name spells neither "step" nor "finish", and the mode enum contributes `into`, `over`,
`instruction`, and `out` but none of the verbs an agent is most likely to type. The curated
`TOOL_KEYWORDS` entry for `debug.advance` therefore carries the union of the four old keyword
sets — `step`, `next`, `finish`, `return`, `stepi`, `asm`, `single-step` — and a test pins that
each retired name plus the intent phrases "step over", "step into", "finish frame", and "stepi"
all rank `debug.advance`.

## Consequences

- The `debug` namespace goes from 24 tools to 21; the live registry from 133 to 130.
- `kdivectl debug step|next|step-instruction|finish` become
  `kdivectl debug advance --session-id <id> --mode <into|over|instruction|out>`. The generated
  verb descriptors are regenerated, not hand-edited; `--mode` derives argparse `choices` from the
  inline enum, the same path the other enum-valued flags already take.
- The `GdbMiEngine` port keeps its four separate methods. The consolidation is a tool-surface
  change; the provider contract, the gdb-MI commands, and their per-verb error handling are
  untouched, and both libvirt providers are unaffected.
- An unknown `mode` is rejected by the schema enum before the handler runs. A direct in-process
  caller that bypasses the schema gets a `configuration_error` failure envelope rather than a
  bare `KeyError` out of the engine thread.
- `scripts/live-debug.py step` — the only real exercise of stepping, since the live gdb-MI smoke
  test deliberately skips it (the panic path parks the CPU in `hlt`, which is not steppable) — is
  rewritten to the mode enum and was run against a live local-libvirt kernel for this change. All
  four modes advanced `rip` from a breakpoint in `vfs_read`, `mode=out` returned with
  `reason=function-finished` and `timed_out=false`, and the live `audit_log` held exactly one row
  each for `advance:into`, `advance:over`, `advance:instruction`, and `advance:out`.
- Reaching that proof required repairing three defects in the script that predate this change: two
  stale `request` wrappers on tools that had been flattened, and — the one that mattered — an
  upload lane that published only the combined kernel tar and never the vmlinux ELF, leaving the
  Run's `debuginfo_ref` NULL so every gdb-MI op short-circuited with `no_debuginfo`. The stepping
  surface ADR-0379 added had therefore never been exercised live.
- An agent calling `debug.step` gets the unknown-tool `configuration_error`; `tools.search` is the
  recovery path.

## Rejected alternatives

- **Folding in `debug.continue` and `debug.interrupt`.** Different result contracts and, for
  `continue`, a different notion of where execution stops. Requirement 5 forbids consolidating
  across result shapes. See Context.
- **Keeping the four names as aliases.** The project is pre-release and follows
  replace-don't-deprecate; an alias keeps four names in the catalog, the RBAC matrix, the
  generated CLI, and the served docs for no capability.
- **Leaving the audit `transition` as a bare `advance` and reading the mode out of `args`.** The
  mode is *the* operation here, and `args` is digested for correlation rather than filtered on. A
  flat `advance` would make "how many instruction-steps ran against this session" unanswerable
  from the transition column. A sibling consolidation put its discriminator in `args` alone, but
  there the discriminator was already a required audited parameter and the audit carried no
  derived `transition` field.
- **Branching `suggested_next_actions` on mode to preserve `finish`'s shorter list.** See decision
  4 — the omitted entry is valid after an `out`, and per-mode branching of an advisory field
  reintroduces the divergence being removed.
- **A `count` parameter for repeated stepping.** ADR-0379 rejected a step-count parameter and this
  change does not revisit it; nothing about the merge makes the case for one stronger.
- **A `StrEnum` instead of a `Literal` for `mode`.** Pydantic renders a `StrEnum` parameter as a
  `$ref` into `$defs`, which the CLI verb generator's scalar derivation does not resolve — the
  `--mode` flag would silently lose its `choices` (or the flag itself). A `Literal` inlines the
  `enum` where both the generator and the `tools.search` schema walker already read it.
