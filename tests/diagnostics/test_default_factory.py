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

import kdive.config as config
from kdive.diagnostics.checks import (
    BASE_IMAGE_STAGING_ID,
    BASE_VOLUME_NOT_STAGED_FIX,
    GDBSTUB_ACL_ID,
    LOCAL_KERNEL_SRC_ID,
    PROVIDER_TLS_ID,
    REACHABILITY_ID,
    SECRET_REF_ID,
    BaseImageStagingOutcome,
    CheckStatus,
    ReachabilityOutcome,
    SecretRefCheck,
)
from kdive.diagnostics.service import (
    FEATURE_NOT_ENABLED_DETAIL,
    WORKER_UNAVAILABLE_DETAIL,
    default_service_factory,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.diagnostics import base_image_staging, reachability

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


# The always-on local_kernel_src check (ADR-0163) reads KDIVE_KERNEL_SRC and FAILs when it is
# unusable, which would flip report.has_failure and add a row to every aggregated run(). The shared
# run-fixtures point it at `root` (an existing absolute tmp dir → USABLE) so the new check passes
# and does not pollute the has_failure expectations the existing tests assert; tests that exercise
# the check's FAIL path set/clear KDIVE_KERNEL_SRC explicitly instead of using these helpers.
def _set_env(monkeypatch, root: Path, **refs: str) -> None:
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(root))
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(root))
    for name, value in refs.items():
        monkeypatch.setenv(name, value)
    config.load()


def _with_remote_instance(monkeypatch, root: Path, *, instances: str = _INSTANCE) -> None:
    path = root / "systems.toml"
    path.write_text(f"schema_version = 2\n{_IMAGE}\n{instances}\n")
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(root))
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(root))
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()


def _no_remote_instance(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(root))
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(root))
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
    service = default_service_factory(None)
    ids = {c.id for c in service._checks}  # noqa: SLF001 - assert the assembled check set
    assert "secret_ref" in ids


def test_secret_ref_passes_when_no_ref_is_required(monkeypatch, tmp_path: Path) -> None:
    # The default registry has no conditionally-required secret refs (the remote mTLS refs that
    # used to drive this moved to systems.toml, #395), so the assembled check resolves the empty
    # set and passes. The FAIL behavior is unit-covered in test_secret_ref.py with injected refs.
    _set_env(monkeypatch, tmp_path)
    check = next(c for c in default_service_factory(None)._checks if isinstance(c, SecretRefCheck))
    result = asyncio.run(check.run())
    assert result.status is CheckStatus.PASS


def test_with_egress_fails_fast_when_no_probe_image_is_wired(monkeypatch, tmp_path: Path) -> None:
    # The default factory has no probe-guest seam (remote needs an operator-staged image until
    # M2.4, ADR-0091), so opting into egress fails fast rather than silently dropping the check.
    _set_env(monkeypatch, tmp_path)
    with pytest.raises(CategorizedError) as exc:
        default_service_factory(None, with_egress=True)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


# ---- remote-libvirt reachability + TLS/ACL wiring (ADR-0125, #453) -------------------


def test_factory_includes_reachability_and_tls_acl_metadata_when_remote_configured(
    monkeypatch, tmp_path: Path
) -> None:
    _with_remote_instance(monkeypatch, tmp_path)
    service = default_service_factory(None)
    runnable_ids = {c.id for c in service._checks}  # noqa: SLF001
    unavailable_ids = {c.id for c in service._unavailable_worker_checks}  # noqa: SLF001
    assert {SECRET_REF_ID, REACHABILITY_ID, BASE_IMAGE_STAGING_ID} <= runnable_ids
    assert {PROVIDER_TLS_ID, GDBSTUB_ACL_ID} == unavailable_ids
    assert PROVIDER_TLS_ID not in runnable_ids
    assert GDBSTUB_ACL_ID not in runnable_ids


