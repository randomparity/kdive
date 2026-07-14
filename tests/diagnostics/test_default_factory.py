"""Default production service-factory assembly (ADR-0091 §2).

The default factory assembles the server-vantage `secret_ref` check from the configured
``secret=True`` settings, resolved against the file-ref backend under ``KDIVE_SECRETS_ROOT``.
A ref that does not resolve is a contract ``fail``; the backend root being absent entirely
is the check's ``error`` boundary, not a ``fail``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.diagnostics.checks import (
    BASE_IMAGE_STAGING_ID,
    GDBSTUB_ACL_ID,
    MULTIARCH_GDB_ID,
    PROVIDER_TLS_ID,
    REACHABILITY_ID,
    SECRET_REF_ID,
    Check,
    CheckResult,
    CheckStatus,
    Vantage,
)
from kdive.diagnostics.provider_checks import (
    BASE_VOLUME_NOT_STAGED_FIX,
    BaseImageStagingOutcome,
    ReachabilityOutcome,
    TlsProbe,
    TlsProbeOutcome,
)
from kdive.diagnostics.provider_contracts import (
    DiagnosticProviderContribution,
    WorkerVantageDescriptor,
)
from kdive.diagnostics.secret_ref import SecretRefCheck
from kdive.diagnostics.service import (
    FEATURE_NOT_ENABLED_DETAIL,
    WORKER_UNAVAILABLE_DETAIL,
    DiagnosticsService,
    WorkerVantageDispatchMode,
    WorkerVantageSubstitutionMode,
    default_service_factory,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig
from kdive.providers.remote_libvirt.diagnostics import base_image_staging, reachability
from kdive.providers.remote_libvirt.diagnostics import contribution as remote_contribution


class _FakeEgressCheck(Check):
    @property
    def id(self) -> str:
        return "guest_egress"

    @property
    def vantage(self) -> Vantage:
        return Vantage.WORKER

    async def run(self) -> CheckResult:
        return CheckResult("guest_egress", CheckStatus.PASS, "egress ok", provider="provider-a")


def _factory(
    provider: str | None,
    *,
    with_egress: bool = False,
    pool: AsyncConnectionPool | None = None,
) -> DiagnosticsService:
    from kdive.providers.assembly.diagnostics import diagnostic_provider_contributions

    return default_service_factory(
        provider,
        with_egress=with_egress,
        pool=pool,
        provider_contributions=diagnostic_provider_contributions(),
    )


_INSTANCE = """
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

_SECOND_INSTANCE = """
[[remote_libvirt]]
name = "ub24-small"
uri = "qemu+tls://host2.example/system"
gdb_addr = "192.168.10.21"
gdbstub_range = "47000:47099"
client_cert_ref = "remote/clientcert.pem"
client_key_ref = "remote/clientkey.pem"  # pragma: allowlist secret
ca_cert_ref = "remote/cacert.pem"
base_image = "fedora-kdive-remote-base-43"
cost_class = "remote"
vcpus = 8
memory_mb = 32768
"""

_IMAGE = """
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
"""


def _set_env(monkeypatch, root: Path, **refs: str) -> None:
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(root))
    for name, value in refs.items():
        monkeypatch.setenv(name, value)
    config.load()


def _with_remote_instance(monkeypatch, root: Path, *, instances: str = _INSTANCE) -> None:
    path = root / "systems.toml"
    path.write_text(f"schema_version = 2\n{_IMAGE}\n{instances}\n")
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(root))
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()


def _no_remote_instance(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(root))
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(root / "absent.toml"))
    config.load()


def _force_probe(monkeypatch, outcome: ReachabilityOutcome) -> None:
    async def _probe() -> ReachabilityOutcome:
        return outcome

    monkeypatch.setattr(reachability, "remote_libvirt_reachability_probe", lambda **_: _probe)


def _force_staging_probe(monkeypatch, outcome: BaseImageStagingOutcome) -> None:
    async def _probe() -> BaseImageStagingOutcome:
        return outcome

    monkeypatch.setattr(base_image_staging, "base_image_staging_probe", lambda **_: _probe)


def test_factory_builds_a_service_with_a_secret_ref_check(monkeypatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, tmp_path)
    service = _factory(None)
    ids = {c.id for c in service._checks}  # noqa: SLF001 - assert the assembled check set
    assert "secret_ref" in ids


