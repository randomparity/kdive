# Readiness probe failures leave the probe as a closed vocabulary

**Goal.** Stop `src/kdive/providers/local_libvirt/lifecycle/boot/readiness.py` returning free-form
`virsh` transport text as `ReadinessResult.probe_error`. The probe classifies its own failure into
a closed `StrEnum`; the bounded raw text is logged for the operator and never returned. Two
agent-facing egresses stop leaking host paths as a result, and no renderer changes.

**Architecture.** `readiness.py` gains `ProbeFailure`, a four-member `StrEnum`. Every failing
return of `_domain_exit_probe` logs the bounded raw text at `WARNING` and returns a member.
`_DomainExitProbe.error` and `ReadinessResult.probe_error` are typed `ProbeFailure | None`, so
`ty`'s strict defaults refuse a free-form `str` at every call site in `src/` and `tests/`.
`install.py` renders `.value` into `details["probe_error"]`.

**Tech stack.** Python 3.14, `uv`, pytest, `ruff`, `ty`.

Expected implementation size: 160–240 changed lines (M) — from the file map below: roughly 42
source lines in `readiness.py`, roughly 6 in `install.py`, and roughly 140 test lines covering
three amended assertions and five new tests. No new module and no new dependency.

Spec: [`docs/workflow/specs/2026-09-03-probe-error-closed-vocabulary-design.md`](../specs/2026-09-03-probe-error-closed-vocabulary-design.md).

## Global Constraints

- Branch `feat/probe-error-closed-vocabulary-2220`; `BASE_BRANCH` is `main`; base commit
  `811538fb2`.
- Guardrails: `just lint`, `just type`, `just test-changed` while iterating; `just ci` bare as the
  pre-push gate. Run gate recipes **bare** — never piped through `tail`/`head`, never with a
  trailing `; echo $?`; capture with `just ci > <file> 2>&1 < /dev/null` when output is needed.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` runs whole-tree (`src` + `tests`).
- `just format` before committing a Python-only change, so the mutating hooks do not rewrite the
  tree during `git commit`.
- Prose rule (project-wide, enforced in review): use plain factual wording; avoid "critical",
  "robust", "comprehensive", "elegant". Applies to code comments and docstrings here.
- Detail values reaching `CategorizedError.details` must be finite JSON scalars — `details` is
  agent-surfaceable, and `safe_error_details` is a type filter that will not catch content.
- Do not modify `src/kdive/serialization.py`, `src/kdive/security/secrets/redaction.py`, or
  anything under `src/kdive/providers/remote_libvirt/`. All three are out of scope for #2220.

## File map

| File | Fate | Answerable for |
|---|---|---|
| `src/kdive/providers/local_libvirt/lifecycle/boot/readiness.py` | modified | classifying a probe failure into the closed vocabulary and logging the raw text |
| `src/kdive/providers/local_libvirt/lifecycle/install.py` | modified | rendering the classified member into `details["probe_error"]` |
| `tests/providers/local_libvirt/test_install.py` | modified | the absence proofs, the classification coverage, and the operator-log proof |

No file is created. `tests/adversarial/test_provider_xml.py` constructs
`ReadinessResult(answered=True, ok=True)` with no `probe_error` and needs no change.

## Task 1 — Classify probe failures in `readiness.py`

**Where this fits.** This is the whole of the fix. Task 2 only renders what this task produces, and
Task 3 proves it.

**Interfaces.** This task defines, and Task 2 and Task 3 consume:

```python
class ProbeFailure(StrEnum):
    VIRSH_MISSING = "virsh_missing"
    VIRSH_TIMEOUT = "virsh_timeout"
    VIRSH_PROBE_FAILED = "virsh_probe_failed"
    VIRSH_NONZERO_EXIT = "virsh_nonzero_exit"


class _DomainExitProbe(NamedTuple):
    exited: bool
    error: ProbeFailure | None = None


class ReadinessResult(NamedTuple):
    answered: bool
    ok: bool
    probe_error: ProbeFailure | None = None
