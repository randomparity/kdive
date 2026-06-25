"""`DiagnosticsService` aggregation tests (ADR-0091 §2, §1 core-up boundary).

The service runs the assembled checks (each bounded by the per-check timeout) and
aggregates them into one report. A down dependency (a worker that cannot pick up the
worker-vantage job) surfaces as an `error` pointing at the health endpoints — **not** a
contract `fail`, and never a hang.
"""

from __future__ import annotations

import asyncio

import pytest

from kdive.db.build_hosts import WORKER_LOCAL_ID
from kdive.diagnostics.checks import Check, CheckResult, CheckStatus, Vantage
from kdive.diagnostics.service import (
    FEATURE_NOT_ENABLED_DETAIL,
    WORKER_UNAVAILABLE_DETAIL,
    DiagnosticsService,
    WorkerVantageCheck,
    WorkerVantageDispatchMode,
    WorkerVantageSubstitution,
    WorkerVantageSubstitutionMode,
    worker_unavailable_results,
)


class _Fixed(Check):
    def __init__(self, result: CheckResult, vantage: Vantage = Vantage.SERVER) -> None:
        self._result = result
        self._vantage = vantage

    @property
    def id(self) -> str:
        return self._result.check_id

    @property
    def vantage(self) -> Vantage:
        return self._vantage

    async def run(self) -> CheckResult:
        return self._result


def _ok(check_id: str) -> CheckResult:
    return CheckResult(check_id=check_id, status=CheckStatus.PASS, detail="ok")


def test_service_runs_every_check_and_collects_results() -> None:
    service = DiagnosticsService(checks=[_Fixed(_ok("a")), _Fixed(_ok("b"))], per_check_timeout=1.0)
    report = asyncio.run(service.run())
    assert {r.check_id for r in report.results} == {"a", "b"}
    assert all(r.status is CheckStatus.PASS for r in report.results)


def test_service_has_failed_when_any_check_fails() -> None:
    fail = CheckResult(check_id="c", status=CheckStatus.FAIL, detail="broke", fix="do it")
    service = DiagnosticsService(checks=[_Fixed(_ok("a")), _Fixed(fail)], per_check_timeout=1.0)
    report = asyncio.run(service.run())
    assert report.has_failure is True
    assert report.has_error is False


def test_service_error_does_not_count_as_failure() -> None:
    err = CheckResult(check_id="c", status=CheckStatus.ERROR, detail="provider down")
    service = DiagnosticsService(checks=[_Fixed(_ok("a")), _Fixed(err)], per_check_timeout=1.0)
    report = asyncio.run(service.run())
    assert report.has_failure is False
    assert report.has_error is True


class _Slow(_Fixed):
    def __init__(self, result: CheckResult, *, delay: float) -> None:
        super().__init__(result)
        self._delay = delay

    async def run(self) -> CheckResult:
        await asyncio.sleep(self._delay)
        return self._result


def test_overall_deadline_reports_unrun_checks_as_error() -> None:
    service = DiagnosticsService(
        checks=[_Slow(_ok("a"), delay=0.05), _Fixed(_ok("b"))],
        per_check_timeout=1.0,
        overall_timeout=0.01,
    )
    report = asyncio.run(service.run())
    by_id = {r.check_id: r for r in report.results}
    assert by_id["b"].status is CheckStatus.ERROR
    assert by_id["b"].fix is None
    assert "deadline" in by_id["b"].detail
    assert report.has_failure is False


def test_overall_deadline_unset_runs_every_check() -> None:
    service = DiagnosticsService(
        checks=[_Fixed(_ok("a")), _Fixed(_ok("b"))],
        per_check_timeout=1.0,
        overall_timeout=None,
    )
    report = asyncio.run(service.run())
    assert all(r.status is CheckStatus.PASS for r in report.results)


def test_checks_run_while_overall_budget_is_positive() -> None:
    # A check with budget still remaining (sub-second, but > 0) must RUN, not be reported as
    # deadline-exceeded. Guards the exhaustion boundary against widening to "<= 1s remaining".
    service = DiagnosticsService(
        checks=[_Fixed(_ok("a")), _Fixed(_ok("b"))],
        per_check_timeout=1.0,
        overall_timeout=0.5,
    )
    report = asyncio.run(service.run())
    by_id = {r.check_id: r for r in report.results}
    assert by_id["a"].status is CheckStatus.PASS
    assert by_id["b"].status is CheckStatus.PASS
    assert all("deadline" not in r.detail for r in report.results)


