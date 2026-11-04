"""Opt-in, bind-only provider whose required ports refuse before I/O (ADR-0813)."""

from __future__ import annotations

from typing import Never

from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.inventory.errors import InventoryError
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.path import systems_toml_path
from kdive.providers.core.discovery_registration import ProviderDiscoveryRegistration
from kdive.providers.core.runtime import ProviderRuntime


class _RefusingPorts:
    kind = ResourceKind.BYO_HOST

    def _unavailable(self, *args: object, **kwargs: object) -> Never:
        raise CategorizedError(
            "BYO host inventory is available, but host operations are not implemented",
            category=ErrorCategory.NOT_IMPLEMENTED,
        )

    provision = _unavailable
    reprovision = _unavailable
    teardown = _unavailable
    read_resolved_cpu = _unavailable
    install = _unavailable
    boot = _unavailable
    open_transport = _unavailable
    close_transport = _unavailable
    recorded_ssh_endpoint = _unavailable
    power = _unavailable
    force_crash = _unavailable
    diagnostic_sysrq = _unavailable
    capture = _unavailable
    run_crash_postmortem = _unavailable
    from_vmcore = _unavailable
    introspect_live = _unavailable
    run_script = _unavailable
    rootfs_source = _unavailable
    drgn_live_seeds_bootstrap_key = _unavailable
    validate_profile = _unavailable
    destructive_opt_in = _unavailable
    capture_method = _unavailable
    gdbstub_provisioned = _unavailable
    host_dump_provisioned = _unavailable
    fadump_provisioned = _unavailable


def build_runtime() -> ProviderRuntime:
    ports = _RefusingPorts()
    return ProviderRuntime(
        profile_policy=ports,
        provisioner=ports,
        installer=ports,
        booter=ports,
        connector=ports,
        controller=ports,
        retriever=ports,
        crash_postmortem=ports,
        vmcore_introspector=ports,
        live_introspector=ports,
        platform_root_cmdline=None,
    )


def discovery_registration() -> ProviderDiscoveryRegistration:
    return ProviderDiscoveryRegistration(
        target_factory=_RefusingPorts().provision,
        kind=ResourceKind.BYO_HOST,
        pool_name="default",
        cost_class="byo",
        creates=False,
    )


def is_configured() -> bool:
    """A malformed shared inventory cannot prevent unrelated provider startup."""
    try:
        doc = load_inventory_optional(systems_toml_path())
    except InventoryError:
        return False
    return doc is not None and bool(doc.byo_host)