def test_factory_service_is_worker_unavailable_when_remote_configured(
    monkeypatch, tmp_path: Path
) -> None:
    _with_remote_instance(monkeypatch, tmp_path)
    service = default_service_factory(None)
    assert service._worker_available is False  # noqa: SLF001


def test_factory_omits_remote_checks_when_not_configured(monkeypatch, tmp_path: Path) -> None:
    _no_remote_instance(monkeypatch, tmp_path)
    ids = {c.id for c in default_service_factory(None)._checks}  # noqa: SLF001
    assert ids == {SECRET_REF_ID, LOCAL_KERNEL_SRC_ID}


def test_run_substitutes_tls_acl_and_runs_reachability_and_secret_ref(
    monkeypatch, tmp_path: Path
) -> None:
    _with_remote_instance(monkeypatch, tmp_path)
    _force_probe(monkeypatch, ReachabilityOutcome.REACHABLE)
    report = asyncio.run(default_service_factory(None).run())
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
        assert by_id[worker_id].failure_category == "not_implemented"

    # Server-vantage checks still RUN under worker_available=False.
    assert by_id[REACHABILITY_ID].status is CheckStatus.PASS
    assert by_id[SECRET_REF_ID].status is CheckStatus.PASS
    assert FEATURE_NOT_ENABLED_DETAIL not in by_id[SECRET_REF_ID].detail


def test_run_reports_configuration_error_when_config_unresolvable_at_run_time(
    monkeypatch, tmp_path: Path
) -> None:
    # An instance that passes the inventory loader / gate (so the check is assembled) but fails
    # remote_config_from_inventory at run time (inverted gdbstub range) → error +
    # configuration_error, with no connection attempt (config resolved before any open).
    bad = _INSTANCE.replace('gdbstub_range = "47000:47099"', 'gdbstub_range = "47099:47000"')
    _with_remote_instance(monkeypatch, tmp_path, instances=bad)
    report = asyncio.run(default_service_factory(None).run())
    by_id = {r.check_id: r for r in report.results}
    assert by_id[REACHABILITY_ID].status is CheckStatus.ERROR
    assert by_id[REACHABILITY_ID].failure_category == "configuration_error"
    assert by_id[REACHABILITY_ID].fix is None


def test_base_image_staging_passes_when_volume_staged(monkeypatch, tmp_path: Path) -> None:
    _with_remote_instance(monkeypatch, tmp_path)
    _force_probe(monkeypatch, ReachabilityOutcome.REACHABLE)
    _force_staging_probe(monkeypatch, BaseImageStagingOutcome.STAGED)
    report = asyncio.run(default_service_factory(None).run())
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
    report = asyncio.run(default_service_factory(None).run())
    by_id = {r.check_id: r for r in report.results}
    assert by_id[REACHABILITY_ID].status is CheckStatus.PASS
    assert by_id[BASE_IMAGE_STAGING_ID].status is CheckStatus.FAIL
    assert by_id[BASE_IMAGE_STAGING_ID].fix == BASE_VOLUME_NOT_STAGED_FIX
    assert by_id[BASE_IMAGE_STAGING_ID].failure_category == "configuration_error"
    assert report.has_failure is True


def test_base_image_staging_absent_when_not_configured(monkeypatch, tmp_path: Path) -> None:
    _no_remote_instance(monkeypatch, tmp_path)
    ids = {c.id for c in default_service_factory(None)._checks}  # noqa: SLF001
    assert BASE_IMAGE_STAGING_ID not in ids


def test_multiple_instances_are_not_configured_so_no_reachability_check(
    monkeypatch, tmp_path: Path
) -> None:
    # The inventory loader rejects >1 [[remote_libvirt]] instance, so is_remote_libvirt_configured()
    # degrades to False and no remote check is assembled — one MCP call cannot fan out across hosts.
    two = _INSTANCE + _INSTANCE.replace('"ub24-big"', '"second-host"')
    _with_remote_instance(monkeypatch, tmp_path, instances=two)
    ids = {c.id for c in default_service_factory(None)._checks}  # noqa: SLF001
    assert ids == {SECRET_REF_ID, LOCAL_KERNEL_SRC_ID}


