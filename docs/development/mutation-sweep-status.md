# Mutation sweep — coverage status and deferred targets

A repository-wide mutation-testing sweep (`just mutate`, mutmut 3.6) ran against the
container-free source modules on 2026-06-21. This note records what was covered, two
reusable tooling workarounds discovered during the run, and the targets deferred to a
follow-up sweep. See `mutation-testing.md` for how `just mutate` itself works.

## Result

- **407** source modules → **273** container-free "fast" targets (have at least one
  covering test that does not use the Postgres fixtures), **112** Postgres-backed targets,
  and the modules with no direct unit test (originally ~22; a reproducible scan found **25** on
  `main`). The no-direct-test bucket is now closed (#665, ADR-0229) — see below.
- The 273 fast targets were swept in 30 weight-balanced buckets, each killing surviving
  mutants and then passing an adversarial `/challenge` review of the added tests.
- **~3,700 mutants killed across ~210 commits.** Every bucket's added tests pass the full
  gate: `just lint`, `just type` (whole-tree), and `just test` (6,349 passed).
- **46** fast targets could not be swept in that run (categorized below); the 112 PG-backed and
  the no-direct-test targets were out of scope for the 2026-06-21 sweep (the latter closed by #665).

## Reusable tooling workarounds

> **Folded into the recipe (ADR-0229).** `just mutate` now applies both workarounds below
> automatically — it generates a per-run `sitecustomize.py` shim on a unique temp dir, prepends it
> to `PYTHONPATH`, and sets `UV_NO_SYNC=1` for the spawned mutmut/pytest subprocesses. No manual
> `export` is needed; the detail below is retained as the rationale.

Two environment issues block `just mutate` on parts of the tree. Both are worked around
without editing repo source — apply them when sweeping cli/mcp/security/config modules.

### 1. beartype.claw circular import in mutmut workers

`key_value` (via `py-key-value-aio`) calls `beartype_this_package()` at import, installing
a meta-path import hook. In a freshly *spawned* mutmut Pool worker that hook can intercept a
stdlib/pytest import while `beartype.claw._clawstate` is still initializing, raising
`ImportError: cannot import name 'claw_state'` and aborting the baseline before any mutant
runs (Python 3.14, beartype 0.22.9).

Workaround — a `sitecustomize.py` on `PYTHONPATH` that eagerly completes the imports at
interpreter startup, before the hook can fire:

```python
# /tmp/kdive-mut-pyhook/sitecustomize.py
import multiprocessing.connection, multiprocessing.context, multiprocessing.pool
import multiprocessing.popen_spawn_posix, multiprocessing.queues, multiprocessing.reduction
import multiprocessing.resource_sharer, multiprocessing.resource_tracker, multiprocessing.spawn
import multiprocessing.synchronize, multiprocessing.util
try:
    import beartype.claw._clawstate
    import beartype.claw._importlib._clawimpload
    import pytest
except Exception:
    pass
```

Run with: `PYTHONPATH=/tmp/kdive-mut-pyhook just mutate <source> <tests...>`

### 2. Shared-venv editable-install contention (parallel runs only)

The venv carries an editable kdive install (`.venv/.../kdive.pth`). Every `uv run` re-points
that `.pth` at the current working directory's `src`. When several worktrees share one venv
(symlink), concurrent `uv run` invocations rewrite each other's `kdive.pth`, intermittently
breaking imports. mutmut mutates its own `mutants/` copy regardless, so the editable pointer
only needs to stay valid — set `export UV_NO_SYNC=1` so `uv run` never rewrites it. (If it
was already corrupted, repoint `kdive.pth` at the real `src` and re-verify
`uv run --no-sync python -c "import kdive"`.)

## Deferred / blocked targets

### Postgres-backed (112) — deferred by scope

Their only covering tests use the `migrated_url`/`pg_conn`/`postgres_url` fixtures. Sweeping
these spins up testcontainers per run (slow, can leak/collide under parallelism); run them
serially in a dedicated session. Subsystems: `services/`, `store/`, `db/`, most
`jobs/handlers/`, and the Postgres-backed `inventory/`/`reconciler/` paths.

### No direct unit test — DONE (#665, ADR-0229)

This bucket is closed. A reproducible AST scan (no test under `tests/` imports the module by
dotted path) found **25** such modules on `main` (the original "22" was approximate;
`config/manifest.py` had since gained a test, and the scan surfaced a few small contract
modules). Each now has a direct unit test; per module:

- **Mutated to 0 surviving (function-body targets):** `mcp/middleware/shared` (12),
  `mcp/middleware/telemetry` (126), `mcp/middleware/usage` (77), `mcp/middleware/exposure` (19),
  `mcp/middleware/denial_audit` (78), `mcp/tools/ops/_reads` (36),
  `providers/local_libvirt/lifecycle/rootfs_catalog_fetch` (16), `services/runs/bind` (23, its
  pure `_run_bindable_error`). `services/runs/admission` (pure helpers: 145 generated, **8
  surviving — all equivalent**: the `cast` runtime no-op, `model_dump` `mode=` variants identical
  for an all-`str` model, the unobservable `<`/`<=` lease-expiry boundary, the `kind=None`→`""`
  sentinel that rejects identically, and a `detail=detail` drop that re-defaults to the same
  string). The async Postgres-locked admission/bind create flow stays a bucket-1 target.
- **Covered, 0 mutatable mutants (import-time-only declarations):** the three provider
  `settings.py`, `services/runs/states`, `domain/lifecycle/rules`, `providers/shared/build_timeouts`,
  `domain/catalog/{image_format,ownership}`, `db/probe_fence`, `providers/ports/handles`,
  `domain/_records`, `diagnostics/provider_contracts`, `domain/profile_documents`, `profiles/types`.
  Their code runs only at import / in a class body, so `mutate_only_covered_lines` (under
  `max_stack_depth=8`) records nothing to mutate; the direct tests still catch a changed default,
  dropped state, renamed enum value, or altered field set. `just mutate` now reports this as a
  clean "0 mutants generated — no covered, mutatable lines" (ADR-0229) rather than a baseline
  failure.
- **Covered, but reclassified to "could not be swept" (below):** `inventory/_row_typing` and
  `mcp/middleware/binding_errors`.

### Could not be swept this run (46)

- **mutmut copy-scope / baseline (≈15):** the module's covering test reads files mutmut does
  not copy into `mutants/` (e.g. the top-level `docs/` tree), or a `tests/conftest.py`
  re-import fails in the copy. Examples: `mcp/resources/registrar.py`, `mcp/app.py`,
  `db/repositories.py`, `config/external_env.py`, `security/secrets/secret_registry.py`,
  `version.py`, and `mcp/middleware/binding_errors` (its import chain resolves a source path that
  404s as `mutants/<frozen importlib._bootstrap>` in the copy — covered by a direct test, but the
  baseline cannot run).
- **mutmut cannot attribute a covering test (≈1):** `inventory/_row_typing` reaches 100% line
  coverage and mutmut generates mutants for its `@dataclass(frozen=True, slots=True)` `RowTyper`
  methods, but the per-mutant coverage map finds no covering test at any `max_stack_depth`, so it
  stops early. Covered by a direct test (all validator accept/reject paths); not unit-mutatable
  here.
- **No covered/mutatable lines (≈17):** logic is reached only through async event-loop
  frames (deep `asyncio.run` stacks exceed `max_stack_depth`, so `mutate_only_covered_lines`
  records nothing) or only via PG-backed/cross-file tests. Examples:
  `services/allocation/admission/core.py`, `jobs/handlers/runs_*.py`,
  `inventory/reconcile*.py`.
- **No mutable surface (≈8):** contract-only modules — `Protocol`s with `...` bodies, frozen
  dataclasses, bare Pydantic field declarations. mutmut generates nothing to mutate; the
  primary tests already pin field sets / frozenness / structural checks behaviorally.
  Examples: `providers/ports/{debug,retrieve,build_transport}.py`,
  `domain/lifecycle/shapes.py`, `domain/operations/jobs.py`.
- **Cross-file kill deferred (≈1):** the killing test belongs in a non-primary test file
  that another bucket owned (skipped to avoid a cross-agent merge conflict).

## Resuming

The mapping is reproducible. Re-running a bucket is cheap: already-clean modules report
0 surviving and are skipped. The "no direct unit test" bucket is now closed (#665); the next
sweep's remaining work is the 112 PG-backed targets (serial, dedicated run with `docker ps`
cleanup afterward) and the tooling/structure-blocked set above.