def test_provider_diagnostics_registration_includes_local_and_remote_libvirt() -> None:
    from kdive.providers.assembly.diagnostics import diagnostic_provider_contributions

    contributions = diagnostic_provider_contributions()
    by_provider = {c.provider: c for c in contributions}
    assert {"local-libvirt", "remote-libvirt"} <= set(by_provider)
    # Remote contributes server-vantage checks; both declare worker-vantage checks (multiarch_gdb
    # for local, tls + gdbstub_acl for remote), so both carry unavailable-worker descriptors.
    assert by_provider["remote-libvirt"].checks
    assert by_provider["local-libvirt"].unavailable_worker_checks
    assert by_provider["remote-libvirt"].unavailable_worker_checks


def test_contribution_names_the_remote_libvirt_provider() -> None:
    # The contribution must carry the provider name so its rows attribute to remote-libvirt.
    assert remote_contribution.diagnostic_contribution().provider == "remote-libvirt"


def test_remote_worker_checks_build_runnable_tls_and_gdbstub_checks(
    monkeypatch, tmp_path: Path
) -> None:
    _with_remote_instance(monkeypatch, tmp_path)

    async def tls_probe(ca_path: str) -> TlsProbeOutcome:
        assert ca_path == "remote/cacert.pem"
        return TlsProbeOutcome.VALID

    async def acl_probe(host: str, port_range: str) -> bool | None:
        assert host == "192.168.10.20"
        assert port_range == "47000-47099"
        return True

    tls_configs: list[RemoteLibvirtConfig] = []

    def _capture_tls_probe(config: RemoteLibvirtConfig) -> TlsProbe:
        # The TLS probe must be bound to the host's real config (its uri), not None.
        tls_configs.append(config)
        return tls_probe

    monkeypatch.setattr(remote_contribution, "provider_tls_probe", _capture_tls_probe)
    monkeypatch.setattr(remote_contribution, "gdbstub_acl_probe", lambda: acl_probe)

    checks = remote_contribution.diagnostic_contribution().worker_checks()
    assert [c.uri for c in tls_configs] == ["qemu+tls://host.example/system"]
    assert {check.id for check in checks} == {PROVIDER_TLS_ID, GDBSTUB_ACL_ID}
    results = [asyncio.run(check.run()) for check in checks]
    by_id = {result.check_id: result for result in results}

    assert by_id[PROVIDER_TLS_ID].status is CheckStatus.PASS
    assert by_id[PROVIDER_TLS_ID].provider == "remote-libvirt"
    assert by_id[GDBSTUB_ACL_ID].status is CheckStatus.PASS
    assert by_id[GDBSTUB_ACL_ID].provider == "remote-libvirt"


def test_checks_fan_out_one_row_per_declared_instance(monkeypatch, tmp_path: Path) -> None:
    # ADR-0187: a doctor describes the fleet, so reachability + base-image-staging each emit one
    # row per declared [[remote_libvirt]] instance (two here → two of each).
    _with_remote_instance(monkeypatch, tmp_path, instances=_INSTANCE + _SECOND_INSTANCE)

    checks = remote_contribution.diagnostic_contribution().checks()
    ids = [check.id for check in checks]

    assert ids.count(REACHABILITY_ID) == 2
    assert ids.count(BASE_IMAGE_STAGING_ID) == 2


def test_fanned_out_checks_carry_each_instance_name_as_resource_id(
    monkeypatch, tmp_path: Path
) -> None:
    # ADR-0194: each per-host reachability + base-image-staging result names which host it probed,
    # so an operator can tell which of N declared hosts is staged. Force the probes so the checks
    # run to a verdict without a live libvirt connection.
    _with_remote_instance(monkeypatch, tmp_path, instances=_INSTANCE + _SECOND_INSTANCE)
    _force_probe(monkeypatch, ReachabilityOutcome.REACHABLE)
    _force_staging_probe(monkeypatch, BaseImageStagingOutcome.STAGED)

    checks = remote_contribution.diagnostic_contribution().checks()
    results = [asyncio.run(check.run()) for check in checks]

    by_id: dict[str, set[str | None]] = {}
    for result in results:
        by_id.setdefault(result.check_id, set()).add(result.resource_id)
    assert by_id[REACHABILITY_ID] == {"ub24-big", "ub24-small"}
    assert by_id[BASE_IMAGE_STAGING_ID] == {"ub24-big", "ub24-small"}

    # Every fanned-out reachability + base-image row attributes to the remote-libvirt provider.
    providers = {result.check_id: result.provider for result in results}
    assert providers[REACHABILITY_ID] == "remote-libvirt"
    assert providers[BASE_IMAGE_STAGING_ID] == "remote-libvirt"


