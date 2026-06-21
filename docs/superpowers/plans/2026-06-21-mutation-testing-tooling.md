# Mutation Testing Tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `just mutate <source-module> <test-path>...` recipe that runs mutmut 3.6.0 against one module and reports surviving mutants, so developers can verify their tests actually catch regressions.

**Architecture:** A stdlib-only wrapper (`scripts/mutate.py`) writes a transient `setup.cfg [mutmut]` section, runs mutmut ephemerally via `uv run --with`, and prints a survivor summary. mutmut runs the suite from an isolated copy under `mutants/`; the wrapper copies the whole `src/kdive` package so `import kdive.*` resolves there, and scopes mutation to the target file with `only_mutate`. Validity is guarded in two layers; the `mutants/` store is reset when the target changes.

**Tech Stack:** Python 3.14, mutmut 3.6.0 (run via `uv run --with`, not a locked dep), pytest, `just`.

## Global Constraints

- Python 3.13+ syntax target; repo runs 3.14. Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict (whole tree, src + tests).
- Absolute imports only (no relative `..`). Scripts are stdlib-only and importable as `scripts.<name>` (the package has `scripts/__init__.py`).
- mutmut is pinned `mutmut==3.6.0` and invoked **only** via `uv run --with 'mutmut==3.6.0'`; it is NOT added to any dependency group in `pyproject.toml`.
- Tests live under `tests/` mirroring the tree; script tests go in `tests/scripts/` and import the script module directly (`from scripts.mutate import ...`).
- Prose rule (CI doc-style guard): use plain factual language; avoid "comprehensive", "robust", "critical", "elegant". Use "Milestone" not "Sprint".
- Commit per task with Conventional Commits; never commit on `main` (work is on branch `feat/mutation-testing-tooling`).

## Verified mechanics (spikes resolved 2026-06-21)

These were confirmed by running mutmut 3.6.0 against the container-free target on this arm64-darwin box. The plan below depends on them — do not re-derive:

