"""Worker handler for the diagnostics_worker_check job (ADR-0164).

Resolves the remote-libvirt config at probe time, builds the two worker-vantage checks with their
production probes, runs each through :func:`run_check` (per-check timeout -> an unreachable host is
an ``error``, never a hang), and returns the serialized CheckResults inline as the job's
``result_ref``. A config-resolution failure propagates so the job dead-letters and the dispatcher
maps it to an ``error`` verdict.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from psycopg import AsyncConnection

from kdive.diagnostics.checks import (
    Check,
    GdbstubAclCheck,
    ProviderTlsCheck,
    run_check,
)
from kdive.diagnostics.gdbstub_acl import gdbstub_acl_probe
from kdive.diagnostics.result_codec import serialize_results
from kdive.domain.models import Job, JobKind
from kdive.jobs.models import HandlerRegistry
from kdive.providers.remote_libvirt.config import (
    RemoteLibvirtConfig,
    remote_config_from_inventory,
)
from kdive.providers.remote_libvirt.diagnostics.provider_tls import provider_tls_probe

_REMOTE_PROVIDER = "remote-libvirt"
_PER_CHECK_TIMEOUT_S = 6.0

ConfigFactory = Callable[[], RemoteLibvirtConfig]
CheckBuilder = Callable[[RemoteLibvirtConfig], Sequence[Check]]


def _build_checks(config: RemoteLibvirtConfig) -> list[Check]:
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


async def diagnostics_worker_check_handler(
    conn: AsyncConnection | None,
    job: Job | None,
    *,
    config_factory: ConfigFactory = remote_config_from_inventory,
    build_checks: CheckBuilder = _build_checks,
) -> str | None:
    """Run the worker-vantage checks and return their results inline as result_ref.

    A config-resolution failure propagates (the job dead-letters); ``conn``/``job`` are unused
    (the handler reads no DB state) but kept for the :data:`JobHandler` signature.
    """
    config = config_factory()
    checks = build_checks(config)
    results = [await run_check(check, timeout=_PER_CHECK_TIMEOUT_S) for check in checks]
    return serialize_results(results)


def register_handlers(registry: HandlerRegistry) -> None:
    """Bind the diagnostics_worker_check job handler."""
    registry.register(
        JobKind.DIAGNOSTICS_WORKER_CHECK,
        lambda conn, job: diagnostics_worker_check_handler(conn, job),
    )