def test_checks_resolve_each_host_by_its_own_name(monkeypatch, tmp_path: Path) -> None:
    # Each fanned-out check binds a config/volume factory that resolves *that host's* name.
    # Capture the factories the contribution hands the probe builders, then invoke them: a factory
    # wired to None, returning None, or resolving the wrong arg cannot produce both real configs.
    _with_remote_instance(monkeypatch, tmp_path, instances=_INSTANCE + _SECOND_INSTANCE)

    captured_config_factories: list = []
    captured_volume_factories: list = []

    def _capture_reach(*, config_factory, **_):
        captured_config_factories.append(config_factory)

        async def _probe() -> ReachabilityOutcome:
            return ReachabilityOutcome.REACHABLE

        return _probe

    def _capture_staging(*, config_factory, volume_factory, **_):
        captured_config_factories.append(config_factory)
        captured_volume_factories.append(volume_factory)

        async def _probe() -> BaseImageStagingOutcome:
            return BaseImageStagingOutcome.STAGED

        return _probe

    monkeypatch.setattr(reachability, "remote_libvirt_reachability_probe", _capture_reach)
    monkeypatch.setattr(base_image_staging, "base_image_staging_probe", _capture_staging)

    remote_contribution.diagnostic_contribution().checks()

    # Each captured config factory must resolve to a real declared host (both URIs present).
    resolved_uris = sorted(factory().uri for factory in captured_config_factories)
    assert resolved_uris == [
        "qemu+tls://host.example/system",
        "qemu+tls://host.example/system",
        "qemu+tls://host2.example/system",
        "qemu+tls://host2.example/system",
    ]
    # Each captured volume factory must resolve a non-empty staged-volume name per host.
    resolved_volumes = [factory() for factory in captured_volume_factories]
    assert len(resolved_volumes) == 2
    assert all(volume for volume in resolved_volumes)


def test_worker_checks_fan_out_one_row_per_declared_instance(monkeypatch, tmp_path: Path) -> None:
    _with_remote_instance(monkeypatch, tmp_path, instances=_INSTANCE + _SECOND_INSTANCE)

    async def tls_probe(ca_path: str) -> TlsProbeOutcome:
        return TlsProbeOutcome.VALID

    async def acl_probe(host: str, port_range: str) -> bool | None:
        return True

    monkeypatch.setattr(remote_contribution, "provider_tls_probe", lambda _config: tls_probe)
    monkeypatch.setattr(remote_contribution, "gdbstub_acl_probe", lambda: acl_probe)

    worker_checks = remote_contribution.diagnostic_contribution().worker_checks()
    worker_ids = [check.id for check in worker_checks]
    unavailable = remote_contribution.diagnostic_contribution().unavailable_worker_checks()
    unavailable_ids = [descriptor.id for descriptor in unavailable]

    assert worker_ids.count(PROVIDER_TLS_ID) == 2
    assert worker_ids.count(GDBSTUB_ACL_ID) == 2
    assert unavailable_ids.count(PROVIDER_TLS_ID) == 2
    assert unavailable_ids.count(GDBSTUB_ACL_ID) == 2


def test_secret_ref_passes_when_no_ref_is_required(monkeypatch, tmp_path: Path) -> None:
    # The default registry has no conditionally-required secret refs (the remote mTLS refs that
    # used to drive this moved to systems.toml, #395), so the assembled check resolves the empty
    # set and passes. The FAIL behavior is unit-covered in test_secret_ref.py with injected refs.
    _set_env(monkeypatch, tmp_path)
    check = next(c for c in _factory(None)._checks if isinstance(c, SecretRefCheck))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS


def test_with_egress_fails_fast_when_no_probe_image_is_wired(monkeypatch, tmp_path: Path) -> None:
    # The default provider contributions have no probe-guest seam, so opting into egress fails fast
    # rather than silently dropping the check.
    _set_env(monkeypatch, tmp_path)
    with pytest.raises(CategorizedError) as exc:
        _factory(None, with_egress=True)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_with_egress_assembles_provider_supplied_egress_check(monkeypatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, tmp_path)
    egress = _FakeEgressCheck()

    def _enabled() -> bool:
        return True

    def _no_checks() -> tuple[Check, ...]:
        return ()

    contribution = DiagnosticProviderContribution(
        provider="provider-a",
        enabled=_enabled,
        checks=_no_checks,
        unavailable_worker_checks=tuple,
        worker_checks=_no_checks,
        egress_checks=lambda: (egress,),
    )
    service = default_service_factory(
        None, with_egress=True, provider_contributions=(contribution,)
    )

    assert egress in service._checks  # noqa: SLF001


# ---- remote-libvirt reachability + TLS/ACL wiring (ADR-0125, #453) -------------------


def test_factory_includes_reachability_and_tls_acl_metadata_when_remote_configured(
    monkeypatch, tmp_path: Path
) -> None:
    _with_remote_instance(monkeypatch, tmp_path)
    service = _factory(None)
    runnable_ids = {c.id for c in service._checks}  # noqa: SLF001
    assert isinstance(service._worker_mode, WorkerVantageSubstitutionMode)  # noqa: SLF001
    unavailable_ids = {c.id for c in service._worker_mode.checks}  # noqa: SLF001
    assert {SECRET_REF_ID, REACHABILITY_ID, BASE_IMAGE_STAGING_ID} <= runnable_ids
    # local-libvirt's multiarch_gdb is also a worker-vantage check, so it joins the substituted
    # set with no pool wired.
    assert {PROVIDER_TLS_ID, GDBSTUB_ACL_ID, MULTIARCH_GDB_ID} == unavailable_ids
    assert PROVIDER_TLS_ID not in runnable_ids
    assert GDBSTUB_ACL_ID not in runnable_ids
    assert MULTIARCH_GDB_ID not in runnable_ids


def test_factory_service_is_worker_unavailable_when_remote_configured(
    monkeypatch, tmp_path: Path
) -> None:
    _with_remote_instance(monkeypatch, tmp_path)
    service = _factory(None)
    assert isinstance(service._worker_mode, WorkerVantageSubstitutionMode)  # noqa: SLF001


def test_factory_omits_remote_checks_when_not_configured(monkeypatch, tmp_path: Path) -> None:
    _no_remote_instance(monkeypatch, tmp_path)
    ids = {c.id for c in _factory(None)._checks}  # noqa: SLF001
    assert ids == {SECRET_REF_ID}


def test_run_substitutes_tls_acl_and_runs_reachability_and_secret_ref(
    monkeypatch, tmp_path: Path
) -> None:
    _with_remote_instance(monkeypatch, tmp_path)
    _force_probe(monkeypatch, ReachabilityOutcome.REACHABLE)
    report = asyncio.run(_factory(None).run())
    by_id = {r.check_id: r for r in report.results}

    # Worker-vantage checks are substituted with the honest feature-not-enabled error — the
    # dispatch is unwired in this deployment, NOT a worker outage, so the detail must not point
    # at /livez//readyz (which reads as a worker down). Never a contract fail (#484, ADR-0139).
    for worker_id in (PROVIDER_TLS_ID, GDBSTUB_ACL_ID):
        assert by_id[worker_id].status is CheckStatus.ERROR
        assert by_id[worker_id].fix is None
        assert by_id[worker_id].provider == "remote-libvirt"
        assert FEATURE_NOT_ENABLED_DETAIL in by_id[worker_id].detail
        assert WORKER_UNAVAILABLE_DETAIL not in by_id[worker_id].detail
        assert by_id[worker_id].failure_category is ErrorCategory.NOT_IMPLEMENTED

    # Server-vantage checks still run when worker-vantage checks are substituted.
    assert by_id[REACHABILITY_ID].status is CheckStatus.PASS
    assert by_id[SECRET_REF_ID].status is CheckStatus.PASS
    assert FEATURE_NOT_ENABLED_DETAIL not in by_id[SECRET_REF_ID].detail


