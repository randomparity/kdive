from __future__ import annotations

from collections import Counter
from dataclasses import replace

import pytest

from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES
from kdive.mcp.tools.lifecycle.vmcore.handlers import VMCORE_METHODS
from scripts.coverage_campaign import contract
from scripts.coverage_campaign.contract import (
    FILTERED_LIST_BOUNDARIES,
    HANDLER_REFUSAL,
    Cell,
    Inventory,
    build_contract,
    load_mapping,
    read_inventory,
)


@pytest.fixture(scope="module")
def inventory() -> Inventory:
    return read_inventory()


def test_failure_ownership_and_remote_quiescence_binding(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells
    failures = [cell for cell in cells if cell.operation == "failure-resource"]
    orchestration = {"worker-interruption", "lost-response", "backing-service"}
    limits = {"minimum-cpu-ram", "insufficient-disk", "slow-boot"}
    expected = {
        f"failure-resource/{provider}/{arch}/{failure}"
        for provider in inventory.capabilities
        for arch in inventory.arches
        for failure in orchestration | limits
    }
    assert {cell.id for cell in failures} == expected
    for cell in failures:
        failure = cell.id.rsplit("/", 1)[1]
        assert cell.owner == (
            2818 if cell.guest_arch == "ppc64le" else 3117 if failure in orchestration else 3118
        )
        assert cell.scenario_id == cell.id
        assert cell.assertions == (failure, "no-duplicates", "protected-state", "cleanup")
        bound = {3118: _LIMIT_NODES, 3117: _FAULT_NODES}.get(cell.owner, {}).get(cell.provider)
        assert cell.node_id == bound
    proofs = [cell for cell in cells if cell.operation == "remote-quiescence"]
    assert {cell.guest_arch for cell in proofs} == set(inventory.arches)
    for cell in proofs:
        assert cell.provider == "remote-libvirt"
        assert cell.inputs == ("image_sha256",)
        assert cell.assertions == (
            "accepted-mutation",
            "client-terminated",
            "fresh-monitor-ordering",
            "cleanup",
        )
        assert cell.owner == (2816 if cell.guest_arch == "x86_64" else 2818)
        assert (cell.node_id is not None) == (cell.guest_arch == "x86_64")


@pytest.mark.parametrize("provider", ["local-libvirt", "remote-libvirt"])
def test_failure_binding_is_provider_and_arch_specific(inventory: Inventory, provider: str) -> None:
    cells = build_contract(inventory=inventory).cells
    selected = next(
        cell
        for cell in cells
        if cell.operation == "failure-resource"
        and cell.provider == provider
        and cell.guest_arch == "x86_64"
        and cell.id.endswith("/worker-interruption")
    )
    mapping = load_mapping().model_copy(deep=True)
    mapping.implementations[selected.scenario_id] = _DEEP_NODE
    mapped = build_contract(mapping=mapping, inventory=inventory).cells
    failures = [cell for cell in mapped if cell.operation == "failure-resource"]
    assert {cell.id for cell in failures if cell.node_id == _DEEP_NODE} == {selected.id}
    unmapped = {cell.id: cell.node_id for cell in cells}
    assert all(cell.node_id == unmapped[cell.id] for cell in failures if cell.id != selected.id)


def test_kernel_corpus_ownership_preserves_required_cells(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells
    corpus = [c for c in cells if c.operation == "kernel-corpus"]
    expected = {
        f"kernel-corpus/local-libvirt/{arch}/{case}/{revision}": (
            2818 if arch == "ppc64le" else 2817,
            f"kernel-corpus/{case}/{revision}",
        )
        for arch in ("x86_64", "ppc64le")
        for case in ("case-1", "case-2")
        for revision in ("broken", "fixed")
    }
    assert len(corpus) == len(expected)
    assert {c.id: (c.owner, c.scenario_id) for c in corpus} == expected


def test_registered_addition_requires_mapping(inventory: Inventory) -> None:
    added = replace(inventory.tools[0], tool="new.operation")
    with pytest.raises(ValueError, match="unmapped"):
        build_contract(inventory=replace(inventory, tools=(*inventory.tools, added)))


def test_removed_tool_does_not_leave_phantom_coverage(inventory: Inventory) -> None:
    with pytest.raises(ValueError, match="unknown tool"):
        build_contract(inventory=replace(inventory, tools=inventory.tools[1:]))


def test_catalog_native_smoke_and_build_purpose_are_required(inventory: Inventory) -> None:
    contract = build_contract(inventory=inventory)
    smoke = [c for c in contract.cells if c.operation == "image-smoke"]
    assert {c.image for c in smoke} == set(inventory.images)
    assert len(smoke) == len(inventory.images)
    for cell in smoke:
        assert cell.image is not None
        entry = inventory.images[cell.image]
        assert cell.host_arch == cell.guest_arch == entry.arch
        assert cell.accelerator == ("kvm" if entry.arch == "x86_64" else "kvm-hv")
        assert "cleanup" in cell.assertions
        if entry.kind == "build":
            assert "build-toolchain" in cell.assertions


def test_native_deep_families_and_foreign_tcg_stay_distinct(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells
    deep = [c for c in cells if c.operation == "deep-lifecycle"]
    assert {c.family for c in deep} == {"debian", "fedora", "enterprise", "suse"}
    assert {c.provider for c in deep} == {"local-libvirt", "remote-libvirt"}
    assert {c.guest_arch for c in deep} == SUPPORTED_ARCHES
    tcg = [c for c in cells if c.operation == "tcg-upload-boot"]
    assert len(tcg) == 2
    assert all(c.accelerator == "tcg" and c.host_arch != c.guest_arch for c in tcg)


_IMAGE_SMOKE_NODE = "tests/integration/test_image_smoke_live.py::test_image_smoke"
_CORE_NODE = "tests/integration/test_core_tool_cells_live.py::test_core_tool_cell"
_CATALOG_NODE = "tests/integration/test_catalog_tool_cells_live.py::test_catalog_tool_cell"
_INVESTIGATION_NODE = (
    "tests/integration/test_investigation_tool_cells_live.py::test_investigation_tool_cell"
)
_OPERATOR_NODE = "tests/integration/test_operator_tool_cells_live.py::test_operator_tool_cell"
_MUTATION_NODE = (
    "tests/integration/test_operator_mutation_tool_cells_live.py::test_operator_mutation_tool_cell"
)
_SYSTEM_NODE = "tests/integration/test_system_tool_cells_live.py::test_system_tool_cell"
_RUN_NODE = "tests/integration/test_run_tool_cells_live.py::test_run_tool_cell"
_BREAKGLASS_NODE = "tests/integration/test_breakglass_tool_cells_live.py::test_breakglass_tool_cell"
_POWER_NODE = (
    "tests/integration/test_power_snapshot_tool_cells_live.py::test_power_snapshot_tool_cell"
)
_CAPTURE_NODE = "tests/integration/test_capture_tool_cells_live.py::test_capture_tool_cell"
_CONTROL_NODE = "tests/integration/test_control_tool_cells_live.py::test_control_tool_cell"
_DEEP_NODE = "tests/integration/test_deep_lifecycle_live.py::test_deep_lifecycle"
_CORPUS_NODE = "tests/integration/test_kernel_corpus_live.py::test_kernel_corpus"
_LIMITS_NODE = "tests/integration/test_resource_limits_live.py::test_local_resource_boundary"
_LIMIT_NODES = {
    "local-libvirt": _LIMITS_NODE,
    "remote-libvirt": (
        "tests/integration/test_resource_limits_live.py::test_remote_resource_boundary"
    ),
}
_FAULTS_LIVE = "tests/integration/test_orchestration_faults_live.py"
_FAULT_NODES = {
    "local-libvirt": f"{_FAULTS_LIVE}::test_local_orchestration_fault",
    "remote-libvirt": f"{_FAULTS_LIVE}::test_remote_orchestration_fault",
}
_REMOTE_DEEP_NODE = (
    "tests/integration/test_remote_deep_lifecycle_live.py::test_remote_deep_lifecycle"
)
_SYSTEM_TOOLS = {
    "systems.authorize_ssh_key",
    "systems.check_ssh_reachable",
    "systems.provision",
    "systems.reprovision",
    "systems.ssh_info",
    "systems.teardown",
}
_RUN_TOOLS = {
    "images.publish",
    "runs.boot",
    "runs.cancel",
    "runs.install",
    "runs.release_external_boot",
}
_LIFECYCLE_TOOLS = _SYSTEM_TOOLS | _RUN_TOOLS
_RUN_SYSTEM_JOB_TOOLS = {
    "jobs.cancel",
    "jobs.list",
    "jobs.wait",
    "runs.bind",
    "runs.complete_build",
    "runs.create",
    "runs.get",
    "runs.list",
    "runs.set",
    "systems.get",
    "systems.list",
}
_RUN_SYSTEM_JOB_NODE = (
    "tests/integration/test_run_system_job_tool_cells_live.py::test_run_system_job_tool_cell"
)
_ACCOUNTING_NODE = (
    "tests/integration/test_allocation_accounting_tool_cells_live.py"
    "::test_allocation_accounting_tool_cell"
)


def test_lifecycle_owners_follow_the_approved_split(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells

    def owners(operations: set[str], provider: str, arch: str) -> set[int]:
        return {
            c.owner
            for c in cells
            if c.operation in operations and c.provider == provider and c.guest_arch == arch
        }

    deep = {"deep-lifecycle"}
    for operations, provider, owner in (
        (_SYSTEM_TOOLS, "local-libvirt", 3062),
        (_RUN_TOOLS, "local-libvirt", 3119),
        (_SYSTEM_TOOLS, "remote-libvirt", 3080),
        (_RUN_TOOLS, "remote-libvirt", 3120),
        (deep, "local-libvirt", 2809),
        (deep, "remote-libvirt", 2810),
    ):
        assert owners(operations, provider, "x86_64") == {owner}
    for provider in ("local-libvirt", "remote-libvirt"):
        assert owners(_LIFECYCLE_TOOLS | deep, provider, "ppc64le") == {2818}
    assert len([c for c in cells if c.owner == 3062]) == 120
    assert len([c for c in cells if c.owner == 3119]) == 96
    assert len([c for c in cells if c.owner == 2809]) == 8
    assert len([c for c in cells if c.owner == 2810]) == 8
    assert len([c for c in cells if c.owner == 3080]) == 120
    assert len([c for c in cells if c.owner == 3120]) == 96
    functional = [c for c in cells if c.kind == "functional" and c.operation in _LIFECYCLE_TOOLS]
    systems = {(c.roles, c.inputs) for c in functional if c.operation in _SYSTEM_TOOLS}
    assert systems == {(("server", "worker", "reconciler"), ())}
    base = ("server", "worker", "reconciler")
    runs = {(c.operation, c.roles, len(c.inputs)) for c in functional if c.operation in _RUN_TOOLS}
    # Operator option B (#3119, 2026-10-07): only the release needs the provider authority,
    # and images.publish uploads no kernel.
    assert runs == {
        ("images.publish", base, 0),
        ("runs.boot", base, 6),
        ("runs.cancel", base, 6),
        ("runs.install", base, 6),
        ("runs.release_external_boot", (*base, "authority"), 6),
    }
    local = {c.scenario_id for c in cells if c.operation in deep and c.provider == "local-libvirt"}
    assert local == {"deep-lifecycle/local-libvirt/longterm", "deep-lifecycle/local-libvirt/stable"}


def test_pending_cells_have_owned_assertions_but_no_invented_nodes(inventory: Inventory) -> None:
    contract = build_contract(inventory=inventory)
    assert contract.cells
    assert all(c.owner > 0 and c.observation and c.assertions for c in contract.cells)
    smoke = [c for c in contract.cells if c.operation == "image-smoke"]
    assert {c.node_id for c in smoke} == {_IMAGE_SMOKE_NODE}
    assert {c.owner for c in smoke if c.guest_arch == "ppc64le"} == {2818}
    deep = [c for c in contract.cells if c.operation == "deep-lifecycle"]
    assert {c.node_id for c in deep if c.provider == "local-libvirt"} == {_DEEP_NODE}
    assert {c.node_id for c in deep if c.provider == "remote-libvirt"} == {_REMOTE_DEEP_NODE}
    host_install = [c for c in contract.cells if c.scenario_id == "host-install"]
    assert len(host_install) == 6
    assert {c.node_id for c in host_install} == {
        "tests/integration/test_host_install_live.py::test_installed_host_boots_pinned_kernel"
    }
    core = [c for c in contract.cells if c.operation in _CORE_TOOLS]
    assert len(core) == 84 and {c.node_id for c in core} == {_CORE_NODE}
    catalog = [c for c in contract.cells if c.operation in _SPLIT[3095]]
    assert len(catalog) == 228 and {c.node_id for c in catalog} == {_CATALOG_NODE}
    investigation = [c for c in contract.cells if c.operation in _SPLIT[3096]]
    assert len(investigation) == 390
    assert {c.node_id for c in investigation} == {_INVESTIGATION_NODE}
    operator = [c for c in contract.cells if c.operation in _OPERATOR_TOOLS]
    assert len(operator) == 124 and {c.node_id for c in operator} == {_OPERATOR_NODE}
    build_uses = [c for c in contract.cells if c.operation in _OPERATOR_SPLIT[3111]]
    assert len(build_uses) == 16 and {c.node_id for c in build_uses} == {_OPERATOR_NODE}
    mutation = [c for c in contract.cells if c.operation in _OPERATOR_SPLIT[3110]]
    assert len(mutation) == 216 and {c.node_id for c in mutation} == {_MUTATION_NODE}
    systems = [c for c in contract.cells if c.operation in _SYSTEM_TOOLS]
    local = [c for c in systems if c.provider == "local-libvirt"]
    assert len(local) == 240 and {c.node_id for c in local} == {_SYSTEM_NODE}
    remote = [c for c in systems if c.provider == "remote-libvirt"]
    assert len(remote) == 240 and {c.node_id for c in remote} == {_SYSTEM_NODE}
    runs = [c for c in contract.cells if c.operation in _RUN_TOOLS]
    local_runs = [c for c in runs if c.provider == "local-libvirt"]
    assert len(local_runs) == 192 and {c.node_id for c in local_runs} == {_RUN_NODE}
    remote_runs = [c for c in runs if c.provider == "remote-libvirt"]
    assert len(remote_runs) == 192 and {c.node_id for c in remote_runs} == {_RUN_NODE}
    glass = [c for c in contract.cells if c.operation in _OPERATOR_SPLIT[3112]]
    assert len(glass) == 272 and {c.node_id for c in glass} == {_BREAKGLASS_NODE}
    power = [c for c in contract.cells if c.operation in _POWER_SNAPSHOT_TOOLS]
    local_power = [c for c in power if c.provider == "local-libvirt"]
    # A local scenario id names no architecture, so the ppc64le cells (#2818's) share the node.
    assert len(local_power) == 200 and {c.node_id for c in local_power} == {_POWER_NODE}
    remote_power = [c for c in power if c.provider == "remote-libvirt"]
    # As for local: the remote ppc64le cells (#2818's) share the provider-qualified ids.
    assert len(remote_power) == 200 and {c.node_id for c in remote_power} == {_POWER_NODE}
    capture = [c for c in contract.cells if c.operation in _CAPTURE_TOOLS]
    local_capture = [c for c in capture if c.provider == "local-libvirt"]
    # As for power: the local ppc64le cells (#2818's) share the arch-free scenario ids.
    assert len(local_capture) == 324 and {c.node_id for c in local_capture} == {_CAPTURE_NODE}
    remote_capture = [c for c in capture if c.provider == "remote-libvirt"]
    # A remote scenario id names no architecture either: the ppc64le cells (#2818's) share it.
    assert len(remote_capture) == 360 and {c.node_id for c in remote_capture} == {_CAPTURE_NODE}
    corpus = [c for c in contract.cells if c.operation == "kernel-corpus"]
    # As for power: the ppc64le corpus cells share the arch-free scenario ids.
    assert len(corpus) == 8 and {c.node_id for c in corpus} == {_CORPUS_NODE}
    control = [c for c in contract.cells if c.operation in _LOCAL_CONTROL_TOOLS]
    local_control = [c for c in control if c.provider == "local-libvirt"]
    # As for power: the local ppc64le cells (#2818's) share the arch-free scenario ids.
    assert len(local_control) == 120 and {c.node_id for c in local_control} == {_CONTROL_NODE}
    remote_control = [c for c in control if c.provider == "remote-libvirt"]
    assert len(remote_control) == 120
    assert {c.node_id for c in remote_control} == {_CONTROL_NODE}
    assert {c.node_id for c in contract.cells if c.owner == 3116} == {_POWER_NODE, _CONTROL_NODE}
    service = [c for c in contract.cells if c.operation in _RUN_SYSTEM_JOB_TOOLS]
    assert len(service) == 330 and {c.node_id for c in service} == {_RUN_SYSTEM_JOB_NODE}
    accounting = [c for c in contract.cells if c.operation in _SPLIT[3098]]
    assert len(accounting) == 330 and {c.node_id for c in accounting} == {_ACCOUNTING_NODE}
    bound = {
        "image-smoke",
        "deep-lifecycle",
        "host-install",
        "remote-quiescence",
        "kernel-corpus",
        *_CORE_TOOLS,
        *_SPLIT[3095],
        *_SPLIT[3096],
        *_OPERATOR_TOOLS,
        *_OPERATOR_SPLIT[3111],
        *_OPERATOR_SPLIT[3110],
        *_SYSTEM_TOOLS,
        *_RUN_TOOLS,
        *_OPERATOR_SPLIT[3112],
        *_POWER_SNAPSHOT_TOOLS,
        *_CAPTURE_TOOLS,
        *(c.operation for c in contract.cells if c.operation.startswith("debug.")),
        *_LOCAL_CONTROL_TOOLS,
        *_RUN_SYSTEM_JOB_TOOLS,
        *_SPLIT[3098],
    }
    limits = [c for c in contract.cells if c.owner == 3118]
    local_limits = [c for c in limits if c.provider == "local-libvirt"]
    assert len(local_limits) == 3 and {c.node_id for c in local_limits} == {_LIMITS_NODE}
    bound_limits = [c for c in limits if c.node_id is not None]
    assert len(bound_limits) == 6
    faults = [c for c in contract.cells if c.owner == 3117]
    assert len(faults) == 6
    assert {(c.provider, c.node_id) for c in faults} == set(_FAULT_NODES.items())
    unbound = [
        c
        for c in contract.cells
        if c.operation not in bound and c not in bound_limits and c not in faults
    ]
    assert all(c.node_id is None for c in unbound)
    assert len({c.id for c in contract.cells}) == len(contract.cells)
    recovery = [c for c in contract.cells if c.operation == "ops.recover_build_use"]
    assert recovery and {c.configuration for c in recovery} == {"recovery"}
    assert {c.exposure for c in recovery} == {"gateway", "direct"}


def test_matrix_digest_covers_mapping_catalog_capabilities_and_configurations(
    inventory: Inventory,
) -> None:
    baseline = build_contract(inventory=inventory).matrix_sha256
    reordered = replace(inventory, tools=tuple(reversed(inventory.tools)))
    assert build_contract(inventory=reordered).matrix_sha256 == baseline
    mapping = load_mapping().model_copy(deep=True)
    group = mapping.groups[0]
    tool = next(iter(group.tools))
    group.tools[tool] += " Compare a second observable field."
    assert build_contract(mapping=mapping, inventory=inventory).matrix_sha256 != baseline
    image = next(iter(inventory.images))
    images = dict(inventory.images)
    images[image] = replace(images[image], version="changed")
    assert build_contract(inventory=replace(inventory, images=images)).matrix_sha256 != baseline
    support = dict(inventory.capabilities)
    support["local-libvirt"] = replace(support["local-libvirt"], supports_snapshots=False)
    assert (
        build_contract(inventory=replace(inventory, capabilities=support)).matrix_sha256 != baseline
    )


def test_duplicate_mapping_and_unknown_implementation_are_rejected(inventory: Inventory) -> None:
    mapping = load_mapping().model_copy(deep=True)
    mapping.groups.append(mapping.groups[0])
    with pytest.raises(ValueError, match="duplicate tool"):
        build_contract(mapping=mapping, inventory=inventory)
    mapping = load_mapping().model_copy(deep=True)
    mapping.implementations["invented-scenario"] = "tests/scripts/test_gridgen.py::test_missing"
    with pytest.raises(ValueError, match="unknown scenario"):
        build_contract(mapping=mapping, inventory=inventory)


def test_scenarios_cannot_claim_nonexistent_pytest_nodes(inventory: Inventory) -> None:
    mapping = load_mapping().model_copy(deep=True)
    cell = build_contract(inventory=inventory).cells[0]
    mapping.implementations[cell.scenario_id] = "tests/scripts/test_gridgen.py::test_missing"
    with pytest.raises(ValueError, match="pytest node"):
        build_contract(mapping=mapping, inventory=inventory)


def test_capability_modes_and_deployed_roles_follow_operation_contract(
    inventory: Inventory,
) -> None:
    cells = build_contract(inventory=inventory).cells
    introspect = [c for c in cells if c.operation == "introspect.run" and c.kind == "functional"]
    assert introspect and all(
        c.scenario_id == f"{_tool_prefix(c.provider)}/introspect.run/live/functional"
        for c in introspect
    )
    queue = [c for c in cells if c.operation == "ops.set_queue_paused" and c.kind == "functional"]
    assert queue and all("worker" in c.roles for c in queue)


_NATIVE = {
    "image-smoke",
    "deep-lifecycle",
    "tcg-upload-boot",
    "host-install",
    "failure-resource",
    "kernel-corpus",
}


def test_authority_role_follows_the_scenario(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells
    native = [c for c in cells if c.operation in _NATIVE]
    assert {c.operation for c in native} == _NATIVE
    assert all(c.roles == ("server", "worker", "reconciler") for c in native)
    routed = {name for g in load_mapping().groups if g.authority for name in g.tools}
    tool = [c for c in cells if c.operation in routed and c.kind == "functional"]
    assert tool and all("authority" in c.roles for c in tool)


def test_debug_session_covers_every_advertised_transport(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells
    sessions = [c for c in cells if c.operation == "debug.start_session" and c.kind == "functional"]
    for provider, support in inventory.capabilities.items():
        scenarios = {c.scenario_id for c in sessions if c.provider == provider}
        assert scenarios == {
            f"{_tool_prefix(provider)}/debug.start_session/{mode}/functional"
            for mode in support.debug_transports
        }


_CORE_TOOLS = {
    "fixtures.validate",
    "projects.list",
    "session.whoami",
    "systems.profile_examples",
    "tools.invoke",
    "tools.search",
}
_SPLIT = {
    3095: {
        *(f"images.{n}" for n in ("delete", "describe", "kernel_config", "list", "upload")),
        *(f"shapes.{n}" for n in ("delete", "list", "set")),
        *(f"resources.{n}" for n in ("availability", "describe", "list")),
    },
    3096: {
        *(
            f"investigations.{n}"
            for n in (
                "close",
                "complete_rootfs_upload",
                "get",
                "link",
                "list",
                "open",
                "set",
                "unlink",
            )
        ),
        *(
            f"artifacts.{n}"
            for n in (
                "create_investigation_upload",
                "create_run_upload",
                "fetch_raw",
                "get",
                "list",
            )
        ),
    },
    3097: {
        *(f"runs.{n}" for n in ("bind", "complete_build", "create", "get", "list", "set")),
        "systems.get",
        "systems.list",
        *(f"jobs.{n}" for n in ("cancel", "list", "wait")),
    },
    3098: {
        *(f"allocations.{n}" for n in ("list", "release", "renew", "request", "wait")),
        *(f"accounting.{n}" for n in ("estimate", "report", "set_budget", "set_quota", "usage")),
        "reports.generate",
    },
}


def test_core_tools_follow_the_approved_split(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells
    owned = {owner: {c.operation for c in cells if c.owner == owner} for owner in (2811, *_SPLIT)}
    assert owned == {2811: _CORE_TOOLS, **_SPLIT}
    assert len([c for c in cells if c.owner == 2811]) == 56
    overrides = {g.owner: set(g.role_overrides) for g in load_mapping().groups if g.owner in _SPLIT}
    assert overrides == {
        3095: set(),
        3096: set(),
        3097: {"jobs.cancel", "jobs.wait"},
        3098: {"allocations.release", "allocations.wait"},
    }


_OPERATOR_TOOLS = {
    "audit.query",
    "inventory.list",
    "ops.diagnostics",
    "ops.export_cost_classes",
    "ops.export_systems_toml",
    "ops.jobs_list",
    "ops.tool_trail",
    "secrets.list",
}
_OPERATOR_SPLIT = {
    3110: {
        "images.extend",
        "images.prune_expired",
        "inventory.clear_override",
        *(f"ops.{n}" for n in ("reconcile_now", "reconcile_systems", "set_cost_class_coeff")),
        *(f"ops.{n}" for n in ("set_host_capacity", "set_queue_paused")),
        *(f"resources.{n}" for n in ("deregister", "drain", "register", "renew")),
        *(f"resources.{n}" for n in ("set_scheduling", "set_status")),
    },
    3111: {"ops.build_uses_list", "ops.recover_build_use"},
    3112: {
        "ops.force_release",
        "ops.force_teardown",
        "ops.resolve_recovery_orphan",
        "systems.resolve_external_boot_conflict",
    },
}


def test_operator_tools_follow_the_approved_split(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells
    split = {*_OPERATOR_SPLIT[3110], *_OPERATOR_SPLIT[3111], *_OPERATOR_SPLIT[3112]}
    ops = [c for c in cells if c.operation in _OPERATOR_TOOLS | split]
    owned = {o: {c.operation for c in ops if c.owner == o} for o in (2812, *_OPERATOR_SPLIT)}
    assert owned == {2812: _OPERATOR_TOOLS, **_OPERATOR_SPLIT}
    assert len([c for c in cells if c.owner == 2812]) == 124
    glass = [c for c in cells if c.operation in _OPERATOR_SPLIT[3112]]
    assert {c.owner for c in glass if c.guest_arch == "ppc64le"} == {2818}
    assert {c.owner for c in glass if c.guest_arch == "x86_64"} == {3112}
    overrides = {g.owner: set(g.role_overrides) for g in load_mapping().groups}
    assert overrides[3110] == {
        "inventory.clear_override",
        "ops.reconcile_now",
        "ops.set_queue_paused",
        "resources.drain",
    }
    assert overrides[3111] == {"ops.recover_build_use"}
    # Operator decision R5 (2026-10-13): recovery needs no provider authority.
    recovery = {
        (c.operation, c.roles)
        for c in cells
        if c.operation in _OPERATOR_SPLIT[3111] and c.kind == "functional"
    }
    assert recovery == {
        ("ops.build_uses_list", ("server",)),
        ("ops.recover_build_use", ("server", "worker")),
    }
    assert len([c for c in cells if c.owner == 3112]) == 136
    # Operator option A (#3112, 2026-10-07): only the resolve tools need the authority.
    base = ("server", "worker", "reconciler")
    roles = {(c.operation, c.roles) for c in glass if c.kind == "functional"}
    assert roles == {
        ("ops.force_release", base),
        ("ops.force_teardown", base),
        ("ops.resolve_recovery_orphan", (*base, "authority")),
        ("systems.resolve_external_boot_conflict", (*base, "authority")),
    }


def _tool_prefix(provider: str) -> str:
    return "tool/remote-libvirt" if provider == "remote-libvirt" else "tool"


def test_remote_tool_scenarios_never_share_a_local_node(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells
    tools = [c for c in cells if c.id.startswith("tool/")]
    remote = {c.scenario_id for c in tools if c.provider == "remote-libvirt"}
    assert remote and all(s.startswith("tool/remote-libvirt/") for s in remote)
    assert not remote & {c.scenario_id for c in tools if c.provider != "remote-libvirt"}
    local = next(c for c in tools if c.provider == "local-libvirt" and c.owner == 3062)
    mapping = load_mapping().model_copy(deep=True)
    mapping.implementations[local.scenario_id] = _DEEP_NODE
    mapped = build_contract(mapping=mapping, inventory=inventory).cells
    same = [c for c in mapped if c.operation == local.operation]
    assert _DEEP_NODE not in {c.node_id for c in same if c.provider == "remote-libvirt"}
    assert _DEEP_NODE in {c.node_id for c in same if c.provider == "local-libvirt"}


_ADR_0756_PAIRS = {
    ("investigations.list", "authorization"),
    ("investigations.list", "project-isolation"),
    ("artifacts.list", "project-isolation"),
}
# ADR-0756's 2026-10-14 amendment (operator decision R3, 2026-10-13).
_R3_LISTS = ("runs.list", "systems.list", "jobs.list", "allocations.list", "debug.list_sessions")


def test_filtered_list_boundaries_are_the_adr_named_pairs(inventory: Inventory) -> None:
    amended = {(t, b) for t in _R3_LISTS for b in ("authorization", "project-isolation")}
    assert _ADR_0756_PAIRS | amended == FILTERED_LIST_BOUNDARIES
    cells = build_contract(inventory=inventory).cells
    filtered = [
        c
        for c in cells
        if c.id.startswith("tool/")
        and c.kind == "rejection"
        and (c.operation, c.scenario_id.rsplit("/", 1)[1]) in FILTERED_LIST_BOUNDARIES
    ]
    named = [c for c in filtered if (c.operation, boundary_of(c)) in _ADR_0756_PAIRS]
    original = [c for c in named if c.exposure != "cli"]
    assert len(original) == 12
    assert {(c.configuration, c.exposure) for c in original} == {
        (configuration, exposure)
        for configuration in ("default", "recovery")
        for exposure in ("direct", "gateway")
    }
    assert all(c.owner == 3096 and c.node_id is not None for c in original)
    twins = [c for c in named if c.exposure == "cli"]
    assert len(twins) == 6 and all(c.owner == 3099 and c.node_id is not None for c in twins)
    for cell in filtered:
        assert cell.assertions == (boundary_of(cell), "unchanged-state", "cleanup")
        assert "empty page to the denied caller" in cell.observation
    assert {(c.operation, boundary_of(c)) for c in filtered} == FILTERED_LIST_BOUNDARIES
    others = [c for c in cells if c.kind == "rejection" and c not in filtered]
    assert not any("empty page" in c.observation for c in others)


def boundary_of(cell: Cell) -> str:
    return cell.scenario_id.rsplit("/", 1)[1]


def test_remote_publish_functional_cells_are_server_only_admission_refusals(
    inventory: Inventory,
) -> None:
    # ADR-0759: images.publish refuses remote-libvirt at admission, so the four functional cells
    # per architecture keep their identity and owner but observe the server alone.
    cells = build_contract(inventory=inventory).cells
    remote = [
        c for c in cells if c.operation == "images.publish" and c.provider == "remote-libvirt"
    ]
    refused = [c for c in remote if c.kind == "unsupported"]
    assert not [c for c in remote if c.kind == "functional"]
    assert sorted((c.configuration, c.exposure, c.host_arch) for c in refused) == sorted(
        (configuration, exposure, arch)
        for configuration in ("default", "recovery")
        for exposure in ("direct", "gateway")
        for arch in SUPPORTED_ARCHES
    )
    for cell in refused:
        assert cell.id == (
            f"tool/images.publish/default/{cell.configuration}/{cell.exposure}"
            f"/remote-libvirt/{cell.host_arch}/functional"
        )
        assert cell.scenario_id == "tool/remote-libvirt/images.publish/default/functional"
        assert cell.owner == (3120 if cell.host_arch == "x86_64" else 2818)
        assert cell.unsupported_reason and "ADR-0759" in cell.unsupported_reason
        assert (cell.roles, cell.guest_arch, cell.accelerator, cell.inputs, cell.image) == (
            ("server",),
            None,
            "none",
            (),
            None,
        )
        assert cell.assertions == ("effect", "cleanup")
    rejections = [c for c in remote if c.kind == "rejection"]
    assert len(rejections) == 3 * len(refused)
    assert all(c.guest_arch == c.host_arch and c.accelerator != "none" for c in rejections)
    local = [c for c in cells if c.operation == "images.publish" and c.provider == "local-libvirt"]
    assert {c.kind for c in local} == {"functional", "rejection"}


_SNAPSHOT_TOOLS = {
    "systems.delete_snapshot",
    "systems.list_snapshots",
    "systems.restore",
    "systems.snapshot",
}


def _capability_cells(cells: tuple[Cell, ...]) -> dict[str, Cell]:
    return {c.id: c for c in cells if c.id.endswith("/capability")}


def test_capability_cells_follow_the_support_verdict(inventory: Inventory) -> None:
    # Operator decision R2 (2026-10-13), ADR-0722: a supported capability is proven by its
    # functional cell, so only an unsupported verdict has a capability refusal to prove.
    cells = build_contract(inventory=inventory).cells
    by_id = {c.id: c for c in cells}
    capability = _capability_cells(cells)
    for cell in capability.values():
        sibling = by_id[cell.id.removesuffix("/capability") + "/functional"]
        assert sibling.kind == "unsupported" and sibling.unsupported_reason, cell.id
        assert cell.kind == "rejection"
        assert cell.assertions == ("capability", "unchanged-state", "cleanup")
    # Every capability the libvirt providers serve is supported, except fadump on x86_64,
    # which ArchTraits refuses after the provider verdict.
    assert sorted(capability) == sorted(
        f"tool/vmcore.fetch/fadump/{configuration}/{exposure}/local-libvirt/x86_64/capability"
        for configuration in ("default", "recovery")
        for exposure in ("direct", "gateway")
    )
    gated = {name for g in load_mapping().groups for name in g.capabilities}
    supported = [c for c in cells if c.operation in gated and c.kind == "functional"]
    assert supported and not any(c.id.rsplit("/", 1)[0] + "/capability" in by_id for c in supported)


def test_unsupported_capability_gains_its_refusal_cell(inventory: Inventory) -> None:
    support = dict(inventory.capabilities)
    support["local-libvirt"] = replace(support["local-libvirt"], supports_snapshots=False)
    baseline = build_contract(inventory=inventory).cells
    changed = build_contract(inventory=replace(inventory, capabilities=support)).cells
    added = _capability_cells(changed).keys() - _capability_cells(baseline).keys()
    assert sorted(added) == sorted(
        f"tool/{tool}/supports_snapshots/{configuration}/{exposure}/local-libvirt/{arch}/capability"
        for tool in _SNAPSHOT_TOOLS
        for configuration in ("default", "recovery")
        for exposure in ("direct", "gateway")
        for arch in inventory.arches
    )
    for cell_id in added:
        functional = next(c for c in changed if c.id == cell_id.rsplit("/", 1)[0] + "/functional")
        assert functional.kind == "unsupported"
        assert functional.unsupported_reason == "ProviderSupport.supports_snapshots"
    # Identities other than the added refusals are unchanged.
    assert {c.id for c in changed} - added == {c.id for c in baseline}


_POWER_SNAPSHOT_TOOLS = {"control.power", *_SNAPSHOT_TOOLS}
_LOCAL_CONTROL_TOOLS = {
    "control.capture_traffic",
    "control.diagnostic_sysrq",
    "control.watch_for_crash",
}


def test_power_snapshot_owners_follow_the_approved_split(inventory: Inventory) -> None:
    # Campaign scope (2026-10-07): #2815 proves the five local x86_64 power/snapshot tools; local
    # traffic/SysRq/watch is #3115's, remote #3116's and every ppc64le cell the POWER lane's.
    cells = [
        c
        for c in build_contract(inventory=inventory).cells
        if c.operation in _POWER_SNAPSHOT_TOOLS | _LOCAL_CONTROL_TOOLS
    ]
    for cell in cells:
        expected = (
            2818
            if cell.guest_arch == "ppc64le"
            else 3116
            if cell.provider == "remote-libvirt"
            else 3115
            if cell.operation in _LOCAL_CONTROL_TOOLS
            else 2815
        )
        assert cell.owner == expected, cell.id
    counts = {o: len([c for c in cells if c.owner == o]) for o in (2815, 2818, 3115, 3116)}
    assert counts == {2815: 100, 2818: 320, 3115: 60, 3116: 160}
    owned = [c for c in cells if c.owner == 2815]
    assert {c.kind for c in owned if c.id.endswith("/functional")} == {"functional"}
    assert len([c for c in owned if c.kind == "functional"]) == 20


_CAPTURE_TOOLS = {
    "control.force_crash",
    "introspect.from_vmcore",
    "introspect.run",
    "introspect.script",
    "postmortem.crash",
    "vmcore.fetch",
}


def test_capture_owners_follow_the_approved_split(inventory: Inventory) -> None:
    # Operator split (2026-10-06) and decision D1-A (2026-10-13): #2814 proves the x86_64 local
    # cells without the authority role; remote is #3114's and every ppc64le cell the POWER lane's.
    cells = [c for c in build_contract(inventory=inventory).cells if c.operation in _CAPTURE_TOOLS]
    assert cells and all("authority" not in c.roles for c in cells)
    for cell in cells:
        expected = (
            2818
            if cell.guest_arch == "ppc64le"
            else 3114
            if cell.provider == "remote-libvirt"
            else 2814
        )
        assert cell.owner == expected, cell.id
    local = [c for c in cells if c.owner == 2814]
    kinds = Counter(c.kind for c in local)
    assert (len(local), kinds["functional"], kinds["unsupported"]) == (164, 28, 4)
    remote = [c for c in cells if c.owner == 3114]
    kinds = Counter(c.kind for c in remote)
    assert (len(remote), kinds["functional"], kinds["unsupported"]) == (180, 28, 8)


def test_remote_capture_cells_bind_the_capture_carrier(inventory: Inventory) -> None:
    cells = [c for c in build_contract(inventory=inventory).cells if c.owner == 3114]
    assert len(cells) == 180 and {c.node_id for c in cells} == {_CAPTURE_NODE}


def test_handler_refused_capture_variants_are_unsupported(inventory: Inventory) -> None:
    # ADR-0722 (2026-10-20): vmcore.fetch refuses console/gdbstub itself, on every lane, so
    # those variants are unsupported and carry no capability refusal cell.
    cells = build_contract(inventory=inventory).cells
    ids = {c.id for c in cells}
    fetch = [c for c in cells if c.operation == "vmcore.fetch" and c.kind != "rejection"]
    refused = {c.id for c in fetch if c.unsupported_reason == HANDLER_REFUSAL}
    assert refused == {
        f"tool/vmcore.fetch/{method}/{configuration}/{exposure}/remote-libvirt/{arch}/functional"
        for method in ("console", "gdbstub")
        for configuration in ("default", "recovery")
        for exposure in ("direct", "gateway")
        for arch in inventory.arches
    }
    assert all(c.kind == "unsupported" for c in fetch if c.id in refused)
    assert not {i.removesuffix("/functional") + "/capability" for i in refused} & ids


def test_handler_refusal_follows_the_core_method_set(inventory: Inventory) -> None:
    advertised = {m.value for m in inventory.capabilities["remote-libvirt"].capture_methods}
    cells = build_contract(inventory=inventory).cells
    refused = {
        c.scenario_id.split("/")[-2]
        for c in cells
        if c.provider == "remote-libvirt" and c.unsupported_reason == HANDLER_REFUSAL
    }
    assert refused == advertised - {m.value for m in VMCORE_METHODS} == {"console", "gdbstub"}


_DEBUG_NODE = "tests/integration/test_debug_tool_cells_live.py::test_debug_tool_cell"


def test_debug_owners_and_carrier_follow_the_approved_split(inventory: Inventory) -> None:
    # Approved exclusions (2026-10-07): #2813 proves the x86_64 local debug cells; remote is
    # #3113's and every ppc64le cell the POWER lane's (#2818).
    cells = [
        c for c in build_contract(inventory=inventory).cells if c.operation.startswith("debug.")
    ]
    assert len({c.operation for c in cells}) == 21
    for cell in cells:
        expected = (
            2818
            if cell.guest_arch == "ppc64le"
            else 3113
            if cell.provider == "remote-libvirt"
            else 2813
        )
        assert cell.owner == expected, cell.id
    local = [c for c in cells if c.provider == "local-libvirt"]
    owned = [c for c in local if c.owner == 2813]
    kinds = Counter(c.kind for c in owned)
    assert (len(owned), kinds["functional"], kinds["rejection"]) == (440, 88, 352)
    assert not [c for c in owned if c.id.endswith("/capability")]
    assert len(local) == 880 and {c.node_id for c in local} == {_DEBUG_NODE}
    remote = [c for c in cells if c.provider == "remote-libvirt"]
    # As for local: the remote ppc64le cells (#2818's) share the provider-qualified ids.
    assert len(remote) == 880 and {c.node_id for c in remote} == {_DEBUG_NODE}


_CLI_GROUPS = (2811, *_SPLIT)


def test_cli_cells_twin_the_core_direct_cells(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells
    by_id = {c.id: c for c in cells}
    cli = [c for c in cells if c.exposure == "cli"]
    assert len(cli) == 454
    for cell in cli:
        twin = by_id[cell.id.replace("/cli/", "/direct/")]
        assert replace(cell, id=twin.id, exposure="direct", owner=twin.owner) == twin
        assert cell.owner == 3099 and cell.node_id is not None
    core = {t for g in load_mapping().groups if g.owner in _CLI_GROUPS for t in g.tools}
    assert {c.operation for c in cli} == core and len(core) == 52


def test_cli_exposure_validators_fail_closed(
    inventory: Inventory, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping = load_mapping().model_copy(deep=True)
    provider = next(g for g in mapping.groups if g.execution == "provider")
    provider.cli = True
    with pytest.raises(ValueError, match="service tools only"):
        build_contract(mapping=mapping, inventory=inventory)
    verbs = tuple(v for v in contract.GENERATED_VERBS if v.tool != "session.whoami")
    monkeypatch.setattr(contract, "GENERATED_VERBS", verbs)
    with pytest.raises(ValueError, match="no generated kdivectl verb"):
        build_contract(inventory=inventory)