def test_per_check_timeout_bounds_a_slow_check_even_without_overall_deadline() -> None:
    # The per-check timeout must be threaded into run_check: a check slower than it is reported
    # `error` (timed out), not run to completion. overall_timeout=None isolates the per-check
    # bound as the only cap, so a check that ignores it would (wrongly) pass.
    service = DiagnosticsService(
        checks=[_Slow(_ok("slow"), delay=1.0)],
        per_check_timeout=0.01,
        overall_timeout=None,
    )
    report = asyncio.run(service.run())
    result = report.results[0]
    assert result.check_id == "slow"
    assert result.status is CheckStatus.ERROR
    assert "did not respond" in result.detail


def test_worker_unavailable_yields_error_pointing_at_health() -> None:
    worker_checks = [
        _Fixed(_ok("provider_tls"), Vantage.WORKER),
        _Fixed(_ok("gdbstub_acl"), Vantage.WORKER),
    ]
    results = worker_unavailable_results(worker_checks)
    assert [r.status for r in results] == [CheckStatus.ERROR, CheckStatus.ERROR]
    assert all(r.fix is None for r in results)
    assert all(WORKER_UNAVAILABLE_DETAIL in r.detail for r in results)
    assert all(r.failure_category == "transport_failure" for r in results)


def test_worker_unavailable_results_default_reason_is_worker_down() -> None:
    # A bare call (no reason) keeps the historical /livez-/readyz worker-down meaning.
    results = worker_unavailable_results([_Fixed(_ok("provider_tls"), Vantage.WORKER)])
    assert WORKER_UNAVAILABLE_DETAIL in results[0].detail
    assert FEATURE_NOT_ENABLED_DETAIL not in results[0].detail


def test_feature_not_enabled_substitution_does_not_point_at_health() -> None:
    results = worker_unavailable_results(
        [_Fixed(_ok("provider_tls"), Vantage.WORKER)],
        reason=WorkerVantageSubstitution.FEATURE_NOT_ENABLED,
    )
    assert results[0].status is CheckStatus.ERROR
    assert results[0].fix is None
    assert FEATURE_NOT_ENABLED_DETAIL in results[0].detail
    assert "/livez" not in results[0].detail
    assert "/readyz" not in results[0].detail
    assert results[0].failure_category == "not_implemented"


def test_feature_not_enabled_and_worker_down_are_category_distinguishable() -> None:
    enabled = worker_unavailable_results(
        [_Fixed(_ok("a"), Vantage.WORKER)],
        reason=WorkerVantageSubstitution.FEATURE_NOT_ENABLED,
    )[0]
    down = worker_unavailable_results(
        [_Fixed(_ok("a"), Vantage.WORKER)],
        reason=WorkerVantageSubstitution.WORKER_UNAVAILABLE,
    )[0]
    assert enabled.failure_category != down.failure_category


def test_unavailable_worker_metadata_yields_error_without_runnable_check() -> None:
    service = DiagnosticsService(
        checks=[],
        per_check_timeout=1.0,
        worker_mode=WorkerVantageSubstitutionMode(
            WorkerVantageSubstitution.FEATURE_NOT_ENABLED,
            [WorkerVantageCheck(id="provider_tls", provider="remote-libvirt")],
        ),
    )
    report = asyncio.run(service.run())
    result = report.results[0]
    assert result.check_id == "provider_tls"
    assert result.provider == "remote-libvirt"
    assert result.status is CheckStatus.ERROR
    assert FEATURE_NOT_ENABLED_DETAIL in result.detail
    assert result.failure_category == "not_implemented"


def test_service_substitutes_worker_results_when_worker_down() -> None:
    service = DiagnosticsService(
        checks=[_Fixed(_ok("a"), Vantage.WORKER)],
        per_check_timeout=1.0,
        worker_mode=WorkerVantageSubstitutionMode(WorkerVantageSubstitution.WORKER_UNAVAILABLE),
    )
    report = asyncio.run(service.run())
    assert report.results[0].status is CheckStatus.ERROR
    assert report.has_error is True
    assert WORKER_UNAVAILABLE_DETAIL in report.results[0].detail


def test_service_substitution_reason_threads_into_results() -> None:
    service = DiagnosticsService(
        checks=[_Fixed(_ok("a"), Vantage.WORKER)],
        per_check_timeout=1.0,
        worker_mode=WorkerVantageSubstitutionMode(WorkerVantageSubstitution.FEATURE_NOT_ENABLED),
    )
    report = asyncio.run(service.run())
    assert report.results[0].status is CheckStatus.ERROR
    assert FEATURE_NOT_ENABLED_DETAIL in report.results[0].detail
    assert report.results[0].failure_category == "not_implemented"


