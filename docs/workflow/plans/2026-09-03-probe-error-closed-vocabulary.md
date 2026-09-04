# Readiness probe failures leave the probe as a closed vocabulary

**Goal.** Stop `readiness.py` returning free-form `virsh` transport text as
`ReadinessResult.probe_error`. The probe classifies its failure into a closed `StrEnum`; the
bounded raw text is logged for the operator and never returned. Two agent-facing egresses stop
leaking host paths, and no renderer changes.

**Architecture.** `readiness.py` gains `ProbeFailure`, a four-member `StrEnum`. Every failing
return of `_domain_exit_probe` logs the bounded raw text at `WARNING` and returns a member.
`_DomainExitProbe.error` and `ReadinessResult.probe_error` are typed `ProbeFailure | None`, so
`ty`'s strict whole-tree check refuses a free-form `str` at every call site in `src/` and `tests/`.
`LocalLibvirtBooter._boot_failure_details` renders `.value` into `details["probe_error"]`.

**Tech stack.** Python 3.14, `uv`, pytest, `ruff`, `ty`.

Expected implementation size: 165–205 changed lines (M) — from the file map below: ~46 in
`readiness.py` (the enum, the helper, two annotations, five rewritten returns), ~12 in
`install.py`, ~113 in `test_install.py` (three amended assertions, six new tests, five imports),
2 in `test_external_boot.py`, and 1 in the ADR status line. The range is unchanged from the
previous revision: test 2.5b adds roughly ten lines and the two shipped docstrings cut in Steps
1.3 and 1.5 remove roughly the same.

Spec: [`docs/workflow/specs/2026-09-03-probe-error-closed-vocabulary-design.md`](../specs/2026-09-03-probe-error-closed-vocabulary-design.md).
Decision: [ADR-0594](../../adr/0594-readiness-probe-failures-leave-the-probe-as-a-closed-vocabulary.md).

## Global Constraints

- Branch `feat/probe-error-closed-vocabulary-2220`; `BASE_BRANCH` `main`; base commit `811538fb2`.
- Guardrails: `just lint`, `just type`, `just test-changed` while iterating; `just ci` as the
  pre-push gate. Run gates **bare** — a pipe or a trailing `; echo $?` replaces the recipe's exit
  status. Capture with `just ci > <file> 2>&1 < /dev/null`.
- **`just lint` covers this plan and the spec**: `ruff format` formats fenced Python blocks in
  Markdown, and `pyproject.toml:66` excludes only `docs/adr`. Run it after editing them, not only
  after editing code.
- Ruff line length 100, lint set `E,F,I,UP,B,SIM`; `ty` whole-tree. Run `just format` before
  committing, and accept its PEP 758 rewrite of `except (A, B):` to `except A, B:` if it makes one.
- Prose rule (project-wide): plain and factual; avoid "critical", "robust", "comprehensive".
- `CategorizedError.details` values must be finite JSON scalars.
- Do **not** modify `src/kdive/serialization.py`, `src/kdive/security/secrets/redaction.py`, or
  anything under `src/kdive/providers/remote_libvirt/` — all three are out of scope for #2220.

## File map

| File | Fate | Answerable for |
|---|---|---|
| `src/kdive/providers/local_libvirt/lifecycle/boot/readiness.py` | modified | classifying a probe failure and logging the raw text |
| `src/kdive/providers/local_libvirt/lifecycle/install.py` | modified | rendering the member into `details["probe_error"]` |
| `tests/providers/local_libvirt/test_install.py` | modified | absence proofs, classification coverage, operator-log proof |
| `tests/providers/local_libvirt/test_external_boot.py` | modified | the one parametrized `ReadinessResult` pinning a non-empty `probe_error` |
| the ADR-0594 record linked above | modified | `## Status` flips `Proposed` → `Accepted` in the citing commit |

Nothing is created. The survey behind this map is
`rg -n 'probe_error|_DomainExitProbe|ReadinessResult\(' src tests`; run that, not a narrower
check, if the map is ever re-derived. Every other construction site passes `None` or omits the
field and needs no change — including `tests/adversarial/test_provider_xml.py:164`,
`tests/providers/local_libvirt/lifecycle/boot/test_session.py:2392`, and
`src/kdive/providers/local_libvirt/lifecycle/boot/external_boot.py:1219`, which compares against
`ReadinessResult(True, True, None)`.

## Names this plan borrows, confirmed at base `811538fb2`

