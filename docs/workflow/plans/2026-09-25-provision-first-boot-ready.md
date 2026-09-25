# Provision first-boot readiness gate — implementation plan

Goal: local-libvirt `provision`/`reprovision` return (and the System reaches `ready`) only after
the guest's first boot emits `kdive-ready`; a guest that does not fails with
`PROVISIONING_FAILURE` and leaves no domain.

Architecture: a shared poll loop in `boot/readiness.py` serves both `runs.boot` and provisioning.
`LocalLibvirtProvisioning` gains an injected `first_boot_readiness` seam, wired only by
`from_env`. `_define_and_start` owns the console truncate and skips it for an already-active
domain. Spec: [provision-first-boot-ready](../specs/2026-09-25-provision-first-boot-ready-design.md);
decision: [ADR-0680](../../adr/0680-local-libvirt-provision-ready-after-first-boot.md).

Tech stack: Python 3.14, libvirt-python, pytest, `uv`, `just`.

Expected implementation size: 350–550 changed lines (L) — five tasks: ~70 lines of readiness
refactor, ~90 lines of provisioning source, ~250–350 lines of unit tests, ~40 lines of docstring
plus regenerated reference, ~60 lines of live test.

## Global Constraints

- Ruff line length 100, lint set `E,F,I,UP,B,SIM`; `ty` strict over `src` and `tests`.
- `ErrorCategory` values only from `kdive.domain.errors`; no new category.
- Error details stay JSON scalars; `crash_signature` only through `is_crash_signature`.
- Prose rule: no "critical", "robust", "comprehensive", "elegant"; "Milestone" not "Sprint".
- No new dependency, setting, migration, or MCP parameter.
- Guardrails: `just lint`, `just type`, `just test-changed`, `just test-verbose <path>`; before
  push `git fetch origin main && just records`, then `just ci > <file> 2>&1 < /dev/null`.

## File map

| File | Now owns | Change |
|---|---|---|
| `src/kdive/providers/local_libvirt/lifecycle/boot/readiness.py` | console probe, verdicts | + `Readiness`, `boot_window_polls`, `ReadinessOutcome`, `poll_readiness`, `readiness_failure_details` |
| `src/kdive/providers/local_libvirt/lifecycle/install.py` | booter/installer | uses the shared loop and details; `_boot_window_polls`, `Readiness` become imports |
| `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` | define/start, teardown | active check, truncate inside `_define_and_start`, domain teardown on failure, first-boot wait seam |
| `src/kdive/mcp/tools/lifecycle/systems/registrar.py` | tool wrappers | provision/reprovision docstring sentence |
| `docs/guide/reference/systems.md` | generated reference | regenerated |
| `tests/providers/local_libvirt/lifecycle/boot/test_readiness_poll.py` | — | new: shared loop tests |
| `tests/providers/local_libvirt/test_provisioning.py` | provisioning unit tests | fake `isActive`, new cases |
| `tests/integration/test_first_boot_host_keys_live.py` | #2757 live proof | + ready-implies-marker test |

No compatibility path is retained: `LocalLibvirtBooter._boot_failure_details` stays as a
`staticmethod` alias because existing tests call it, and `install._boot_window_polls` stays an
import alias for the same reason.

## Task 1 — shared readiness poll loop

Files: modify `boot/readiness.py`, `lifecycle/install.py`; create
`tests/providers/local_libvirt/lifecycle/boot/test_readiness_poll.py`.

Interfaces (later tasks rely on these exact names):

```python
type Readiness = Callable[[UUID], ReadinessResult]
def boot_window_polls() -> int: ...
class ReadinessOutcome(NamedTuple):
    result: ReadinessResult | None
    first_probe_error: ProbeFailure | None
def poll_readiness(readiness: Readiness, system_id: UUID, polls: int) -> ReadinessOutcome: ...
def readiness_failure_details(
    system_id: UUID, first_probe_error: ProbeFailure | None, crash_signature: str | None = None
) -> dict[str, object]: ...
```

Verification:

- Contract `poll_readiness` returns the first answer, the first probe error, `None` on
  exhaustion. Mode: focused-test — `test_readiness_poll.py`; red: `ImportError` for
  `poll_readiness`; green: `just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_readiness_poll.py`.
- Contract `runs.boot` errors unchanged. Mode: focused-test — existing
  `tests/providers/local_libvirt/test_install.py`; green: `just test-verbose tests/providers/local_libvirt/test_install.py`
  passes with no test edits.

Steps:

1. Write the new test file:

```python
from uuid import UUID

from kdive.providers.local_libvirt.lifecycle.boot.readiness import (
    ProbeFailure,
    ReadinessResult,
    poll_readiness,
)

_SYS = UUID("11111111-1111-1111-1111-111111111111")


def _seq(*results: ReadinessResult):
    it = iter(results)
    calls: list[UUID] = []

    def probe(system_id: UUID) -> ReadinessResult:
        calls.append(system_id)
        return next(it)

    return probe, calls


def test_returns_first_answer() -> None:
    probe, calls = _seq(ReadinessResult(False, False), ReadinessResult(True, True))
    outcome = poll_readiness(probe, _SYS, 5)
    assert outcome.result == ReadinessResult(True, True)
    assert len(calls) == 2


def test_keeps_first_probe_error() -> None:
    probe, _ = _seq(
        ReadinessResult(False, False, ProbeFailure.VIRSH_TIMEOUT),
        ReadinessResult(False, False, ProbeFailure.VIRSH_MISSING),
        ReadinessResult(True, False, crash_signature="Kernel panic"),
    )
    outcome = poll_readiness(probe, _SYS, 5)
    assert outcome.first_probe_error is ProbeFailure.VIRSH_TIMEOUT
    assert outcome.result is not None and outcome.result.crash_signature == "Kernel panic"


def test_exhaustion_returns_none() -> None:
    probe, calls = _seq(*[ReadinessResult(False, False)] * 3)
    assert poll_readiness(probe, _SYS, 3).result is None
    assert len(calls) == 3
```

2. Run it; expect `ImportError: cannot import name 'poll_readiness'`.
3. In `readiness.py` add the interfaces above. `boot_window_polls` is
   `math.ceil(config.require(LIBVIRT_BOOT_WINDOW_S) / _POLL_INTERVAL_SECONDS)`.
   `poll_readiness`:

```python
def poll_readiness(readiness: Readiness, system_id: UUID, polls: int) -> ReadinessOutcome:
    """Poll ``readiness`` up to ``polls`` times; return the first answer (ADR-0680)."""
    first_probe_error: ProbeFailure | None = None
    for _ in range(polls):
        result = readiness(system_id)
        if first_probe_error is None and result.probe_error is not None:
            first_probe_error = result.probe_error
        if result.answered:
            return ReadinessOutcome(result, first_probe_error)
    return ReadinessOutcome(None, first_probe_error)
```

   `readiness_failure_details` is the body of `LocalLibvirtBooter._boot_failure_details`
   (import `is_crash_signature` from `kdive.domain.lifecycle.crash_signatures`).
4. In `install.py`: delete `_boot_window_polls` and `type Readiness`; import
   `boot_window_polls as _boot_window_polls`, `Readiness`, `poll_readiness`,
   `readiness_failure_details`. Keep the comment block above the import. Replace
   `_boot_failure_details` with `_boot_failure_details = staticmethod(readiness_failure_details)`
   and `_await_ready` with:

```python
    def _await_ready(self, system_id: UUID, polls: int) -> None:
        outcome = poll_readiness(self._readiness, system_id, polls)
        result = outcome.result
        if result is None:
            raise CategorizedError(
                "System did not become ready within the boot window",
                category=ErrorCategory.BOOT_TIMEOUT,
                details=readiness_failure_details(system_id, outcome.first_probe_error),
            )
        if not result.ok:
            raise CategorizedError(
                "System booted but a run-readiness check failed",
                category=ErrorCategory.READINESS_FAILURE,
                details=readiness_failure_details(
                    system_id, outcome.first_probe_error, result.crash_signature
                ),
            )
```

5. Run both focused commands; expect all pass. `just lint && just type`. Commit
   `refactor(local-libvirt): share the readiness poll loop`.

## Task 2 — define/start owns the truncate; failure removes the domain

Files: modify `lifecycle/provisioning.py`, `tests/providers/local_libvirt/test_provisioning.py`.

Interfaces: `_LibvirtDomain.isActive() -> int`; `_define_and_start(xml, system_id) -> None`
(unchanged signature); new `_best_effort_teardown_domain(domain_name: str) -> None`.

Verification:

- Contract an active domain skips truncate and `create()`. Mode: focused-test —
  `test_provision_active_domain_skips_truncate_and_create`; red: `prepare` recorded;
  green: `just test-verbose tests/providers/local_libvirt/test_provisioning.py`.
- Contract a fresh domain truncates before `create()`. Mode: focused-test —
  `test_provision_truncates_console_before_create`; red: truncate precedes `defineXML`.