```

It consumes nothing from an earlier task.

Existing names this task relies on, confirmed present in
`src/kdive/providers/local_libvirt/lifecycle/boot/readiness.py` at base `811538fb2`:
`_bounded_probe_error(message: str) -> str` (line 58), `_DOMSTATE_PROBE_TIMEOUT = 10` (line 19),
`_VIRSH = "virsh"` (line 21), `_TERMINAL_DOMSTATES` (line 20), and the stdlib `StrEnum` already
imported from `enum` for `ConsoleVerdict` (line 8).

### Step 1.1 — Write the failing absence test for the nonzero-exit branch

Add to `tests/providers/local_libvirt/test_install.py`, beside the existing probe tests:

```python
# The transport substrings a probe failure must never carry into an agent-facing payload.
# `_LEAKY_DOMSTATE_STDERR` is the ordinary stderr `virsh` writes when the session daemon is
# unreachable; every fragment below is host-derived and none of it is actionable for an agent.
_LEAKY_DOMSTATE_STDERR = (
    "error: failed to connect to the hypervisor\n"
    "error: Failed to connect socket to '/run/user/1000/libvirt/virtqemud-sock': "
    "No such file or directory"
)
_TRANSPORT_SUBSTRINGS = (
    "/run/user/1000/libvirt/virtqemud-sock",
    "/run/user",
    "virtqemud-sock",
)


def test_nonzero_domstate_exit_keeps_transport_text_out_of_the_mcp_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Absence, not difference: a transform that returned a *different* leaky string passes a
    # `!=` assertion and fails this one (#2220).
    _capture_domstate(monkeypatch, returncode=1, stdout="", stderr=_LEAKY_DOMSTATE_STDERR)

    probe = readiness_mod._domain_exit_probe("kdive-abc")
    error = CategorizedError(
        "System did not become ready within the boot window",
        category=ErrorCategory.BOOT_TIMEOUT,
        details=LocalLibvirtInstall._boot_failure_details(_SYSTEM_ID, probe.error),
    )
    payload = dict(ToolResponse.failure_from_error(str(_SYSTEM_ID), error).data or {})

    assert payload["probe_error"] == "virsh_nonzero_exit"
    rendered = repr(payload)
    for substring in _TRANSPORT_SUBSTRINGS:
        assert substring not in rendered
```

`_SYSTEM_ID` is `UUID("22222222-2222-2222-2222-222222222222")` written inline at each use. That
literal already appears inline 11 times in this file and no module constant for it exists; adding
one and leaving the other 11 uses inline would be worse than matching the file's existing style.

`_capture_domstate` exists at line 1741 with exactly the signature these tests use — confirmed at
base `811538fb2`:

```python
def _capture_domstate(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int, stdout: str, stderr: str = ""
) -> dict[str, object]
```

It is keyword-only after `monkeypatch`, it already stubs `readiness_mod.shutil.which` and
`readiness_mod.subprocess.run`, and it needs no extension. It is defined below the tests that will
call it, which is fine: the name resolves at call time, not at collection.

Imports at the top of the file. `CategorizedError` and `ErrorCategory` are **already** imported
(line 26), as are `subprocess`, `pytest`, `UUID`, and `LocalLibvirtInstall`. Two names are missing
and must be added:

```python
import logging  # for Step 3.6

from kdive.mcp.responses import ToolResponse
```

### Step 1.2 — Run it and confirm it fails

```sh
uv run python -m pytest \
  "tests/providers/local_libvirt/test_install.py::test_nonzero_domstate_exit_keeps_transport_text_out_of_the_mcp_payload" \
  -q
```

Expect `1 failed`, with the assertion on `payload["probe_error"]` failing because the current code
returns the raw stderr. This is the confirm-it-fails step; do not skip it.

### Step 1.3 — Add the vocabulary and the classifying helper

In `src/kdive/providers/local_libvirt/lifecycle/boot/readiness.py`, add `import logging` to the
stdlib imports and a module logger beside the existing module constants:

```python
_log = logging.getLogger(__name__)
```

Add the vocabulary immediately after `ConsoleVerdict`:

```python
class ProbeFailure(StrEnum):
    """Why a ``virsh domstate`` probe failed, as a closed agent-facing vocabulary (#2220).

    ``CategorizedError.details`` is surfaceable to the agent and reaches it through two renderers
    that are both type filters, not content filters: ``safe_error_details`` on the MCP path and
    the worker's ``_failure_context`` on ``jobs.wait``. Free-form transport text in this slot
    therefore reaches the agent verbatim, host paths and all. Members are the only values this
    slot can hold, so the leak is not representable rather than filtered downstream. The raw text
    stays available to the operator in the log.
    """

    VIRSH_MISSING = "virsh_missing"
    VIRSH_TIMEOUT = "virsh_timeout"
    VIRSH_PROBE_FAILED = "virsh_probe_failed"
    VIRSH_NONZERO_EXIT = "virsh_nonzero_exit"
