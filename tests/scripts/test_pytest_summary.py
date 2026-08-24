"""The CI test step's compact failure artifact (#2062).

Diagnosing a red PR currently means reading the raw job log: on the last failed `ci.yml` run
`gh run view --log-failed` was 74,735 bytes and `gh run view --log` 539,478 bytes, every line
timestamp- and job-prefixed. `--tb=short` (ADR-0577) shrinks that but does not replace it —
pytest disables assertion-explanation truncation whenever `CI` is set, so the traceback bound
buys less in CI than locally. `scripts/pytest_summary.py` renders the run's JUnit report into
`$GITHUB_STEP_SUMMARY`, which is read from the run page without pulling a log at all.

Two properties decide whether it is worth having, and both are asserted here rather than
inferred from a green CI run:

1. **It never decides anything.** The test step's exit code is the gate. This runs after it,
   reads a file, and returns 0 for every input — a missing report, a truncated one, XML that
   does not parse. A reporting step that can fail is a reporting step that can redden a green
   gate.
2. **What it prints is addressable.** A node id you can paste back into `just test-verbose`,
   and the reason, bounded so that a mass failure cannot reproduce the 75 KB problem inside
   the summary.

The fixtures below are **real** `pytest 9.1.1 --junit-xml` output captured from a run with a
plain assertion failure, a fixture (setup) error, a skip, two parametrized failures whose ids
contain a space and angle brackets, and a class-based failure — not hand-invented shapes. Both
JUnit families appear: pytest defaults to `xunit2`, which drops the `file` and `line`
attributes, and CI asks for `xunit1`, which keeps them and is what makes an exact node id
reconstructable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.pytest_summary import MAX_FAILURES, main, summarize

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pytest_summary.py"

#: Real `pytest -o junit_family=xunit1 --junit-xml` output. `file`/`line` present.
_XUNIT1 = """<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite \
name="pytest" errors="1" failures="4" skipped="1" tests="7" time="0.028" \
timestamp="2026-08-24T12:17:40.846173-07:00" hostname="runner"><testcase \
classname="tests.sample.test_sample" name="test_passes" file="tests/sample/test_sample.py" \
line="3" time="0.000" /><testcase classname="tests.sample.test_sample" name="test_fails" \
file="tests/sample/test_sample.py" line="7" time="0.000"><failure message="assert 1 == 2">\
tests/sample/test_sample.py:9: in test_fails
    assert 1 == 2
E   assert 1 == 2</failure></testcase><testcase classname="tests.sample.test_sample" \
name="test_errors" file="tests/sample/test_sample.py" line="16" time="0.000"><error \
message="failed on setup with &quot;RuntimeError: fixture blew up&quot;">\
tests/sample/test_sample.py:14: in broken_fixture
    raise RuntimeError("fixture blew up")
E   RuntimeError: fixture blew up</error></testcase><testcase \
classname="tests.sample.test_sample" name="test_skipped" file="tests/sample/test_sample.py" \
line="20" time="0.000"><skipped type="pytest.skip" message="nope">\
/abs/tests/sample/test_sample.py:21: nope</skipped></testcase><testcase \
classname="tests.sample.test_sample" name="test_param[a b]" \
file="tests/sample/test_sample.py" line="26" time="0.000"><failure \
message="AssertionError: assert 'a b' == 'zzz'&#10;  &#10;  - zzz&#10;  + a b">\
tests/sample/test_sample.py:28: in test_param
    assert value == "zzz"</failure></testcase><testcase classname="tests.sample.test_sample" \
name="test_param[c&lt;d&gt;]" file="tests/sample/test_sample.py" line="26" time="0.000">\
<failure message="AssertionError: assert 'c&lt;d&gt;' == 'zzz'">\
tests/sample/test_sample.py:28: in test_param
    assert value == "zzz"</failure></testcase><testcase \