class _FakeDispatcher:
    """A worker-check dispatcher stub returning a fixed result list (ADR-0164)."""

    def __init__(self, results: list[CheckResult]) -> None:
        self._results = results

    async def run_worker_checks(self) -> list[CheckResult]:
        return self._results


def test_dispatcher_results_replace_substitution() -> None:
    from kdive.diagnostics.checks import PROVIDER_TLS_ID

    dispatcher = _FakeDispatcher(
        [CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt")]
    )
    service = DiagnosticsService(
        checks=[],
        per_check_timeout=1.0,
        worker_mode=WorkerVantageDispatchMode(dispatcher),
    )
    report = asyncio.run(service.run())
    assert [r.check_id for r in report.results] == [PROVIDER_TLS_ID]
    assert not report.has_error


def test_server_and_real_worker_results_compose_into_one_verdict() -> None:
    # Composition: a server-vantage check + a JobWorkerCheckDispatcher whose job SUCCEEDS with
    # serialized real results -> one verdict carrying both, no substitution (AC 1 of #514).
    from kdive.diagnostics.checks import GDBSTUB_ACL_ID, PROVIDER_TLS_ID, SECRET_REF_ID
    from kdive.diagnostics.result_codec import serialize_results
    from kdive.diagnostics.worker_dispatch import JobWorkerCheckDispatcher
    from kdive.domain.capacity.state import JobState

    class _Job:
        def __init__(self, state: JobState, result_ref: str | None) -> None:
            self.id = "j"
            self.state = state
            self.result_ref = result_ref
            self.error_category = None

    serialized = serialize_results(
        [
            CheckResult(PROVIDER_TLS_ID, CheckStatus.PASS, "ok", provider="remote-libvirt"),
            CheckResult(
                GDBSTUB_ACL_ID,
                CheckStatus.FAIL,
                "blocked",
                fix="open the ACL",
                provider="remote-libvirt",
                failure_category="configuration_error",
            ),
        ]
    )

    async def _enqueue(dedup_key: str, payload: object, authorizing: object) -> _Job:
        return _Job(JobState.QUEUED, None)

    async def _get(dedup_key: str) -> _Job:
        return _Job(JobState.SUCCEEDED, serialized)

    dispatcher = JobWorkerCheckDispatcher(
        pool=None,
        provider="remote-libvirt",
        worker_check_ids=(PROVIDER_TLS_ID, GDBSTUB_ACL_ID),
        enqueue_fn=_enqueue,  # ty: ignore[invalid-argument-type]
        get_fn=_get,  # ty: ignore[invalid-argument-type]
        clock=lambda: 0.0,
        dedup_suffix="x",
    )
    service = DiagnosticsService(
        checks=[_Fixed(_ok(SECRET_REF_ID))],
        per_check_timeout=1.0,
        worker_mode=WorkerVantageDispatchMode(dispatcher),
    )
    report = asyncio.run(service.run())
    by_id = {r.check_id: r for r in report.results}
    assert set(by_id) == {SECRET_REF_ID, PROVIDER_TLS_ID, GDBSTUB_ACL_ID}
    assert by_id[GDBSTUB_ACL_ID].status is CheckStatus.FAIL  # real result, not a substitution
    assert report.has_failure


# ---- default_service_factory opt-in flags + fail-fast messages -----------------------


def _load_minimal_config(monkeypatch, tmp_path) -> None:
    import kdive.config as config

    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(tmp_path))
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(tmp_path))
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "absent.toml"))
    config.load()


def test_factory_defaults_do_not_opt_into_egress_or_buildhost_agent(monkeypatch, tmp_path) -> None:
    # The opt-in flags default OFF: a bare factory call builds a service rather than raising the
    # egress fail-fast (with_egress defaulting True) or the no-pool buildhost-agent fail-fast
    # (with_buildhost_agent defaulting True, which needs a pool).
    from kdive.diagnostics.service import _DEFAULT_PER_CHECK_TIMEOUT, default_service_factory

    _load_minimal_config(monkeypatch, tmp_path)
    service = default_service_factory(None)
    assert isinstance(service, DiagnosticsService)
    # The default (no buildhost-agent) keeps the tight per-check timeout, not the generous 600s
    # buildhost cap. Pin the exact constant so any mutation of the default value is caught.
    assert service._timeout == _DEFAULT_PER_CHECK_TIMEOUT  # noqa: SLF001
    assert _DEFAULT_PER_CHECK_TIMEOUT == 10.0


