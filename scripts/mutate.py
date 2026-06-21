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

MARKER = "# kdive-mutate transient config — delete only when no run is in flight\n"


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
        raise MutateError(
            f"source must be a .py file (directory targets unsupported): {source_arg}"
        )
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
