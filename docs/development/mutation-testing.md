# Mutation testing (`just mutate`)

Mutation testing checks whether the test suite would *fail* if the code were wrong, not just
whether a line ran. mutmut changes one small thing (a mutant) — `x > 0` becomes `x >= 0`,
`False` becomes `True` — and re-runs the covering tests. A mutant that *survives* (tests still
pass) marks code whose behavior no test pins down.

This is on-demand local tooling, not a CI gate. You run it against one module while
strengthening its tests.

## Usage

    just mutate <source-module> <test-path>...

Both arguments are required. The test path is explicit because the test tree does not mirror
`src/` — the tests for a module are often in a different package (see the table below).

Example (fast, no Postgres):

    just mutate src/kdive/domain/errors.py tests/domain/test_errors.py

Reading the output: it prints how many mutants were generated and how many survived, then lists
each non-killed mutant with its mutmut status in brackets (e.g. `[survived]`, `[no tests]`,
`[timeout]`). Inspect one with `mutmut show <name>` (or browse interactively with
`mutmut browse`), then add or strengthen a test until it is killed, and re-run.

## Starting targets

| target module | covering tests | cost |
|---|---|---|
| `src/kdive/domain/errors.py` | `tests/domain/test_errors.py` | container-free (fast) |
| `src/kdive/domain/capacity/state.py` | `tests/services/allocation tests/services/systems` | Postgres-backed |
| `src/kdive/security/authz/gate.py` | `tests/security/authz` | Postgres-backed |
| `src/kdive/security/secrets/redaction.py` | `tests/security/secrets` | container-free (fast) |

v1 targets one `.py` file at a time (directory targets are not yet supported). Confirm the
covering tests for any new target yourself — the layout does not mirror `src/`.

## Cost and the Postgres caveat

Runtime depends on the target's tests. Container-free targets run in seconds. Targets whose
tests use the Postgres fixtures (`migrated_url`/`pg_conn`/`postgres_url`) start a disposable
container, so each run is slower — prefer container-free targets for tight iteration.

Container leak warning: mutmut kills slow mutants on timeout, and a killed process may not run
testcontainers cleanup, so a Postgres-backed run can leave orphaned containers. After such a
run, check `docker ps` and remove any leftovers with `docker rm -f <id>`. (Note: an empirical
run against `redaction.py` started no containers — that target's tests are pure-logic — so the
leak path remains untested in practice.)

## Host prerequisite

mutmut runs ephemerally via `uv run --with 'mutmut==3.6.0'` — nothing to install into the
project. mutmut needs `os.fork`, so it runs on macOS/Linux only (Windows needs WSL).

## How it works

The wrapper writes a transient `setup.cfg` `[mutmut]` section, runs mutmut, and removes the
config afterward. mutmut runs the suite from an isolated copy under `mutants/` (gitignored), so
the wrapper copies the whole `src/kdive` package (otherwise `import kdive.*` fails in the copy)
and scopes mutation to your target file with `only_mutate`. The `mutants/` cache is reused when
you re-run the same target and reset when you switch targets.

The wrapper also spawns mutmut with two environment workarounds applied automatically, so a run
against any import chain (mcp/cli/security/config) needs no manual setup (ADR-0229): it generates
a per-run `sitecustomize.py` shim on a unique temp dir and prepends it to `PYTHONPATH` (eagerly
completing the `beartype.claw` + `multiprocessing` imports so the beartype meta-path hook cannot
abort a spawned worker's baseline), and sets `UV_NO_SYNC=1` (so `uv run` never rewrites the shared
editable `kdive.pth` under a parallel worktree). The shim dir is removed when the run ends.