- **src-layout import fix:** `source_paths=src/kdive` (copies the whole package so `import kdive.config` resolves in the `mutants/` copy) **plus** `only_mutate=<target file>` (keeps generation scoped — `errors.py` produced exactly 10 mutants at ~220/s). `source_paths=<single file>` FAILS with `ModuleNotFoundError: No module named 'kdive.config'`.
- **`also_copy = pyproject.toml, tests`** is required (mutmut runs from the copy; pyproject carries pytest's config, and conftests import across `tests/` packages).
- **`pytest_add_cli_args_test_selection`** in `setup.cfg` is newline-token form: each token on its own indented line, so `not live_vm and not live_stack` stays one token.
- **`mutmut run` exits 0 even when mutants survive**; it exits non-zero (with a traceback) when the in-copy baseline/copy is broken. So: non-zero → abort and show stderr; survivors → parse from `mutmut results`.
- **`mutmut results`** prints non-killed mutants as `    <name>: <status>` (e.g. `kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_1: survived`); it exits 0. **`mutmut show <name>`** prints a unified diff of the mutation.
- **The `mutants/` store** holds `mutmut-stats.json` and the copied tree; the stats file has no covered/total line counts, so the summary uses the **mutant count** (from the run's final `N/N` line) as the coverage proxy, not a ratio.
- **arm64-darwin** installs `libcst` from a prebuilt wheel (no Rust). x86_64-darwin needs `rustc`/`cargo` — documented in the user doc.
- **Spike 5 (testcontainers leak on Postgres targets)** is NOT yet verified and is deferred to Task 8.

## Deviations from the spec (all grounded in the spikes)

- **Config:** spec said `source_paths = <target module>`; verified reality is `source_paths=src/kdive` + `only_mutate=<file>` (single-file `source_paths` breaks `import kdive.*`).
- **Exit codes:** spec's error-handling implied a non-zero mutmut exit when survivors exist; mutmut actually exits 0 on survivors and non-zero only on a broken baseline. The wrapper treats non-zero as abort and reads survivors from `mutmut results`.
- **Test file:** `tests/scripts/test_mutate.py` (repo convention) rather than the spec's `tests/test_mutate_script.py`.
- **Host prereq:** the libcst/Rust note lives in the user doc only; `scripts/check-setup-deps.sh` is not modified (it gates `just setup`, and this is an x86_64-darwin-only prereq for an ephemeral tool).
- **v1 scope:** one `.py` file per run; directory targets are rejected until `only_mutate` glob/dir semantics are verified.

---

### Task 1: `.gitignore` + module skeleton with path resolution

**Files:**
- Modify: `.gitignore`
- Create: `scripts/mutate.py`
- Test: `tests/scripts/test_mutate.py`

**Interfaces:**
- Produces: `resolve_source(source_arg: str) -> str` (returns repo-relative POSIX path, e.g. `"src/kdive/domain/errors.py"`; raises `MutateError` if missing, not under `src/kdive`, or not a `.py` file); `resolve_test_paths(test_args: list[str]) -> list[str]` (repo-relative POSIX paths; raises `MutateError` if any is missing or the list is empty); `class MutateError(Exception)`; `_ROOT: Path`.

**Note:** v1 targets a single `.py` file. Only the file form of `only_mutate` is verified; directory/glob targets are rejected with a clear message until that form is verified (see Task 8 / future work).

- [ ] **Step 1: Add ignore entries**

Append to `.gitignore`:

```gitignore

# mutation testing (scripts/mutate.py, mutmut working dir + transient config)
/mutants/
/setup.cfg
```

- [ ] **Step 2: Write the failing test**

Create `tests/scripts/test_mutate.py`:

```python
"""Behavioral tests for scripts/mutate.py (the `just mutate` wrapper).

mutmut itself is never run here; these test the wrapper's pure decision logic.
"""

from __future__ import annotations

import pytest

from scripts.mutate import (
    MutateError,
    resolve_source,
    resolve_test_paths,
)


def test_resolve_source_returns_repo_relative_posix_path() -> None:
    assert resolve_source("src/kdive/domain/errors.py") == "src/kdive/domain/errors.py"


def test_resolve_source_rejects_path_outside_package() -> None:
    with pytest.raises(MutateError, match="under src/kdive"):
        resolve_source("scripts/mutate.py")


def test_resolve_source_rejects_missing_file() -> None:
    with pytest.raises(MutateError, match="does not exist"):
        resolve_source("src/kdive/domain/nope.py")


def test_resolve_source_rejects_directory() -> None:
    with pytest.raises(MutateError, match="must be a .py file"):
        resolve_source("src/kdive/domain")


def test_resolve_test_paths_accepts_existing_paths() -> None:
    assert resolve_test_paths(["tests/domain/test_errors.py"]) == [
        "tests/domain/test_errors.py"
    ]


def test_resolve_test_paths_rejects_missing_path() -> None:
    with pytest.raises(MutateError, match="does not exist"):
        resolve_test_paths(["tests/domain/test_nope.py"])


def test_resolve_test_paths_rejects_empty_list() -> None:
    with pytest.raises(MutateError, match="at least one test path"):
        resolve_test_paths([])
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_mutate.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.mutate'`

- [ ] **Step 4: Write minimal implementation**

Create `scripts/mutate.py`:

```python
#!/usr/bin/env python3
"""On-demand mutation testing wrapper around mutmut 3.x.

Drives mutmut against ONE source module and an explicit test path. mutmut runs the suite
from an isolated copy under ``mutants/``; to make ``import kdive.*`` resolve there, the whole
package is copied (``source_paths=src/kdive``) while mutation is scoped to the target file
(``only_mutate``). Validity is guarded in two layers (a cheap repo-root ``pytest --co`` for a
bad/empty test path, and mutmut's own in-copy baseline for failing tests / copy-scope breakage).
The ``mutants/`` store is reset when the target changes so summaries never conflate targets.

Usage (via the ``just mutate`` recipe):
    uv run --with 'mutmut==3.6.0' python scripts/mutate.py <source-module> <test-path>...

See docs/development/mutation-testing.md.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_REL = "src/kdive"


class MutateError(Exception):
    """A user-facing wrapper error (bad arguments, broken baseline, etc.)."""


def resolve_source(source_arg: str) -> str:
    """Validate the source module path and return it repo-relative (POSIX).

    v1 targets a single ``.py`` file: only the file form of ``only_mutate`` is verified.
    """
    path = (_ROOT / source_arg).resolve()
    package = (_ROOT / _PACKAGE_REL).resolve()
    if package not in path.parents:
        raise MutateError(f"source must be under {_PACKAGE_REL}: {source_arg}")
    if not path.exists():
        raise MutateError(f"source does not exist: {source_arg}")
    if path.suffix != ".py" or not path.is_file():
        raise MutateError(f"source must be a .py file (directory targets unsupported): {source_arg}")
    return path.relative_to(_ROOT).as_posix()


def resolve_test_paths(test_args: list[str]) -> list[str]:
    """Validate each test path exists and return them repo-relative (POSIX)."""
    if not test_args:
        raise MutateError("provide at least one test path")
    resolved: list[str] = []
    for arg in test_args:
        path = (_ROOT / arg).resolve()
        if not path.exists():
            raise MutateError(f"test path does not exist: {arg}")
        resolved.append(path.relative_to(_ROOT).as_posix())
    return resolved
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run python -m pytest tests/scripts/test_mutate.py -q`
Expected: PASS (6 passed)

- [ ] **Step 6: Lint, type-check, commit**

Run: `uv run ruff check scripts/mutate.py tests/scripts/test_mutate.py && uv run ruff format scripts/mutate.py tests/scripts/test_mutate.py && uv run ty check scripts/mutate.py tests/scripts/test_mutate.py`
Expected: no errors

```bash
git add .gitignore scripts/mutate.py tests/scripts/test_mutate.py
git commit -m "feat(mutate): add wrapper skeleton with path resolution"
```

---

### Task 2: Transient config rendering

**Files:**
- Modify: `scripts/mutate.py`
- Test: `tests/scripts/test_mutate.py`

**Interfaces:**
- Consumes: `resolve_source`, `resolve_test_paths` (Task 1).
- Produces: `MARKER: str` (config header marker); `render_config(only_mutate_rel: str, test_paths: list[str]) -> str` (returns the full `setup.cfg` text, beginning with `MARKER`).

- [ ] **Step 1: Write the failing test**

Add to `tests/scripts/test_mutate.py`:

```python
from scripts.mutate import MARKER, render_config


def test_render_config_scopes_mutation_and_copies_whole_package() -> None:
    text = render_config(
        "src/kdive/domain/errors.py", ["tests/domain/test_errors.py"]
    )
    assert text.startswith(MARKER)
    assert "[mutmut]" in text
    # whole package copied so imports resolve; mutation scoped to the one file
    assert "source_paths=src/kdive\n" in text
    assert "only_mutate=src/kdive/domain/errors.py\n" in text
    assert "mutate_only_covered_lines=true\n" in text
    assert "max_stack_depth=8\n" in text


def test_render_config_uses_newline_token_test_selection_with_live_filter() -> None:
    text = render_config(
        "src/kdive/security/authz/gate.py",
        ["tests/security/authz", "tests/security/test_x.py"],
    )
    # each token on its own indented line so the marker expression stays one token
    assert "pytest_add_cli_args_test_selection=\n" in text
    assert "    -m\n" in text
    assert "    not live_vm and not live_stack\n" in text
    assert "    tests/security/authz\n" in text
    assert "    tests/security/test_x.py\n" in text


def test_render_config_suppresses_logger_but_not_raise() -> None:
    text = render_config("src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    assert "do_not_mutate_patterns=logger.\\w+\n" in text
    assert "raise" not in text  # removed/weakened guard raises must stay mutable


def test_render_config_copies_pyproject_and_tests() -> None:
    text = render_config("src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    assert "also_copy=\n    pyproject.toml\n    tests\n" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_mutate.py -k render_config -q`
Expected: FAIL — `ImportError: cannot import name 'MARKER'`

- [ ] **Step 3: Write minimal implementation**

Add to `scripts/mutate.py` (after `_PACKAGE_REL`):

```python
MARKER = "# kdive-mutate transient config — delete only when no run is in flight\n"
```

Add the function:

```python
def render_config(only_mutate_rel: str, test_paths: list[str]) -> str:
    """Render the transient ``setup.cfg`` ``[mutmut]`` section as text.

    ``source_paths`` is the whole package so ``import kdive.*`` resolves in mutmut's
    isolated copy; ``only_mutate`` scopes generation to the target file. The test
    selection uses newline-token form so the marker expression stays one token.
    """
    selection_tokens = ["-m", "not live_vm and not live_stack", *test_paths]
    selection = "".join(f"    {token}\n" for token in selection_tokens)
    return (
        f"{MARKER}"
        "[mutmut]\n"
        f"source_paths={_PACKAGE_REL}\n"
        f"only_mutate={only_mutate_rel}\n"
        "pytest_add_cli_args_test_selection=\n"
        f"{selection}"
        "mutate_only_covered_lines=true\n"
        "max_stack_depth=8\n"
        "do_not_mutate_patterns=logger.\\w+\n"
        "also_copy=\n"
        "    pyproject.toml\n"
        "    tests\n"
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python -m pytest tests/scripts/test_mutate.py -k render_config -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check scripts/mutate.py tests/scripts/test_mutate.py && uv run ruff format scripts/mutate.py tests/scripts/test_mutate.py && uv run ty check scripts/mutate.py tests/scripts/test_mutate.py`
Expected: no errors

```bash
git add scripts/mutate.py tests/scripts/test_mutate.py
git commit -m "feat(mutate): render transient mutmut config"
```

---

### Task 3: Result parsing and summary formatting

**Files:**
- Modify: `scripts/mutate.py`
- Test: `tests/scripts/test_mutate.py`

**Interfaces:**
- Produces: `parse_survivors(results_stdout: str) -> list[str]` (mutant names whose status is `survived`, `timeout`, or `suspicious`); `parse_total_mutants(run_stdout: str) -> int | None` (the `N` from the last `N/N` progress token); `format_summary(total: int | None, survivors: list[str], source_rel: str, test_paths: list[str]) -> str`.

- [ ] **Step 1: Write the failing test**

Add to `tests/scripts/test_mutate.py`:

```python
from scripts.mutate import format_summary, parse_survivors, parse_total_mutants

_RESULTS = (
    "    kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_1: survived\n"
    "    kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_6: survived\n"
)
_RUN_TAIL = "⠇ 10/10  🎉 8 🫥 0  ⏰ 0  🤔 0  🙁 2  🔇 0  🧙 0\n200.09 mutations/second\n"


def test_parse_survivors_extracts_non_killed_names() -> None:
    assert parse_survivors(_RESULTS) == [
        "kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_1",
        "kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_6",
    ]


def test_parse_survivors_includes_timeout_and_suspicious() -> None:
    text = "    a.b_mutmut_1: timeout\n    a.b_mutmut_2: suspicious\n"
    assert parse_survivors(text) == ["a.b_mutmut_1", "a.b_mutmut_2"]


def test_parse_survivors_empty_when_all_killed() -> None:
    assert parse_survivors("") == []


def test_parse_total_mutants_reads_last_progress_token() -> None:
    assert parse_total_mutants(_RUN_TAIL) == 10


def test_parse_total_mutants_none_when_absent() -> None:
    assert parse_total_mutants("no progress here\n") is None


def test_format_summary_lists_survivors_and_count() -> None:
    out = format_summary(
        10,
        ["kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_1"],
        "src/kdive/domain/errors.py",
        ["tests/domain/test_errors.py"],
    )
    assert "10 mutants" in out
    assert "1 surviving" in out
    assert "kdive.domain.errors.xǁCategorizedErrorǁ__init____mutmut_1" in out
    assert "mutmut show" in out  # tells the dev how to inspect
    assert "relative to the test path" in out


def test_format_summary_celebrates_zero_survivors() -> None:
    out = format_summary(10, [], "src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    assert "0 surviving" in out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_mutate.py -k "parse_ or format_summary" -q`
Expected: FAIL — `ImportError: cannot import name 'parse_survivors'`

- [ ] **Step 3: Write minimal implementation**

Add `import re` to the imports of `scripts/mutate.py`, then add:

```python
_RESULT_LINE = re.compile(r"^\s*(\S+):\s*(survived|timeout|suspicious)\s*$")
_PROGRESS = re.compile(r"(\d+)/(\d+)")


def parse_survivors(results_stdout: str) -> list[str]:
    """Return names of mutants that were not killed (survived/timeout/suspicious)."""
    names: list[str] = []
    for line in results_stdout.splitlines():
        match = _RESULT_LINE.match(line)
        if match:
            names.append(match.group(1))
    return names


def parse_total_mutants(run_stdout: str) -> int | None:
    """Return the total mutant count from the last ``N/N`` progress token, if any."""
    matches = _PROGRESS.findall(run_stdout)
    if not matches:
        return None
    return int(matches[-1][1])


def format_summary(
    total: int | None,
    survivors: list[str],
    source_rel: str,
    test_paths: list[str],
) -> str:
    """Build the human-facing summary printed at the end of a run."""
    total_text = "unknown" if total is None else str(total)
    lines = [
        f"Mutation testing: {source_rel}",
        f"  tests: {' '.join(test_paths)}",
        f"  {total_text} mutants generated, {len(survivors)} surviving",
    ]
    for name in survivors:
        lines.append(f"    survived: {name}")
    if survivors:
        lines.append("  inspect a survivor: mutmut show <name>  (or: mutmut browse)")
        lines.append("  each survivor is a code change no test caught — add an assertion.")
    lines.append("  note: result is relative to the test path supplied.")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python -m pytest tests/scripts/test_mutate.py -k "parse_ or format_summary" -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check scripts/mutate.py tests/scripts/test_mutate.py && uv run ruff format scripts/mutate.py tests/scripts/test_mutate.py && uv run ty check scripts/mutate.py tests/scripts/test_mutate.py`
Expected: no errors

```bash
git add scripts/mutate.py tests/scripts/test_mutate.py
git commit -m "feat(mutate): parse mutmut results and format survivor summary"
```

---

### Task 4: Store isolation and in-flight config refusal

**Files:**
- Modify: `scripts/mutate.py`
- Test: `tests/scripts/test_mutate.py`

**Interfaces:**
- Produces: `signature(source_rel: str, test_paths: list[str]) -> str`; `guard_no_existing_config(config_path: Path) -> None` (raises `MutateError` if the file exists, with a marker-aware message); `prepare_store(sig: str, mutants_dir: Path) -> None` (removes `mutants_dir` unless its `.kdive-target` matches `sig`); `write_signature(sig: str, mutants_dir: Path) -> None`.

- [ ] **Step 1: Write the failing test**

Add to `tests/scripts/test_mutate.py`:

```python
from pathlib import Path

from scripts.mutate import (
    guard_no_existing_config,
    prepare_store,
    signature,
    write_signature,
)


def test_signature_is_stable_for_same_target() -> None:
    a = signature("src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    b = signature("src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    assert a == b


def test_signature_differs_when_target_differs() -> None:
    a = signature("src/kdive/domain/errors.py", ["tests/domain/test_errors.py"])
    b = signature("src/kdive/domain/cost.py", ["tests/domain/test_errors.py"])
    assert a != b


def test_guard_refuses_existing_marked_config(tmp_path: Path) -> None:
    cfg = tmp_path / "setup.cfg"
    cfg.write_text("# kdive-mutate transient config — delete only when no run is in flight\n")
    with pytest.raises(MutateError, match="in flight"):
        guard_no_existing_config(cfg)


def test_guard_refuses_existing_foreign_config(tmp_path: Path) -> None:
    cfg = tmp_path / "setup.cfg"
    cfg.write_text("[flake8]\nmax-line-length = 100\n")
    with pytest.raises(MutateError, match="refusing to overwrite"):
        guard_no_existing_config(cfg)


def test_guard_passes_when_no_config(tmp_path: Path) -> None:
    guard_no_existing_config(tmp_path / "setup.cfg")  # no raise


def test_prepare_store_clears_on_target_change(tmp_path: Path) -> None:
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    (mutants / ".kdive-target").write_text("old-signature")
    (mutants / "stale.json").write_text("{}")
    prepare_store("new-signature", mutants)
    assert not mutants.exists()


def test_prepare_store_keeps_on_same_target(tmp_path: Path) -> None:
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    (mutants / ".kdive-target").write_text("sig")
    (mutants / "stats.json").write_text("{}")
    prepare_store("sig", mutants)
    assert (mutants / "stats.json").exists()


def test_prepare_store_clears_when_no_signature(tmp_path: Path) -> None:
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    (mutants / "leftover.json").write_text("{}")  # broken prior run, no signature
    prepare_store("sig", mutants)
    assert not mutants.exists()


def test_write_signature_records_target(tmp_path: Path) -> None:
    mutants = tmp_path / "mutants"
    mutants.mkdir()
    write_signature("sig", mutants)
    assert (mutants / ".kdive-target").read_text() == "sig"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_mutate.py -k "signature or guard or store" -q`
Expected: FAIL — `ImportError: cannot import name 'signature'`

- [ ] **Step 3: Write minimal implementation**

Add `import shutil` to the imports of `scripts/mutate.py`, then add:

```python
def signature(source_rel: str, test_paths: list[str]) -> str:
    """A stable identifier for a (source, tests) target, used to detect target changes."""
    return "\n".join([source_rel, *test_paths])


def guard_no_existing_config(config_path: Path) -> None:
    """Refuse to run if a setup.cfg is already present (in-flight run or foreign file)."""
    if not config_path.exists():
        return
    if config_path.read_text().startswith(MARKER):
        raise MutateError(
            "a mutate run is in flight (transient setup.cfg present); "
            "if none is running, delete setup.cfg and retry"
        )
    raise MutateError(f"refusing to overwrite existing {config_path.name}")


def prepare_store(sig: str, mutants_dir: Path) -> None:
    """Remove the mutmut store unless it already belongs to this exact target.

    Keeping a matching store lets a re-run reuse mutmut's cache (fast iteration);
    a changed or signature-less store is removed so summaries never conflate targets.
    """
    if not mutants_dir.exists():
        return
    marker = mutants_dir / ".kdive-target"
    if marker.exists() and marker.read_text() == sig:
        return
    shutil.rmtree(mutants_dir)


def write_signature(sig: str, mutants_dir: Path) -> None:
    """Record the current target in the store so the next run can detect a change."""
    if mutants_dir.exists():
        (mutants_dir / ".kdive-target").write_text(sig)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python -m pytest tests/scripts/test_mutate.py -k "signature or guard or store" -q`
Expected: PASS (9 passed)

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check scripts/mutate.py tests/scripts/test_mutate.py && uv run ruff format scripts/mutate.py tests/scripts/test_mutate.py && uv run ty check scripts/mutate.py tests/scripts/test_mutate.py`
Expected: no errors

```bash
git add scripts/mutate.py tests/scripts/test_mutate.py
git commit -m "feat(mutate): isolate the mutmut store per target and refuse in-flight config"
```

---

### Task 5: Validity guard and orchestration (`main`)

**Files:**
- Modify: `scripts/mutate.py`
- Test: `tests/scripts/test_mutate.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: `preflight_collect(test_paths: list[str], runner=...) -> None` (raises `MutateError` if `pytest --co` errors or collects nothing); `run_mutmut(runner=...) -> str` (runs `mutmut run`, returns its stdout, raises `MutateError` on non-zero); `collect_results(runner=...) -> str` (returns `mutmut results` stdout); `main(argv: list[str] | None = None) -> int`. The `runner` parameter defaults to a module-level `_run_subprocess` and is injected in tests.

- [ ] **Step 1: Write the failing test**

Add to `tests/scripts/test_mutate.py`:

```python
import subprocess

from scripts.mutate import collect_results, preflight_collect, run_mutmut


def _fake_runner(returncode: int, stdout: str = "", stderr: str = ""):
    def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    return run


def test_preflight_passes_when_tests_collected() -> None:
    preflight_collect(["tests/domain/test_errors.py"], runner=_fake_runner(0, "35 tests"))


def test_preflight_aborts_on_no_tests_collected() -> None:
    # pytest exit code 5 == "no tests collected"
    with pytest.raises(MutateError, match="no tests collected"):
        preflight_collect(["tests/x"], runner=_fake_runner(5))


def test_preflight_aborts_on_collection_error() -> None:
    with pytest.raises(MutateError, match="collection failed"):
        preflight_collect(["tests/x"], runner=_fake_runner(2, stderr="bad import"))


def test_run_mutmut_returns_stdout_on_success() -> None:
    out = run_mutmut(runner=_fake_runner(0, "10/10 done"))
    assert out == "10/10 done"


def test_run_mutmut_aborts_on_broken_baseline() -> None:
    with pytest.raises(MutateError, match="baseline"):
        run_mutmut(runner=_fake_runner(1, stderr="ModuleNotFoundError: kdive.config"))


def test_collect_results_returns_stdout() -> None:
    assert collect_results(runner=_fake_runner(0, "a: survived\n")) == "a: survived\n"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run python -m pytest tests/scripts/test_mutate.py -k "preflight or run_mutmut or collect_results" -q`
Expected: FAIL — `ImportError: cannot import name 'preflight_collect'`

- [ ] **Step 3: Write minimal implementation**

Add `import argparse`, `import subprocess`, `import sys` to the imports. Add the constants and functions:

```python
_CONFIG_PATH = _ROOT / "setup.cfg"
_MUTANTS_DIR = _ROOT / "mutants"


def _run_subprocess(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=_ROOT, text=True, capture_output=True, check=False)


def preflight_collect(test_paths: list[str], runner=_run_subprocess) -> None:
    """Cheap repo-root check: collect-only the scoped tests; abort on bad/empty path."""
    cmd = [
        sys.executable, "-m", "pytest", "--co", "-q",
        "-m", "not live_vm and not live_stack", *test_paths,
    ]
    result = runner(cmd)
    if result.returncode == 5:
        raise MutateError(f"no tests collected for: {' '.join(test_paths)}")
    if result.returncode != 0:
        raise MutateError(f"test collection failed: {result.stderr.strip()}")


def run_mutmut(runner=_run_subprocess) -> str:
    """Run ``mutmut run``; non-zero means a broken in-copy baseline/copy — abort."""
    result = runner([sys.executable, "-m", "mutmut", "run"])
    if result.returncode != 0:
        raise MutateError(
            "mutmut baseline failed (failing tests or copy-scope breakage):\n"
            + result.stderr.strip()
        )
    return result.stdout


def collect_results(runner=_run_subprocess) -> str:
    """Return ``mutmut results`` stdout (the non-killed mutants)."""
    return runner([sys.executable, "-m", "mutmut", "results"]).stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mutate",
        description="Run mutmut against one module and report surviving mutants.",
    )
    parser.add_argument("source", help="source module under src/kdive (file or dir)")
    parser.add_argument("tests", nargs="+", help="explicit covering test path(s)")
    args = parser.parse_args(argv)

    try:
        source_rel = resolve_source(args.source)
        test_paths = resolve_test_paths(args.tests)
        guard_no_existing_config(_CONFIG_PATH)
        preflight_collect(test_paths)
        sig = signature(source_rel, test_paths)
        prepare_store(sig, _MUTANTS_DIR)
        _CONFIG_PATH.write_text(render_config(source_rel, test_paths))
        try:
            run_stdout = run_mutmut()
            survivors = parse_survivors(collect_results())
            total = parse_total_mutants(run_stdout)
            write_signature(sig, _MUTANTS_DIR)
            print(format_summary(total, survivors, source_rel, test_paths))
        finally:
            _CONFIG_PATH.unlink(missing_ok=True)
    except MutateError as exc:
        print(f"mutate: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python -m pytest tests/scripts/test_mutate.py -q`
Expected: PASS (whole file green)

- [ ] **Step 5: Lint, type-check, commit**

Run: `uv run ruff check scripts/mutate.py tests/scripts/test_mutate.py && uv run ruff format scripts/mutate.py tests/scripts/test_mutate.py && uv run ty check scripts/mutate.py tests/scripts/test_mutate.py`
Expected: no errors

```bash
git add scripts/mutate.py tests/scripts/test_mutate.py
git commit -m "feat(mutate): add validity guard and main orchestration"
```

---

### Task 6: `just mutate` recipe and end-to-end smoke

**Files:**
- Modify: `justfile`

**Interfaces:**
- Consumes: `scripts/mutate.py` `main` (Task 5).

- [ ] **Step 1: Add the recipe**

Add to `justfile` (after the `test-live-stack` recipe, near the other test recipes):

```makefile
# Mutation-test ONE module against an explicit test path (see docs/development/mutation-testing.md).
# Reports surviving mutants — code changes no test caught. mutmut runs ephemerally (not a locked dep).
#   just mutate src/kdive/domain/errors.py tests/domain/test_errors.py
mutate source *tests:
    uv run --with 'mutmut==3.6.0' python scripts/mutate.py {{source}} {{tests}}
```

- [ ] **Step 2: Verify the recipe lists and rejects bad input**

Run: `just mutate 2>&1 | head -3`
Expected: usage/error text (argparse complains about the missing test path), non-zero exit.

Run: `just mutate src/kdive/domain/errors.py tests/domain/test_nope.py; echo "EXIT=$?"`
Expected: `mutate: test path does not exist: tests/domain/test_nope.py` and `EXIT=1`

- [ ] **Step 3: End-to-end smoke against the verified container-free target**

Run: `just mutate src/kdive/domain/errors.py tests/domain/test_errors.py`
Expected: a summary reporting `10 mutants generated, 2 surviving` and two `survived: kdive.domain.errors...` lines (matches the spike). `git status --short` shows no `setup.cfg` left behind; `mutants/` is gitignored.

- [ ] **Step 4: Commit**

```bash
git add justfile
git commit -m "feat(mutate): add just mutate recipe"
```

---

### Task 7: User documentation

**Files:**
- Create: `docs/development/mutation-testing.md`

**Interfaces:** none (documentation).

- [ ] **Step 1: Write the doc**

Create `docs/development/mutation-testing.md`:

```markdown
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
each survivor. Inspect one with `mutmut show <name>` (or browse interactively with
`mutmut browse`), then add or tighten an assertion until it is killed, and re-run.

## Starting targets

| target module | covering tests | cost |
|---|---|---|
| `src/kdive/domain/errors.py` | `tests/domain/test_errors.py` | container-free (fast) |
| `src/kdive/domain/capacity/state.py` | `tests/services/allocation tests/services/systems` | Postgres-backed |
| `src/kdive/security/authz/gate.py` | `tests/security/authz` | Postgres-backed |
| `src/kdive/security/secrets/redaction.py` | `tests/security` | Postgres-backed |

v1 targets one `.py` file at a time (directory targets are not yet supported). Confirm the
covering tests for any new target yourself — the layout does not mirror `src/`.

## Cost and the Postgres caveat

Runtime depends on the target's tests. Container-free targets run in seconds. Targets whose
tests use the Postgres fixtures (`migrated_url`/`pg_conn`/`postgres_url`) start a disposable
container, so each run is slower — prefer container-free targets for tight iteration.

Container leak warning: mutmut kills slow mutants on timeout, and a killed process may not run
testcontainers cleanup, so a Postgres-backed run can leave orphaned containers. After such a
run, check `docker ps` and remove any leftovers with `docker rm -f <id>`.

## Host prerequisite

mutmut runs ephemerally via `uv run --with 'mutmut==3.6.0'` — nothing to install into the
project. mutmut needs `os.fork` (macOS/Linux; Windows needs WSL). Its `libcst` dependency ships
prebuilt wheels for arm64-darwin and Linux; on **x86_64-darwin** there is no wheel, so install
the Rust toolchain (`rustc`/`cargo`) first or the ephemeral install will fail.

## How it works

The wrapper writes a transient `setup.cfg` `[mutmut]` section, runs mutmut, and removes the
config afterward. mutmut runs the suite from an isolated copy under `mutants/` (gitignored), so
the wrapper copies the whole `src/kdive` package (otherwise `import kdive.*` fails in the copy)
and scopes mutation to your target file with `only_mutate`. The `mutants/` cache is reused when
you re-run the same target and reset when you switch targets.
```

- [ ] **Step 2: Verify doc guards pass**

Run: `just docs-links && just docs-paths && just check-mermaid`
Expected: no errors (the doc has no broken links, no concrete `docs/<path>` references to missing files, no mermaid).

- [ ] **Step 3: Commit**

```bash
git add docs/development/mutation-testing.md
git commit -m "docs(mutate): document the just mutate workflow"
```

---

### Task 8: Verify testcontainers behavior on a Postgres target (spike 5)

**Files:**
- Modify: `docs/development/mutation-testing.md` (only if the observed behavior differs from the warning already written)

**Interfaces:** none (verification + doc correction).

- [ ] **Step 1: Record running containers before the run**

Run: `docker ps -q | sort > /tmp/before.txt; wc -l < /tmp/before.txt`
Expected: a count (baseline).

- [ ] **Step 2: Run a Postgres-backed target end-to-end**

Run: `just mutate src/kdive/security/secrets/redaction.py tests/security`
Expected: a survivor summary (any counts). The run may be slow (Postgres). If `tests/security` turns out not to cover `redaction.py`, substitute the correct covering path found via `rg -l redaction tests`.

- [ ] **Step 3: Check for leaked containers after the run**

Run: `docker ps -q | sort > /tmp/after.txt; comm -13 /tmp/before.txt /tmp/after.txt`
Expected: ideally empty. If lines appear, containers leaked — note how many.

- [ ] **Step 4: Reconcile the doc with reality**

If containers leaked, the warning in `docs/development/mutation-testing.md` is correct as written — leave it. If the run timed out mutants and leaked many, strengthen the warning to recommend running container-free targets only, or add a post-run `docker ps` reminder to the wrapper's summary. If nothing leaked across a run that did time out at least one mutant, soften the warning to "may leak" → "did not leak in testing, but watch `docker ps`". Make at most a one-paragraph doc edit; do not add wrapper cleanup logic unless leaks were severe (that would be a follow-up).

- [ ] **Step 5: Clean up any leaked containers and commit any doc change**

Run: `comm -13 /tmp/before.txt /tmp/after.txt | xargs -r docker rm -f`
Expected: leaked containers removed.

```bash
git add docs/development/mutation-testing.md
git commit -m "docs(mutate): reconcile container-leak note with observed behavior" || echo "no doc change needed"
```

---

## Final verification

- [ ] Run the wrapper unit tests: `uv run python -m pytest tests/scripts/test_mutate.py -q` → all pass.
- [ ] Lint + type the whole tree: `just lint && just type` → clean.
- [ ] Confirm no stray artifacts: `git status --short` shows only intended files; `setup.cfg` is absent; `mutants/` is gitignored.
- [ ] End-to-end: `just mutate src/kdive/domain/errors.py tests/domain/test_errors.py` prints the survivor summary.
```