- Contract a failure after define/start destroys and undefines the domain, and a teardown fault
  does not replace the error. Mode: focused-test — `test_provision_console_failure_removes_domain`,
  `test_provision_teardown_fault_keeps_original_error`.

Steps:

1. In `_ProvDomain` add `active: bool = False`, `isActive()` returning
   `int(self.active or self.created)`, and set `self.created = True` only as today. Add an
   `events: list[str]` field to `_ProvConn`, appended `"define"` in `defineXML`; domains share it
   by recording `"create"` through a `conn` back-reference passed at construction
   (`_ProvDomain(name, events=self.events)` with `events: list[str] = field(default_factory=list)`).
2. Write the tests:

```python
def test_provision_truncates_console_before_create() -> None:
    conn = _ProvConn()
    order = conn.events
    prov = _prov(conn, prepare_console_log=lambda _p: order.append("truncate"))
    prov.provision(_SYS, _profile())
    assert order == ["define", "truncate", "create"]


def test_provision_active_domain_skips_truncate_and_create() -> None:
    conn = _ProvConn()
    name = "kdive-11111111-1111-1111-1111-111111111111"
    conn.defined[name] = _ProvDomain(name, active=True, events=conn.events)
    prov = _prov(conn, prepare_console_log=lambda _p: conn.events.append("truncate"))
    assert prov.provision(_SYS, _profile()) == name
    assert conn.events == ["define"]


def test_provision_console_failure_removes_domain() -> None:
    conn = _ProvConn()

    def fail(_p: object) -> None:
        raise CategorizedError("console", category=ErrorCategory.PROVISIONING_FAILURE)

    with pytest.raises(CategorizedError):
        _prov(conn, prepare_console_log=fail).provision(_SYS, _profile())
    assert next(iter(conn.defined.values())).undefined is True


def test_provision_teardown_fault_keeps_original_error() -> None:
    conn = _ProvConn()
    dom_name = "kdive-11111111-1111-1111-1111-111111111111"
    conn.defined[dom_name] = _ProvDomain(
        dom_name, create_error=libvirt.VIR_ERR_INTERNAL_ERROR,
        undefine_error=libvirt.VIR_ERR_INTERNAL_ERROR, events=conn.events,
    )
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).provision(_SYS, _profile())
    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
```

   `_prov` gains a `prepare_console_log` keyword defaulting to `lambda _path: None`.
3. Run; expect the ordering and skip tests to fail.
4. Implement in `provisioning.py`: add `isActive` to `_LibvirtDomain`; remove
   `self._files.prepare_console(system_id)` from `provision`; set `started = True` on the line
   before `self._define_and_start(xml, system_id)` (initialise `started = False` beside the other
   flags); in the `except CategorizedError` arm call
   `self._best_effort_teardown_domain(domain_name_for(system_id))` first when `started`.
   `_define_and_start` body inside its `try`:

```python
            domain = conn.defineXML(xml)
            if domain.isActive():
                # A retry after a lease reclaim: an earlier attempt of this provision truncated
                # the console and started this boot, so its log holds the whole boot (ADR-0680).
                _log.info("domain for System %s is already running; waiting on it", system_id)
                return
            self._files.prepare_console(system_id)
            try:
                domain.create()
            ...unchanged...
```

   `_best_effort_teardown_domain` wraps `self._teardown_domain(domain_name)` in
   `try/except CategorizedError` logging a warning with `exc_info=True`, as
   `_best_effort_reclaim` does.
5. Focused green; `just lint && just type`. Commit
   `fix(local-libvirt): skip console truncate for a running domain on retry`.

## Task 3 — the first-boot wait

Files: modify `lifecycle/provisioning.py`, `tests/providers/local_libvirt/test_provisioning.py`.

Interfaces: `LocalLibvirtProvisioning.__init__(..., first_boot_readiness: Readiness | None = None)`;
private `_await_first_boot(readiness: Readiness, system_id: UUID, accel: str) -> None`.

Verification (all Mode: focused-test in `test_provisioning.py`; red: `TypeError: unexpected
keyword argument 'first_boot_readiness'`; green: `just test-verbose tests/providers/local_libvirt/test_provisioning.py`):

- ready → returns name (`test_first_boot_ready_returns_name`);
- pending then ready (`test_first_boot_waits_through_pending`);
- timeout → `PROVISIONING_FAILURE`, `first_boot == "timeout"`, domain undefined, overlay and
  baseline removed (`test_first_boot_timeout_fails_and_reclaims`);