```

Add the classifying helper immediately after `_bounded_probe_error`:

```python
def _probe_failed(domain_name: str, failure: ProbeFailure, detail: str) -> _DomainExitProbe:
    """Log the bounded probe diagnostic for the operator and return the classified failure.

    The raw text goes to the host log and not into the returned value, mirroring what
    ``_install_failure`` and ``_libvirt_transport_failure`` already do on this module's raise
    path: a static error carrying only the domain name, with the underlying transport text left
    to the operator's log.
    """
    _log.warning(
        "domstate probe failed for %s (%s): %s",
        domain_name,
        failure.value,
        _bounded_probe_error(detail),
    )
    return _DomainExitProbe(False, failure)
```

### Step 1.4 — Retype the two result carriers

Change the two annotations, leaving everything else in both classes as it is:

```python
class ReadinessResult(NamedTuple):
    """The run-readiness preflight result: did the System answer, and did its checks pass."""

    answered: bool
    ok: bool
    probe_error: ProbeFailure | None = None


class _DomainExitProbe(NamedTuple):
    """The domstate probe result plus its classified probe-failure reason."""

    exited: bool
    error: ProbeFailure | None = None
```

`_DomainExitProbe` is declared before `ProbeFailure` would be if the vocabulary is added after
`ConsoleVerdict`; `from __future__ import annotations` is already at the top of this file
(line 3), so the forward reference resolves and no reordering is needed.

### Step 1.5 — Route every failing return through the helper

Rewrite `_domain_exit_probe`'s five failure returns. The function's body becomes:

```python
def _domain_exit_probe(domain_name: str) -> _DomainExitProbe:  # pragma: no cover - live_vm
    """Return whether ``virsh domstate`` reports terminal state plus its classified failure."""
    uri = config.require(LIBVIRT_URI)
    virsh = shutil.which(_VIRSH)
    if virsh is None:
        return _probe_failed(domain_name, ProbeFailure.VIRSH_MISSING, "virsh executable not found")
    try:
        proc = subprocess.run(  # noqa: S603 - virsh argv; URI/domain are data  # nosec B603
            [virsh, "-c", uri, "domstate", domain_name],
            capture_output=True,
            text=True,
            timeout=_DOMSTATE_PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _probe_failed(
            domain_name,
            ProbeFailure.VIRSH_TIMEOUT,
            f"virsh domstate timed out after {exc.timeout:g}s",
        )
    except FileNotFoundError:
        return _probe_failed(domain_name, ProbeFailure.VIRSH_MISSING, "virsh executable not found")
    except (subprocess.SubprocessError, OSError) as exc:
        return _probe_failed(
            domain_name, ProbeFailure.VIRSH_PROBE_FAILED, f"virsh domstate probe failed: {exc}"
        )
    if proc.stdout.strip().lower() in _TERMINAL_DOMSTATES:
        return _DomainExitProbe(True)
    stderr = proc.stderr.strip().lower()
    exited = (
        proc.returncode != 0
        and domain_name.startswith("kdive-")
        and "failed to get domain" in stderr
    )
    if exited:
        return _DomainExitProbe(True)
    if proc.returncode != 0:
        return _probe_failed(
            domain_name,
            ProbeFailure.VIRSH_NONZERO_EXIT,
            stderr or f"virsh domstate exited {proc.returncode}",
        )
    return _DomainExitProbe(False)
```

Note the `except (subprocess.SubprocessError, OSError)` tuple: `ruff format` under PEP 758 may
rewrite this to `except subprocess.SubprocessError, OSError:`. Both forms exist on `main`. Run
`just format` and keep whichever form it produces rather than fighting it.

`_bounded_probe_error` keeps its name and its body; it now bounds the logged text rather than the
returned value, which is what it always did structurally.

### Step 1.6 — Run the test and confirm it passes

```sh
uv run python -m pytest \
  "tests/providers/local_libvirt/test_install.py::test_nonzero_domstate_exit_keeps_transport_text_out_of_the_mcp_payload" \
  -q
```

Expect `1 passed`. This step will still fail on the `_boot_failure_details` call until Task 2
lands if `ty` is run; the runtime test passes because `StrEnum` members are `str`. Run `just type`
only after Task 2.

**Acceptance criteria.** `ProbeFailure` exists with exactly four members and the values in the
table above. `_domain_exit_probe` returns a member or `None` and never a caller-supplied string.
Every failing branch logs the bounded raw text once. `_bounded_probe_error` is still applied to
every logged diagnostic.

## Task 2 — Render the member into the boot-failure details

**Where this fits.** Task 1 produces a `ProbeFailure`; this renders it as the JSON scalar both
agent-facing renderers forward.

**Interfaces.** Consumes `ProbeFailure` and `ReadinessResult.probe_error` from Task 1. Provides,
for Task 3:

```python
@staticmethod
def _boot_failure_details(system_id: UUID, first_probe_error: ProbeFailure | None) -> dict[str, object]
```

Existing names relied on, confirmed present in
`src/kdive/providers/local_libvirt/lifecycle/install.py` at base `811538fb2`: the import block
already pulls `_POLL_INTERVAL_SECONDS`, `ReadinessResult`, and `_real_readiness` from
`...lifecycle.boot.readiness` (lines 53-57). This task adds no logging to `install.py`; the raw
text is logged in `readiness.py`, where it exists.

### Step 2.1 — Extend the import and retype the aggregation

Add `ProbeFailure` to the existing `from ...lifecycle.boot.readiness import (...)` block, keeping
the names alphabetically ordered as `ruff`'s `I` rules require:

```python
from kdive.providers.local_libvirt.lifecycle.boot.readiness import (
    _POLL_INTERVAL_SECONDS,
    ProbeFailure,
    ReadinessResult,
    _real_readiness,
)
```

In `_await_ready`, change one annotation:

```python
        first_probe_error: ProbeFailure | None = None
```

### Step 2.2 — Render the value

Replace `_boot_failure_details` with:

```python
    @staticmethod
    def _boot_failure_details(
        system_id: UUID, first_probe_error: ProbeFailure | None
    ) -> dict[str, object]:
        """The agent-facing details for a failed boot: the System and a closed probe reason.

        ``first_probe_error`` is rendered as its ``.value`` so the detail is a plain ``str``
        rather than an enum member, which keeps the two renderers that forward it —
        ``safe_error_details`` and the worker's ``_failure_context`` — handling a JSON scalar
        (#2220).
        """
        details: dict[str, object] = {"system_id": str(system_id)}
        if first_probe_error is not None:
            details["probe_error"] = first_probe_error.value
        return details
```

### Step 2.3 — Verify the types and the touched tests

```sh
just type
```

Expect `ty` to report no diagnostics. Then:

```sh
just test-changed
```

Expect the three existing tests that assert the old free-text values to fail —
`test_boot_timeout_includes_first_readiness_probe_error`,
`test_real_readiness_running_guest_stays_unanswered_with_probe_error`, and
`test_real_readiness_reports_domstate_probe_timeout`. Task 3 amends them. Do not amend them here.

**Acceptance criteria.** `just type` is clean. `details["probe_error"]` is a plain `str` whose
value is one of the four vocabulary values. No other detail key changes.

## Task 3 — Prove absence at both egresses and amend the pinned tests

**Where this fits.** The proof obligations #2220 sets, plus the migration of the three tests whose
contract Task 1 changed.

**Interfaces.** Consumes `ProbeFailure` (Task 1) and `_boot_failure_details` (Task 2). Provides
nothing to a later task.

Existing names relied on, confirmed present in `tests/providers/local_libvirt/test_install.py` at
base `811538fb2`: `readiness_mod` (line 27), `_capture_domstate` (line 1741), and the `_Readiness`
dataclass fake (lines 179-190) whose `probe_error` field is annotated `str | None` at line 184.
The file's existing import from `...boot.readiness` (lines 37-43) pulls `ConsoleVerdict`,
`ReadinessResult`, `_verdict_to_result`, `classify_console`, and `first_crash_signature`;
`ProbeFailure` is added to it.

### Step 3.1 — Retype the `_Readiness` fake

Line 184 annotates `probe_error: str | None = None` and line 190 passes it into a real
`ReadinessResult`. Change that annotation to `ProbeFailure | None = None` and add `ProbeFailure`
to the import block at lines 37-43, keeping it alphabetically ordered as `ruff`'s `I` rules
require. Without this, `just type` fails whole-tree — which is the type gate doing its job.

### Step 3.2 — Amend the three pinned tests

Each keeps asserting the same behaviour against the new vocabulary:

- `test_boot_timeout_includes_first_readiness_probe_error` (line ~1131): build the seam with
  `_Readiness(answered=False, probe_error=ProbeFailure.VIRSH_TIMEOUT)` and assert
  `caught.value.details["probe_error"] == "virsh_timeout"`.
- `test_real_readiness_running_guest_stays_unanswered_with_probe_error` (line ~1668): return
  `readiness_mod._DomainExitProbe(False, ProbeFailure.VIRSH_NONZERO_EXIT)` from the patched probe
  and assert `result.probe_error is ProbeFailure.VIRSH_NONZERO_EXIT`.
- `test_real_readiness_reports_domstate_probe_timeout` (line ~1892): assert
  `result.probe_error is ProbeFailure.VIRSH_TIMEOUT`.

### Step 3.3 — Add the worker-egress absence test

```python
def test_nonzero_domstate_exit_keeps_transport_text_out_of_the_worker_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The second agent-facing egress: the boot step runs as a worker job, and
    # `_failure_context` projects details into `failure_detail_*` on `jobs.wait`. `Redactor` is a
    # secrets filter and does not strip a host path, so a boundary-only fix would leave this open.
    _capture_domstate(monkeypatch, returncode=1, stdout="", stderr=_LEAKY_DOMSTATE_STDERR)

    probe = readiness_mod._domain_exit_probe("kdive-abc")
    error = CategorizedError(
        "System did not become ready within the boot window",
        category=ErrorCategory.BOOT_TIMEOUT,
        details=LocalLibvirtInstall._boot_failure_details(_SYSTEM_ID, probe.error),
    )
    context = _failure_context(error, SecretRegistry())

    assert context["failure_detail_probe_error"] == "virsh_nonzero_exit"
    rendered = repr(context)
    for substring in _TRANSPORT_SUBSTRINGS:
        assert substring not in rendered
```

Imports it needs:

```python
from kdive.jobs.worker import _failure_context
from kdive.security.secrets.secret_registry import SecretRegistry
```

Both were confirmed to exist at base `811538fb2`: `_failure_context` at `jobs/worker.py:768`, and
`SecretRegistry` at `security/secrets/secret_registry.py:15`.

### Step 3.4 — Add the `OSError` absence test

```python
def test_oserror_probe_keeps_its_filename_out_of_the_mcp_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An OSError renders `.filename` and `.strerror` in `str(exc)`, so the socket path reaches the
    # payload through the exception rather than through stderr. Same absence assertion.
    def domstate_oserror(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise OSError(2, "No such file or directory", "/run/user/1000/libvirt/virtqemud-sock")

    monkeypatch.setattr(readiness_mod.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(readiness_mod.subprocess, "run", domstate_oserror)

    probe = readiness_mod._domain_exit_probe("kdive-abc")
    error = CategorizedError(
        "System booted but a run-readiness check failed",
        category=ErrorCategory.READINESS_FAILURE,
        details=LocalLibvirtInstall._boot_failure_details(_SYSTEM_ID, probe.error),
    )
    payload = dict(ToolResponse.failure_from_error(str(_SYSTEM_ID), error).data or {})

    assert payload["probe_error"] == "virsh_probe_failed"
    rendered = repr(payload)
    for substring in _TRANSPORT_SUBSTRINGS:
        assert substring not in rendered
```

### Step 3.5 — Add the classification-coverage test

```python
@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (1, "", "error: some transport fault", readiness_mod.ProbeFailure.VIRSH_NONZERO_EXIT),
        (1, "", "", readiness_mod.ProbeFailure.VIRSH_NONZERO_EXIT),
    ],
)
def test_domstate_nonzero_exit_classifications(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
    stderr: str,
    expected: readiness_mod.ProbeFailure,
) -> None:
    _capture_domstate(monkeypatch, returncode=returncode, stdout=stdout, stderr=stderr)
    assert readiness_mod._domain_exit_probe("kdive-abc").error is expected


def test_missing_virsh_classifies_as_virsh_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness_mod.shutil, "which", lambda tool: None)
    probe = readiness_mod._domain_exit_probe("kdive-abc")
    assert probe.exited is False
    assert probe.error is readiness_mod.ProbeFailure.VIRSH_MISSING


