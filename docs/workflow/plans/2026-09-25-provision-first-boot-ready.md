# Provision first-boot readiness gate — implementation plan

Goal: local-libvirt `provision`/`reprovision` return (and the System reaches `ready`) only after
the guest's first boot emits `kdive-ready`; a guest that does not fails with
`PROVISIONING_FAILURE` and leaves no domain.

Architecture: a shared poll loop in `boot/readiness.py` serves both `runs.boot` and provisioning.
`LocalLibvirtProvisioning` gains an injected `first_boot_readiness` seam, wired only by
`from_env`. `_define_and_start` checks whether the domain is already active before the ADR-0576
truncate and skips truncate and `create()` when it is. Spec:
[provision-first-boot-ready](../specs/2026-09-25-provision-first-boot-ready-design.md); decision:
[ADR-0680](../../adr/0680-local-libvirt-provision-ready-after-first-boot.md).

Tech stack: Python 3.14, libvirt-python, pytest, `uv`, `just`.

Expected implementation size: 500–650 changed lines (L) — ~80 lines of readiness refactor and
import renames, ~100 lines of provisioning source, ~250–350 lines of unit tests, ~50 lines of
docstring, help text and regenerated references, ~60 lines of live test.

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
| `src/kdive/providers/local_libvirt/lifecycle/install.py` | booter/installer | uses the shared loop and details; its `_boot_window_polls`, `Readiness`, `_boot_failure_details` are deleted |
| `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` | define/start, teardown | active check, domain teardown on failure, first-boot wait seam |
| `src/kdive/providers/local_libvirt/settings.py` | settings | `LIBVIRT_BOOT_WINDOW_S` help clause |
| `src/kdive/mcp/tools/lifecycle/systems/registrar.py` | tool wrappers | provision/reprovision docstring sentence |
| `docs/guide/reference/systems.md`, `docs/guide/reference/config.md` | generated references | regenerated |
| `tests/providers/local_libvirt/lifecycle/boot/test_readiness_poll.py` | — | new: shared loop tests |
| `tests/providers/local_libvirt/test_install.py` | booter tests | imports renamed |
| `tests/providers/local_libvirt/test_provisioning.py` | provisioning unit tests | fake `isActive`, new cases, two closed counts |
| `tests/integration/test_first_boot_host_keys_live.py` | #2757 live proof | + ready-implies-marker test |

No compatibility path is retained: the moved names have no production caller outside
`install.py`, so `test_install.py` imports them from `boot/readiness.py`.

## Task 1 — shared readiness poll loop

Files: modify `boot/readiness.py`, `lifecycle/install.py`, `tests/providers/local_libvirt/test_install.py`;
create `tests/providers/local_libvirt/lifecycle/boot/test_readiness_poll.py`.

Interfaces (later tasks rely on these exact names):

```python
type Readiness = Callable[[UUID], ReadinessResult]
def boot_window_polls() -> int: ...
class ReadinessOutcome(NamedTuple):
    result: ReadinessResult | None
    first_probe_error: ProbeFailure | None
def poll_readiness(
    readiness: Readiness,
    system_id: UUID,
    polls: int,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ReadinessOutcome: ...
def readiness_failure_details(
    system_id: UUID, first_probe_error: ProbeFailure | None, crash_signature: str | None = None
) -> dict[str, object]: ...
```

Verification:

- Contract `poll_readiness` returns the first answer, the first probe error, `None` on
  exhaustion, and stops at the deadline. Mode: focused-test — `test_readiness_poll.py`; red:
  `ImportError` for `poll_readiness`; green:
  `just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_readiness_poll.py`.
- Contract `runs.boot` categories and details unchanged. Mode: focused-test — existing
  `tests/providers/local_libvirt/test_install.py` with only its imports of `_boot_window_polls`
  and `LocalLibvirtBooter._boot_failure_details` renamed to `boot_window_polls` and
  `readiness_failure_details`; green: `just test-verbose tests/providers/local_libvirt/test_install.py`.

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