def test_run_reports_configuration_error_when_config_unresolvable_at_run_time(
    monkeypatch, tmp_path: Path
) -> None:
    # An instance that passes the inventory loader / gate (so the check is assembled) but fails
    # remote_config_for_resource at probe time (inverted gdbstub range) → error +
    # configuration_error, with no connection attempt (config resolved before any open).
    bad = _INSTANCE.replace('gdbstub_range = "47000:47099"', 'gdbstub_range = "47099:47000"')
    _with_remote_instance(monkeypatch, tmp_path, instances=bad)
    report = asyncio.run(_factory(None).run())
    by_id = {r.check_id: r for r in report.results}
    assert by_id[REACHABILITY_ID].status is CheckStatus.ERROR
    assert by_id[REACHABILITY_ID].failure_category is ErrorCategory.CONFIGURATION_ERROR
    assert by_id[REACHABILITY_ID].fix is None


def test_base_image_staging_passes_when_volume_staged(monkeypatch, tmp_path: Path) -> None:
    _with_remote_instance(monkeypatch, tmp_path)
    _force_probe(monkeypatch, ReachabilityOutcome.REACHABLE)
    _force_staging_probe(monkeypatch, BaseImageStagingOutcome.STAGED)
    report = asyncio.run(_factory(None).run())
    by_id = {r.check_id: r for r in report.results}
    assert by_id[BASE_IMAGE_STAGING_ID].status is CheckStatus.PASS
    assert by_id[BASE_IMAGE_STAGING_ID].provider == "remote-libvirt"
    assert report.has_failure is False


def test_base_image_staging_fails_when_volume_absent(monkeypatch, tmp_path: Path) -> None:
    # The headline #513 case: a reachable host whose base-image volume is not staged is a FAIL
    # with the staging fix — surfaced before any allocation is requested, so the doctor gate exits
    # nonzero (has_failure True) rather than passing reachability and failing at provision.
    _with_remote_instance(monkeypatch, tmp_path)
    _force_probe(monkeypatch, ReachabilityOutcome.REACHABLE)
    _force_staging_probe(monkeypatch, BaseImageStagingOutcome.NOT_STAGED)
    report = asyncio.run(_factory(None).run())
    by_id = {r.check_id: r for r in report.results}
    assert by_id[REACHABILITY_ID].status is CheckStatus.PASS
    assert by_id[BASE_IMAGE_STAGING_ID].status is CheckStatus.FAIL
    assert by_id[BASE_IMAGE_STAGING_ID].fix == BASE_VOLUME_NOT_STAGED_FIX
    assert by_id[BASE_IMAGE_STAGING_ID].failure_category is ErrorCategory.CONFIGURATION_ERROR
    assert report.has_failure is True


def test_base_image_staging_absent_when_not_configured(monkeypatch, tmp_path: Path) -> None:
    _no_remote_instance(monkeypatch, tmp_path)
    ids = {c.id for c in _factory(None)._checks}  # noqa: SLF001
    assert BASE_IMAGE_STAGING_ID not in ids


def test_multiple_instances_each_get_a_reachability_check(monkeypatch, tmp_path: Path) -> None:
    # ADR-0187, #395: multiple [[remote_libvirt]] instances are now supported; the doctor assembles
    # one runnable reachability + base-image-staging check per declared host (fan-out).
    _with_remote_instance(monkeypatch, tmp_path, instances=_INSTANCE + _SECOND_INSTANCE)
    ids = [c.id for c in _factory(None)._checks]  # noqa: SLF001
    assert ids.count(REACHABILITY_ID) == 2
    assert ids.count(BASE_IMAGE_STAGING_ID) == 2


def test_default_factory_keeps_tight_timeouts(monkeypatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, tmp_path)
    service = _factory(None)
    assert service._timeout == 10.0  # noqa: SLF001
    assert service._overall_timeout == 30.0  # noqa: SLF001


# ---- worker-vantage dispatch wiring (ADR-0164, #514) ---------------------------------


def _enabled_worker_contribution(
    provider: str, worker_check_id: str
) -> DiagnosticProviderContribution:
    def _enabled() -> bool:
        return True

    def _no_checks() -> tuple[Check, ...]:
        return ()

    def _unavailable_worker_checks() -> tuple[WorkerVantageDescriptor, ...]:
        return (WorkerVantageDescriptor(id=worker_check_id, provider=provider),)

    return DiagnosticProviderContribution(
        provider=provider,
        enabled=_enabled,
        checks=_no_checks,
        unavailable_worker_checks=_unavailable_worker_checks,
        worker_checks=_no_checks,
    )