- `readiness.py`: `_bounded_probe_error(message: str) -> str` (58); `StrEnum` already imported from
  `enum` (9); `from __future__ import annotations` (3), so forward references resolve. The
  constants the unchanged body uses (`_DOMSTATE_PROBE_TIMEOUT`, `_TERMINAL_DOMSTATES`, `_VIRSH`)
  are at 19-21 and are not touched.
- `install.py`: **`_boot_failure_details` is a `@staticmethod` of `LocalLibvirtBooter`** (class 170,
  method 266) — *not* of `LocalLibvirtInstall`, a separate composing class at 582 exposing only
  `install`, `boot`, `from_env`. The `...boot.readiness` import block (53-57) already pulls
  `_POLL_INTERVAL_SECONDS`, `ReadinessResult`, `_real_readiness`. `_install_failure` returns at
  132-136, `_libvirt_transport_failure` at 149-152.
- `test_install.py`: `readiness_mod` (27); the `...boot.readiness` import block (37-43); the
  `_Readiness` fake (179-190) with `probe_error: str | None` at **185**; and, at line 1741,
  `_capture_domstate(monkeypatch, *, returncode: int, stdout: str, stderr: str = "")`, which stubs
  `shutil.which` and `subprocess.run`. Already imported: `CategorizedError`, `ErrorCategory` (26),
  `subprocess`, `pytest`, `UUID`, `LocalLibvirtInstall` (46). **Not** imported and required:
  `logging`, `kdive.mcp.responses.ToolResponse`, `LocalLibvirtBooter`,
  `kdive.jobs.worker._failure_context`,
  `kdive.security.secrets.secret_registry.SecretRegistry`.
- `jobs/worker.py`: `_failure_context(exc, registry)` (768).
  `security/secrets/secret_registry.py`: `SecretRegistry` (15), no-argument constructible.

`_SYS` (`11111111-…`) and `_RUN` (`22222222-…`) are module constants at `test_install.py:58-59`.
The new tests use **`_SYS`** as the System id, matching every existing boot test in the file
(`inst.boot(_SYS)` at :1138, `details["system_id"] == str(_SYS)` at :1128). Do not inline a UUID
literal, and do not use `_RUN` — it is a Run id.

## Task 1 — Classify at the probe and render the member

One task: `just type` is red between the two edits, so no reviewer could accept half of it.

