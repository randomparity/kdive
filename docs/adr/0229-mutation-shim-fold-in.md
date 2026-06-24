# ADR-0229: Fold the `just mutate` environment workarounds into the recipe

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->

## Context

The #664 mutation sweep ([mutation-sweep-status.md](../development/mutation-sweep-status.md))
found two environment issues that block `just mutate` on parts of the tree, and worked around them
by hand:

1. **beartype.claw circular import in spawned mutmut workers.** `key_value` (via
   `py-key-value-aio`) calls `beartype_this_package()` at import, installing a meta-path import
   hook. In a freshly *spawned* mutmut Pool worker that hook can intercept a stdlib/pytest import
   while `beartype.claw._clawstate` is still initializing, raising
   `ImportError: cannot import name 'claw_state'` and aborting mutmut's baseline before any mutant
   runs. The manual fix is a `sitecustomize.py` on `PYTHONPATH` that eagerly completes the
   `multiprocessing.*` + `beartype.claw` + `pytest` imports at interpreter startup, before the hook
   can fire.
2. **Shared-venv editable-install contention.** Every `uv run` re-points the editable `kdive.pth`
   at the current working dir's `src`; concurrent runs across worktrees rewrite each other's
   `.pth`. mutmut mutates its own `mutants/` copy regardless, so the editable pointer only needs to
   stay valid — `UV_NO_SYNC=1` stops `uv run` rewriting it.

Both are applied today only if the operator remembers to `export PYTHONPATH=…` /
`export UV_NO_SYNC=1` before `just mutate`. A run against any `mcp/*`, `cli/*`, `security/*`, or
`config/*` import chain silently fails its baseline without them, which reads as a broken target
rather than a missing shim. #665 needs these workarounds on every bucket-2 mcp/middleware run, so
the manual step has to go.

## Decision

`scripts/mutate.py` applies both workarounds itself, transparently, for the mutmut/pytest
subprocesses it spawns:

- Generate a transient `sitecustomize.py` (the eager-import shim) in a **per-run unique** temp
  directory (`mkdtemp`) for the duration of the run, and remove that dir in a `finally`, like the
  existing `setup.cfg` handling. The unique dir matters because the shared-venv parallel scenario
  `UV_NO_SYNC` addresses runs several `just mutate` invocations at once; a fixed shim path would let
  one run's cleanup delete another's live shim mid-run, intermittently reintroducing the very
  beartype baseline failure the shim prevents.
- Spawn the `pytest --co` preflight, `mutmut run`, and `mutmut results` subprocesses with an env
  that **prepends** the shim dir to any inherited `PYTHONPATH` (never replacing it) and sets
  `UV_NO_SYNC=1`.

The env construction and shim contents are pure functions, tested behaviorally alongside the rest
of the harness (mutmut itself is never run in the unit tests).

## Consequences

- `just mutate <module> <tests>` works on every import chain with no manual environment setup; the
  beartype baseline failure can no longer be mistaken for a broken target.
- The shim is process-local and torn down per run; it never lingers in the checkout (unlike a
  committed root `sitecustomize.py`) and so cannot perturb normal `uv run` / `just test` /
  developer interpreters.
- `UV_NO_SYNC=1` is scoped to the spawned subprocesses' env, so it does not change the behavior of
  the operator's interactive shell.
- The mutation-testing docs drop the "export these first" manual step and point at the recipe.

## Considered & rejected

- **Commit a real `sitecustomize.py` at the repo root.** Python imports `sitecustomize` at *every*
  interpreter startup whose path includes the root, so it would eagerly run those imports for every
  `uv run`, `just test`, and dev REPL — global blast radius to fix a mutation-only problem.
- **Set the env in the `justfile` recipe** (`PYTHONPATH=… UV_NO_SYNC=1 uv run …`). The shim file
  still has to exist somewhere committed or generated; generating + cleaning it belongs with the
  other transient state (`setup.cfg`, `mutants/`) the Python wrapper already owns, not split across
  the recipe and a checked-in file. Keeping it in one place keeps the teardown guarantee.
- **Eagerly import beartype/multiprocessing at the top of `scripts/mutate.py`.** The wrapper is the
  *parent*; the failure is in mutmut's *spawned* Pool workers, which start fresh interpreters. Only
  a `sitecustomize` on their `PYTHONPATH` runs in those children.
- **Fix the circular import upstream / in repo source.** It originates in beartype.claw + a
  third-party package's import-time `beartype_this_package()`; the manifest forbids editing repo
  source to work around tooling, and the global guidance is to fix it without touching product
  code. The shim is test-tooling-local.