def test_factory_with_egress_fail_fast_names_the_egress_flag(monkeypatch, tmp_path) -> None:
    from kdive.diagnostics.service import default_service_factory
    from kdive.domain.errors import CategorizedError, ErrorCategory

    _load_minimal_config(monkeypatch, tmp_path)
    with pytest.raises(CategorizedError) as exc:
        default_service_factory(None, with_egress=True)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value).startswith("guest_egress (--with-egress)")


def test_factory_buildhost_agent_without_pool_fail_fast_names_the_flag(
    monkeypatch, tmp_path
) -> None:
    from kdive.diagnostics.service import default_service_factory
    from kdive.domain.errors import CategorizedError, ErrorCategory

    _load_minimal_config(monkeypatch, tmp_path)
    with pytest.raises(CategorizedError) as exc:
        default_service_factory(None, with_buildhost_agent=True)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value).startswith("ephemeral_libvirt_buildhost_agent (--with-buildhost-agent)")


class _StubCursor:
    """An async cursor stub for ``SELECT * FROM build_hosts WHERE id = %s``.

    Honors the queried id: the seeded row comes back only when the lookup uses ``WORKER_LOCAL_ID``,
    so a probe that passes the wrong host id (or ``None``) reads ``None`` and fails open to enabled.
    """

    def __init__(self, row: dict[str, object]) -> None:
        self._row = row
        self._matched = False

    async def __aenter__(self) -> _StubCursor:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, _sql: str, params: object = None) -> None:
        queried_id = params[0] if isinstance(params, (tuple, list)) and params else params
        self._matched = queried_id == self._row["id"]

    async def fetchone(self) -> dict[str, object] | None:
        return self._row if self._matched else None


class _StubConn:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def cursor(self, *_args: object, **_kwargs: object) -> _StubCursor:
        return _StubCursor(self._row)


class _StubConnCtx:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    async def __aenter__(self) -> _StubConn:
        return _StubConn(self._row)

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _StubPool:
    """A minimal AsyncConnectionPool stand-in whose connection yields one seeded row.

    Unlike ``cast(AsyncConnectionPool, object())`` — which raises inside ``pool.connection()`` and
    makes ``local_host_enabled_probe`` fail OPEN to enabled (indistinguishable from the default
    always-enabled probe) — this exercises the real DB-read path, so the wired ``enabled`` flag is
    observable in the verdict.
    """

    def __init__(self, *, enabled: bool) -> None:
        self._row: dict[str, object] = {
            "id": WORKER_LOCAL_ID,
            "name": "worker-local",
            "kind": "local",
            "address": None,
            "ssh_credential_ref": None,
            "base_image_volume": None,
            "workspace_root": "/srv/kdive",
            "max_concurrent": 1,
            "toolchain_desc": None,
            "enabled": enabled,
            "state": "ready",
        }

    def connection(self) -> _StubConnCtx:
        return _StubConnCtx(self._row)


def test_build_host_check_with_pool_runs_its_warm_tree_and_enabled_probes(
    monkeypatch, tmp_path
) -> None:
    # With a pool, _build_host_checks wires BOTH a warm-tree source probe and an enabled probe into
    # the assembled local_kernel_src check. KDIVE_KERNEL_SRC points at an existing tree (USABLE),
    # and the seeded worker-local host reads back ENABLED, so the warm-tree verdict is surfaced.
    from typing import cast

    from psycopg_pool import AsyncConnectionPool

    from kdive.diagnostics.checks import LOCAL_KERNEL_SRC_ID, run_check
    from kdive.diagnostics.service import _build_host_checks

    _load_minimal_config(monkeypatch, tmp_path)
    checks = _build_host_checks(cast(AsyncConnectionPool, _StubPool(enabled=True)))
    assert [c.id for c in checks] == [LOCAL_KERNEL_SRC_ID]
    result = asyncio.run(run_check(checks[0], timeout=5.0))
    assert result.status is CheckStatus.PASS
    # The ENABLED host means the warm-tree probe is what decides the verdict, not the suppression.
    assert "KDIVE_KERNEL_SRC points at an existing absolute tree" in result.detail


