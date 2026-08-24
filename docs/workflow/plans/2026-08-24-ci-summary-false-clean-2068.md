# Implementation plan — CI test summary must not report a red run as clean (#2068)

Derived from
[the spec](../specs/2026-08-24-ci-summary-false-clean-2068-design.md) and
[ADR-0578](../../adr/0578-the-ci-test-summary-fails-closed-on-an-unusable-report.md),
both hardened by adversarial review. **Do not re-open the decisions they record.**

**Goal.** `scripts/pytest_summary.py` must never render a red or aborted run as clean, and
must not raise on a truncated report. Two independent parts (a renderer floor, an environment
scrub) plus a decode fix.

**Architecture.** A CI reporting step reads the run's JUnit report and writes markdown to
`$GITHUB_STEP_SUMMARY`. It is additive: a separate workflow step with `continue-on-error`,
whose script returns 0 for every input. Nothing here may change that.

**Stack.** Python 3.14, pytest 9.1.1, pytest-xdist 3.8.0, `uv`, `just`, GitHub Actions.

## Global Constraints

Transcribed from the spec and ADR — values exactly as written there.

- `scripts/pytest_summary.py` is **stdlib only** and imports nothing from `kdive`.
- `main()` returns **0 for every input**, always. No change may make the summary a gate.
- The scrub hook is `pytest_collection` and **must not** be marked `tryfirst`, `wrapper`, or
  `hookwrapper`. Any of those orders it ahead of `DSession.pytest_collection`, which makes the
  pop happen on the xdist controller before workers spawn and silently strips every worker's
  configuration.
- The pop is `os.environ.pop("PYTEST_ADDOPTS", None)` — **with the `None` default**. The
  variable is unset on every local run; a bare `pop` raises `KeyError` and breaks `just test`.
- The floor condition is `tests == 0 and not _failing_cases(root)` — **not** `tests == 0`.
  `_int` returns `0` for an unparseable attribute, so the bare condition would discard a real
  failure list.
- Guardrail: `env -u FORCE_COLOR just ci`. Run gates bare — no `| tail`, no redirect,
  no `|| true`.
- Conventional commits. Never commit to `main`. Never force-push.

## File map

| file | action | answerable for |
|---|---|---|
| `scripts/pytest_summary.py` | modify | reading the report as bytes; the zero-test floor |
| `tests/scripts/test_pytest_summary.py` | modify | renderer + decode behaviour |
| `tests/conftest.py` | modify | the `pytest_collection` scrub hook |
| `tests/scripts/test_addopts_scrub.py` | create | the scrub's behaviour, via nested pytest |
| `tests/guards/test_collection_hook_ordering.py` | create | the `tryfirst`/wrapper constraint |
| `AGENTS.md` | modify | commit-ordering guidance |
| `tests/guards/test_commit_hook_guidance.py` | modify | that guidance's anchor |
| `.github/workflows/ci.yml` | modify | `--no-sync` on the summary step |
| `tests/scripts/test_ci_test_summary_wiring.py` | modify | the assertion pinning that step's `run` |

---

## Task 1 — Renderer: read bytes, floor on a zero-test report

**Files:** modify `scripts/pytest_summary.py`, `tests/scripts/test_pytest_summary.py`.

**Interfaces.** `summarize(report: str | Path) -> str` keeps its signature. `_render` changes
from `_render(root: ET.Element) -> str` to `_render(root: ET.Element, path: Path) -> str` —
it needs the path for the floor's prose. Both are consumed only within this module and by
`tests/scripts/test_pytest_summary.py`, which imports `main` and `summarize` only.

### Step 1.1 — Write the failing tests

Add to `tests/scripts/test_pytest_summary.py`. It already has `_report(tmp_path, raw)` for
text; add a bytes sibling beside it:

