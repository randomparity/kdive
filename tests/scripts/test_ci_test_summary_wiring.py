"""The two halves of the CI test-failure summary have to keep agreeing (#2062).

`ci.yml` asks the gate's pytest run for a JUnit report and then renders that report into the
job summary from a second step. Nothing else ties the two together: they are joined only by a
path that appears twice, and by a family flag whose loss degrades the summary silently rather
than failing anything. Both are one careless edit away, and neither shows up as a red run —
the failure mode is a green pipeline whose summary is quietly empty or quietly less useful,
noticed only by whoever next has to diagnose a red PR without it.

The properties below are exactly the ones a passing CI run cannot demonstrate.
"""

from __future__ import annotations

import pathlib
import re
from collections.abc import Callable

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_SCRIPT = "scripts/pytest_summary.py"

#: The report path as `--junit-xml=` carries it, and as the summary step's env carries it.
#: Read here before Actions substitutes it, so `${{ runner.temp }}` is still literal — and it
#: contains spaces, which is why this is not a plain `\S+`. (At run time the expression is
#: substituted before the variable is set, so the value pytest shlex-splits has no space.)
_JUNIT_PATH = re.compile(r"--junit-xml=((?:\$\{\{[^}]*\}\}|\S)+)")


def _steps() -> list[dict]:
    workflow = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    return workflow["jobs"]["lint-type-test"]["steps"]


def _step(predicate: Callable[[dict], bool]) -> dict:
    matches = [step for step in _steps() if predicate(step)]
    assert len(matches) == 1, f"expected exactly one matching step, found {len(matches)}"
    return matches[0]


def _test_step() -> dict:
    return _step(lambda step: step.get("run", "").strip() == "just test")


def _summary_step() -> dict:
    return _step(lambda step: _SCRIPT in step.get("run", ""))


def test_the_gate_still_runs_the_recipe_verbatim() -> None:
    # The report is requested through PYTEST_ADDOPTS specifically so that CI keeps invoking
    # `just test` unchanged — the justfile stays the single definition of the gate's command.
    # Moving the flags onto the command line here would fork that definition.
    assert _test_step()["run"].strip() == "just test"


def test_the_gate_run_produces_a_junit_report() -> None:
    addopts = _test_step()["env"]["PYTEST_ADDOPTS"]
    assert _JUNIT_PATH.search(addopts), (
        "the test step no longer asks for a JUnit report, so the summary step below has "
        "nothing to render"
    )


def test_the_report_keeps_the_family_that_carries_file_paths() -> None:
    # pytest's default `xunit2` family has no `file` attribute, and without it the summary
    # degrades from `tests/x/test_y.py::TestZ::test_a` to a dotted classname that cannot be
    # pasted back into pytest. That degradation is silent: the summary still renders.
    addopts = _test_step()["env"]["PYTEST_ADDOPTS"]
    assert "junit_family=xunit1" in addopts


def test_the_summary_step_reads_the_report_the_gate_wrote() -> None:
    # The one path that appears in two places. If they drift the summary step finds no report
    # and says so — in a job that is otherwise green, where nobody is looking.
    written = _JUNIT_PATH.search(_test_step()["env"]["PYTEST_ADDOPTS"])
    assert written is not None
    read = _summary_step()["env"]["PYTEST_JUNIT_REPORT"]
    assert read == written.group(1), (
        f"the summary step reads {read!r} but the gate writes {written.group(1)!r}"
    )


def test_the_summary_step_runs_after_a_failing_gate() -> None:
    # Without `if: always()` the step is skipped exactly when it is wanted: GitHub skips
    # subsequent steps once one fails, so the summary would only ever render on green runs.
    assert _summary_step()["if"] == "always()"


def test_the_summary_step_cannot_fail_the_job() -> None:
    # `just test`'s exit code is the gate. A reporting step that can go red turns an
    # infrastructure hiccup into a failed PR, which is a worse outcome than no summary.
    assert _summary_step()["continue-on-error"] is True


def test_the_summary_step_uses_the_project_interpreter() -> None:
    # scripts/ is linted at the project's py314 target, so the ambient python3 on the runner
    # can die on a SyntaxError — which `continue-on-error` would then hide completely.
    assert _summary_step()["run"].startswith("uv run python ")


def test_the_summary_script_exists() -> None:
    assert (_ROOT / _SCRIPT).is_file()
