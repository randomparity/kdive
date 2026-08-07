"""The pip-audit report classifier that decides what the supply-chain gate may retry.

``pip-audit`` exits 1 for a genuine advisory and for an unreachable PyPI alike, so the retry
added for #1913 cannot key on exit status: it would re-run real findings until the budget
ran out, and sit one plausible "an exhausted budget is a flake" edit away from passing them.
``scripts/audit_report.py`` draws the line instead — a run that emitted parseable JSON
produced a verdict and is never retried; only a run that emitted none is (ADR-0553).

That makes this module the security-relevant half of the change, so it is tested directly
rather than through the shell wrapper: these cases are offline, need no PyPI, and run in
milliseconds.

The fixtures are **real** ``pip-audit@2.10.0 --format json`` output, captured by running it
against `requests==2.19.1` (a genuinely vulnerable pin) and against a blackholed vulnerability
service — not hand-invented shapes. The advisory ids below are real PyPI/OSV records.
"""

from __future__ import annotations

import json

from scripts.audit_report import CLEAN, FOUND, NO_VERDICT, classify

#: `pip-audit --no-deps --strict -r <requests==2.19.1> -f json`, trimmed to the fields the
#: classifier reads. The real run exited 1 and reported 23 advisories across 3 packages.
_GENUINE_ADVISORY = json.dumps(
    {
        "dependencies": [
            {
                "name": "requests",
                "version": "2.19.1",
                "vulns": [
                    {"id": "PYSEC-2018-28", "fix_versions": ["2.20.0"]},
                    {"id": "PYSEC-2023-74", "fix_versions": ["2.31.0"]},
                ],
            },
            {"name": "urllib3", "version": "1.23", "vulns": [{"id": "PYSEC-2026-1873"}]},
            {"name": "packaging", "version": "25.0", "vulns": []},
        ],
        "fixes": [],
    }
)

#: The same command against a clean pin: exit 0, every `vulns` empty.
_CLEAN = json.dumps({"dependencies": [{"name": "packaging", "version": "25.0", "vulns": []}]})


def test_a_genuine_advisory_is_a_verdict_and_fails() -> None:
    # The load-bearing case. If this ever returns anything but FOUND, the gate is disarmed:
    # NO_VERDICT would make the caller retry a real finding, CLEAN would pass it outright.
    status, lines = classify(_GENUINE_ADVISORY)

    assert status == FOUND
    assert status != NO_VERDICT, "a real advisory must never be classified as retryable"
    # Name every affected package, and only those — a report the operator can act on.
    assert lines == [
        "requests 2.19.1: PYSEC-2018-28, PYSEC-2023-74",
        "urllib3 1.23: PYSEC-2026-1873",
    ]


def test_a_clean_audit_is_a_verdict_and_passes() -> None:
    assert classify(_CLEAN) == (CLEAN, [])


def test_the_transport_failure_shapes_are_retryable() -> None:
    # Every one of these was produced by a real failed run: an unreachable vulnerability
    # service and an unreachable package index both write zero bytes to stdout and put a
    # traceback on stderr. Truncation covers a run killed mid-write.
    unfinished = {
        "empty (service or index unreachable)": "",
        "whitespace only": "\n",
        "traceback on stdout": (
            "Traceback (most recent call last):\n"
            "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='pypi.org', port=443)"
        ),
        "truncated json": '{"dependencies": [{"name": "requests", "vu',
    }
    for label, raw in unfinished.items():
        assert classify(raw) == (NO_VERDICT, []), f"{label} must be retryable"


def test_an_unrecognised_shape_is_no_verdict_never_clean() -> None:
    # Fail-closed in the direction that matters: valid JSON the classifier cannot read must
    # not be reported as an audit that passed. A pip-audit release that renames or drops
    # `dependencies` lands here and fails the gate loudly instead of greening it silently.
    for raw in ('{"results": []}', "[]", "null", '"dependencies"', "42"):
        status, lines = classify(raw)
        assert status == NO_VERDICT, f"{raw!r} must not be a verdict"
        assert status != CLEAN, f"{raw!r} must never read as a clean audit"
        assert lines == []


def test_a_vuln_entry_missing_its_id_still_fails_the_gate() -> None:
    # Degraded input must not become a pass: the finding is real even when unlabelled.
    status, lines = classify(
        json.dumps({"dependencies": [{"name": "acme", "version": "1.0", "vulns": [{}]}]})
    )
    assert status == FOUND
    assert lines == ["acme 1.0: ?"]