```python
def _report_bytes(tmp_path: Path, raw: bytes) -> Path:
    path = tmp_path / "report.xml"
    path.write_bytes(raw)
    return path


_ZERO = (
    '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
    '<testsuite name="pytest" errors="0" failures="0" skipped="0" tests="0" time="0.016" />'
    "</testsuites>"
)

_UNPARSEABLE_COUNT_WITH_FAILURE = (
    '<?xml version="1.0" encoding="utf-8"?><testsuite name="pytest" tests="abc" '
    'failures="1" errors="0" skipped="0" time="1.0">'
    '<testcase classname="tests.domain.test_x" name="test_boom" file="tests/domain/test_x.py">'
    '<failure message="AssertionError: boom" /></testcase></testsuite>'
)

#: A report cut mid-`é`. `read_text(encoding="utf-8")` raises UnicodeDecodeError here — a
#: ValueError, which neither of summarize()'s handlers catches (#2068).
_TRUNCATED_NON_ASCII = (
    b'<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" '
    b'tests="1" failures="1"><testcase classname="t" name="caf\xc3'
)
```

Then the tests:

```python
def test_a_zero_test_report_is_not_reported_as_a_clean_run(tmp_path: Path) -> None:
    # The defect this floor exists for: pytest exits 5 on an empty selection having written a
    # well-formed zero-test report, and a killed run can leave a foreign one behind. Either
    # way a red run must not render as totals (#2068, ADR-0578).
    summary = summarize(_report(tmp_path, _ZERO))
    assert "totals zero tests" in summary
    # Assert the totals line by its separator, not by "0 failed": a renderer emitting
    # "0 failures" would slip a literal match.
    assert " tests · " not in summary


def test_the_zero_test_reason_is_distinguishable_from_the_other_two(tmp_path: Path) -> None:
    # `assert a != b` would pass on any one-word difference. The floor's own phrase must be
    # present in its prose and absent from both neighbours, or a reader cannot tell an
    # exit-5 collection from a report the step never wrote.
    zero = summarize(_report(tmp_path, _ZERO))
    missing = summarize(tmp_path / "absent.xml")
    unparseable = summarize(_report(tmp_path, "not xml at all"))
    assert "totals zero tests" in zero
    assert "totals zero tests" not in missing
    assert "totals zero tests" not in unparseable


def test_an_unparseable_count_with_real_failures_still_lists_them(tmp_path: Path) -> None:
    # The regression the floor could introduce. `_int` returns 0 for a non-integer attribute,
    # so this report sums to zero tests while carrying a real failure. Flooring on
    # `tests == 0` alone would throw the failure list away and claim the report described
    # nothing — a false-clean in the opposite direction (#2068).
    summary = summarize(_report(tmp_path, _UNPARSEABLE_COUNT_WITH_FAILURE))
    assert "totals zero tests" not in summary
    assert "tests/domain/test_x.py::test_boom" in summary


def test_a_truncated_non_ascii_report_says_so_rather_than_raising(tmp_path: Path) -> None:
    # The killed-mid-write case the module docstring already claims to cover. read_text raises
    # UnicodeDecodeError (a ValueError) past both handlers; reading bytes makes it the
    # ParseError the unparseable branch already owns (#2068).
    summary = summarize(_report_bytes(tmp_path, _TRUNCATED_NON_ASCII))
    assert "did not parse" in summary


def test_a_zero_byte_report_says_so(tmp_path: Path) -> None:
    summary = summarize(_report_bytes(tmp_path, b""))
    assert "did not parse" in summary
```

Then rebuild the argv table at `test_main_returns_zero_for_every_input`. It needs the two new
inputs, and it has a latent defect that must be fixed in the same edit or the additions inherit
it.

**The defect:** `_report` writes to a fixed filename, `tmp_path / "pytest-junit.xml"`, and the
tuple literal is evaluated eagerly. So the three existing `_report(tmp_path, …)` calls each
overwrite the same file and return the same path — the loop then runs `main()` against
`"not xml at all"` three times. The entries claiming to cover `_XUNIT1` and `_GREEN` cover
nothing. Verified: the three calls yield 1 distinct path, whose content is `not xml at all`.

