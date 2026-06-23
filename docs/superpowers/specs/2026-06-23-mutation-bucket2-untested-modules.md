# Mutation bucket 2 — direct unit tests for the untested modules (#665)

- **Type:** test coverage + developer tooling · **Tool:** [mutmut 3.6.0](https://mutmut.readthedocs.io/) · **No CI gate, no new locked dependency.**
- **Follow-up to:** #664 (repo-wide sweep) / [mutation-sweep-status.md](../../development/mutation-sweep-status.md)
- **ADR:** [0229](../../adr/0229-mutation-shim-fold-in.md) (tooling fold-in)

## Goal

Close bucket 2 of #665: the source modules that **no test imports directly**, so mutmut's
`mutate_only_covered_lines=true` finds nothing to mutate. For each, add a direct unit test that
pins the module's behavior, then run `just mutate` and kill every surviving non-equivalent mutant.
Also fold the two manual `just mutate` workarounds (the beartype.claw eager-import shim and
`UV_NO_SYNC=1`) into the recipe so future sweeps need no manual `PYTHONPATH` setup.

## Why these modules are invisible to the sweep

`scripts/mutate.py` renders `mutate_only_covered_lines=true`. mutmut only generates mutants on
lines a covering test *executes*. A module that no test imports directly is reached only
transitively (or not at all), so mutmut generates zero mutants for it — it is untested *and*
unmutatable. The fix is a direct unit test; that is the prerequisite, not the whole of it.

## Scope: the module set

The set is reproducible: a module under `src/kdive/` is in scope when no file under `tests/`
imports it (by dotted path) via `import`/`from … import`. Running that analysis on `main` today
yields **25** modules (the issue's "22" was approximate; `config/manifest.py` has since gained a
direct test, and the analysis surfaces a few small contract modules the issue folded into "~9
remaining"). The 25 split by **mutable surface**, which determines the Definition of Done per
module:

### 2a. Modules with behavioral/data surface — test + mutate to 0 survivors (19)

| module | surface a mutant can break |
|---|---|
| `mcp/middleware/binding_errors.py` | binding-error → envelope conversion logic |
| `mcp/middleware/denial_audit.py` | denial audit record construction |
| `mcp/middleware/exposure.py` | RBAC tool-exposure filtering |
| `mcp/middleware/shared.py` | `ToolOutcome` enum + outcome/category extraction |
| `mcp/middleware/telemetry.py` | metric labelling / emission |
| `mcp/middleware/usage.py` | usage-row construction |
| `mcp/tools/ops/_reads.py` | ops read-projection helpers |
| `providers/local_libvirt/lifecycle/rootfs_catalog_fetch.py` | env-driven catalog fetch helper |
| `services/runs/admission.py` | run-admission gating (largest; 30 defs) — see caveat below |
| `services/runs/bind.py` | run→system bind logic — see caveat below |
| `inventory/_row_typing.py` | `RowTyper` isinstance-narrowing validators |
| `services/runs/states.py` | run lifecycle `frozenset` state sets |
| `domain/lifecycle/rules.py` | non-terminal/terminal state tuples + sets |
| `domain/catalog/ownership.py` | `ManagedBy` enum string values |
| `providers/fault_inject/settings.py` | `Setting` defaults / `secret=True` / processes |
| `providers/local_libvirt/settings.py` | `Setting` defaults / processes |
| `providers/remote_libvirt/settings.py` | `Setting` defaults / processes |
| `providers/shared/build_timeouts.py` | `SLOW_BUILD_TOOL_TIMEOUT_S = 30 * 60` arithmetic |
| `domain/catalog/image_format.py` | `SUPPORTED_IMAGE_FORMATS` tuple |

### 2b. Near-contract modules — structural pin test, expect ~0 mutants (6)

These have little or no mutable runtime surface (pure `type` aliases, a bare `Exception`
subclass, `NewType`/`TypedDict`, field-only frozen dataclasses / Pydantic bases). mutmut generates
~0 mutants; the test pins the contract (the type/field/exception identity exists and behaves) so
the module is *covered*, and any survivor is recorded as equivalent with a reason.

| module | contract pinned |
|---|---|
| `db/probe_fence.py` | `ProbeInFlightError` is an `Exception`; `__all__` |
| `providers/ports/handles.py` | `SystemHandle`/`TransportHandle` NewTypes; `OwnedInfra` keys |
| `domain/_records.py` | `DomainModel` fields; `extra="forbid"` / `validate_assignment` behavior |
| `diagnostics/provider_contracts.py` | descriptor dataclass field sets + frozenness |
| `domain/profile_documents.py` | document alias exports exist |
| `profiles/types.py` | profile-input alias exports exist |

> Where 2b modules turn out to carry a real mutant (e.g. `extra="forbid"` on `domain/_records`,
> the enum/tuple in `image_format`), they are treated as 2a — the split is a starting hypothesis,
> the per-module mutate run is the arbiter.

#### Caveat: `admission.py` / `bind.py` may not be bucket-2 targets at all

Both take an `AsyncConnectionPool`/`AsyncConnection`, hold advisory locks, and run real
`dict_row` SQL via `db/repositories.py`; the sweep status doc placed `services/` in bucket 1
(Postgres-backed) and named deep-`asyncio.run`-frame service modules as bucket 3 ("no
covered/mutatable lines" under `max_stack_depth=8`). They land here only because no test imports
them *directly* today — a different fact from being unit-mutatable. Before committing them to
bucket 2, **trial-mutate first**: write a minimal direct test and run `just mutate`. If mutmut
reports **zero mutants generated** (the covered gating lines sit deeper than `max_stack_depth=8`
under `asyncio.run`, or are only reachable through Postgres), the module is reclassified to
bucket 1 or 3 with a recorded reason in `mutation-sweep-status.md` rather than forced into a
brittle no-Postgres fake. The DoD (below) is written to admit that outcome so "Closes bucket 2"
stays falsifiable.

## Definition of done

1. Every module in the set has at least one test that **imports it directly** and asserts
   behavior (not just `import`-smokes it, except where there is genuinely no runtime surface).
2. For each in-scope module, **either** `just mutate <module> <new-test>` reports **0 surviving
   non-equivalent mutants** (run with the folded-in tooling — no manual `PYTHONPATH`; any retained
   survivor listed in `mutation-sweep-status.md` with an equivalence reason), **or** a trial
   mutate showed the module is not unit-mutatable here (zero mutants generated under
   `max_stack_depth=8`, or only Postgres-reachable) and it is **reclassified to bucket 1 or 3 with
   a recorded reason** in `mutation-sweep-status.md`. A module is never left silently uncovered.
3. The two `just mutate` workarounds are applied automatically by `scripts/mutate.py` (ADR-0229);
   the harness's behavioral tests cover the new env/shim logic.
4. `mutation-sweep-status.md` is updated: bucket 2 moves from deferred to done, with the residual
   survivor/equivalence notes.
5. `just ci` is green (lint, type whole-tree, tests).

## Failure modes / edge cases the tests must cover

- **`RowTyper`**: each validator's reject path (wrong type, `None` for required, `bool`-as-`int`
  trap) — the isinstance guards are the surface, not the happy path alone.
- **`Setting` modules**: assert each `Setting`'s `default`, `name`, `processes`, and `secret`
  flag; a mutated default or a flipped `secret` must fail a test (secret-flag mutation is a
  redaction-correctness risk, not cosmetic).
- **State-set modules** (`runs/states`, `lifecycle/rules`): assert exact membership; a mutant that
  drops or swaps a state must fail.
- **`admission` / `bind`**: drive the gating logic with injected dependencies at the unit boundary
  (no transport, no Postgres) per CLAUDE.md; cover the reject/deny branches, not just admit.
- **Tooling**: the env builder prepends (not replaces) `PYTHONPATH`, sets `UV_NO_SYNC=1`, and the
  generated `sitecustomize.py` eagerly imports the beartype/multiprocessing modules. The shim dir
  is a **per-run unique** directory (`mkdtemp`) so concurrent `just mutate` runs across worktrees —
  the parallel scenario `UV_NO_SYNC` itself targets — cannot clobber or prematurely delete each
  other's shim; cleanup removes only that run's own dir, even on failure.

## Constraints / non-goals

- **Boundary**: unit tests drive each module directly with injected fakes. No new Postgres
  fixtures (those modules belong to bucket 1).
- **No production-source change** beyond `scripts/mutate.py` (the tooling fold-in). If a mutate run
  surfaces a genuine *bug* in a module, fix it in a separate commit and note it; the default
  expectation is test-only additions.
- **No un-gating** of `live_vm`/`live_stack` tests.
- Buckets 1 (PG-backed, 112) and 3 (tooling/structure-blocked, 46) stay deferred; this spec does
  not touch them.