def test_domstate_timeout_classifies_as_virsh_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def domstate_timeout(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["virsh"], timeout=2)

    monkeypatch.setattr(readiness_mod.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(readiness_mod.subprocess, "run", domstate_timeout)
    assert readiness_mod._domain_exit_probe("kdive-abc").error is (
        readiness_mod.ProbeFailure.VIRSH_TIMEOUT
    )
```

### Step 3.6 — Add the operator-log proof

```python
def test_probe_failure_logs_the_raw_transport_text_for_the_operator(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The AC2 relocation, proven rather than asserted: the operator keeps the full diagnostic, it
    # just arrives in the host log instead of the agent-facing payload.
    _capture_domstate(monkeypatch, returncode=1, stdout="", stderr=_LEAKY_DOMSTATE_STDERR)

    with caplog.at_level(logging.WARNING, logger=readiness_mod.__name__):
        readiness_mod._domain_exit_probe("kdive-abc")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "virtqemud-sock" in logged
    assert "kdive-abc" in logged
    assert "virsh_nonzero_exit" in logged
```

Needs `import logging` at the top of the test file if absent.

### Step 3.7 — Run the focused suite

```sh
just test-changed
```

Expect every test in `tests/providers/local_libvirt/test_install.py` to pass, with no collection
or import error. Then:

```sh
just lint
just type
```

Both bare, each expected to exit 0 with no findings.

**Acceptance criteria.** Both absence tests assert absence of all three transport substrings, not
inequality against the raw stderr. All four vocabulary members have a test that reaches them. The
log proof asserts the raw text is still available to the operator. The three amended tests still
pin the same behaviour they pinned before.

## Task 4 — Bite-prove every new test

**Where this fits.** #2220's fourth acceptance criterion, and the campaign's standing evidence
rule. Nothing ships until this runs.

**Interfaces.** Consumes the tests from Tasks 1 and 3. Provides the bite report.

### Step 4.1 — Commit first, then guard the tree

Commit Tasks 1-3 before injecting anything. The harness must refuse to run on a dirty tree:

```sh
test -z "$(git status --porcelain)" || { echo "refusing: dirty tree"; exit 1; }
```

Back the target files up by copy to a path outside the repo, and record `sha256sum` for each.
**Restore from that copy, never `git checkout --`.**

### Step 4.2 — Inject one controlled fault per new test and classify the result

For each new test, make the single smallest edit that should break it, run only that test, and
record the outcome. The faults:

| Test | Fault |
|---|---|
| MCP-egress absence | `_probe_failed` returns `_DomainExitProbe(False, detail)` — the pre-fix leak, restored |
| worker-egress absence | the same fault; it must fail this test too |
| `OSError` absence | the same fault |
| classification coverage | swap `ProbeFailure.VIRSH_TIMEOUT` for `ProbeFailure.VIRSH_MISSING` in the timeout arm |
| operator-log proof | delete the `_log.warning(...)` call from `_probe_failed` |

Classify each result into exactly one of three, and report which:

- **assertion bite** — pytest `FAILED` with an `AssertionError` from the test body;
- **exception bite** — pytest `FAILED` with an exception raised from the code under test (this
  counts as a bite; a classifier that only recognises assertion shapes reports a false NO BITE);
- **no bite** — the test passed, or the run produced a collection, import, or fixture error, which
  is not a bite and must be fixed before the proof means anything.

Injecting the leak fault will make the two absence tests fail on `payload["probe_error"] ==
"virsh_nonzero_exit"`. That is an assertion bite, and it also demonstrates the absence assertion
itself: with the fault in place the transport substrings are present again.

### Step 4.3 — Prove the gate does not fire spuriously

A fault proves only one direction. Also make a genuine no-op edit — add a blank line inside
`_probe_failed`'s body region — run the same tests, and confirm every one passes. A gate that
fires on a no-op is as useless as one that never fires.

### Step 4.4 — Restore and verify byte identity

Restore each file from the filesystem backup, then:

```sh
sha256sum src/kdive/providers/local_libvirt/lifecycle/boot/readiness.py \
          src/kdive/providers/local_libvirt/lifecycle/install.py \
          tests/providers/local_libvirt/test_install.py
git status --porcelain
```

Every digest must equal the pre-injection digest and `git status --porcelain` must be empty.

**Acceptance criteria.** Every new test recorded an assertion bite or an exception bite, with the
shape named. The no-op control recorded no bite for every test. Byte identity restored, verified
by digest, and the tree is clean.

## Task 5 — Full gate

### Step 5.1

```sh
just ci > /tmp/ci-2220.log 2>&1 < /dev/null
```

Run it bare, as the last command, with no pipe and no trailing `; echo $?`. Expect exit 0. The
redirects give ansible-core the blocking streams it requires. A fresh worktree needs
`just install-mermaid-deps` once before `check-mermaid` passes.

**Acceptance criteria.** `just ci` exits 0.

## Deferrals

None recorded yet. Any deferral a `$trial-loop` run disposes of on this branch is appended here
with its owning record path or tracker issue, whichever way the run ended.
