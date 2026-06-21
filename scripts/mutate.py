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

import argparse
import re
import shutil
import subprocess
import sys
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
    )


_RESULT_LINE = re.compile(r"^\s*(\S+):\s*(.+?)\s*$")
_PROGRESS = re.compile(r"(\d+)/(\d+)")


def parse_survivors(results_stdout: str) -> list[tuple[str, str]]:
    """Return ``(name, status)`` for every mutant mutmut did not kill.

    ``mutmut results`` prints one ``<name>: <status>`` line per non-killed mutant,
    where status is any of mutmut's exit-code labels (survived, no tests, skipped,
    suspicious, timeout, segfault, caught by type check, not checked). We match the
    status open-endedly and exclude only ``killed`` so a status the wrapper never
    anticipated can never be silently dropped from the count.
    """
    survivors: list[tuple[str, str]] = []
    for line in results_stdout.splitlines():
        match = _RESULT_LINE.match(line)
        if match and match.group(2) != "killed":
            survivors.append((match.group(1), match.group(2)))
    return survivors


def parse_total_mutants(run_stdout: str) -> int | None:
    """Return the total mutant count from the last ``N/N`` progress token, if any."""
    matches = _PROGRESS.findall(run_stdout)
    if not matches:
        return None
    return int(matches[-1][1])


def format_summary(
    total: int | None,
    survivors: list[tuple[str, str]],
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
    for name, status in survivors:
        lines.append(f"    survived: {name} [{status}]")
    if survivors:
        lines.append("  inspect a survivor: mutmut show <name>  (or: mutmut browse)")
        lines.append(
            "  each surviving mutant is a code change the tests did not catch"
            " — add or strengthen a test."
        )
    lines.append("  note: result is relative to the test path supplied.")
    return "\n".join(lines) + "\n"


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


_CONFIG_PATH = _ROOT / "setup.cfg"
_MUTANTS_DIR = _ROOT / "mutants"


def _run_subprocess(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=_ROOT, text=True, capture_output=True, check=False)


def preflight_collect(test_paths: list[str], runner=_run_subprocess) -> None:
    """Cheap repo-root check: collect-only the scoped tests; abort on bad/empty path."""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--co",
        "-q",
        "-m",
        "not live_vm and not live_stack",
        *test_paths,
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
    parser.add_argument("source", help="source .py file under src/kdive")
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