That is exactly the "true by construction" defect issue #2068 criterion 4 names, so fix it
rather than adding to it. Give every file-backed entry its own directory:

```python
def test_main_returns_zero_for_every_input(tmp_path: Path) -> None:
    # The property the CI step depends on. `render` deciding nothing is what lets the summary
    # be additive to a gate whose exit code is the verdict.
    #
    # Each report gets its own directory: `_report`/`_report_bytes` write a fixed filename, and
    # this tuple is built eagerly, so sharing `tmp_path` made every entry resolve to the
    # last-written file (#2068).
    cases = {
        "xunit1": _report(_fresh(tmp_path, "xunit1"), _XUNIT1),
        "green": _report(_fresh(tmp_path, "green"), _GREEN),
        "unparseable": _report(_fresh(tmp_path, "unparseable"), "not xml at all"),
        "zero": _report(_fresh(tmp_path, "zero"), _ZERO),
        "truncated": _report_bytes(_fresh(tmp_path, "truncated"), _TRUNCATED_NON_ASCII),
        "absent": tmp_path / "absent.xml",
    }
    for name, report in cases.items():
        assert main(["pytest_summary.py", str(report)]) == 0, f"non-zero exit for {name}"
    for argv in (["pytest_summary.py"], ["pytest_summary.py", "a", "b"]):
        assert main(argv) == 0, f"non-zero exit for {argv[1:]}"
```

with the helper beside `_report_bytes`:

```python
def _fresh(tmp_path: Path, name: str) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    return directory
```

**Verify:** `uv run python -m pytest tests/scripts/test_pytest_summary.py -q`
**Expect:** **three** of the five new tests fail, plus the argv table. Confirmed against the
unfixed module — do not expect all five:

| test | before the fix | why |
|---|---|---|
| `..._zero_test_report_is_not_reported_as_a_clean_run` | **fails** (assert) | no floor yet |
| `..._zero_test_reason_is_distinguishable_...` | **fails** (assert) | no floor yet |
| `..._truncated_non_ascii_report_says_so...` | **fails** (`UnicodeDecodeError`) | the decode defect |
| `..._unparseable_count_with_real_failures_still_lists_them` | **passes** | regression guard: today there is no floor to discard the list, so this pins behaviour the fix must not break |
| `..._zero_byte_report_says_so` | **passes** | regression guard: `read_text("")` already raises `ParseError: no element found` |

A test that is green before the change proves nothing about the change; both are here to stay
green through it. Say so in their comments so a later reader does not mistake them for
TDD-red tests.

### Step 1.2 — Read the report as bytes

Replace the body of `summarize`:

```python
def summarize(report: str | Path) -> str:
    """Return the markdown for one ``--junit-xml`` report at ``report``.

    Never raises. Every unreadable shape returns prose naming the path instead, because the
    failure mode to avoid is a summary that looks like a run in which nothing went wrong.
    """
    path = Path(report)
    try:
        raw = path.read_bytes()
    except OSError:
        return _no_report(path, "the test step wrote none — it may have been killed first")
    try:
        # Bytes rather than text so the parser honours the report's own encoding declaration,
        # and so a tail cut mid-character arrives as ParseError instead of the
        # UnicodeDecodeError that slipped past both handlers (#2068, ADR-0578).
        #
        # The report is pytest's own output, written by this job moments earlier — not
        # attacker-controlled input, so the stdlib parser is the right one to reach for.
        root = ET.fromstring(raw)
    except ET.ParseError:
        return _no_report(path, "its XML did not parse — the report is truncated or corrupt")
    return _render(root, path)
```

**Verify:** `uv run python -m pytest tests/scripts/test_pytest_summary.py -q`
**Expect:** the truncated test and the argv table now pass; **two** floor tests still fail on
assertions (not exceptions). The two regression guards stay green throughout.

### Step 1.3 — Floor the renderer

In `_render`, take `path`, compute the failing cases before the totals line, and return early:

```python
def _render(root: ET.Element, path: Path) -> str:
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

    bad = _failing_cases(root)
    if tests == 0 and not bad:
        # The gate never legitimately runs zero tests: an empty selection is pytest exit 5,
        # which is red. So a zero-test report is a signal, not a result — an exit-5 run, or a
        # foreign report a nested pytest left behind (ADR-0578). Worded as what was observed,
        # because this cannot tell those two apart and must not claim to.
        #
        # `and not bad` is load-bearing: `_int` returns 0 for an unparseable `tests`
        # attribute, so without it a report carrying real failures would be discarded here.
        return _no_report(
            path,
            "it parsed but totals zero tests — the run collected nothing, "
            "or the report is not this run's",
        )

    lines = [
        _HEADING,
        "",
        f"{tests} tests · {passed} passed · {skipped} skipped · {failures} failed · "
        f"{errors} {'error' if errors == 1 else 'errors'} — {duration:.1f}s",
    ]

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
```

Also update the module docstring's contract paragraph. It currently reads:

> :func:`main` returns 0 for every input, including a report that is missing (pytest
> killed before writing one) or truncated (killed mid-write).

Replace the parenthetical list with, verbatim:

> :func:`main` returns 0 for every input, including a report that is missing (pytest
> killed before writing one), truncated (killed mid-write), or well-formed but describing
> no tests at all (an empty selection, or a report another process left behind).

**Verify:** `uv run python -m pytest tests/scripts/test_pytest_summary.py -q`
**Expect:** all tests pass, including the pre-existing ones (`_GREEN` describes a real run and
must still render totals).

### Step 1.4 — Mutation-verify

Break each new assertion in turn, confirm the intended test reddens, restore. In particular:
delete `and not bad` from the floor condition and confirm
`test_an_unparseable_count_with_real_failures_still_lists_them` fails. Copy the file to a
scratchpad before probing; do **not** use `git checkout --` to restore, and clear
`__pycache__` afterwards.

**Acceptance:** `summarize` never raises on any input; a zero-test report renders prose
carrying "totals zero tests"; a report with real failures and an unparseable count still lists
them; `main` returns 0 for every entry in the argv table.

**Commit:** `fix(ci): fail closed when the pytest report totals zero tests`

---

## Task 2 — Scrub `PYTEST_ADDOPTS` so no nested pytest inherits the report path

**Files:** modify `tests/conftest.py`; create `tests/scripts/test_addopts_scrub.py` and
`tests/guards/test_collection_hook_ordering.py`.

**Interfaces.** Adds one module-level function `pytest_collection(session)` to
`tests/conftest.py`. It is a pytest hook, called by pytest, not by repo code.
`tests/conftest.py` currently defines **no** `pytest_*` hooks, and no `pytest_collection`
exists anywhere in the tree — verified.

### Step 2.1 — Write the failing tests

`tests/scripts/test_addopts_scrub.py`:

