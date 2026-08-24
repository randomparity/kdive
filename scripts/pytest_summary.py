"""Render a pytest JUnit report into a GitHub Actions job summary (#2062).

Diagnosing a red PR means reading the raw job log: on the last failed ``ci.yml`` run
``gh run view --log-failed`` was 74,735 bytes and ``gh run view --log`` 539,478 bytes, every
line timestamp- and job-prefixed. ``--tb=short`` (ADR-0577) shrinks the failure path but does
not replace that read — pytest disables assertion-explanation truncation whenever ``CI`` is
set, so the traceback bound buys less in CI than it does locally. This turns the run's JUnit
report into a short list of *which* node ids failed and *why*, readable on the run page.

It is a reporting step, not a gate. ``just test``'s exit code is the verdict, so this decides
nothing: :func:`main` returns 0 for every input, including a report that is missing (pytest
killed before writing one) or truncated (killed mid-write). Reading a summary is never the
reason a job goes red, and — more importantly — an unreadable report must never render as a
run with no failures.

Output is bounded twice over. Listing 3,000 failures would rebuild the 75 KB problem inside
the summary, and GitHub drops a step summary larger than 1 MiB outright, so an unbounded list
can delete the artifact it was meant to be.

Stdlib only, and it imports nothing from ``kdive`` — but like ``scripts/audit_report.py`` it is
**not** portable to an arbitrary interpreter: ruff lints it at the project's
``target-version = "py314"``, so ``ci.yml`` runs it under ``uv run python`` rather than the
runner's ambient ``python3``.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

#: How many failing tests are named individually. Past this the list stops being read and
#: starts being a log; the totals line still carries the true count.
MAX_FAILURES = 50

#: Characters of reason kept per failure. `CI` disables pytest's assertion-explanation
#: truncation (ADR-0577), so a single comparison of two large structures has no bound of its
#: own — one failure could otherwise fill the whole summary.
MAX_REASON_CHARS = 200

#: Hard ceiling on the rendered summary. GitHub's own limit is 1 MiB per step and it drops the
#: summary rather than truncating it, so this stays comfortably under.
MAX_BYTES = 900_000

_HEADING = "### Tests"


def summarize(report: str | Path) -> str:
    """Return the markdown for one ``--junit-xml`` report at ``report``.

    Never raises. Every unreadable shape returns prose naming the path instead, because the
    failure mode to avoid is a summary that looks like a run in which nothing went wrong.
    """
    path = Path(report)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return _no_report(path, "the test step wrote none — it may have been killed first")
    try:
        # The report is pytest's own output, written by this job moments earlier — not
        # attacker-controlled input, so the stdlib parser is the right one to reach for.
        root = ET.fromstring(raw)
    except ET.ParseError:
        return _no_report(path, "its XML did not parse — the report is truncated or corrupt")
    return _render(root)


def _no_report(path: Path, why: str) -> str:
    return (
        f"{_HEADING}\n\nNo usable pytest report at `{path}`: {why}. "
        "The job log is the only record of this run.\n"
    )


def _render(root: ET.Element) -> str:
    suites = root.iter("testsuite")
    tests = failures = errors = skipped = 0
    duration = 0.0
    for suite in suites:
        tests += _int(suite, "tests")
        failures += _int(suite, "failures")
        errors += _int(suite, "errors")
        skipped += _int(suite, "skipped")
        duration += _float(suite, "time")
    passed = max(tests - failures - errors - skipped, 0)

    lines = [
        _HEADING,
        "",
        f"{tests} tests · {passed} passed · {skipped} skipped · {failures} failed · "
        f"{errors} {'error' if errors == 1 else 'errors'} — {duration:.1f}s",
    ]

    bad = _failing_cases(root)
    if bad:
        lines.append("")
        for node_id, reason in bad[:MAX_FAILURES]:
            lines.append(f"- `{_span(node_id)}` — `{_span(reason)}`")
        if len(bad) > MAX_FAILURES:
            omitted = len(bad) - MAX_FAILURES
            lines.append(
                f"- … {omitted} more not listed. Re-run the failing area locally with "
                "`just test-verbose <path>`, or read the job log."
            )

    rendered = "\n".join(lines) + "\n"
    if len(rendered.encode("utf-8")) > MAX_BYTES:
        rendered = rendered.encode("utf-8")[:MAX_BYTES].decode("utf-8", "ignore")
        rendered += "\n\n(summary truncated)\n"
    return rendered


def _failing_cases(root: ET.Element) -> list[tuple[str, str]]:
    """Every ``<testcase>`` carrying a ``<failure>`` or ``<error>``, in report order.

    Skips are deliberately not listed: the suite skips by design on a host without Docker or a
    libvirt guest, so they are a normal state of the gate rather than something to address.
    """
    cases: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        outcome = case.find("failure")
        if outcome is None:
            outcome = case.find("error")
        if outcome is None:
            continue
        cases.append((_node_id(case), _reason(outcome)))
    return cases


def _node_id(case: ET.Element) -> str:
    """Reconstruct a pasteable ``path::Class::test[param]`` id from one ``<testcase>``.

    pytest's ``xunit1`` family carries ``file``, so the module path is exact and whatever the
    dotted ``classname`` has beyond it is the class chain — which is what makes a class-based
    or parametrized failure selectable rather than merely identifiable.

    Its default ``xunit2`` family drops ``file`` (the xunit2 schema has no such attribute), and
    a dotted prefix cannot be split back into a path and a class chain without guessing at
    naming conventions. That case degrades to ``classname::name``: still unique, still names
    the module, no longer directly pasteable. ``ci.yml`` asks for ``xunit1`` for this reason;
    the fallback is what a future pytest dropping ``xunit1`` would leave.
    """
    name = case.get("name", "?")
    classname = case.get("classname", "")
    file_path = case.get("file")
    if not file_path:
        return f"{classname}::{name}" if classname else name
    module = file_path.removesuffix(".py").replace("/", ".")
    classes = [part for part in classname.removeprefix(module).split(".") if part]
    return "::".join([file_path, *classes, name])


def _reason(outcome: ET.Element) -> str:
    """One line of *why*, from the ``message`` attribute pytest puts the short form in.

    Whitespace is collapsed rather than cut at the first newline: an assertion diff puts its
    expected/actual on continuation lines, and those are the informative half.
    """
    message = " ".join((outcome.get("message") or outcome.text or "").split())
    if not message:
        return f"<{outcome.tag} with no message>"
    if len(message) > MAX_REASON_CHARS:
        message = message[: MAX_REASON_CHARS - 1] + "…"
    return message


def _span(text: str) -> str:
    """Make ``text`` safe to sit inside a markdown code span.

    A code span already neutralises HTML and every other markdown construct, so the only
    character that has to change is the backtick that would close the span — leaving the rest
    of an arbitrary node id or assertion diff rendered as live markup. Node ids stay otherwise
    verbatim so they can be pasted straight back into pytest.
    """
    return text.replace("`", "'")


def _int(element: ET.Element, attribute: str) -> int:
    try:
        return int(element.get(attribute, "0"))
    except ValueError:
        return 0


def _float(element: ET.Element, attribute: str) -> float:
    try:
        return float(element.get(attribute, "0"))
    except ValueError:
        return 0.0


def main(argv: list[str]) -> int:
    """Write the summary and return 0 — always, whatever the report turned out to be."""
    if len(argv) != 2:
        print(f"usage: {argv[0] if argv else 'pytest_summary.py'} REPORT.xml", file=sys.stderr)
        return 0
    summary = summarize(argv[1])
    destination = os.environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        # Appending, not truncating: the file is shared with anything else the step writes.
        try:
            with open(destination, "a", encoding="utf-8") as handle:
                handle.write(summary)
            return 0
        except OSError as error:
            print(f"pytest_summary: cannot write {destination}: {error}", file=sys.stderr)
    print(summary)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via ci.yml
    sys.exit(main(sys.argv))
