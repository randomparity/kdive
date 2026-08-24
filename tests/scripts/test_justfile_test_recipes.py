# tests/scripts/test_justfile_test_recipes.py
"""Guard the `test` / `test-verbose` / `test-lf` / `test-changed` flag split (#2007, ADR-0577).

Four properties the recipes' prose asserts and nothing else enforced. Each has already
drifted or was one edit from drifting:

1. Every suite recipe bounds its failure output with ``--tb=short``. Without it every failing
   test prints a traceback under pytest's ``--tb=auto`` default, whose first and last frames
   carry full source context and argument values — ``-q`` trims neither those nor assertion
   introspection — and a mass failure buries its own cause (#1913). `test-lf` and
   `test-changed` are included because both fall back to the whole suite: an empty ``--lf``
   cache, an unmappable change.
2. `just test-verbose` with *any* argument runs serially. It is the recipe you reach for in
   order to read a failure, so interleaving up to 16 xdist workers' output defeats it. Bare,
   it keeps the parallelism, because the whole suite serially is not a loop anyone waits on.
3. Every whole-suite recipe still runs under xdist. Splitting the shared variable put
   parallelism on its own axis, so losing it is a one-line edit that changes nothing a test
   would otherwise notice — only how long the gate takes.
4. All four suite recipes select the same tier. They deliberately differ on parallelism and
   output, which is what makes a divergent marker expression easy to introduce by hand.

Read through ``just --dry-run``, which expands the variables exactly as a real run does, so
this holds over the command the recipe actually issues rather than over justfile source text.

Assertions are over the *effective* flag, never over substring presence: pytest takes the
last ``--tb`` on the line, so ``--tb=short --tb=long`` contains "--tb=short" and does not do
it. Likewise xdist is looked for under both spellings, so renaming ``-n`` to
``--numprocesses`` cannot satisfy a negative assertion by accident.
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

#: Every traceback style on the line, so the *effective* (last) one can be asserted.
_TB = re.compile(r"--tb[= ](\w+)")
#: Both spellings of the xdist worker-count flag, plus the flags that only make sense with it.
_PARALLEL = re.compile(
    r"(?:^|\s)(?:-n\s*\S+|--numprocesses[= ]?\S+|--maxprocesses[= ]?\S+|--dist[= ]?\S+)"
)
#: The marker expression as `-m "…"` passes it, captured without its quotes.
_DASH_M = re.compile(r'-m "([^"]*)"')
#: `test-changed` is a bash recipe: it takes the same expression as a shell variable.
_MARKS_VAR = re.compile(r'marks="([^"]*)"')
#: The tier every suite recipe selects. Pinned to the literal, not only compared across
#: recipes: sharing one variable makes the recipes agree with each other by construction, so
#: a narrowing edit to it would keep every relative assertion green while the gate silently
#: stopped covering a tier.
_TIER = "not live_vm and not live_stack and not agent_smoke"


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


def test_the_gate_selects_the_expected_tier() -> None:
    gate = _DASH_M.search(_expand("test"))
    assert gate is not None, "`just test` no longer carries a -m expression"
    assert gate.group(1) == _TIER, (
        f"`just test` selects {gate.group(1)!r}, not {_TIER!r} — widening or narrowing the "
        "gated tier is a deliberate two-place edit, not a one-line justfile change"
    )


@pytest.mark.parametrize("recipe", ["test", "test-lf", "test-verbose"])
def test_every_suite_recipe_pins_the_hash_seed(recipe: str) -> None:
    # xdist aborts with "Different tests were collected" when workers disagree on the order a
    # set-backed parametrize yields, so the pin is load-bearing wherever xdist runs.
    assert 'PYTHONHASHSEED="${PYTHONHASHSEED:-0}"' in _expand(recipe), (
        f"`just {recipe}` no longer pins PYTHONHASHSEED, so its xdist workers can disagree "
        "on collection order"
    )


@pytest.mark.parametrize("recipe", ["test", "test-lf"])
def test_the_suite_recipes_stay_parallel(recipe: str) -> None:
    # The mirror of the bare-verbose assertion. Splitting _TEST_SELECT put parallelism on its
    # own axis, so losing xdist from a suite recipe is now a one-line edit that changes only
    # how long the gate takes — nothing else here would notice.
    assert _PARALLEL.search(_expand(recipe)) is not None, (
        f"`just {recipe}` must run under xdist: it is a whole-suite recipe, and serially the "
        "suite is not a gate anyone waits on"
    )


@pytest.mark.parametrize("recipe", ["test", "test-lf"])
def test_the_suite_recipes_bound_their_failure_output(recipe: str) -> None:
    assert _TB.findall(_expand(recipe)) == ["short"], (
        f"`just {recipe}` must pass exactly one --tb, and it must be short (ADR-0577): -q "
        "bounds nothing on the failure path, so without it one broken fixture prints a full "
        "traceback per failing test"
    )


def test_the_changed_test_recipe_bounds_every_invocation() -> None:
    # A bash recipe with two pytest invocations — the changed-target one and the full-suite
    # fallback. The fallback is the one that can run all 13,000 tests, so it needs the bound
    # at least as much as the gate does.
    lines = [
        line
        for line in _expand("test-changed").splitlines()
        if "python -m pytest" in line and not line.lstrip().startswith("#")
    ]
    assert len(lines) == 2, f"expected two pytest invocations in test-changed, got {len(lines)}"
    for line in lines:
        assert _TB.findall(line) == ["short"], f"unbounded pytest invocation: {line.strip()}"
        assert _PARALLEL.search(line) is not None, f"serial pytest invocation: {line.strip()}"
        # The tier check below reads the `marks=` assignment; this is what ties the two pytest
        # invocations to it, so dropping `-m "$marks"` from one cannot pass both.
        assert '-m "$marks"' in line, f"invocation does not use the shared tier: {line.strip()}"


def test_the_verbose_recipe_keeps_every_frame() -> None:
    assert _TB.findall(_expand("test-verbose")) == ["long"], (
        "`just test-verbose` is the escalation from `just test`'s --tb=short, so it must pass "
        "exactly one --tb and it must be long"
    )


@pytest.mark.parametrize(
    "argument",
    [
        pytest.param("tests/domain/test_errors.py", id="path"),
        # Any argument drops xdist, not only a path: `-x` and `--pdb` narrow nothing but want
        # serial ordering just as much, and the recipe's condition tests for arguments.
        pytest.param("-x", id="flag"),
    ],
)
def test_a_verbose_run_with_arguments_is_serial(argument: str) -> None:
    expanded = _expand("test-verbose", argument)
    assert _PARALLEL.search(expanded) is None, (
        "`just test-verbose <args>` must drop xdist (ADR-0577): it is the recipe for reading a "
        f"failure, and interleaved worker output defeats that; got: {expanded}"
    )
    # Dropping xdist must drop only xdist: the serial branch is a separate expansion, so it is
    # where the tier exclusion or the verbose output could silently go missing.
    assert _TB.findall(expanded) == ["long"], f"the serial branch lost --tb=long: {expanded}"
    selected = _DASH_M.search(expanded)
    gate = _DASH_M.search(_expand("test"))
    assert selected is not None and gate is not None
    assert selected.group(1) == gate.group(1), (
        f"the serial branch selects a different tier: {expanded}"
    )
    # The conditional sits between the flags and {{PATHS}}, so an edit there can swallow the
    # arguments entirely — which reads as a green whole-suite run, not as an error.
    assert expanded.rstrip().endswith(argument), f"the argument never reached pytest: {expanded}"


def test_a_bare_verbose_run_keeps_the_parallelism() -> None:
    assert _PARALLEL.search(_expand("test-verbose")) is not None, (
        "bare `just test-verbose` runs the whole suite, which is not a loop anyone waits on "
        "serially — it keeps xdist"
    )


@pytest.mark.parametrize("recipe", ["test-verbose", "test-lf"])
def test_every_recipe_selects_the_same_tier(recipe: str) -> None:
    gate = _DASH_M.search(_expand("test"))
    assert gate is not None, "`just test` no longer carries a -m expression"
    other = _DASH_M.search(_expand(recipe))
    assert other is not None, f"`just {recipe}` no longer carries a -m expression"
    assert other.group(1) == gate.group(1), (
        f"`just {recipe}` selects {other.group(1)!r} but `just test` selects {gate.group(1)!r} "
        "— the gated-tier exclusion is shared through _TEST_MARKERS so it cannot drift"
    )


def test_the_changed_test_recipe_selects_the_same_tier() -> None:
    gate = _DASH_M.search(_expand("test"))
    assert gate is not None, "`just test` no longer carries a -m expression"
    # A bash-shebang recipe: --dry-run echoes its body, where the expression is a shell value.
    marks = _MARKS_VAR.search(_expand("test-changed"))
    assert marks is not None, "`just test-changed` no longer assigns a marks= expression"
    assert marks.group(1) == gate.group(1), (
        f"`just test-changed` selects {marks.group(1)!r} but `just test` selects "
        f"{gate.group(1)!r} — both take it from _TEST_MARKERS so it cannot drift"
    )
