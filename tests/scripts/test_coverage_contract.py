from __future__ import annotations

from dataclasses import replace

import pytest

from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES
from scripts.coverage_campaign.contract import (
    Inventory,
    build_contract,
    load_mapping,
    read_inventory,
)


@pytest.fixture(scope="module")
def inventory() -> Inventory:
    return read_inventory()


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
_DEEP_NODE = "tests/integration/test_deep_lifecycle_live.py::test_deep_lifecycle"
_REMOTE_DEEP_NODE = (
    "tests/integration/test_remote_deep_lifecycle_live.py::test_remote_deep_lifecycle"
)
_LIFECYCLE_TOOLS = {
    "images.publish",
    "runs.boot",
    "runs.cancel",
    "runs.install",
    "runs.release_external_boot",
    "systems.authorize_ssh_key",
    "systems.check_ssh_reachable",
    "systems.provision",
    "systems.reprovision",
    "systems.ssh_info",
    "systems.teardown",
}


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
        (_LIFECYCLE_TOOLS, "local-libvirt", 3062),
        (_LIFECYCLE_TOOLS, "remote-libvirt", 3080),
        (deep, "local-libvirt", 2809),
        (deep, "remote-libvirt", 2810),
    ):
        assert owners(operations, provider, "x86_64") == {owner}
    for provider in ("local-libvirt", "remote-libvirt"):
        assert owners(_LIFECYCLE_TOOLS | deep, provider, "ppc64le") == {2818}
    assert len([c for c in cells if c.owner == 2809]) == 8
    assert len([c for c in cells if c.owner == 2810]) == 8
    assert len([c for c in cells if c.owner == 3080]) == 216
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
    bound = {"image-smoke", "deep-lifecycle", "host-install"}
    assert all(c.node_id is None for c in contract.cells if c.operation not in bound)
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
    assert {c.node_id for c in same if c.provider == "remote-libvirt"} == {None}
    assert _DEEP_NODE in {c.node_id for c in same if c.provider == "local-libvirt"}
