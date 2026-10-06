"""Independent, reviewed coverage obligations; inventory is never execution evidence."""

from __future__ import annotations

import ast
import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass, fields, is_dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES, arch_traits
from kdive.images.rootfs.catalog import RootfsCatalogEntry, load_rootfs_catalog
from kdive.providers.core.runtime import ProviderSupport
from kdive.providers.local_libvirt.composition import build_runtime as build_local_runtime
from kdive.providers.remote_libvirt.composition import build_runtime as build_remote_runtime
from kdive.security.secrets.secret_registry import SecretRegistry
from scripts.coverage_campaign.evidence import NodeID
from scripts.coverage_campaign.gridgen import CensusRow, generate_rows

_ROOT = Path(__file__).resolve().parents[2]
_MAPPING = Path(__file__).with_name("obligations.toml")
_KERNEL_INPUTS = (
    "image_sha256",
    "kernel_sha256",
    "kernel_source_sha",
    "kernel_config_sha256",
    "compiler_id",
    "kernel_build_id",
)


class OperationGroup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    owner: int = Field(gt=0)
    execution: Literal["service", "provider"]
    kernel: bool = False
    authority: bool = False
    tools: dict[str, str]
    capabilities: dict[str, str] = Field(default_factory=dict)
    role_overrides: dict[str, list[Literal["server", "worker", "reconciler", "authority"]]] = Field(
        default_factory=dict
    )


class ContractMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    groups: list[OperationGroup]
    implementations: dict[str, str]


@dataclass(frozen=True)
class Inventory:
    tools: tuple[CensusRow, ...]
    images: dict[str, RootfsCatalogEntry]
    arches: frozenset[str]
    capabilities: dict[str, ProviderSupport]


@dataclass(frozen=True)
class Cell:
    id: str
    scenario_id: str
    owner: int
    operation: str
    observation: str
    assertions: tuple[str, ...]
    node_id: str | None = None
    configuration: str = "default"
    exposure: str = "direct"
    provider: str = "service"
    host_arch: str | None = None
    guest_arch: str | None = None
    accelerator: str = "none"
    image: str | None = None
    family: str | None = None
    kind: Literal["functional", "rejection", "unsupported"] = "functional"
    roles: tuple[str, ...] = ("server",)
    inputs: tuple[str, ...] = ()
    unsupported_reason: str | None = None


@dataclass(frozen=True)
class Contract:
    version: int
    matrix_sha256: str
    cells: tuple[Cell, ...]
    inventory: Inventory


