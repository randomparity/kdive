"""Remote-libvirt provider-owned diagnostic service contribution."""

from __future__ import annotations

from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    BaseImageStagingCheck,
    Check,
    RemoteLibvirtReachabilityCheck,
)
from kdive.diagnostics.provider_contracts import (
    DiagnosticProviderContribution,
    WorkerVantageDescriptor,
)
from kdive.providers.remote_libvirt.config import is_remote_libvirt_configured
from kdive.providers.remote_libvirt.diagnostics import base_image_staging, reachability

_REMOTE_PROVIDER = "remote-libvirt"


def _checks() -> list[Check]:
    return [
        RemoteLibvirtReachabilityCheck(
            provider=_REMOTE_PROVIDER,
            probe=reachability.remote_libvirt_reachability_probe(),
        ),
        BaseImageStagingCheck(
            provider=_REMOTE_PROVIDER,
            probe=base_image_staging.base_image_staging_probe(),
        ),
    ]


def _unavailable_worker_checks() -> list[WorkerVantageDescriptor]:
    return [
        WorkerVantageDescriptor(id=PROVIDER_TLS_ID, provider=_REMOTE_PROVIDER),
        WorkerVantageDescriptor(id=GDBSTUB_ACL_ID, provider=_REMOTE_PROVIDER),
    ]


def diagnostic_contribution() -> DiagnosticProviderContribution:
    return DiagnosticProviderContribution(
        enabled=is_remote_libvirt_configured,
        checks=_checks,
        unavailable_worker_checks=_unavailable_worker_checks,
    )
