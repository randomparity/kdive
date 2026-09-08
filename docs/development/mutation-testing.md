# Mutation testing

Use `just mutate` to check whether selected tests detect small changes to one source module.
This is local test-development tooling, not a CI gate or a repository-wide coverage score.
The [historical sweep record](mutation-sweep-status.md) preserves past results and limitations.

## Run one target

From the repository root, use one `.py` source file under `src/kdive` and one or more existing
repository test files or directories. The wrapper accepts paths, not pytest node IDs or `-k`
expressions. Find the tests that exercise the target; directory names alone do not prove coverage.

```bash
just mutate src/kdive/domain/errors.py tests/domain/test_errors.py
```

The recipe supplies the pinned mutmut dependency through `uv run --with`; a standalone
`mutmut` installation is unnecessary. Use a prepared project environment in which the selected
tests collect. Even a container-free test file loads the root `tests/conftest.py` and its
Linux-dependent imports. Container-backed tests additionally need their normal backends; see
[test-backend setup and cleanup](../../deploy/compose/README.md#using-the-stack-as-a-test-backend).
Check skipped tests before interpreting coverage. Set `KDIVE_REQUIRE_DOCKER=1` when disposable
Docker backends are required so an unavailable daemon fails instead of silently skipping tests.

The wrapper collects the selected tests first, then mutmut checks its copied baseline before
running mutants. Both selections exclude `live_vm` and `live_stack`. Subprocess output is
captured until completion; a quiet terminal does not mean the run has finished. Runtime depends
on the selected tests, backend availability and generated mutants.

## Interpret and inspect results

The summary's “surviving” count includes **every status other than `killed`**. Read the status:

- `survived`: the selected tests passed with that change. Inspect whether this reveals a missing
  assertion or fixture, or whether the change has no observable effect under the actual contract.
- `no tests`, `skipped`, `timeout`, `segfault`, or other non-killed statuses: investigate coverage,
  execution or tooling limits. They do not by themselves prove equivalence or a useful test kill.
- `0 mutants generated`: no covered mutation was available under covered-line selection and
  `max_stack_depth=8`. It does not establish test effectiveness; import-time code and attribution
  limits can both produce it.

A successful wrapper exit does not require zero survivors. Results are relative to the supplied
tests, and a serial, UTC-only or single-profile fixture cannot establish behavior outside those
conditions. Check the baseline and result status before drawing conclusions from the counts.
The wrapper also ignores the exit status of `mutmut results`; check that command directly if its
summary is empty or unexpected:

```bash
uv run --no-sync --with 'mutmut==3.6.0' python -m mutmut results
```

Inspect a name from the summary, using the same pinned dependency as the recipe:

```bash
uv run --no-sync --with 'mutmut==3.6.0' python -m mutmut show "${MUTANT_NAME:?set a name from the summary}"
```

For interactive inspection, replace `show "$MUTANT_NAME"` with `browse`. Inspect before changing
targets: the wrapper may discard the old results. Strengthen the relevant test, verify it fails
for the intended behavioral change, then rerun that target.

## Temporary state and concurrent work

The [wrapper](../../scripts/mutate.py) writes a marked `setup.cfg` and copies the package into
`mutants/`. It reuses that cache only when the source **and ordered test-path list** match;
changing either, or finding no cache signature, removes the old `mutants/` directory.

Run only one mutation process per worktree. The existing-`setup.cfg` check refuses both another
run's marked config and an unrelated config; it is not an atomic concurrency lock. Normal cleanup
removes the transient config and shim directory. After an interrupted run, confirm no process
still uses them before removing a marked leftover `setup.cfg`; preserve an unrelated config.

The per-run import shim and `UV_NO_SYNC=1` are applied to the wrapper's spawned subprocesses
([ADR-0229](../adr/0229-mutation-shim-fold-in.md)). They do not prevent the outer `uv run` from
syncing first. If worktrees share an already-prepared environment, set `UV_NO_SYNC=1` on the
outer `just mutate` invocation as well. The shim addresses the recorded import race; it does not
make every copied baseline work. Resources outside `src/kdive` can still be missing in the copy.
