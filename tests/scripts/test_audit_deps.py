"""The shell half of the supply-chain gate: what a classification becomes as a CI verdict.

``tests/scripts/test_audit_report.py`` proves the classifier. This proves the part that acts on
it — the retry loop, the rule that a verdict is never retried, the agreement check against
pip-audit's own exit status, and the runtime-fails / dev-warns exit contracts (ADR-0553).

Leaving this untested is how #1913's first implementation shipped a gate that could not run:
every in-process test passed while the script itself was dead. So these drive
``scripts/audit-deps.sh`` as a subprocess, with a stub ``uv`` on PATH standing in for the
network. ``sleep`` is stubbed out alongside it — the backoff is a real boundary to mock, and
stubbing it keeps the retry cases instant instead of costing 20s each.

The stub records every invocation, so these assert *how many times* pip-audit ran. That count
is the direct evidence for the acceptance criterion: a genuine advisory must be classified on
the first attempt and never retried.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "audit-deps.sh"

_ADVISORY = '{"dependencies": [{"name": "acme", "version": "1.0", "vulns": [{"id": "PYSEC-1"}]}]}'
_CLEAN = '{"dependencies": [{"name": "acme", "version": "1.0", "vulns": []}]}'

_STUB_UV = """#!/usr/bin/env bash
echo "$*" >> "$STUB_CALLS"
case "$1" in
  export) echo "acme==1.0"; exit 0 ;;
  run)
    shift
    if [ "$1" = "python" ]; then shift; exec "$STUB_PYTHON" "$@"; fi
    cat "$STUB_REPORT"
    exit "${STUB_AUDIT_EXIT:-0}"
    ;;
esac
exit 99
"""


class _Run:
    def __init__(self, completed: subprocess.CompletedProcess[str], calls: Path) -> None:
        self.exit_code = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        self.audit_calls = [line for line in lines if "pip-audit" in line]


@pytest.fixture
def run_audit(tmp_path: Path) -> object:
    """Drive ``audit-deps.sh`` with a stubbed ``uv`` and an instant ``sleep``."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    (stub_dir / "uv").write_text(_STUB_UV, encoding="utf-8")
    (stub_dir / "uv").chmod(0o755)
    # A no-op sleep: the backoff is real elapsed time, which is a boundary, not logic.
    (stub_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (stub_dir / "sleep").chmod(0o755)
    # A poisoned `python3`. The classifier is linted at the project's target-version, so ruff
    # rewrites it to syntax an older ambient interpreter cannot parse — which is how #1913's
    # first attempt shipped a dead gate. The script must reach it through `uv run python`, and
    # nothing else asserts that: with a working system python3 a regression to the bare form
    # passes every other test here. Failing the stub makes the invariant load-bearing.
    (stub_dir / "python3").write_text(
        '#!/usr/bin/env bash\necho "audit-deps.sh must not use ambient python3" >&2\nexit 127\n',
        encoding="utf-8",
    )
    (stub_dir / "python3").chmod(0o755)

    def _run(
        mode: str,
        report: str,
        audit_exit: int = 0,
        summary: Path | None = None,
        classifier_exit: int | None = None,
    ) -> _Run:
        import os
        import sys

        report_path = tmp_path / "report.json"
        report_path.write_text(report, encoding="utf-8")
        calls = tmp_path / "calls.txt"
        calls.write_text("", encoding="utf-8")

        python = sys.executable
        if classifier_exit is not None:
            # Stand in for a classifier that died rather than answered — a missing interpreter
            # (127), an OOM kill (137), an uncaught exception (1).
            broken = stub_dir / "broken-python"
            broken.write_text(f"#!/usr/bin/env bash\nexit {classifier_exit}\n", encoding="utf-8")
            broken.chmod(0o755)
            python = str(broken)

        env = dict(os.environ)
        env.pop("GITHUB_STEP_SUMMARY", None)
        env["PATH"] = f"{stub_dir}{os.pathsep}{env['PATH']}"
        env["STUB_REPORT"] = str(report_path)
        env["STUB_CALLS"] = str(calls)
        env["STUB_PYTHON"] = python
        env["STUB_AUDIT_EXIT"] = str(audit_exit)
        if summary is not None:
            env["GITHUB_STEP_SUMMARY"] = str(summary)

        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(_SCRIPT), mode],
            capture_output=True,
            text=True,
            check=False,
            cwd=_ROOT,
            env=env,
        )
        return _Run(completed, calls)

    return _run