def test_deadline_stops_before_the_poll_count() -> None:
    now = [0.0]

    def probe(_system_id: UUID) -> ReadinessResult:
        now[0] += 15.0  # a hung virsh probe plus the poll sleep
        return ReadinessResult(False, False)

    outcome = poll_readiness(probe, _SYS, 100, deadline=45.0, clock=lambda: now[0])
    assert outcome.result is None
    assert now[0] == 45.0
```

2. Run it; expect `ImportError: cannot import name 'poll_readiness'`.
3. In `readiness.py` add the interfaces. `boot_window_polls` is
   `math.ceil(config.require(LIBVIRT_BOOT_WINDOW_S) / _POLL_INTERVAL_SECONDS)` and carries the
   comment block now above `install._boot_window_polls`. `poll_readiness`:

```python
def poll_readiness(
    readiness: Readiness,
    system_id: UUID,
    polls: int,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ReadinessOutcome:
    """Poll ``readiness`` until it answers, ``polls`` run out, or ``deadline`` passes."""
    first_probe_error: ProbeFailure | None = None
    for _ in range(polls):
        if deadline is not None and clock() >= deadline:
            break
        result = readiness(system_id)
        if first_probe_error is None and result.probe_error is not None:
            first_probe_error = result.probe_error
        if result.answered:
            return ReadinessOutcome(result, first_probe_error)
    return ReadinessOutcome(None, first_probe_error)
```

   `readiness_failure_details` is the body of `LocalLibvirtBooter._boot_failure_details`
   (import `is_crash_signature` from `kdive.domain.lifecycle.crash_signatures`).
4. In `install.py`: delete `_boot_window_polls`, `type Readiness`, and `_boot_failure_details`;
   import `boot_window_polls`, `Readiness`, `poll_readiness`, `readiness_failure_details`;
   `from_env` passes `boot_window_polls=boot_window_polls()`. Replace `_await_ready` with:

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

5. In `test_install.py` import `boot_window_polls` and `readiness_failure_details` from
   `kdive.providers.local_libvirt.lifecycle.boot.readiness` and replace the old names.
6. Run both focused commands; expect all pass. `just lint && just type`. Commit
   `refactor(local-libvirt): share the readiness poll loop`.

## Task 2 — skip truncate for a running domain; failure removes the domain

Files: modify `lifecycle/provisioning.py`, `tests/providers/local_libvirt/test_provisioning.py`.

Interfaces: `_LibvirtDomain.isActive() -> int`; `_define_and_start(xml, system_id) -> None`
(unchanged signature); new `_best_effort_teardown_domain(domain_name: str) -> None`.

Verification (Mode: focused-test for each; green:
`just test-verbose tests/providers/local_libvirt/test_provisioning.py`):

- An active domain is redefined but gets no truncate and no `create()`
  (`test_provision_active_domain_skips_truncate_and_create`; red: truncate recorded).
- A fresh provision keeps `prepare` before `define` (existing
  `test_provision_prepares_console_log_before_define`, unchanged, stays green).
- A failure after define/start destroys and undefines the domain
  (`test_provision_start_failure_path_tears_domain_down`; red: `undefine_flags` is `None`), and a
  teardown fault does not replace the error (`test_provision_teardown_fault_keeps_original_error`).
- Existing `test_provision_real_create_failure_undefines_domain` and
  `test_provision_failure_still_closes_connection` change `conn.closed == 3` to `== 4` with the
  comment `# + the ADR-0680 domain-teardown connection`.

Steps:

1. In `_ProvDomain` add `active: bool = False` and
   `def isActive(self) -> int: return int(self.active or self.created)`.
2. Write the tests:

```python
_NAME = "kdive-11111111-1111-1111-1111-111111111111"


def test_provision_active_domain_skips_truncate_and_create() -> None:
    conn = _ProvConn()
    conn.defined[_NAME] = _ProvDomain(_NAME, active=True)
    truncated: list[Path] = []
    prov = _prov(conn, prepare_console_log=truncated.append)
    assert prov.provision(_SYS, _profile()) == _NAME
    assert truncated == []
    assert conn.defined[_NAME].created is False
    assert len(conn.recorded_xml) == 1


def test_provision_start_failure_path_tears_domain_down() -> None:
    conn = _ProvConn()
    conn.defined[_NAME] = _ProvDomain(_NAME, create_error=libvirt.VIR_ERR_INTERNAL_ERROR)
    with pytest.raises(CategorizedError):
        _prov(conn).provision(_SYS, _profile())
    assert conn.defined[_NAME].undefine_flags is not None  # teardown's undefineFlags ran


def test_provision_teardown_fault_keeps_original_error() -> None:
    conn = _ProvConn()
    conn.defined[_NAME] = _ProvDomain(
        _NAME,
        create_error=libvirt.VIR_ERR_INTERNAL_ERROR,
        undefine_error=libvirt.VIR_ERR_INTERNAL_ERROR,
    )
    with pytest.raises(CategorizedError) as caught:
        _prov(conn).provision(_SYS, _profile())
    assert caught.value.category is ErrorCategory.PROVISIONING_FAILURE
```

   `_prov` gains `prepare_console_log: Callable[[Path], None] = lambda _path: None`, passed to
   `ProvisioningFiles`.
3. Run; expect the active-domain test and the teardown test to fail.
4. Implement in `provisioning.py`: add `isActive` to `_LibvirtDomain`; remove
   `self._files.prepare_console(system_id)` from `provision`; initialise `started = False` beside
   the other flags and set `started = True` on the line before
   `self._define_and_start(xml, system_id)`; in the `except CategorizedError` arm call
   `self._best_effort_teardown_domain(domain_name_for(system_id))` first when `started`.
   `_define_and_start` body inside its `try`:

```python
            if self._domain_active(conn, domain_name_for(system_id)):
                # A retry after a lease reclaim: an earlier attempt of this provision truncated
                # the console and started this boot, so its log holds the whole boot (ADR-0680).
                conn.defineXML(xml)
                _log.info("domain for System %s is already running; waiting on it", system_id)
                return
            self._files.prepare_console(system_id)  # ADR-0576: truncate before define+create
            domain = conn.defineXML(xml)
            try:
                domain.create()
            ...unchanged...
```

   with

```python
    @staticmethod
    def _domain_active(conn: _LibvirtConn, name: str) -> bool:
        try:
            return bool(conn.lookupByName(name).isActive())
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return False
            raise
```

   (a raised `libvirtError` reaches `_define_and_start`'s existing
   `except libvirt.libvirtError` → `PROVISIONING_FAILURE`). `_best_effort_teardown_domain` wraps
   `self._teardown_domain(domain_name)` in `try/except CategorizedError`, logging a warning with
   `exc_info=True`, as `_best_effort_reclaim` does.
5. Update the two `closed` counts. Focused green; `just lint && just type`. Commit
   `fix(local-libvirt): skip console truncate for a running domain on retry`.

## Task 3 — the first-boot wait

Files: modify `lifecycle/provisioning.py`, `tests/providers/local_libvirt/test_provisioning.py`.

Interfaces: `LocalLibvirtProvisioning.__init__(..., first_boot_readiness: Readiness | None = None,
clock: Callable[[], float] = time.monotonic)`; private
`_await_first_boot(readiness: Readiness, system_id: UUID, accel: str) -> None`.

Verification (Mode: focused-test for each, in `test_provisioning.py`; red:
`TypeError: unexpected keyword argument 'first_boot_readiness'`; green:
`just test-verbose tests/providers/local_libvirt/test_provisioning.py`):

- ready → returns name (`test_first_boot_ready_returns_name`);
- pending then ready (`test_first_boot_waits_through_pending`);
- timeout → `PROVISIONING_FAILURE`, `first_boot == "timeout"`, domain undefined, overlay and
  baseline removed (`test_first_boot_timeout_fails_and_reclaims`);
- crash → `first_boot == "not_ready"`, `crash_signature` kept
  (`test_first_boot_crash_fails_not_ready`);
- exit → `first_boot == "not_ready"` (`test_first_boot_exit_fails_not_ready`);
- a probe raising `CategorizedError(INFRASTRUCTURE_FAILURE)` propagates that category and the
  domain is undefined (`test_first_boot_probe_error_propagates_and_tears_down`);
- the deadline ends the wait before the poll count (`test_first_boot_deadline_bounds_wall_clock`,
  injected clock advancing 15 s per probe, window 10 s → one probe);
- TCG accel multiplies the poll count (`test_first_boot_tcg_scales_polls`: monkeypatch
  `KDIVE_LIBVIRT_BOOT_WINDOW_S=10` and `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER=3`, set
  `conn.caps_xml` to capabilities advertising a `ppc64` guest with a `qemu` domain type so
  `_resolve_guest_arch` returns `tcg`, provision a `ppc64le` profile, expect 6 probe calls);
- active domain still waits (`test_first_boot_waits_on_running_domain`);
- reprovision ready, timeout and crash (`test_reprovision_first_boot_ready_returns_name`,
  `test_reprovision_first_boot_timeout_fails`, `test_reprovision_first_boot_crash_fails_not_ready`);
- `from_env` wires `_real_readiness` (`test_from_env_wires_real_first_boot_readiness`: asserts
  `prov._first_boot_readiness is _real_readiness`);
- no seam → no probe call (every existing test stays green).

Steps:

1. `_prov` gains `first_boot_readiness: Readiness | None = None` and
   `clock: Callable[[], float] = time.monotonic`, passed through. Write the tests with a scripted
   probe (`ReadinessResult(False, False)` pending, `(True, True)` ready,
   `(True, False, crash_signature="Kernel panic")` crash, `(True, False)` exit) and
   `monkeypatch.setenv("KDIVE_LIBVIRT_BOOT_WINDOW_S", "10")` (two polls). Copy the ppc64
   capabilities fixture from the existing `_resolve_guest_arch` tests in the same file.
2. Run; expect the `TypeError`.
3. Implement: store both seams; `from_env` passes `first_boot_readiness=_real_readiness`; after
   `_define_and_start` in `provision`:

```python
            if self._first_boot_readiness is not None:
                self._await_first_boot(self._first_boot_readiness, system_id, accel)
```

```python
    def _await_first_boot(self, readiness: Readiness, system_id: UUID, accel: str) -> None:
        """Wait for the baseline first boot's readiness marker (ADR-0680)."""
        scale = tcg_deadline_multiplier(accel)
        polls = math.ceil(boot_window_polls() * scale)
        deadline = self._clock() + config.require(LIBVIRT_BOOT_WINDOW_S) * scale
        outcome = poll_readiness(readiness, system_id, polls, deadline=deadline, clock=self._clock)
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

## Task 4 — agent- and operator-facing text

Files: `src/kdive/mcp/tools/lifecycle/systems/registrar.py`,
`src/kdive/providers/local_libvirt/settings.py`, regenerated `docs/guide/reference/systems.md`
and `docs/guide/reference/config.md`.

Verification: Mode: focused-test — `just docs-check`, `just config-docs-check`, and
`just resources-docs-check`; red before regeneration, green after `just docs`,
`just config-docs`, and `just resources-docs`.

Steps: add to the `systems.provision` wrapper docstring, after its first paragraph: "On
local-libvirt the job succeeds, and the System reaches `ready`, only after the guest's first boot
writes its readiness marker to the console — minutes on KVM, longer on an emulated arch. A guest
that crashes or never writes it ends `failed` with `provisioning_failure`." Add the same sentence,
reworded for reprovision, to `systems.reprovision`. Add to the `LIBVIRT_BOOT_WINDOW_S` help: "It
also bounds the local-libvirt provision/reprovision first-boot wait (ADR-0680)." Regenerate; run
the three checks and `just mcp-spec-check`; if a tool-schema snapshot or the runner-task fixture
changes, regenerate it with its recipe, never by hand. Commit
`docs(systems): state provision waits for first boot`.

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
