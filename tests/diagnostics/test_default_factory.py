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
import kdive.diagnostics.reachability as reachability
from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    REACHABILITY_ID,
    SECRET_REF_ID,
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


def test_factory_includes_reachability_and_tls_acl_when_remote_configured(
    monkeypatch, tmp_path: Path
) -> None:
    _with_remote_instance(monkeypatch, tmp_path)
    ids = {c.id for c in default_service_factory(None)._checks}  # noqa: SLF001
    assert {SECRET_REF_ID, PROVIDER_TLS_ID, GDBSTUB_ACL_ID, REACHABILITY_ID} <= ids


def test_factory_service_is_worker_unavailable_when_remote_configured(
    monkeypatch, tmp_path: Path
) -> None:
    _with_remote_instance(monkeypatch, tmp_path)
    service = default_service_factory(None)
    assert service._worker_available is False  # noqa: SLF001


def test_factory_omits_remote_checks_when_not_configured(monkeypatch, tmp_path: Path) -> None:
    _no_remote_instance(monkeypatch, tmp_path)
    ids = {c.id for c in default_service_factory(None)._checks}  # noqa: SLF001
    assert ids == {SECRET_REF_ID}


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


def test_multiple_instances_are_not_configured_so_no_reachability_check(
    monkeypatch, tmp_path: Path
) -> None:
    # The inventory loader rejects >1 [[remote_libvirt]] instance, so is_remote_libvirt_configured()
    # degrades to False and no remote check is assembled — one MCP call cannot fan out across hosts.
    two = _INSTANCE + _INSTANCE.replace('"ub24-big"', '"second-host"')
    _with_remote_instance(monkeypatch, tmp_path, instances=two)
    ids = {c.id for c in default_service_factory(None)._checks}  # noqa: SLF001
    assert ids == {SECRET_REF_ID}