def test_build_host_check_without_pool_runs_the_warm_tree_probe(monkeypatch, tmp_path) -> None:
    # Without a pool, _build_host_checks still assembles the local_kernel_src check with a real
    # warm-tree probe (and the always-enabled default). KDIVE_KERNEL_SRC points at an existing tree,
    # so it PASSes on the warm-tree verdict. A dropped/None warm-tree probe would raise at run time.
    from kdive.diagnostics.checks import LOCAL_KERNEL_SRC_ID, run_check
    from kdive.diagnostics.service import _build_host_checks

    _load_minimal_config(monkeypatch, tmp_path)
    checks = _build_host_checks(None)
    assert [c.id for c in checks] == [LOCAL_KERNEL_SRC_ID]
    result = asyncio.run(run_check(checks[0], timeout=5.0))
    assert result.status is CheckStatus.PASS
    assert "KDIVE_KERNEL_SRC points at an existing absolute tree" in result.detail


def test_build_host_check_enabled_probe_suppresses_when_seeded_host_disabled(
    monkeypatch, tmp_path
) -> None:
    # The enabled_probe wiring is observable: a DISABLED seeded worker-local host makes the check
    # suppress its warm-tree verdict and return the n/a PASS instead. Dropping the
    # enabled_probe=... wiring (or passing pool=None) falls back to the always-enabled default,
    # which would surface the warm-tree USABLE detail here instead — killing that mutant.
    from typing import cast

    from psycopg_pool import AsyncConnectionPool

    from kdive.diagnostics.checks import run_check
    from kdive.diagnostics.service import _build_host_checks

    _load_minimal_config(monkeypatch, tmp_path)
    checks = _build_host_checks(cast(AsyncConnectionPool, _StubPool(enabled=False)))
    result = asyncio.run(run_check(checks[0], timeout=5.0))
    assert result.status is CheckStatus.PASS
    assert "the seeded local build host is disabled" in result.detail
    assert "KDIVE_KERNEL_SRC points at an existing absolute tree" not in result.detail


_REMOTE_SYSTEMS = """\
schema_version = 2

[[image]]
provider = "remote-libvirt"
name = "fedora-kdive-remote-base-43"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "fedora-kdive-remote-base-43.qcow2"

[[remote_libvirt]]
name = "ub24-big"
uri = "qemu+tls://host.example/system"
gdb_addr = "192.168.10.20"
gdbstub_range = "47000:47099"
client_cert_ref = "remote/clientcert.pem"
client_key_ref = "remote/clientkey.pem"  # pragma: allowlist secret
ca_cert_ref = "remote/cacert.pem"
base_image = "fedora-kdive-remote-base-43"
cost_class = "remote"
vcpus = 16
memory_mb = 65536
"""


def _load_remote_config(monkeypatch, tmp_path) -> None:
    import kdive.config as config

    path = tmp_path / "systems.toml"
    path.write_text(_REMOTE_SYSTEMS)
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(tmp_path))
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(tmp_path))
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()


def test_factory_dispatcher_carries_pool_provider_and_worker_check_ids(
    monkeypatch, tmp_path
) -> None:
    # With a pool AND remote-libvirt configured, worker-vantage outcomes are delegated to a
    # JobWorkerCheckDispatcher built with the supplied pool, the contribution's provider, and
    # the unavailable worker-check ids (provider_tls + gdbstub_acl) — ADR-0164. A misrouted pool,
    # provider, or id tuple would leave the dispatcher unable to enqueue against the right job.
    from typing import cast

    from psycopg_pool import AsyncConnectionPool

    from kdive.diagnostics.checks import GDBSTUB_ACL_ID, PROVIDER_TLS_ID
    from kdive.diagnostics.service import default_service_factory
    from kdive.diagnostics.worker_dispatch import JobWorkerCheckDispatcher
    from kdive.providers.assembly.diagnostics import diagnostic_provider_contributions

    _load_remote_config(monkeypatch, tmp_path)
    pool = cast(AsyncConnectionPool, object())
    service = default_service_factory(
        None, pool=pool, provider_contributions=diagnostic_provider_contributions()
    )
    assert isinstance(service._worker_mode, WorkerVantageDispatchMode)  # noqa: SLF001
    # The dispatcher field is typed as the WorkerCheckDispatcher Protocol; narrow to the
    # concrete impl to inspect its wired pool/provider/check-id internals.
    dispatcher = cast(JobWorkerCheckDispatcher, service._worker_mode.dispatcher)  # noqa: SLF001
    assert dispatcher._pool is pool  # noqa: SLF001
    assert dispatcher._provider == "remote-libvirt"  # noqa: SLF001
    assert set(dispatcher._worker_check_ids) == {PROVIDER_TLS_ID, GDBSTUB_ACL_ID}  # noqa: SLF001