classname="tests.sample.test_sample.TestGrouped" name="test_in_class" \
file="tests/sample/test_sample.py" line="32" time="0.000"><failure \
message="AssertionError: class failure&#10;assert False">\
tests/sample/test_sample.py:33: in test_in_class
    assert False, "class failure"</failure></testcase></testsuite></testsuites>"""

#: The same run under pytest's default `xunit2` family, which carries no `file` attribute.
_XUNIT2 = """<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite \
name="pytest" errors="0" failures="1" skipped="0" tests="2" time="0.026" \
timestamp="2026-08-24T12:17:25.152246-07:00" hostname="runner"><testcase \
classname="tests.sample.test_sample" name="test_passes" time="0.000" /><testcase \
classname="tests.sample.test_sample.TestGrouped" name="test_in_class" time="0.000"><failure \
message="AssertionError: class failure">boom</failure></testcase></testsuite></testsuites>"""

#: A run in which nothing failed. The summary still has to say so — see the test below.
_GREEN = """<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite \
name="pytest" errors="0" failures="0" skipped="2" tests="8941" time="272.4" \
timestamp="2026-08-24T12:17:25.152246-07:00" hostname="runner"><testcase \
classname="tests.sample.test_sample" name="test_passes" time="0.000" /></testsuite>\
</testsuites>"""


def _report(tmp_path: Path, raw: str) -> Path:
    path = tmp_path / "pytest-junit.xml"
    path.write_text(raw, encoding="utf-8")
    return path


def _bulk(count: int, *, reason: str = "AssertionError: assert 0") -> str:
    cases = "".join(
        f'<testcase classname="tests.bulk.test_bulk" name="test_{index}" '
        f'file="tests/bulk/test_bulk.py" line="1" time="0.0">'
        f'<failure message="{reason}">frame</failure></testcase>'
        for index in range(count)
    )
    return (
        f'<testsuites><testsuite name="pytest" errors="0" failures="{count}" skipped="0" '
        f'tests="{count}" time="1.0">{cases}</testsuite></testsuites>'
    )


def test_every_failure_is_named_with_a_runnable_node_id(tmp_path: Path) -> None:
    # The whole point of the artifact: a reader can re-run the failure without opening the log.
    # Class-based and parametrized ids are the two that a naive `classname` split gets wrong,
    # and both are silent — they produce a plausible-looking id that pytest cannot select.
    summary = summarize(_report(tmp_path, _XUNIT1))
    assert "tests/sample/test_sample.py::test_fails" in summary
    assert "tests/sample/test_sample.py::test_errors" in summary
    assert "tests/sample/test_sample.py::test_param[a b]" in summary
    assert "tests/sample/test_sample.py::test_param[c<d>]" in summary
    assert "tests/sample/test_sample.py::TestGrouped::test_in_class" in summary


def test_the_reason_travels_with_the_node_id(tmp_path: Path) -> None:
    # A node id alone says which test broke, not why — which is the half that decides whether
    # the reader still has to fetch the log.
    summary = summarize(_report(tmp_path, _XUNIT1))
    assert "assert 1 == 2" in summary
    assert "RuntimeError: fixture blew up" in summary
    assert "class failure" in summary


def test_a_setup_error_is_reported_as_an_error_not_a_failure(tmp_path: Path) -> None:
    # pytest counts them separately and they mean different things — a failure is a broken
    # assertion, an error is a test that never ran. Collapsing them hides a broken fixture
    # inside a wall of assertion failures it caused.
    summary = summarize(_report(tmp_path, _XUNIT1))
    assert "4 failed" in summary
    assert "1 error" in summary


def test_only_the_failures_are_listed(tmp_path: Path) -> None:
    # 8,900 passing node ids would rebuild the problem the summary exists to solve.
    summary = summarize(_report(tmp_path, _XUNIT1))
    assert "test_passes" not in summary
    assert "test_skipped" not in summary


def test_a_green_run_still_renders_a_summary(tmp_path: Path) -> None:
    # An empty summary is indistinguishable from a step that never ran (the same reasoning
    # scripts/audit-deps.sh records for the dev audit). It is also the only way the mechanism
    # can be shown to work on a run that is not red.
    summary = summarize(_report(tmp_path, _GREEN))
    assert summary.strip(), "a passing run must still leave a totals line"
    assert "8941 tests" in summary
    assert "8939 passed" in summary
    assert "0 failed" in summary


def test_a_report_without_file_attributes_still_names_every_failure(tmp_path: Path) -> None:
    # pytest's default junit_family is xunit2, which drops `file`. CI asks for xunit1, so this
    # is the degraded path: if a future pytest drops xunit1, the summary must lose id precision
    # rather than stop naming failures.
    summary = summarize(_report(tmp_path, _XUNIT2))
    assert "test_in_class" in summary
    assert "tests.sample.test_sample.TestGrouped" in summary
    assert "1 failed" in summary


def test_a_missing_report_says_so_and_names_the_path(tmp_path: Path) -> None:
    # The crash case: pytest killed before it wrote a report. Silence here reads as "no
    # failures", which is the one thing it must never read as.
    summary = summarize(tmp_path / "absent.xml")
    assert "absent.xml" in summary
    assert summary.strip()


def test_unparseable_xml_says_so_rather_than_reporting_a_clean_run(tmp_path: Path) -> None:
    # A truncated report is what a SIGKILL mid-write leaves behind.
    summary = summarize(_report(tmp_path, "<testsuites><testsuite failures="))
    assert summary.strip()
    assert "0 failed" not in summary


def test_the_failure_list_is_bounded(tmp_path: Path) -> None:
    # The artifact exists because 75 KB of log is unreadable; a summary that reproduces it at
    # the same size has bought nothing. GitHub also drops a step summary over 1 MiB outright,
    # so an unbounded list can delete the artifact it was meant to be.
    summary = summarize(_report(tmp_path, _bulk(500)))
    assert summary.count("tests/bulk/test_bulk.py::test_") == MAX_FAILURES
    assert "500 failed" in summary
    assert f"{500 - MAX_FAILURES} more" in summary
    assert len(summary.encode("utf-8")) < 900_000


def test_enormous_node_ids_hit_the_byte_ceiling(tmp_path: Path) -> None:
    # Capping the count and each reason still leaves the node ids unbounded: a parametrized id
    # is built from the parameter's repr, so a test over a large fixture produces a single very
    # long id, and 50 of those clear a megabyte between them. Node ids are deliberately not
    # truncated — a cut id is no longer pasteable — so the byte ceiling is what holds, and
    # GitHub drops an oversized summary outright rather than trimming it.
    cases = "".join(
        f'<testcase classname="tests.bulk.test_bulk" name="test[{"q" * 40_000}-{index}]" '
        f'file="tests/bulk/test_bulk.py" line="1" time="0.0">'
        f'<failure message="AssertionError">frame</failure></testcase>'
        for index in range(MAX_FAILURES)
    )
    report = (
        f'<testsuites><testsuite name="pytest" errors="0" failures="{MAX_FAILURES}" '
        f'skipped="0" tests="{MAX_FAILURES}" time="1.0">{cases}</testsuite></testsuites>'
    )
    summary = summarize(_report(tmp_path, report))
    assert len(summary.encode("utf-8")) <= 900_100
    assert "summary truncated" in summary


def test_one_enormous_reason_cannot_blow_the_budget(tmp_path: Path) -> None:
    # `CI` being set disables pytest's assertion-explanation truncation (ADR-0577), so a single
    # failure comparing two large structures carries a reason with no upper bound of its own.
    summary = summarize(_report(tmp_path, _bulk(1, reason="x" * 50_000)))
    assert len(summary.encode("utf-8")) < 5_000


def test_a_backtick_in_a_reason_cannot_escape_its_code_span(tmp_path: Path) -> None:
    # GitHub renders the summary as markdown with HTML allowed, and a reason is arbitrary test
    # data. Node ids and reasons are printed inside code spans, which neutralises `<img …>` and
    # every other markdown construct — except a backtick, which closes the span and lets the
    # rest of the line out. `repr()` of a string in an assertion diff is a routine way to get
    # one, so this is the escape that has to be handled rather than relied upon.
    summary = summarize(_report(tmp_path, _bulk(1, reason="a `b` &lt;img src=x&gt;")))
    line = next(line for line in summary.splitlines() if "test_0" in line)
    assert "`b`" not in line, "a backtick in a reason escaped its code span"
    # The spans are what make the rest safe, so both have to still be spans: the line carries
    # exactly two, one around the node id and one around the reason.
    assert line.count("`") == 4, f"code spans are unbalanced: {line}"


def test_angle_brackets_in_a_node_id_survive_verbatim(tmp_path: Path) -> None:
    # The mirror of the test above: escaping is confined to backticks precisely so a
    # parametrized id stays pasteable. `test_param[c<d>]` must come back as it was.
    summary = summarize(_report(tmp_path, _XUNIT1))
    assert "test_param[c<d>]" in summary


def test_main_returns_zero_for_every_input(tmp_path: Path) -> None:
    # The property the CI step depends on. `render` deciding nothing is what lets the summary
    # be additive to a gate whose exit code is the verdict.
    for argv in (
        ["pytest_summary.py", str(_report(tmp_path, _XUNIT1))],
        ["pytest_summary.py", str(_report(tmp_path, _GREEN))],
        ["pytest_summary.py", str(_report(tmp_path, "not xml at all"))],
        ["pytest_summary.py", str(tmp_path / "absent.xml")],
        ["pytest_summary.py"],
        ["pytest_summary.py", "a", "b"],
    ):
        assert main(argv) == 0, f"non-zero exit for {argv[1:]}"


def test_main_appends_to_the_step_summary_when_one_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Appending, not truncating: `$GITHUB_STEP_SUMMARY` is a file other writers may already
    # have added to, which is why scripts/audit-deps.sh uses `>>` as well.
    destination = tmp_path / "step-summary.md"
    destination.write_text("earlier writer\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(destination))

    assert main(["pytest_summary.py", str(_report(tmp_path, _XUNIT1))]) == 0

    written = destination.read_text(encoding="utf-8")
    assert written.startswith("earlier writer\n")
    assert "tests/sample/test_sample.py::test_fails" in written
    assert "test_fails" not in capsys.readouterr().out


def test_main_falls_back_to_stdout_outside_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Run by hand against a local `--junit-xml` report there is no summary file; printing
    # nothing would make the script look broken.
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert main(["pytest_summary.py", str(_report(tmp_path, _XUNIT1))]) == 0
    assert "tests/sample/test_sample.py::test_fails" in capsys.readouterr().out


def test_the_script_runs_as_a_subprocess_and_exits_zero(tmp_path: Path) -> None:
    # CI invokes it as a script, not as an import, so the module has to be executable on its
    # own — an import of anything unavailable to a bare interpreter would surface only there.
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), str(_report(tmp_path, _XUNIT1))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "tests/sample/test_sample.py::test_fails" in result.stdout