def test_factory_wires_dispatcher_when_pool_and_remote_configured(
    monkeypatch, tmp_path: Path
) -> None:
    from typing import cast

    from psycopg_pool import AsyncConnectionPool

    _with_remote_instance(monkeypatch, tmp_path)
    service = _factory(None, pool=cast(AsyncConnectionPool, object()))
    assert isinstance(service._worker_mode, WorkerVantageDispatchMode)  # noqa: SLF001


def test_factory_keeps_substitution_when_no_pool(monkeypatch, tmp_path: Path) -> None:
    _with_remote_instance(monkeypatch, tmp_path)
    service = _factory(None)
    assert isinstance(service._worker_mode, WorkerVantageSubstitutionMode)  # noqa: SLF001
    unavailable_ids = {c.id for c in service._worker_mode.checks}  # noqa: SLF001
    assert unavailable_ids == {PROVIDER_TLS_ID, GDBSTUB_ACL_ID, MULTIARCH_GDB_ID}


def test_factory_substitutes_every_enabled_worker_contribution_without_pool(
    monkeypatch, tmp_path: Path
) -> None:
    _set_env(monkeypatch, tmp_path)
    service = default_service_factory(
        None,
        provider_contributions=(
            _enabled_worker_contribution("provider-a", "worker-a"),
            _enabled_worker_contribution("provider-b", "worker-b"),
        ),
    )
    assert isinstance(service._worker_mode, WorkerVantageSubstitutionMode)  # noqa: SLF001
    unavailable = {
        (check.id, check.provider)
        for check in service._worker_mode.checks  # noqa: SLF001
    }
    assert unavailable == {("worker-a", "provider-a"), ("worker-b", "provider-b")}


def test_factory_dispatches_every_enabled_worker_contribution_with_pool(
    monkeypatch, tmp_path: Path
) -> None:
    from typing import cast

    import kdive.diagnostics.worker_dispatch as worker_dispatch

    _set_env(monkeypatch, tmp_path)
    created: list[tuple[str, tuple[str, ...]]] = []

    class RecordingWorkerCheckDispatcher:
        def __init__(
            self,
            pool: AsyncConnectionPool | None,
            *,
            provider: str,
            worker_check_ids: tuple[str, ...],
        ) -> None:
            assert pool is not None
            self.provider = provider
            self.worker_check_ids = worker_check_ids
            created.append((provider, worker_check_ids))

        async def run_worker_checks(self) -> list[CheckResult]:
            return [
                CheckResult(
                    check_id=check_id,
                    status=CheckStatus.PASS,
                    detail="ok",
                    provider=self.provider,
                )
                for check_id in self.worker_check_ids
            ]

    monkeypatch.setattr(worker_dispatch, "JobWorkerCheckDispatcher", RecordingWorkerCheckDispatcher)
    service = default_service_factory(
        None,
        pool=cast(AsyncConnectionPool, object()),
        provider_contributions=(
            _enabled_worker_contribution("provider-a", "worker-a"),
            _enabled_worker_contribution("provider-b", "worker-b"),
        ),
    )
    assert isinstance(service._worker_mode, WorkerVantageDispatchMode)  # noqa: SLF001
    report = asyncio.run(service.run())
    worker_results = {
        (result.check_id, result.provider)
        for result in report.results
        if result.check_id.startswith("worker-")
    }
    assert created == [("provider-a", ("worker-a",)), ("provider-b", ("worker-b",))]
    assert worker_results == {("worker-a", "provider-a"), ("worker-b", "provider-b")}


def test_factory_dispatches_local_libvirt_when_pool_but_remote_not_configured(
    monkeypatch, tmp_path: Path
) -> None:
    # local-libvirt is always enabled and contributes the multiarch_gdb worker check, so a pool
    # yields a dispatch mode even with remote-libvirt unconfigured — the dispatcher just carries
    # the one local worker check id.
    from typing import cast

    from psycopg_pool import AsyncConnectionPool

    _no_remote_instance(monkeypatch, tmp_path)
    service = _factory(None, pool=cast(AsyncConnectionPool, object()))
    assert isinstance(service._worker_mode, WorkerVantageDispatchMode)  # noqa: SLF001