**Interfaces.** Defines, for Task 2 (bodies in Steps 1.3-1.5; the four member values are in the
spec's vocabulary contract):

```python
# readiness.py
class ProbeFailure(StrEnum): ...                      # four members, snake_case values
def _probe_failed(domain_name: str, failure: ProbeFailure, detail: str) -> _DomainExitProbe
class _DomainExitProbe(NamedTuple): exited: bool; error: ProbeFailure | None = None
class ReadinessResult(NamedTuple): answered: bool; ok: bool; probe_error: ProbeFailure | None = None

# install.py — @staticmethod on LocalLibvirtBooter
def _boot_failure_details(system_id: UUID, first_probe_error: ProbeFailure | None) -> dict[str, object]
```

### Step 1.1 — Write the failing test

Two module constants, verbatim:

```python
# The ordinary stderr `virsh` writes when the session daemon is unreachable. Every fragment is
# host-derived and none of it is actionable for an agent.
_LEAKY_DOMSTATE_STDERR = (
    "error: failed to connect to the hypervisor\n"
    "error: Failed to connect socket to '/run/user/1000/libvirt/virtqemud-sock': "
    "No such file or directory"
)
_TRANSPORT_SUBSTRINGS = ("/run/user/1000/libvirt/virtqemud-sock", "/run/user", "virtqemud-sock")
```

Then `test_nonzero_domstate_exit_keeps_transport_text_out_of_the_mcp_payload(monkeypatch)`, the
shape every later absence test follows: `_capture_domstate(monkeypatch, returncode=1, stdout="",
stderr=_LEAKY_DOMSTATE_STDERR)`; `probe = readiness_mod._domain_exit_probe("kdive-abc")`; a
`CategorizedError("System did not become ready within the boot window",
category=ErrorCategory.BOOT_TIMEOUT, details=LocalLibvirtBooter._boot_failure_details(_SYS,
probe.error))`; `payload = dict(ToolResponse.failure_from_error(str(_SYS), error).data or {})`;
then `assert payload["probe_error"] == "virsh_nonzero_exit"` followed by
`assert substring not in repr(payload)` for every `_TRANSPORT_SUBSTRINGS` entry. Carry a comment
saying why absence rather than difference: a transform returning a *different* leaky string passes
a `!=` assertion and fails this one (#2220).

Add `from kdive.mcp.responses import ToolResponse` and `LocalLibvirtBooter` to the existing
`...lifecycle.install` import.

### Step 1.2 — Confirm it fails

```sh
uv run python -m pytest "tests/providers/local_libvirt/test_install.py::test_nonzero_domstate_exit_keeps_transport_text_out_of_the_mcp_payload" -q
```

Expect `1 failed` with an **assertion** failure on `payload["probe_error"]`, showing the raw stderr
as the actual value. An `AttributeError` or any collection error here means an import or class name
is wrong — fix that before continuing; it is not the red this step is for.

### Step 1.3 — `readiness.py`

Add `import logging` and `_log = logging.getLogger(__name__)`. After `ConsoleVerdict`, add:

```python
class ProbeFailure(StrEnum):
    """Why a ``virsh domstate`` probe failed, as a closed agent-facing vocabulary (ADR-0594)."""

    VIRSH_MISSING = "virsh_missing"
    VIRSH_TIMEOUT = "virsh_timeout"
    VIRSH_PROBE_FAILED = "virsh_probe_failed"
    VIRSH_NONZERO_EXIT = "virsh_nonzero_exit"
```

After `_bounded_probe_error`, add `_probe_failed` — a one-line docstring citing ADR-0594, then:

```python
def _probe_failed(domain_name: str, failure: ProbeFailure, detail: str) -> _DomainExitProbe:
    _log.warning(
        "domstate probe failed for %s (%s): %s",
        domain_name,
        failure.value,
        _bounded_probe_error(detail),
    )
    return _DomainExitProbe(False, failure)
```

Retype the two carriers — annotation only, plus `_DomainExitProbe`'s docstring, which becomes
"The domstate probe result plus its classified probe-failure reason":

- `ReadinessResult.probe_error: ProbeFailure | None = None`
- `_DomainExitProbe.error: ProbeFailure | None = None`

### Step 1.4 — Rewrite the five failing returns

In `_domain_exit_probe`, leave every non-failing line as it is and replace each failing return:

| Arm | Replacement |
|---|---|
| `virsh is None` | `return _probe_failed(domain_name, ProbeFailure.VIRSH_MISSING, "virsh executable not found")` |
| `except subprocess.TimeoutExpired as exc` | `return _probe_failed(domain_name, ProbeFailure.VIRSH_TIMEOUT, f"virsh domstate timed out after {exc.timeout:g}s")` |
| `except FileNotFoundError as exc` | `return _probe_failed(domain_name, ProbeFailure.VIRSH_MISSING, f"virsh domstate probe failed: {exc}")` |
| `except (subprocess.SubprocessError, OSError) as exc` | `return _probe_failed(domain_name, ProbeFailure.VIRSH_PROBE_FAILED, f"virsh domstate probe failed: {exc}")` |
| `if proc.returncode != 0` | `return _probe_failed(domain_name, ProbeFailure.VIRSH_NONZERO_EXIT, stderr or f"virsh domstate exited {proc.returncode}")` |

Note the third row: the `FileNotFoundError` arm gains `as exc` and now logs `str(exc)` instead of
the fixed literal, so an ENOENT transport fault leaves the operator its errno and path (ADR-0594).
Its agent-facing member is unchanged.

The two success returns (`_DomainExitProbe(True)`) and the final `_DomainExitProbe(False)` are
untouched. `_bounded_probe_error` keeps its name and body; it now bounds the logged text.

### Step 1.5 — `install.py`

Add `ProbeFailure` to the `...boot.readiness` import block, alphabetically ordered. In
`LocalLibvirtBooter._await_ready`, annotate `first_probe_error: ProbeFailure | None = None`. Then:

Keep its one-line docstring, noting `.value` keeps the detail a JSON scalar (ADR-0594):

```python
    @staticmethod
    def _boot_failure_details(
        system_id: UUID, first_probe_error: ProbeFailure | None
    ) -> dict[str, object]:
        details: dict[str, object] = {"system_id": str(system_id)}
        if first_probe_error is not None:
            details["probe_error"] = first_probe_error.value
        return details
```

### Step 1.6 — Flip the ADR, then verify

ADR-0594 is `Proposed`. `scripts/guards/check_adr_status.py` fails any ADR cited from `src/` or
`tests/` while `Proposed`, and Steps 1.3 and 1.5 add exactly those citations — so in **this same
commit**, flip its `## Status` to `Accepted (2026-09-03)`. That is `docs/adr/README.md`'s
ratification rule, not a workaround: the PR that adds the citations is the PR that ratifies.

Re-run the Step 1.2 command; expect `1 passed`. Then `just type` — expect diagnostics from exactly
**three** sites, all repaired in Task 2: `test_install.py:190` (the `ReadinessResult(...)` call
inside `_Readiness.readiness`; ty anchors it at the call, but the fix is the annotation on 185),
`test_install.py:1679` (a `_DomainExitProbe(False, "virsh hiccup")` lambda, cleared by Step 2.2's
second bullet), and `test_external_boot.py:2666`. Then `just adr-status-check`, expecting exit 0.

**Acceptance criteria.** `ProbeFailure` has exactly the four members above. `_domain_exit_probe`
returns a member or `None`, never caller text. Every failing branch logs the bounded text once, and
the `FileNotFoundError` arm logs `str(exc)`. `details["probe_error"]` is a plain `str` from the
vocabulary.

## Task 2 — Prove absence at both egresses; amend the pinned tests

### Step 2.1 — Retype the sites `ty` flagged

`test_install.py:185` becomes `probe_error: ProbeFailure | None = None`; add `ProbeFailure` to the
`...boot.readiness` import block (37-43), alphabetically ordered.

`test_external_boot.py:2666` becomes
`ReadinessResult(True, True, ProbeFailure.VIRSH_PROBE_FAILED)`; add `ProbeFailure` to that file's
`...boot.readiness` import at :44. It must stay a **non-None** `probe_error`: the parametrization
at :2661-2667 enumerates results that are *not* exact success, and
`external_boot.py:1219` compares against `ReadinessResult(True, True, None)`, so a `None` here
would silently stop pinning what the test exists to pin.

`just type` fails whole-tree without both — the type gate doing its job.

### Step 2.2 — Amend the three tests that pin the old free text

- `test_boot_timeout_includes_first_readiness_probe_error` (~1131): seam becomes
  `_Readiness(answered=False, probe_error=ProbeFailure.VIRSH_TIMEOUT)`; assert
  `caught.value.details["probe_error"] == "virsh_timeout"`.
- `test_real_readiness_running_guest_stays_unanswered_with_probe_error` (~1668): patched probe
  returns `readiness_mod._DomainExitProbe(False, ProbeFailure.VIRSH_NONZERO_EXIT)`; assert
  `result.probe_error is ProbeFailure.VIRSH_NONZERO_EXIT`.
- `test_real_readiness_reports_domstate_probe_timeout` (~1892): assert
  `result.probe_error is ProbeFailure.VIRSH_TIMEOUT`. This is the real `TimeoutExpired` arm, and it
  is what covers `VIRSH_TIMEOUT`.

### Steps 2.3-2.6 — Five more tests

Each follows Step 1.1's shape — arrange the probe, build the `CategorizedError` from
`LocalLibvirtBooter._boot_failure_details(_SYS, probe.error)`, render, assert the member and
then the absence of every `_TRANSPORT_SUBSTRINGS` entry from `repr(...)`. Only the arrangement and
the expected member differ:

| Step / test | Arrangement | Asserts |
|---|---|---|
| 2.3 `…_out_of_the_worker_payload` | same `_capture_domstate` as 1.1; render with `_failure_context(error, SecretRegistry())` instead of `ToolResponse` | `context["failure_detail_probe_error"] == "virsh_nonzero_exit"`, then absence |
| 2.4 `test_oserror_probe_keeps_its_filename_out_of_the_mcp_payload` | patch `shutil.which` to `f"/usr/bin/{tool}"` and `subprocess.run` to raise `OSError(13, "Permission denied", "/run/user/1000/libvirt/virtqemud-sock")`; category `READINESS_FAILURE` | `payload["probe_error"] == "virsh_probe_failed"`, then absence |
| 2.5 `test_missing_virsh_classifies_as_virsh_missing` | patch `shutil.which` to `lambda tool: None`; no rendering | `probe.exited is False` and `probe.error is readiness_mod.ProbeFailure.VIRSH_MISSING` |
| 2.5b `test_enoent_from_the_exec_classifies_as_virsh_missing_and_logs_the_path` | patch `shutil.which` to a path and `subprocess.run` to raise `FileNotFoundError(2, "No such file or directory", "<socket path>")`, inside `caplog.at_level(...)` | `probe.error is ProbeFailure.VIRSH_MISSING` **and** the `caplog` record contains the socket basename |
| 2.6 `test_probe_failure_logs_the_raw_transport_text_for_the_operator` | same `_capture_domstate` as 1.1, inside `caplog.at_level(logging.WARNING, logger=readiness_mod.__name__)` | the joined `record.getMessage()` contains `virtqemud-sock`, `kdive-abc`, and `virsh_nonzero_exit` |

Step 2.3 carries a comment saying why it exists: the boot step runs as a worker job,
`_failure_context` is persisted to the job row, `jobs.wait` reads it back, and `Redactor` leaves a
host path untouched — so a boundary-only fix would leave this egress open.

Step 2.4 carries a comment saying why errno 13: errno 2 would construct a `FileNotFoundError` and
be taken by the earlier arm, testing the wrong branch entirely.

All four members are reached — `VIRSH_NONZERO_EXIT` (2.3, 2.6), `VIRSH_PROBE_FAILED` (2.4),
`VIRSH_TIMEOUT` (2.2's third test), `VIRSH_MISSING` (2.5, 2.5b) — and so are **both**
`VIRSH_MISSING` branches. Member coverage alone would hide the second: `shutil.which` returning
`None` and an ENOENT from the exec are different arms mapping to one member, and only 2.5b
exercises the arm whose `str(exc)` log is the compensating control ADR-0594 relies on. That arm is
invisible to coverage tooling too — `_domain_exit_probe` carries `# pragma: no cover - live_vm`.

### Step 2.7 — Verify

Run `just test-changed`, `just lint`, `just type` — each bare, each expected to exit 0, with no
collection or import error.

**Acceptance criteria.** Both absence tests assert absence of all three transport substrings. All
four members are reached. The log proof shows the raw text still reaches the operator. The three
amended tests pin the behaviour they pinned before.

## Task 3 — Bite-prove every new test

#2220's fourth acceptance criterion. Nothing ships until this runs.

### Step 3.1 — Commit first, then guard the tree

Commit Tasks 1-2 first. The harness refuses a dirty tree (`test -z "$(git status --porcelain)"`).
Copy the **four** modified source and test files to a backup **outside** the repo with a
`sha256sum` for each, and restore from that copy — **never `git checkout --`**.

### Step 3.2 — One controlled fault per test

Make the smallest edit that should break each test, run only that test, and record the outcome
**and the failing assertion or exception text**:

| Test | Fault |
|---|---|
| MCP-egress absence | the two-sided leak fault below |
| worker-egress absence | the same two-sided fault |
| `OSError` absence | the same two-sided fault |
| `virsh_missing` classification | return `VIRSH_PROBE_FAILED` from the `shutil.which is None` arm |
| ENOENT-from-exec (2.5b) | revert the `FileNotFoundError` arm's detail to the fixed literal `"virsh executable not found"` |
| operator-log proof | delete the `_log.warning(...)` call from `_probe_failed` |

**The leak fault must span both source files.** Restoring only the producer half leaves
`probe.error` a plain `str`, so `_boot_failure_details`'s `.value` raises `AttributeError` in the
test's *arrange* line before any assertion runs — a message naming an attribute, not the leak, which
the rule below correctly calls a defective test. So inject both halves: `_probe_failed` returns
`_DomainExitProbe(False, detail)` **and** `_boot_failure_details` renders
`details["probe_error"] = str(first_probe_error)`. The three absence tests then fail on the payload
assertion with the raw stderr as the actual value — the pre-fix behaviour, and the clean assertion
failure criterion 4 asks for.

Classify each result as exactly one of three, and report which: an **assertion bite** (`FAILED` with
an `AssertionError` from the test body); an **exception bite** (`FAILED` with an exception from the
code under test — this counts, and a classifier recognising only assertion shapes reports a false
NO BITE); or **no bite** (passed, or a collection/import/fixture error, which proves nothing and
must be fixed first).

**The recorded failure text must name the behaviour the fault changed.** A bite whose message is
about something else — a missing attribute, a wrong class, an unrelated branch — is a defective
test. This is the check that catches a test calling a method on the wrong class, or aimed at an arm
the fault never reaches.

### Step 3.3 — Prove the gate does not fire spuriously

With the tree restored and **no fault injected**, run all five tests: every one must pass. Any test
failing here is defective and must be fixed before its Step 3.2 bite counts — a stop condition, not
an observation.

### Step 3.4 — Restore and verify byte identity

Restore from the backup, then `sha256sum` the four files and check `git status --porcelain`. Every
digest must match the pre-injection digest and the status must be empty.

**Acceptance criteria.** Every new test recorded an assertion or exception bite, with the shape
named and the failure text confirmed to name the faulted behaviour. Every test passes under the
no-fault control. Byte identity restored and verified.

## Task 4 — Full gate

`just ci > /tmp/ci-2220.log 2>&1 < /dev/null`, bare, as the last command. Expect exit 0. A fresh
worktree needs `just install-mermaid-deps` once first.

## Deferrals

None recorded. Any deferral a `$trial-loop` run disposes of on this branch is appended here with
its owning record path or tracker issue, whichever way the run ended.