def _canonical(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical(asdict(value))
    if isinstance(value, BaseModel):
        return _canonical(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (set, frozenset)):
        return sorted((_canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, (tuple, list)):
        return [_canonical(v) for v in value]
    return value


def digest(value: object) -> str:
    encoded = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def load_mapping(path: Path = _MAPPING) -> ContractMapping:
    return ContractMapping.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


def read_inventory() -> Inventory:
    registry = SecretRegistry()
    return Inventory(
        tuple(generate_rows()),
        load_rootfs_catalog(),
        SUPPORTED_ARCHES,
        {
            "local-libvirt": build_local_runtime(secret_registry=registry).support,
            "remote-libvirt": build_remote_runtime(secret_registry=registry).support,
        },
    )


def _validate_mapping(mapping: ContractMapping, inventory: Inventory) -> None:
    names = [name for group in mapping.groups for name in group.tools]
    if len(names) != len(set(names)):
        raise ValueError("duplicate tool mapping; assign each registered tool once")
    registered = {row.tool for row in inventory.tools}
    if registered - set(names):
        raise ValueError("unmapped registered tool; add an explicit observation and owner")
    if set(names) - registered:
        raise ValueError("unknown tool mapping; remove obsolete names")
    if not registered or len(registered) != len(inventory.tools):
        raise ValueError("empty or duplicate registry census")
    support_fields = {f.name for f in fields(ProviderSupport)}
    for group in mapping.groups:
        if not group.tools or any(not text.strip() for text in group.tools.values()):
            raise ValueError("tool mapping needs a concrete observable assertion")
        if (set(group.capabilities) | set(group.role_overrides)) - set(group.tools):
            raise ValueError("capability mapping names an unknown tool")
        if any(name.split(".")[0] not in support_fields for name in group.capabilities.values()):
            raise ValueError("unknown provider capability owner")
    if not inventory.arches or any(
        image.arch not in inventory.arches for image in inventory.images.values()
    ):
        raise ValueError("catalog architecture has no supported architecture owner")


def _capability_variants(
    group: OperationGroup, tool: str, support: ProviderSupport
) -> list[tuple[str, bool, str | None]]:
    capability = group.capabilities.get(tool)
    if capability is None:
        return [("default", True, None)]
    field, _, selected = capability.partition(".")
    value = getattr(support, field)
    if isinstance(value, bool):
        return [(field, value, f"ProviderSupport.{field}")]
    if selected:
        return [(selected, selected in value, f"ProviderSupport.{field}")]
    return [(str(v), True, f"ProviderSupport.{field}") for v in sorted(value)] or [
        (field, False, f"ProviderSupport.{field}")
    ]


def _tool_cells(row: CensusRow, group: OperationGroup, inventory: Inventory) -> list[Cell]:
    cells: list[Cell] = []
    lanes = (
        [("service", None)]
        if group.execution == "service"
        else [
            (provider, arch)
            for provider in sorted(inventory.capabilities)
            for arch in sorted(inventory.arches)
        ]
    )
    for provider, arch in lanes:
        variants = (
            [("default", True, None)]
            if provider == "service"
            else _capability_variants(group, row.tool, inventory.capabilities[provider])
        )
        for variant, supported, source in variants:
            accelerator = "none" if arch is None else ("kvm" if arch == "x86_64" else "kvm-hv")
            if variant == "fadump" and arch is not None and not arch_traits(arch).fadump_capture:
                supported, source = False, "ArchTraits.fadump_capture; native ppc64le/KVM-HV only"
            for configuration in row.configurations:
                for exposure in ("direct", "gateway"):
                    scenario = f"tool/{row.tool}/{variant}"
                    identity = (
                        f"{scenario}/{configuration}/{exposure}/{provider}/{arch or 'service'}"
                    )
                    # Provider-qualified so a local-only node never marks a remote cell (#2810).
                    scenario_id = (
                        f"tool/{provider}/{row.tool}/{variant}"
                        if provider == "remote-libvirt"
                        else scenario
                    )
                    roles = ("server",) if arch is None else ("server", "worker", "reconciler")
                    roles = tuple(group.role_overrides.get(row.tool, roles))
                    if group.authority:
                        roles = (*roles, "authority")
                    owner = group.owner
                    # Lifecycle and break-glass ppc64le cells wait for the POWER lane (#2818).
                    if group.owner in (3062, 3112) and arch == "ppc64le":
                        owner = 2818
                    elif group.owner == 3062 and provider == "remote-libvirt":
                        owner = 3080
                    cell = Cell(
                        identity + "/functional",
                        scenario_id + "/functional",
                        owner,
                        row.tool,
                        group.tools[row.tool],
                        ("effect", "cleanup"),
                        configuration=configuration,
                        exposure=exposure,
                        provider=provider,
                        host_arch=arch,
                        guest_arch=arch,
                        accelerator=accelerator,
                        roles=roles,
                        inputs=_KERNEL_INPUTS if group.kernel else (),
                        kind="functional" if supported else "unsupported",
                        unsupported_reason=None if supported else source,
                    )
                    cells.append(cell)
                    boundaries = ["authentication"]
                    if row.scopes:
                        boundaries.append("authorization")
                    if any(scope.startswith("project_") for scope in row.scopes):
                        boundaries.append("project-isolation")
                    if row.parameters.get("properties"):
                        boundaries.append("validation")
                    if group.capabilities.get(row.tool):
                        boundaries.append("capability")
                    cells.extend(
                        replace(
                            cell,
                            id=identity + "/" + boundary,
                            scenario_id=scenario_id + "/" + boundary,
                            kind="rejection",
                            observation=(
                                f"Verify {boundary} rejection for {row.tool} "
                                "and unchanged protected state."
                            ),
                            assertions=(boundary, "unchanged-state", "cleanup"),
                            roles=("server",),
                            inputs=(),
                            unsupported_reason=None,
                        )
                        for boundary in boundaries
                    )
    return cells


def image_family(image: RootfsCatalogEntry) -> str:
    return (
        "fedora"
        if image.distro == "fedora"
        else ("enterprise" if image.family == "rhel" else image.family)
    )


def _native_cell(
    operation: str,
    owner: int,
    arch: str,
    provider: str,
    observation: str,
    *,
    image: str | None = None,
    family: str | None = None,
) -> Cell:
    identity = f"{operation}/{provider}/{arch}"
    return Cell(
        id=identity,
        scenario_id=operation,
        owner=owner,
        operation=operation,
        observation=observation,
        assertions=("effect", "terminal", "cleanup"),
        provider=provider,
        host_arch=arch,
        guest_arch=arch,
        accelerator="kvm" if arch == "x86_64" else "kvm-hv",
        roles=("server", "worker", "reconciler"),
        inputs=_KERNEL_INPUTS,
        image=image,
        family=family,
    )


def _matrix_cells(inventory: Inventory) -> list[Cell]:
    cells = []
    for name, entry in sorted(inventory.images.items()):
        cell = _native_cell(
            "image-smoke",
            2808 if entry.arch == "x86_64" else 2818,
            entry.arch,
            "local-libvirt",
            "Acquire/customize image, authenticate, verify OS/architecture, reboot "
            "and reclaim domain/storage/capacity.",
            image=name,
            family=image_family(entry),
        )
        assertions = (
            "acquire",
            "first-boot",
            "authenticated-access",
            "os-architecture",
            "reboot",
            "cleanup",
        )
        if entry.kind == "build":
            assertions = (*assertions, "build-toolchain")
        cells.append(
            replace(
                cell,
                id=f"image-smoke/{name}/{entry.arch}",
                assertions=assertions,
                inputs=("image_sha256",),
            )
        )
    families = sorted({(image_family(image), image.arch) for image in inventory.images.values()})
    for family, arch in families:
        for provider in sorted(inventory.capabilities):
            cell = _native_cell(
                "deep-lifecycle",
                2818 if arch == "ppc64le" else (2810 if provider == "remote-libvirt" else 2809),
                arch,
                provider,
                "Upload each baseline, install, boot, verify actual build and modules, "
                "reconnect and reclaim owned resources.",
                family=family,
            )
            for baseline in ("longterm", "stable"):
                cells.append(
                    replace(
                        cell,
                        id=f"{cell.id}/{family}/{baseline}",
                        scenario_id=f"deep-lifecycle/{provider}/{baseline}",
                        assertions=(
                            "upload",
                            "install",
                            "boot-identity",
                            "modules",
                            "reconnect",
                            "cleanup",
                        ),
                    )
                )
    for guest in sorted(inventory.arches):
        for host in sorted(inventory.arches - {guest}):
            cell = _native_cell(
                "tcg-upload-boot",
                2818,
                guest,
                "local-libvirt",
                "Upload and boot the foreign guest; verify its kernel identity and owned cleanup.",
            )
            cells.append(
                replace(
                    cell, id=f"tcg-upload-boot/{host}/{guest}", host_arch=host, accelerator="tcg"
                )
            )
        for family in ("debian", "fedora", "enterprise"):
            cell = _native_cell(
                "host-install",
                2807 if guest == "x86_64" else 2818,
                guest,
                "local-libvirt",
                "Install on an exclusive clean host, provision/boot, repeat setup and prove "
                "another boot with confinement enabled.",
                family=family,
            )
            cells.append(
                replace(
                    cell,
                    id=f"{cell.id}/{family}",
                    assertions=(
                        "clean-install",
                        "first-boot",
                        "repeat-setup",
                        "second-boot",
                        "confinement",
                        "cleanup",
                    ),
                )
            )
        for provider in sorted(inventory.capabilities):
            for failure in (
                "worker-interruption",
                "lost-response",
                "backing-service",
                "minimum-cpu-ram",
                "insufficient-disk",
                "slow-boot",
            ):
                cell = _native_cell(
                    "failure-resource",
                    2816,
                    guest,
                    provider,
                    "Prove operation-specific recovery, no duplicate resources, "
                    "protected-state retention and cleanup.",
                )
                cells.append(
                    replace(
                        cell,
                        id=f"{cell.id}/{failure}",
                        scenario_id=f"failure-resource/{failure}",
                        assertions=(failure, "no-duplicates", "protected-state", "cleanup"),
                    )
                )
        for case in ("case-1", "case-2"):
            for revision in ("broken", "fixed"):
                cell = _native_cell(
                    "kernel-corpus",
                    2817,
                    guest,
                    "local-libvirt",
                    "Run the same qualified reproducer/config; verify the specific broken or fixed "
                    "outcome and useful debug/crash evidence.",
                )
                cells.append(
                    replace(
                        cell,
                        id=f"{cell.id}/{case}/{revision}",
                        scenario_id=f"kernel-corpus/{case}/{revision}",
                        assertions=(
                            revision,
                            "same-reproducer-config",
                            "debug-or-crash-evidence",
                            "cleanup",
                        ),
                    )
                )
    return cells


def _validate_node(node: str) -> None:
    TypeAdapter(NodeID).validate_python(node, strict=True)
    parts = node.split("[", 1)[0].split("::")
    relative = Path(parts[0])
    if (
        len(parts) < 2
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[:1] != ("tests",)
    ):
        raise ValueError("invalid pytest node; use a repository test function")
    path = _ROOT / relative
    if (
        path.suffix != ".py"
        or not path.is_file()
        or not path.resolve().is_relative_to(_ROOT / "tests")
    ):
        raise ValueError("pytest node file is absent or outside tests")
    body = ast.parse(path.read_text(encoding="utf-8")).body
    for name in parts[1:]:
        matches = [
            n
            for n in body
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == name
        ]
        if len(matches) != 1:
            raise ValueError("pytest node does not name an existing test function")
        body = matches[0].body
    if not isinstance(matches[0], (ast.FunctionDef, ast.AsyncFunctionDef)) or not name.startswith(
        "test_"
    ):
        raise ValueError("pytest node must identify a test function")


def build_contract(
    *, mapping: ContractMapping | None = None, inventory: Inventory | None = None
) -> Contract:
    mapping = load_mapping() if mapping is None else mapping
    inventory = read_inventory() if inventory is None else inventory
    inventory = replace(inventory, tools=tuple(sorted(inventory.tools, key=lambda row: row.tool)))
    _validate_mapping(mapping, inventory)
    groups = {name: group for group in mapping.groups for name in group.tools}
    cells = [
        cell for row in inventory.tools for cell in _tool_cells(row, groups[row.tool], inventory)
    ]
    cells.extend(_matrix_cells(inventory))
    scenarios = {cell.scenario_id for cell in cells}
    if set(mapping.implementations) - scenarios:
        raise ValueError("implementation names an unknown scenario")
    for node in mapping.implementations.values():
        _validate_node(node)
    cells = [replace(cell, node_id=mapping.implementations.get(cell.scenario_id)) for cell in cells]
    if len({cell.id for cell in cells}) != len(cells):
        raise ValueError("duplicate required cell identity")
    ordered = tuple(sorted(cells, key=lambda cell: cell.id))
    identity = digest(
        {
            "version": 1,
            "inventory": inventory,
            "architecture_traits": {arch: arch_traits(arch) for arch in sorted(inventory.arches)},
            "mapping": mapping,
            "cells": ordered,
        }
    )
    return Contract(1, identity, ordered, inventory)