- crash → `first_boot == "not_ready"`, `crash_signature` kept
  (`test_first_boot_crash_fails_not_ready`);
- exit → `first_boot == "not_ready"` (`test_first_boot_exit_fails_not_ready`);
- TCG accel multiplies the poll count (`test_first_boot_tcg_scales_polls`, monkeypatch
  `KDIVE_LIBVIRT_BOOT_WINDOW_S=10`, `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER=3`, caps with a
  `<guest>` for `ppc64` giving `accel="tcg"`, count 6 calls);
- no seam → no probe call (existing tests stay green);
- active domain still waits (`test_first_boot_waits_on_running_domain`);
- reprovision timeout (`test_reprovision_first_boot_timeout_fails`).
- `from_env` wires `_real_readiness` (`test_from_env_wires_real_first_boot_readiness`, asserts
  `prov._first_boot_readiness is _real_readiness`).

Steps:

1. `_prov` gains `first_boot_readiness: Readiness | None = None` passed through. Write the tests
   with a scripted probe (`ReadinessResult(False, False)` pending, `(True, True)` ready,
   `(True, False, crash_signature="Kernel panic")` crash, `(True, False)` exit) and
   `KDIVE_LIBVIRT_BOOT_WINDOW_S=10` (two polls) via `monkeypatch.setenv`.
2. Run; expect the `TypeError`.
3. Implement: store the seam; `from_env` passes `first_boot_readiness=_real_readiness`; after
   `_define_and_start` in `provision`:

```python
            if self._first_boot_readiness is not None:
                self._await_first_boot(self._first_boot_readiness, system_id, accel)
```

```python
    @staticmethod
    def _await_first_boot(readiness: Readiness, system_id: UUID, accel: str) -> None:
        """Wait for the baseline first boot's readiness marker (ADR-0680)."""
        polls = math.ceil(boot_window_polls() * tcg_deadline_multiplier(accel))
        outcome = poll_readiness(readiness, system_id, polls)
        result = outcome.result
        if result is not None and result.ok:
            return
        crash = None if result is None else result.crash_signature
        details = readiness_failure_details(system_id, outcome.first_probe_error, crash)
        details["first_boot"] = "timeout" if result is None else "not_ready"
        raise CategorizedError(
            "the guest's first boot did not emit its readiness marker"
            if result is None
            else "the guest's first boot failed before its readiness marker",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details=details,
        )
```

   Update the `provision` and `reprovision` docstrings' Raises sections.
4. Focused green; `just lint && just type && just test-changed`. Commit
   `feat(local-libvirt): gate provision ready on first-boot readiness`.

## Task 4 — agent-facing text

Files: `src/kdive/mcp/tools/lifecycle/systems/registrar.py`, regenerated
`docs/guide/reference/systems.md`.

Verification: Mode: focused-test — the repo's docs generator check (`just docs-check`); red
before regeneration, green after `just docs` and `just resources-docs`.

Steps: add to the `systems.provision` wrapper docstring, after its first paragraph:
"On local-libvirt the job succeeds, and the System reaches `ready`, only after the guest's first
boot writes its readiness marker to the console — minutes on KVM, longer on an emulated arch. A
guest that crashes or never writes it ends `failed` with `provisioning_failure`." Add the same
sentence, reworded for reprovision, to `systems.reprovision`. Regenerate with `just docs` and `just resources-docs`; run `just docs-check`, `just resources-docs-check`
and `just mcp-spec-check`; if a tool-schema snapshot or the runner-task fixture changes, regenerate
it with its recipe, never by hand. Commit `docs(systems): state provision waits for first boot`.

## Task 5 — live proof

File: `tests/integration/test_first_boot_host_keys_live.py`.

Verification: Mode: task-test-not-applicable for CI — `live_vm`-gated, run by hand on a KVM host
with the stack (`examples/local-libvirt/demo-up.sh`); result recorded in the PR.

Steps: add `test_provision_ready_implies_first_boot_marker`: allocate, `systems.provision`,
`await_system_state(..., "ready")`, then immediately read
`read_console_log(console_log_path(UUID(system_id)))` and assert `b"kdive-ready"` is in it, then
reuse `_assert_host_keys`. Print the provision duration and `cloud-init status` over the same SSH
path. Release in `finally`. `just lint && just type`; commit
`test(live): prove provision ready follows the first-boot marker`.

## Rollback

Each task is one commit; `git revert` of Task 3 alone restores the old contract while keeping
the retry fix.