# ---- local build-host warm-tree source check (ADR-0163, #532) ------------------------


def test_factory_always_includes_local_kernel_src(monkeypatch, tmp_path: Path) -> None:
    # The seeded worker-local LOCAL host is a DB invariant, so local_kernel_src is always
    # assembled — remote configured or not.
    _no_remote_instance(monkeypatch, tmp_path)
    assert LOCAL_KERNEL_SRC_ID in {c.id for c in default_service_factory(None)._checks}  # noqa: SLF001
    _with_remote_instance(monkeypatch, tmp_path)
    assert LOCAL_KERNEL_SRC_ID in {c.id for c in default_service_factory(None)._checks}  # noqa: SLF001


def test_local_kernel_src_fails_when_unset(monkeypatch, tmp_path: Path) -> None:
    # The #532 headline: an unset KDIVE_KERNEL_SRC is a contract fail surfaced at preflight, so
    # the doctor gate exits nonzero (has_failure True) rather than passing while every local
    # warm-tree build fails. Set KDIVE_KERNEL_SRC explicitly (not via the shared helper, which
    # makes it usable) to exercise the FAIL path.
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(tmp_path))
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "absent.toml"))
    monkeypatch.delenv("KDIVE_KERNEL_SRC", raising=False)
    config.load()
    report = asyncio.run(default_service_factory(None).run())
    by_id = {r.check_id: r for r in report.results}
    assert by_id[LOCAL_KERNEL_SRC_ID].status is CheckStatus.FAIL
    assert by_id[LOCAL_KERNEL_SRC_ID].failure_category == "configuration_error"
    assert by_id[LOCAL_KERNEL_SRC_ID].fix is not None
    assert by_id[LOCAL_KERNEL_SRC_ID].provider is None
    assert report.has_failure is True


def test_local_kernel_src_passes_when_usable(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KDIVE_SECRETS_ROOT", str(tmp_path))
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(tmp_path))
    config.load()
    report = asyncio.run(default_service_factory(None).run())
    by_id = {r.check_id: r for r in report.results}
    assert by_id[LOCAL_KERNEL_SRC_ID].status is CheckStatus.PASS
    assert by_id[LOCAL_KERNEL_SRC_ID].provider is None
    assert report.has_failure is False


# ---- worker-vantage dispatch wiring (ADR-0164, #514) ---------------------------------


def test_factory_wires_dispatcher_when_pool_and_remote_configured(
    monkeypatch, tmp_path: Path
) -> None:
    from typing import cast

    from psycopg_pool import AsyncConnectionPool

    _with_remote_instance(monkeypatch, tmp_path)
    service = default_service_factory(None, pool=cast(AsyncConnectionPool, object()))
    assert service._worker_dispatcher is not None  # noqa: SLF001
    # The dispatcher owns the worker-vantage outcome, so no static unavailable metadata is emitted.
    assert service._unavailable_worker_checks == []  # noqa: SLF001


def test_factory_keeps_substitution_when_no_pool(monkeypatch, tmp_path: Path) -> None:
    _with_remote_instance(monkeypatch, tmp_path)
    service = default_service_factory(None)
    assert service._worker_dispatcher is None  # noqa: SLF001
    unavailable_ids = {c.id for c in service._unavailable_worker_checks}  # noqa: SLF001
    assert unavailable_ids == {PROVIDER_TLS_ID, GDBSTUB_ACL_ID}


def test_factory_no_dispatcher_when_pool_but_remote_not_configured(
    monkeypatch, tmp_path: Path
) -> None:
    from typing import cast

    from psycopg_pool import AsyncConnectionPool

    _no_remote_instance(monkeypatch, tmp_path)
    service = default_service_factory(None, pool=cast(AsyncConnectionPool, object()))
    assert service._worker_dispatcher is None  # noqa: SLF001