```python
"""The run's JUnit report path must not reach anything a test spawns (#2068, ADR-0578).

`.github/workflows/ci.yml` sets `PYTEST_ADDOPTS` for the whole `Test` step, so a nested pytest
inherits `--junit-xml=<shared path>` and writes its own report over the run's. When the outer
session never reaches `sessionfinish` — a cancelled job, an OOM-killed controller, a step
timeout — the summary step reads that leftover and reports a clean run.

These are nested-pytest runs because the behaviour cannot be observed in-process: the hook
pops at collection, before any test body runs, so a test that sets the variable itself and
spawns a child watches the child inherit it, and one that does not set it proves nothing.
The nested conftest imports the *real* hook from `tests.conftest`, so this exercises the
shipped implementation rather than a copy of it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

_CONFTEST = """
import sys
sys.path.insert(0, {root!r})
from tests.conftest import pytest_collection  # noqa: F401  the hook under test
"""

_GRANDCHILD = """
import os
import subprocess
import sys


def _seen() -> str:
    result = subprocess.run(
        (sys.executable, "-c", "import os; print(os.environ.get('PYTEST_ADDOPTS'))"),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()
"""


def _run_nested(tmp_path: Path, module: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "conftest.py").write_text(_CONFTEST.format(root=str(_ROOT)), encoding="utf-8")
    (tmp_path / "test_inner.py").write_text(module, encoding="utf-8")
    environment = dict(os.environ)
    environment["PYTEST_ADDOPTS"] = "--tb=long"
    return subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path / "test_inner.py"),
            "-q",
            "-p",
            "no:cacheprovider",
        ),
        cwd=_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_a_process_spawned_from_a_test_does_not_inherit_addopts(tmp_path: Path) -> None:
    module = _GRANDCHILD + (
        "def test_grandchild():\n"
        "    observed = _seen()\n"
        "    assert observed == 'None', observed\n"
    )
    result = _run_nested(tmp_path, module)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_process_spawned_at_module_import_does_not_inherit_addopts(tmp_path: Path) -> None:
    # The case that distinguishes `pytest_collection` from a session-scoped fixture: a fixture
    # runs after test modules are imported, so this spawn would still inherit the value.
    module = _GRANDCHILD + (
        "_AT_IMPORT = _seen()\n"
        "def test_import_time():\n"
        "    assert _AT_IMPORT == 'None', _AT_IMPORT\n"
    )
    result = _run_nested(tmp_path, module)
    assert result.returncode == 0, result.stdout + result.stderr
```

**Transcribe the block above verbatim — it is already `ruff format` output.** Everything else
in this plan is format-clean as written; this one file was not, because ruff explodes the
`subprocess.run` argument tuple one element per line. `just lint` runs
`ruff format --check`, so an un-formatted transcription fails `just ci` at the first gate.

`tests/guards/test_collection_hook_ordering.py`:

```python
"""The scrub hook must not be reordered ahead of xdist's own (#2068, ADR-0578).

Under xdist the hook never runs on the controller: `DSession.pytest_collection` returns True
and the hookspec is `firstresult`, so the chain stops before ours. That is precisely why
workers keep their configuration — the controller still holds `PYTEST_ADDOPTS` when it spawns
them. Marking our implementation `tryfirst` (or as a wrapper) orders it ahead of `DSession`,
which moves the pop onto the controller and silently strips every worker's options.

Guarded rather than tested behaviourally because the observable is a worker process's option
state, which needs a second nested `-n 2` run to reach. This asserts pluggy's own runtime
metadata: `pytest_impl` is `{}` on an undecorated function and carries the flags when marked.
"""

from __future__ import annotations

import tests.conftest


def test_the_scrub_hook_is_not_ordered_ahead_of_xdist() -> None:
    hook = tests.conftest.pytest_collection
    impl = getattr(hook, "pytest_impl", {})
    for flag in ("tryfirst", "wrapper", "hookwrapper"):
        assert not impl.get(flag), (
            f"tests.conftest.pytest_collection is marked {flag}=True. That orders it ahead of "
            "DSession.pytest_collection, so the pop lands on the xdist controller before the "
            "workers are spawned and every worker silently loses its PYTEST_ADDOPTS options "
            "(ADR-0578)."
        )
```

**Verify:** `uv run python -m pytest tests/scripts/test_addopts_scrub.py tests/guards/test_collection_hook_ordering.py -q`
**Expect:** all three fail, but read the *reason* — it is not the one you would guess. The
guard fails with `AttributeError: module 'tests.conftest' has no attribute
'pytest_collection'`. Both scrub tests fail because the nested conftest's
`from tests.conftest import pytest_collection` raises `ImportError`, so the nested run exits
**4** (usage error) before any grandchild is spawned.

That means these two go green the instant the *name* exists, whatever its body does — they do
not yet prove the pop. Step 2.4 is what closes that gap; do not skip it.

### Step 2.2 — Add the hook

In `tests/conftest.py`, after the imports and before the first fixture:

