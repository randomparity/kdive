"""Remote-libvirt provider-owned diagnostic service contribution."""

from __future__ import annotations

from kdive.diagnostics.checks import (
    GDBSTUB_ACL_ID,
    PROVIDER_TLS_ID,
    BaseImageStagingCheck,
    Check,
    GdbstubAclCheck,
    ProviderTlsCheck,
    RemoteLibvirtReachabilityCheck,
)
from kdive.diagnostics.gdbstub_acl import gdbstub_acl_probe
from kdive.diagnostics.provider_contracts import (
    DiagnosticProviderContribution,
    WorkerVantageDescriptor,
)
from kdive.providers.remote_libvirt.config import (
    is_remote_libvirt_configured,
    remote_config_from_inventory,
)
from kdive.providers.remote_libvirt.diagnostics import base_image_staging, reachability
from kdive.providers.remote_libvirt.diagnostics.provider_tls import provider_tls_probe

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


def _worker_checks() -> list[Check]:
    config = remote_config_from_inventory()
    return [
        ProviderTlsCheck(
            provider=_REMOTE_PROVIDER,
            ca_path=config.cert_refs.ca_cert_ref,
            probe=provider_tls_probe(config),
        ),
        GdbstubAclCheck(
            provider=_REMOTE_PROVIDER,
            host=config.gdb_addr or "",
            port_range=f"{config.gdb_port_min}-{config.gdb_port_max}",
            probe=gdbstub_acl_probe(),
        ),
    ]


def diagnostic_contribution() -> DiagnosticProviderContribution:
    return DiagnosticProviderContribution(
        provider=_REMOTE_PROVIDER,
        enabled=is_remote_libvirt_configured,
        checks=_checks,
        unavailable_worker_checks=_unavailable_worker_checks,
        worker_checks=_worker_checks,
    )
