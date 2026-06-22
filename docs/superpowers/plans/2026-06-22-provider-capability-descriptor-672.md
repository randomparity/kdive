# Plan — A1 provider-neutral capability descriptor on `ProviderRuntime` (#672)

- **Issue:** #672 (M2.8 Epic A, A1)
- **ADR:** [ADR-0208](../../adr/0208-provider-capability-descriptor.md) (already on main)
- **Spec:** [honesty](../specs/2026-06-22-local-libvirt-service-parity-honesty.md)
- **No schema/migration.** No new ADR.

## Where this fits

A1 is the foundation of Epic A. It generalizes the one-off `supported_capture_methods`
field on `ProviderRuntime` into a uniform, fail-closed capability descriptor that the
surface reads to tell an agent "what can *this* System do?" A2 (#…, admission) and all of
Epic B build on it. A1 is read-only honesty: no behavior change beyond reporting and the
fail-closed default flip.

## Capability vocabulary

- `DebugTransportKind` — **existing** `Literal["gdbstub", "drgn-live"]` in
  `providers/ports/lifecycle.py`. Reused, not introduced.
- `IntrospectionMode` — **new** `Literal["offline-vmcore", "live"]`, added to
  `providers/ports/lifecycle.py` next to `DebugTransportKind`, re-exported from
  `providers/ports/__init__.py`. (`lifecycle.py` already owns `DebugTransportKind`; co-locating
  keeps the two capability literals together and avoids a new module.)
- `supported_capture_methods` — existing `frozenset[CaptureMethod]`; stays the authority for
  which core-producing methods `vmcore.fetch` admits.

## Tasks

### Task 1 — descriptor fields + fail-closed defaults on `ProviderRuntime`

Files: `src/kdive/providers/ports/lifecycle.py`, `src/kdive/providers/ports/__init__.py`,
`src/kdive/providers/core/runtime.py`, `tests/providers/test_composition.py` (the unconfigured
assertion), `tests/mcp/core/test_provider_runtime_boundaries.py` if it asserts the old default.

- Add `IntrospectionMode = Literal["offline-vmcore", "live"]` and
  `INTROSPECTION_MODES: frozenset[IntrospectionMode]` to `lifecycle.py`, mirroring the existing
  `DebugTransportKind` / `DEBUG_TRANSPORT_KINDS` pair. Re-export both from `ports/__init__.py`
  `__all__`.
- On `ProviderRuntime` add two fields next to `supported_capture_methods`:
  - `supported_debug_transports: frozenset[DebugTransportKind] = frozenset()`
  - `supported_introspection: frozenset[IntrospectionMode] = frozenset()`
- **Flip** `supported_capture_methods`' default from `frozenset(CaptureMethod)` to
  `frozenset()` (a plain default, not a `default_factory`, since an empty frozenset is
  immutable and shared-safe). Keep field ordering valid for the frozen dataclass (all three are
  defaulted, so order among them is free; they stay before the other defaulted fields they
  already precede / follow consistently).
- Import `DebugTransportKind` and `IntrospectionMode` into `runtime.py` from
  `kdive.providers.ports`.

TDD: failing test first — `tests/providers/test_runtime_descriptor.py` (new):
- an unconfigured `ProviderRuntime(...)` built with only the required ports reports
  `frozenset()` for **all three** capability fields (`supported_capture_methods`,
  `supported_debug_transports`, `supported_introspection`).

Acceptance: the three fields exist; unconfigured runtime reports empty for every one.

### Task 2 — populate the descriptor in all three providers' `composition.py`

Files: `src/kdive/providers/local_libvirt/composition.py`,
`src/kdive/providers/remote_libvirt/composition.py`,
`src/kdive/providers/fault_inject/composition.py`, and their composition tests.

- **local** — narrow `supported_capture_methods` to `frozenset({CaptureMethod.KDUMP})`
  (drop CONSOLE/HOST_DUMP/GDBSTUB). `supported_debug_transports=frozenset()` and
  `supported_introspection=frozenset()` (Epic B fills these).
- **remote** — keep `supported_capture_methods` as the current 4-method set; add
  `supported_debug_transports=frozenset({"gdbstub", "drgn-live"})` and
  `supported_introspection=frozenset({"offline-vmcore", "live"})` (it wires both
  `vmcore_introspector` and `live_introspector` real ports + gdbstub/drgn-live connect today).
- **fault-inject** — keep its current `supported_capture_methods`
  (`{CONSOLE, HOST_DUMP, GDBSTUB}`); add
  `supported_debug_transports=frozenset({"gdbstub", "drgn-live"})` (verified: `FaultInjectConnect`
  accepts the full `DEBUG_TRANSPORT_KINDS`) and
  `supported_introspection=frozenset({"offline-vmcore", "live"})` (verified: `FaultInjectIntrospect`
  realizes both `from_vmcore` and `introspect_live`).

TDD: failing tests first, then populate:
- update `tests/providers/local_libvirt/test_composition.py` and
  `tests/providers/test_capture_capabilities.py` to assert local's narrowed
  `supported_capture_methods == frozenset({KDUMP})` and the two empty sibling sets.
- update `tests/providers/test_composition.py` remote assertions: add the debug/introspection
  set assertions (keep the existing 4-method capture assertions).
- add a fault-inject descriptor assertion (in `tests/providers/fault_inject/test_composition.py`
  or `test_composition.py`).

**Verify fault-inject's real transports/introspection** by reading `FaultInjectConnect`,
`FaultInjectIntrospect`, and `fault_inject_attach_seam` before asserting — do not assume.

Acceptance: each provider reports its true sets; an honest local reports
`{KDUMP}` capture + empty debug/introspect.

### Task 3 — project the descriptor through `resources.describe`

Files: `src/kdive/mcp/tools/catalog/resources.py`, `tests/mcp/catalog/test_resources_tools.py`.

- In `describe_resource`, after the envelope is built and the resource is visible, resolve the
  bound provider runtime via the already-passed `resolver` (`resolver.resolve(resource.kind)`,
  then `.for_resource(resource.name)` when `name` is not None — mirroring `_runtime_staged_probe`).
  If `resolver` is None or resolution raises `CategorizedError`, **omit** the capability block
  (degrade, never fail the describe — same contract as the staged probe).
- Project a provider-neutral `capabilities` list of supported plane tokens derived **only** from
  the descriptor + the universal ports, with **no `ResourceKind` branching**:
  - `"build"`, `"boot"` — always present (every runtime has `builder`/`booter`).
  - `"kdump"` — iff `CaptureMethod.KDUMP in supported_capture_methods`.
  - `"host-dump"` — iff `CaptureMethod.HOST_DUMP in supported_capture_methods`.
  - `"debug"` — iff `supported_debug_transports` is non-empty.
  - `"introspect"` — iff `supported_introspection` is non-empty.
  Emit as `envelope.data["capabilities"]` = sorted list of present tokens, plus the raw sets for
  precision: `data["supported_capture_methods"]`, `data["supported_debug_transports"]`,
  `data["supported_introspection"]` as sorted lists of their string values.

  Decision: a flat sorted `capabilities` token list satisfies the acceptance ("reports a local
  System as build/boot/kdump and NOT debug/introspect/host-dump") and is provider-neutral. The
  raw sets give an agent the exact transports/methods without re-deriving them.

TDD: failing tests first —
- local System describe (with a resolver wired to the real local runtime, or a fake runtime with
  `supported_capture_methods={KDUMP}` and empty sibling sets) → `capabilities` contains
  `build, boot, kdump` and **not** `debug, introspect, host-dump`.
- remote System describe (fake runtime with the remote descriptor) → `capabilities` contains the
  full set (`build, boot, kdump, host-dump, debug, introspect`).
- describe with `resolver=None` → no `capabilities` key (degraded, describe still `available`).

Acceptance: per-System capability projected; local partial / remote full; no `ResourceKind`
branch in the projection.

### Task 4 — fix all fallout from the default flip

Search every `ProviderRuntime(...)` construction and every assertion of
`supported_capture_methods` (`rg "supported_capture_methods"`). Test fixtures that relied on the
fail-open default and assert specific membership must set the field explicitly. Functional uses
that never read the field are unaffected. Run the **full** suite, not just touched dirs.

Known fallout from narrowing local to `{KDUMP}`:
- `scripts/m2_portability_gate.py` — `CAPTURE_COVERAGE["local-libvirt"]` is a pinned constant
  asserted by `tests/scripts/test_m2_portability_gate.py` to equal the real advertised set.
  Narrow it to `frozenset({"kdump"})` and update the surrounding "both providers advertise all
  four" comment + the `"kdump" in local` assertion stays true. The script is stdlib-only; the
  test imports the real builder and fails on drift, so the constant MUST track the narrowing.
- `tests/providers/test_capture_capabilities.py`,
  `tests/providers/local_libvirt/test_composition.py` — assert the new `{KDUMP}` set.

## Guardrails (run directly, not via `just ci | tail`)

- `just lint`
- `just type` (if ty diverges ONLY from local live-deps in unrelated modules, neutralize per
  repo convention and restore; new typing must be clean)
- `just test`
- `just resources-docs-check` (the resources projection feeds generated resource docs)

Run the full suite before the first push. Zero warnings.

## Rollback / cleanup

Pure additive on the runtime + read-only projection; revert is `git revert` of the branch. No
data migration, no persisted state. Remove the external worktree after merge.

## File-overlap with #673

#673 owns `_docmeta.py` maturity rows, the `test_tool_docs` maturity guard, and
`docs/guide/reference/*` maturity content. This plan touches none of those. If
`just resources-docs-check` regenerates a resources reference row #673 also touches, keep the
edit minimal and flag it in the report.