def test_a_genuine_advisory_fails_the_gate_on_the_first_attempt(run_audit) -> None:  # noqa: ANN001
    # The acceptance criterion for #1913, asserted end to end. pip-audit exits 1 for an
    # advisory and for a timeout alike, so the count is the evidence that matters: one call
    # means the verdict was read, not retried.
    result = run_audit("runtime", _ADVISORY, audit_exit=1)

    assert result.exit_code == 1
    assert len(result.audit_calls) == 1, (
        f"a real advisory must never be retried, ran {len(result.audit_calls)}x"
    )
    assert "acme 1.0: PYSEC-1" in result.stderr


def test_a_clean_audit_passes(run_audit) -> None:  # noqa: ANN001
    result = run_audit("runtime", _CLEAN, audit_exit=0)

    assert result.exit_code == 0
    assert len(result.audit_calls) == 1
    assert "No known advisories" in result.stdout


def test_a_transport_failure_is_retried_then_fails_closed(run_audit) -> None:  # noqa: ANN001
    result = run_audit("runtime", "", audit_exit=1)

    assert result.exit_code == 1, "an audit that never completed must not pass"
    assert len(result.audit_calls) == 3, (
        f"expected the full attempt budget, ran {len(result.audit_calls)}x"
    )


def test_a_clean_report_from_a_failed_run_never_goes_green(run_audit) -> None:  # noqa: ANN001
    # The `--strict` collection-abort case: the report parses and looks clean, but pip-audit
    # itself failed. The two disagree, and a disagreement this script cannot explain must not
    # be the one path to exit 0.
    result = run_audit("runtime", _CLEAN, audit_exit=1)

    assert result.exit_code != 0
    assert len(result.audit_calls) == 3, "a disagreement is treated as no verdict, so retried"


def test_dev_mode_reports_an_advisory_without_failing_the_build(  # noqa: ANN001
    run_audit, tmp_path: Path
) -> None:
    summary = tmp_path / "summary.md"
    result = run_audit("dev", _ADVISORY, audit_exit=1, summary=summary)

    assert result.exit_code == 0, "dev advisories do not ship to users and never fail the build"
    assert "::warning::" in result.stdout
    assert "acme 1.0: PYSEC-1" in summary.read_text(encoding="utf-8")


def test_dev_mode_says_so_in_the_summary_when_it_finds_nothing(  # noqa: ANN001
    run_audit, tmp_path: Path
) -> None:
    # The dev job reports only through the summary, so an empty one is indistinguishable from
    # a step that never ran.
    summary = tmp_path / "summary.md"
    result = run_audit("dev", _CLEAN, audit_exit=0, summary=summary)

    assert result.exit_code == 0
    assert "No known advisories" in summary.read_text(encoding="utf-8")


@pytest.mark.parametrize("classifier_exit", [1, 127, 137], ids=["crash", "no-interpreter", "oom"])
def test_a_broken_classifier_reads_as_no_verdict_not_as_an_advisory(  # noqa: ANN001
    run_audit, classifier_exit: int
) -> None:
    """A classifier that died must not be reported as a discovered advisory.

    The classifier's contract is exactly 0, 1, 2. Python exits 1 on an uncaught exception,
    127 when the interpreter is absent, 137 under the OOM killer — and 1 is the FOUND code.
    Untreated, every one of those becomes `pip-audit found advisories in the shipped
    dependency set`, naming none. That is the misdiagnosis that made #1913's first attempt
    look like a security finding instead of a broken gate, so pin it: these are incomplete
    audits, which means retried and then failed, never a phantom finding.
    """
    result = run_audit("runtime", _ADVISORY, audit_exit=1, classifier_exit=classifier_exit)

    assert result.exit_code == 1, "an audit that produced no verdict must still fail"
    assert len(result.audit_calls) == 3, "a dead classifier is no verdict, so it is retried"
    assert "found advisories" not in result.stderr, (
        "a broken classifier must not be reported as a discovered advisory"
    )
    assert "no verdict" in result.stderr


def test_an_unknown_mode_is_rejected_rather_than_guessed(run_audit) -> None:  # noqa: ANN001
    result = run_audit("bogus", _CLEAN)

    assert result.exit_code == 2
    assert not result.audit_calls, "a bad mode must not reach the network"
