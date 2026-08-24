# tests/scripts/test_justfile_test_recipes.py
"""Guard the `test` / `test-verbose` / `test-lf` flag split (#2007, ADR-0577).

Three properties the recipes' prose asserts and nothing else enforced. Each has already
drifted or was one edit from drifting:

1. `just test` bounds its failure output with ``--tb=short``. Without it every failing test
   prints a full traceback — ``-q`` trims neither tracebacks nor assertion introspection —
   and a mass failure buries its own cause (#1913).
2. A *scoped* `just test-verbose <paths>` runs serially. It is the recipe you reach for in
   order to read a failure, so interleaving up to 16 xdist workers' output defeats it.
3. All three recipes select the same tier. They deliberately differ on parallelism and
   output, which is what makes a divergent marker expression easy to introduce by hand.

Read through ``just --dry-run``, which expands the variables exactly as a real run does, so
this holds over the command the recipe actually issues rather than over justfile source text.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _ROOT / "justfile"
_JUST = shutil.which("just")

# The repo is driven through `just` (CI runs `just lint` / `just type` / `just test`), so this
# never skips in CI; the guard is for a `just`-less direct-pytest invocation.
pytestmark = pytest.mark.skipif(_JUST is None, reason="just is required to expand a recipe")

#: The marker expression, captured with its quotes so a change in quoting is a difference too.
_MARKERS = re.compile(r'-m "[^"]*"')


def _expand(*args: str) -> str:
    """Return the command line ``just`` would run for ``args``, without running it."""
    assert _JUST is not None
    command = [_JUST, "--justfile", str(_JUSTFILE), "--working-directory", str(_ROOT), "--dry-run"]
    result = subprocess.run(
        [*command, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    # --dry-run echoes the expanded line to stderr, leaving stdout for the recipe's own output.
    return result.stderr.strip()


def test_the_default_suite_bounds_its_failure_output() -> None:
    assert "--tb=short" in _expand("test"), (
        "`just test` must pass --tb=short (ADR-0577): -q bounds nothing on the failure path, "
        "so without it one broken fixture prints a full traceback per failing test"
    )


def test_a_scoped_verbose_run_is_serial() -> None:
    expanded = _expand("test-verbose", "tests/domain/test_errors.py")
    assert " -n " not in expanded, (
        "a scoped `just test-verbose <paths>` must drop xdist (ADR-0577): it is the recipe for "
        f"reading a failure, and interleaved worker output defeats that; got: {expanded}"
    )


def test_an_unscoped_verbose_run_keeps_the_parallelism() -> None:
    assert " -n auto " in _expand("test-verbose"), (
        "`just test-verbose` with no paths runs the whole suite, which is not a loop anyone "
        "waits on serially — it keeps xdist"
    )


@pytest.mark.parametrize("recipe", ["test-verbose", "test-lf"])
def test_every_recipe_selects_the_same_tier(recipe: str) -> None:
    gate = _MARKERS.search(_expand("test"))
    assert gate is not None, "`just test` no longer carries a -m expression"
    other = _MARKERS.search(_expand(recipe))
    assert other is not None, f"`just {recipe}` no longer carries a -m expression"
    assert other.group(0) == gate.group(0), (
        f"`just {recipe}` selects {other.group(0)} but `just test` selects {gate.group(0)} — "
        "the gated-tier exclusion is shared through _TEST_MARKERS so it cannot drift"
    )
