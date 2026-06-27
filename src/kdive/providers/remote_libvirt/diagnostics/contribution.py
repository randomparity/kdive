"""Remote-libvirt provider-owned diagnostic service contribution.

A doctor describes the whole fleet, so every probe fans out over each declared
``[[remote_libvirt]]`` instance (ADR-0187, #395): the reachability + base-image-staging checks
and the worker-vantage TLS + gdbstub-ACL checks each emit one result row per host. With one
declared instance the behavior is unchanged (one row each); with N instances each probe returns N.
"""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.diagnostics.buildhost_agent_check import EphemeralLibvirtBuildHostAgentCheck
from kdive.diagnostics.checks import GDBSTUB_ACL_ID, PROVIDER_TLS_ID, Check
from kdive.diagnostics.gdbstub_acl import gdbstub_acl_probe
from kdive.diagnostics.provider_checks import (
    BaseImageStagingCheck,
    GdbstubAclCheck,
    ProviderTlsCheck,
    RemoteLibvirtReachabilityCheck,
)
from kdive.diagnostics.provider_contracts import (
    DiagnosticProviderContribution,
    WorkerVantageDescriptor,
)
from kdive.providers.remote_libvirt.config import (
    all_remote_configs_by_name,
    is_remote_libvirt_configured,
    remote_config_for_resource,
    remote_instance_names,
    resolve_base_image_staged_volume_for,
)
from kdive.providers.remote_libvirt.diagnostics import (
    base_image_staging,
    buildhost_agent,
    reachability,
)
from kdive.providers.remote_libvirt.diagnostics.provider_tls import provider_tls_probe

_REMOTE_PROVIDER = "remote-libvirt"


def _checks() -> list[Check]:
    # Reachability + base-image config resolution is deferred to probe time so a single malformed
    # instance surfaces as that host's per-check configuration_error, not an assembly-time crash
    # (ADR-0125 deferral, fanned out per host — ADR-0187): each probe resolves its own host by name.
    checks: list[Check] = []
    for name in remote_instance_names():
        checks.append(
            RemoteLibvirtReachabilityCheck(
                provider=_REMOTE_PROVIDER,
                resource_id=name,
                probe=reachability.remote_libvirt_reachability_probe(
                    config_factory=lambda n=name: remote_config_for_resource(n)
                ),
            )
        )
        checks.append(
            BaseImageStagingCheck(
                provider=_REMOTE_PROVIDER,
                resource_id=name,
                probe=base_image_staging.base_image_staging_probe(
                    config_factory=lambda n=name: remote_config_for_resource(n),
                    volume_factory=lambda n=name: resolve_base_image_staged_volume_for(n),
                ),
            )
        )
    return checks


def _unavailable_worker_checks() -> list[WorkerVantageDescriptor]:
    descriptors: list[WorkerVantageDescriptor] = []
    for _name in remote_instance_names():
        descriptors.append(WorkerVantageDescriptor(id=PROVIDER_TLS_ID, provider=_REMOTE_PROVIDER))
        descriptors.append(WorkerVantageDescriptor(id=GDBSTUB_ACL_ID, provider=_REMOTE_PROVIDER))
    return descriptors


def _worker_checks() -> list[Check]:
    checks: list[Check] = []
    for _name, config in all_remote_configs_by_name():
        checks.append(
            ProviderTlsCheck(
                provider=_REMOTE_PROVIDER,
                ca_path=config.cert_refs.ca_cert_ref,
                probe=provider_tls_probe(config),
            )
        )
        checks.append(
            GdbstubAclCheck(
                provider=_REMOTE_PROVIDER,
                host=config.gdb_addr or "",
                port_range=f"{config.gdb_port_min}-{config.gdb_port_max}",
                probe=gdbstub_acl_probe(),
            )
        )
    return checks


def _buildhost_agent_check(pool: AsyncConnectionPool) -> Check:
    return EphemeralLibvirtBuildHostAgentCheck(probe=buildhost_agent.buildhost_agent_probe(pool))


def diagnostic_contribution() -> DiagnosticProviderContribution:
    return DiagnosticProviderContribution(
        provider=_REMOTE_PROVIDER,
        enabled=is_remote_libvirt_configured,
        checks=_checks,
        unavailable_worker_checks=_unavailable_worker_checks,
        worker_checks=_worker_checks,
        buildhost_agent_check=_buildhost_agent_check,
    )