```python
def pytest_collection(session: pytest.Session) -> None:
    """Stop this process handing ``PYTEST_ADDOPTS`` to anything it spawns (#2068, ADR-0578).

    CI sets it for the whole ``Test`` step to ask for a JUnit report, so a nested pytest would
    inherit ``--junit-xml=<shared path>`` and overwrite the run's own report.

    The timing is the decision. This has to run after the process has configured itself from
    the variable and before it imports any test module, and ``pytest_collection`` is the only
    hook that is both. Under xdist it never runs on the controller at all —
    ``DSession.pytest_collection`` returns ``True`` and the hookspec is ``firstresult`` — so
    the controller keeps the variable and hands it to every worker, and each worker pops it
    itself. **Do not mark this ``tryfirst`` or as a wrapper**: that orders it ahead of
    ``DSession`` and strips every worker's options
    (``tests/guards/test_collection_hook_ordering.py``).

    Returns ``None`` so the default collection still runs.
    """
    os.environ.pop("PYTEST_ADDOPTS", None)
    return None
```

`os` and `pytest` are already imported in that module.

**Verify:** `uv run python -m pytest tests/scripts/test_addopts_scrub.py tests/guards/test_collection_hook_ordering.py -q`
**Expect:** 3 passed.

### Step 2.3 — Prove the hook did not disturb collection

**Verify:** `uv run python -m pytest tests/domain -q` then
`uv run python -m pytest tests/domain -n 4 --dist worksteal -q`
**Expect:** 627 passed both times, identical counts. `pytest_collection` is `firstresult`, so a
mistake here would suppress or duplicate collection rather than fail loudly.

### Step 2.4 — Mutation-verify

Three probes, each reverted before the next. Copy the file to a scratchpad first; do **not**
use `git checkout --` to restore, and clear `__pycache__` afterwards.

1. **Prove the pop, not just the name.** Replace the hook's body with a bare `return None`
   (keep the function). Both scrub tests must fail with `assert '--tb=long' == 'None'` — the
   grandchild inheriting the value. This is the assertion Step 2.1 could not make, because
   there the tests were red for an `ImportError` instead.
2. **Prove the guard.** Mark the hook `@pytest.hookimpl(tryfirst=True)`; the guard must redden
   with its own message. Repeat with `wrapper=True`.
3. **Prove the default matters.** Change the pop to `os.environ.pop("PYTEST_ADDOPTS")` with no
   default and confirm the suite errors with `KeyError` on a run where the variable is unset.

**Acceptance:** a grandchild spawned from a test body and one spawned at module import both
see no `PYTEST_ADDOPTS`; the guard fails if the hook is reordered; collection counts unchanged
serial and under xdist.

**Commit:** `fix(test): stop nested pytest inheriting the run's JUnit report path`

---

## Task 3 — The three corrections carried from the #2067 review

**Files:** modify `AGENTS.md`, `tests/guards/test_commit_hook_guidance.py`,
`.github/workflows/ci.yml`.

### Step 3.1 — `AGENTS.md`: stage the paths you staged

The guidance's closing sentence (`AGENTS.md:84`) currently reads:

> Then `git add -A` and commit, which now has nothing left to rewrite.

`git add -A` stages every untracked and unstaged file `prek run` just restored, turning a
targeted commit into a whole-tree one, against this repo's one-logical-change-per-commit rule.
`git add -u` is **not** the fix — it is still whole-tree over tracked files. Replace that
sentence with, verbatim:

> Record the staged set first (`git diff --cached --name-only`), then re-add exactly those
> paths — `git add -- <the paths you staged>` — and commit, which now has nothing left to
> rewrite. Not `git add -A` or `git add -u`: `prek run` restored every unrelated unstaged
> file when it finished, and both would sweep those into this commit.

The replacement still contains `git add`, so the guard's
`assert "git add" in paragraph` continues to hold.

### Step 3.2 — `tests/guards/test_commit_hook_guidance.py`: anchor the slice

`_paragraph()` at line 33 splits on `"\n\n"`, so a routine reflow that splits today's single
unbroken paragraph fails the guard on intact guidance. Anchor the slice on the next bold
lead-in instead — `**Running the live tiers**`, present at `AGENTS.md:86`. Replace the body:

```python
#: The bold lead-in that follows the guidance. Slicing to it rather than to the first blank
#: line means a routine reflow that splits the paragraph in two does not fail the guard on
#: intact guidance (#2068).
_END = "**Running the live tiers**"


def _paragraph() -> str:
    text = _AGENTS.read_text(encoding="utf-8")
    assert _ANCHOR in text, "the pre-commit ordering guidance is gone from AGENTS.md (#2062)"
    after = text.split(_ANCHOR, 1)[1]
    assert _END in after, (
        f"the guidance's closing anchor {_END!r} is gone from AGENTS.md, so this guard can no "
        "longer bound its slice and would silently check the rest of the file (#2068)"
    )
    return after.split(_END, 1)[0]
```

The `assert _END in after` is the point: without it a renamed lead-in would widen the slice to
the rest of the file and the guard would keep passing while checking the wrong text.

**Verify:** `uv run python -m pytest tests/guards/test_commit_hook_guidance.py -q`
**Expect:** passes. Then temporarily reflow the guidance paragraph in two, confirm it still
passes (it failed before this change), and restore.

### Step 3.3 — `.github/workflows/ci.yml`: `--no-sync`, not `--no-project`

Change the summary step's `run:` from `uv run python` to `uv run --no-sync python`.

**Not `--no-project`.** `pyproject.toml` pins `requires-python = "==3.14.*"` and `ci.yml`
installs no Python — `setup-uv` is called with no `python-version` and there is no
`actions/setup-python` — so `--no-project` would fall back to the runner's ambient interpreter
or trigger a download, against the script's own docstring. `--no-sync` keeps the project
environment and skips only the resolve. Update the step's comment to say which and why.

`tests/scripts/test_ci_test_summary_wiring.py:100` pins this step's command:

```python
    assert _summary_step()["run"].startswith("uv run python ")
```

`--no-sync` fails it. Widen it to keep the intent — that the step runs the summary script
through `uv` — without pinning the flags:

```python
    run = _summary_step()["run"]
    assert run.startswith("uv run "), run
    assert " python scripts/pytest_summary.py" in run, run
```

Also note in the step's comment that `--no-sync` has a failure mode `uv run python` does not:
`if: always()` fires this step even when `Sync dependencies` (`ci.yml:53`) failed, and with no
`.venv` present `uv run --no-sync` errors out. `continue-on-error` swallows it, so the result
is no summary rather than a red job. That is the correct trade — the alternative resolves a
fresh environment on a runner whose install already failed — but it should be written down.

**Verify:** `uv run python -m pytest tests/scripts/test_ci_test_summary_wiring.py -q` then
`just lint-workflows`
**Expect:** the wiring test passes. Note `just lint-workflows` alone is **not** a verification
of this step: zizmor and actionlint do not parse `uv`'s flags, so it is equally clean before
and after the edit.

**Acceptance:** guidance names path-scoped staging; the guard tolerates a reflow and fails on a
renamed anchor; the summary step uses `--no-sync`.

**Commits — three, not one.** Task 3's own justification is the
one-logical-change-per-commit rule, so it does not land as a single bundle:

1. `docs(agents): stage the paths you staged, not the whole tree` — `AGENTS.md`
2. `test(guards): anchor the commit-guidance slice on its closing lead-in` —
   `tests/guards/test_commit_hook_guidance.py`
3. `ci: run the summary script with --no-sync, not a bare uv run` — `.github/workflows/ci.yml`
   and `tests/scripts/test_ci_test_summary_wiring.py`

---

## Final verification

```
env -u FORCE_COLOR just ci
```

**Expect:** exit 0. Read the status from a dedicated file rather than a pipeline's exit code.

The CI half of Task 3 cannot be proven locally. Say plainly in the PR which arms ran locally
and which only the PR's own CI run can demonstrate, and read that run rather than assuming.
