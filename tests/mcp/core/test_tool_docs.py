"""The ADR-0047 documentation guard, over the live FastMCP registry.

Builds the app with a null pool + a local-keypair verifier (the service-test
path; needs no DB and no OIDC env), then asserts every tool is fully
documented, the destructive hint matches the reviewed set, and every
`implemented` tool is assigned to a non-live behavior test module.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import textwrap
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast, get_type_hints

import pytest
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.tools.function_tool import FunctionTool
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import SystemState
from kdive.mcp.assembly.app import build_app
from kdive.mcp.tools import _docmeta
from kdive.profiles.build import BuildProfile
from kdive.security.secrets.secret_registry import SecretRegistry
from scripts.gen_tool_reference import (
    _BUILD_PROFILE_EXAMPLES,
    _MAX_SCHEMA_DEPTH,
    _is_structured,
    render_param_detail,
    render_schema_type,
)
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair

_HERE = Path(__file__).resolve()
_REPO_ROOT = next(parent for parent in _HERE.parents if (parent / "pyproject.toml").is_file())
_NON_LIVE_MARKERS = ("pytest.mark.live_vm", "pytest.mark.live_stack")
_BEHAVIOR_TESTS_BY_TOOL = {
    "accounting.estimate": ("tests/mcp/accounting/test_accounting_tools.py",),
    "accounting.report_all_projects": ("tests/mcp/accounting/test_accounting_report.py",),
    "accounting.report_granted_set": ("tests/mcp/accounting/test_accounting_report.py",),
    "accounting.set_budget": ("tests/mcp/accounting/test_accounting_admin_tools.py",),
    "accounting.set_quota": ("tests/mcp/accounting/test_accounting_admin_tools.py",),
    "accounting.usage_investigation": ("tests/mcp/accounting/test_accounting_usage.py",),
    "accounting.usage_project": ("tests/mcp/accounting/test_accounting_usage.py",),
    "allocations.get": ("tests/mcp/lifecycle/test_allocations_tools.py",),
    "allocations.list": ("tests/mcp/lifecycle/test_allocations_tools.py",),
    "allocations.release": ("tests/mcp/lifecycle/test_allocations_reconcile.py",),
    "allocations.renew": ("tests/mcp/lifecycle/test_allocations_renew.py",),
    "allocations.request": ("tests/mcp/lifecycle/test_allocations_tools.py",),
    "allocations.wait": ("tests/mcp/lifecycle/test_allocations_tools.py",),
    "artifacts.create_run_upload": ("tests/mcp/lifecycle/test_create_upload_tool.py",),
    "artifacts.create_system_upload": ("tests/mcp/lifecycle/test_create_upload_tool.py",),
    "artifacts.expected_uploads": ("tests/mcp/catalog/test_expected_uploads_tool.py",),
    "artifacts.feature_config_requirements": (
        "tests/mcp/catalog/test_feature_config_requirements_tool.py",
    ),
    "artifacts.fetch_raw": ("tests/mcp/catalog/test_raw_fetch_tool.py",),
    "artifacts.find": ("tests/mcp/catalog/test_artifacts_tools.py",),
    "artifacts.get": ("tests/mcp/catalog/test_artifacts_tools.py",),
    "artifacts.list": ("tests/mcp/catalog/test_artifacts_tools.py",),
    "audit.query": ("tests/mcp/ops/test_audit_query.py",),
    "control.force_crash": ("tests/mcp/lifecycle/test_control_tools.py",),
    "control.power": ("tests/mcp/lifecycle/test_control_tools.py",),
    "control.diagnostic_sysrq": ("tests/mcp/lifecycle/test_control_tools.py",),
    "control.watch_for_crash": ("tests/mcp/lifecycle/test_control_tools.py",),
    "control.capture_traffic": ("tests/mcp/lifecycle/test_control_tools.py",),
    "debug.clear_breakpoint": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.backtrace": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.continue": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.step": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.next": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.step_instruction": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.finish": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.disassemble": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.end_session": (
        "tests/mcp/debug/test_debug_tools.py",
        "tests/mcp/debug/test_debug_ops.py",
    ),
    "debug.get_session": ("tests/mcp/debug/test_debug_session_read.py",),
    "debug.interrupt": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.list_breakpoints": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.list_sessions": ("tests/mcp/debug/test_debug_session_read.py",),
    "debug.read_frame": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.read_memory": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.read_registers": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.resolve_symbol": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.set_breakpoint": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.set_watchpoint": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.list_watchpoints": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.clear_watchpoint": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.list_modules": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.load_module_symbols": ("tests/mcp/debug/test_debug_ops.py",),
    "debug.start_session": ("tests/mcp/debug/test_debug_tools.py",),
    "fixtures.list": ("tests/mcp/catalog/test_fixtures_list.py",),
    "fixtures.validate": ("tests/mcp/catalog/test_fixtures_validate.py",),
    "images.build": ("tests/mcp/ops/test_images_tools.py",),
    "images.delete": ("tests/mcp/ops/test_images_tools.py",),
    "images.describe": ("tests/mcp/catalog/test_images_describe.py",),
    "images.extend": ("tests/mcp/ops/test_images_tools.py",),
    "images.kernel_config": ("tests/mcp/catalog/test_images_kernel_config.py",),
    "images.list": ("tests/mcp/catalog/test_images_list.py",),
    "images.prune_expired": ("tests/mcp/ops/test_images_tools.py",),
    "images.publish": ("tests/mcp/ops/test_images_tools.py",),
    "images.upload": ("tests/mcp/ops/test_images_tools.py",),
    "inventory.clear_override": ("tests/mcp/ops/test_inventory_clear_override.py",),
    "inventory.list": ("tests/mcp/ops/test_inventory_list.py",),
    "investigations.close": ("tests/mcp/lifecycle/test_investigations_tools.py",),
    "investigations.get": ("tests/mcp/lifecycle/test_investigations_tools.py",),
    "investigations.link": ("tests/mcp/lifecycle/test_investigations_tools.py",),
    "investigations.list": ("tests/mcp/lifecycle/test_investigations_tools.py",),
    "investigations.open": ("tests/mcp/lifecycle/test_investigations_tools.py",),
    "investigations.set": ("tests/mcp/lifecycle/test_investigations_tools.py",),
    "investigations.unlink": ("tests/mcp/lifecycle/test_investigations_tools.py",),
    "jobs.cancel": ("tests/mcp/jobs/test_jobs_tools.py",),
    "jobs.get": ("tests/mcp/jobs/test_jobs_tools.py",),
    "jobs.list": ("tests/mcp/jobs/test_jobs_tools.py",),
    "jobs.wait": ("tests/mcp/jobs/test_jobs_tools.py",),
    "introspect.from_vmcore": ("tests/mcp/debug/test_introspect_tools.py",),
    "introspect.run": ("tests/mcp/debug/test_introspect_tools.py",),
    "introspect.script": ("tests/mcp/debug/test_introspect_tools.py",),
    "ops.diagnostics": ("tests/mcp/ops/test_diagnostics.py",),
    "ops.force_release": ("tests/mcp/ops/test_breakglass.py",),
    "ops.force_teardown": ("tests/mcp/ops/test_breakglass.py",),
    "ops.jobs_list": ("tests/mcp/ops/test_queue_tools.py",),
    "ops.queue_pause": ("tests/mcp/ops/test_queue_tools.py",),
    "ops.queue_resume": ("tests/mcp/ops/test_queue_tools.py",),
    "ops.reconcile_now": ("tests/mcp/ops/test_reconcile_now.py",),
    "ops.export_cost_classes": ("tests/mcp/ops/test_ops_tuning.py",),
    "ops.export_systems_toml": ("tests/mcp/ops/test_ops_tuning.py",),
    "ops.reconcile_systems": ("tests/mcp/ops/test_reconcile_systems.py",),
    "ops.set_cost_class_coeff": ("tests/mcp/ops/test_ops_tuning.py",),
    "ops.set_host_capacity": ("tests/mcp/ops/test_ops_tuning.py",),
    "ops.tool_trail": ("tests/mcp/ops/test_tool_trail.py",),
    "reports.generate_all_projects": ("tests/mcp/tools/reports/test_generate.py",),
    "reports.generate_granted_set": ("tests/mcp/tools/reports/test_generate.py",),
    "resources.availability": ("tests/mcp/catalog/test_availability_tools.py",),
    "resources.cordon": ("tests/mcp/catalog/test_resources_tools.py",),
    "resources.deregister": ("tests/mcp/ops/test_resources_mutation.py",),
    "resources.describe": ("tests/mcp/catalog/test_resources_tools.py",),
    "resources.drain": ("tests/mcp/catalog/test_resources_tools.py",),
    "resources.list": ("tests/mcp/catalog/test_resources_tools.py",),
    "resources.register_fault_inject": ("tests/mcp/ops/test_resources_mutation.py",),
    "resources.register_local_libvirt": ("tests/mcp/ops/test_resources_mutation.py",),
    "resources.register_remote_libvirt": ("tests/mcp/ops/test_resources_mutation.py",),
    "resources.renew": ("tests/mcp/ops/test_resources_mutation.py",),
    "resources.set_status": ("tests/mcp/catalog/test_resources_tools.py",),
    "resources.uncordon": ("tests/mcp/catalog/test_resources_tools.py",),
    "postmortem.crash": ("tests/mcp/lifecycle/test_vmcore_tools.py",),
    "postmortem.triage": ("tests/mcp/lifecycle/test_vmcore_tools.py",),
    "projects.list": ("tests/mcp/identity/test_projects_tools.py",),
    "session.whoami": ("tests/mcp/identity/test_session_tools.py",),
    "runs.bind": ("tests/mcp/lifecycle/test_runs_tools.py",),
    "runs.boot": ("tests/mcp/lifecycle/test_runs_tools.py",),
    "runs.cancel": ("tests/mcp/lifecycle/test_runs_tools.py",),
    "runs.complete_build": ("tests/mcp/lifecycle/test_complete_build_tool.py",),
    "runs.create": ("tests/mcp/lifecycle/test_runs_tools.py",),
    "runs.get": ("tests/mcp/lifecycle/test_runs_tools.py",),
    "runs.install": ("tests/mcp/lifecycle/test_runs_tools.py",),
    "runs.list": ("tests/mcp/lifecycle/test_runs_list.py",),
    "secrets.list": ("tests/mcp/ops/test_secrets_list.py",),
    "shapes.delete": ("tests/mcp/catalog/test_shapes_tools.py",),
    "shapes.list": ("tests/mcp/catalog/test_shapes_tools.py",),
    "shapes.set": ("tests/mcp/catalog/test_shapes_tools.py",),
    "systems.define": ("tests/mcp/lifecycle/test_systems_tools.py",),
    "systems.get": ("tests/mcp/lifecycle/test_systems_tools.py",),
    "systems.authorize_ssh_key": ("tests/mcp/lifecycle/test_systems_ssh_access.py",),
    "systems.check_ssh_reachable": ("tests/mcp/lifecycle/test_systems_ssh_access.py",),
    "systems.ssh_info": ("tests/mcp/lifecycle/test_systems_ssh_access.py",),
    "systems.list": ("tests/mcp/lifecycle/test_systems_list.py",),
    "systems.profile_examples": ("tests/mcp/lifecycle/test_systems_profile_examples.py",),
    "systems.provision": ("tests/mcp/lifecycle/test_systems_tools.py",),
    "systems.provision_defined": ("tests/mcp/lifecycle/test_systems_tools.py",),
    "systems.reprovision": ("tests/mcp/lifecycle/test_systems_tools.py",),
    "systems.teardown": ("tests/mcp/lifecycle/test_systems_tools.py",),
    "systems.snapshot": ("tests/mcp/lifecycle/test_systems_snapshot.py",),
    "systems.restore": ("tests/mcp/lifecycle/test_systems_snapshot.py",),
    "systems.list_snapshots": ("tests/mcp/lifecycle/test_systems_snapshot.py",),
    "systems.delete_snapshot": ("tests/mcp/lifecycle/test_systems_snapshot.py",),
    "tools.invoke": ("tests/mcp/tools/test_gateway_invoke.py",),
    "tools.search": ("tests/mcp/tools/test_gateway_search.py",),
    "vmcore.fetch": ("tests/mcp/lifecycle/test_vmcore_tools.py",),
    "vmcore.list": ("tests/mcp/lifecycle/test_vmcore_tools.py",),
}


def _build_tools() -> list[FunctionTool]:
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    kp = make_keypair()
    verifier = JWTVerifier(public_key=kp.public_key, issuer=ISSUER, audience=AUDIENCE)
    app = build_app(pool, verifier=verifier, secret_registry=SecretRegistry())
    # list_tools() is typed as Sequence[mcp.types.Tool] but the fastmcp runtime
    # returns list[FunctionTool] — cast to the concrete type so the rest of the
    # module can access .fn / .meta / .annotations without type errors.
    return cast(list[FunctionTool], asyncio.run(app.list_tools()))


def _reaches_symbol(fn: Callable[..., Any], target: str) -> bool:
    """Whether ``fn`` calls ``target`` directly or via a delegate it transitively calls.

    The `@app.tool` wrappers are 1:1 delegators: the security-relevant call
    (``assert_destructive_allowed``) lives one frame deeper, in the module-level handler the
    wrapper invokes (`force_crash_system`, `reprovision_system`), never in the wrapper body.
    Parsing only ``fn`` would miss it — so follow each called ``Name`` that resolves to a
    function in ``fn``'s own module globals (a nested closure still carries its module's
    globals). Termination is the ``seen`` set over the finite function graph; there is no
    depth cap, because a numeric horizon would silently fail open (report "no gate reached")
    for a call buried below it — the very vacuity this backstop exists to prevent.
    """
    seen: set[int] = set()

    def _method_from_factory_return(
        factory: Callable[..., Any],
        attr: str,
        nonlocals: Mapping[str, Any],
    ) -> Callable[..., Any] | None:
        if inspect.ismethod(factory):
            factory = factory.__func__
        if not inspect.isfunction(factory):
            return None
        try:
            hints = get_type_hints(factory, globalns=factory.__globals__, localns=nonlocals)
        except (NameError, TypeError) as _exc:
            return None
        owner_type = hints.get("return")
        delegate = getattr(owner_type, attr, None)
        return delegate if callable(delegate) else None

    def _walk(f: Callable[..., Any]) -> bool:
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(f)))
        except (OSError, TypeError) as _exc:
            return False
        glb = getattr(f, "__globals__", {})
        try:
            nonlocals = inspect.getclosurevars(f).nonlocals
        except TypeError:
            nonlocals = {}
        local_calls: set[str] = set()
        attribute_calls: list[Callable[..., Any]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                callee = node.func
                if isinstance(callee, ast.Name):
                    if callee.id == target:
                        return True
                    local_calls.add(callee.id)
                elif isinstance(callee, ast.Attribute) and callee.attr == target:
                    return True
                elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
                    owner = nonlocals.get(callee.value.id, glb.get(callee.value.id))
                    delegate = getattr(owner, callee.attr, None)
                    if callable(delegate):
                        attribute_calls.append(delegate)
                elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Call):
                    factory_call = callee.value.func
                    if isinstance(factory_call, ast.Name):
                        factory = nonlocals.get(factory_call.id, glb.get(factory_call.id))
                        if callable(factory):
                            delegate = _method_from_factory_return(factory, callee.attr, nonlocals)
                            if delegate is not None:
                                attribute_calls.append(delegate)
                    elif isinstance(factory_call, ast.Attribute) and isinstance(
                        factory_call.value, ast.Name
                    ):
                        owner = nonlocals.get(factory_call.value.id, glb.get(factory_call.value.id))
                        factory = getattr(owner, factory_call.attr, None)
                        if callable(factory):
                            delegate = _method_from_factory_return(factory, callee.attr, nonlocals)
                            if delegate is not None:
                                attribute_calls.append(delegate)
        for name in local_calls:
            delegate = glb.get(name)
            if inspect.isfunction(delegate) and id(delegate) not in seen:
                seen.add(id(delegate))
                if _walk(delegate):
                    return True
        for delegate in attribute_calls:
            if id(delegate) not in seen:
                seen.add(id(delegate))
                if _walk(delegate):
                    return True
        return False

    return _walk(fn)


TOOLS = _build_tools()


def test_every_tool_has_a_description() -> None:
    missing = [t.name for t in TOOLS if not (t.description or "").strip()]
    assert not missing, f"tools missing a description: {missing}"


def test_every_parameter_has_a_description() -> None:
    offenders: list[str] = []
    for t in TOOLS:
        props = (t.parameters or {}).get("properties", {})
        for param, schema in props.items():
            if not (schema.get("description") or "").strip():
                offenders.append(f"{t.name}:{param}")
    assert not offenders, f"parameters missing a description: {offenders}"


def _object_schema(schema: dict[str, object]) -> dict[str, object]:
    if "anyOf" not in schema:
        return schema
    choices = schema["anyOf"]
    assert isinstance(choices, list)
    for choice in choices:
        assert isinstance(choice, dict)
        if choice.get("type") == "object":
            return cast(dict[str, object], choice)
    raise AssertionError(f"no object schema in {schema!r}")


def _request_properties(params: dict[str, object]) -> dict[str, object]:
    properties = cast(dict[str, object], params["properties"])
    request_schema = _object_schema(cast(dict[str, object], properties["request"]))
    ref = request_schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        defs = cast(dict[str, object], params["$defs"])
        request_schema = cast(dict[str, object], defs[ref.removeprefix("#/$defs/")])
    return cast(dict[str, object], request_schema["properties"])


def test_filtered_list_tools_use_request_payloads() -> None:
    tools = {t.name: t for t in TOOLS}
    expected_fields = {
        "accounting.report_all_projects": {"group_by", "window"},
        "accounting.report_granted_set": {"projects", "group_by", "window"},
        "artifacts.find": {"artifact_id", "query", "byte_offset", "max_bytes", "direction"},
        "artifacts.get": {"artifact_id", "byte_offset", "max_bytes", "direction"},
        "debug.list_sessions": {"run_id", "system_id", "project", "state", "limit", "cursor"},
        "investigations.list": {"project", "state", "limit", "cursor"},
        "resources.list": {"kind", "limit", "cursor"},
    }

    for tool_name, fields in expected_fields.items():
        params = tools[tool_name].parameters
        assert set(params["properties"]) == {"request"}
        assert set(_request_properties(params)) == fields


def test_composite_mutations_use_flat_top_level_params() -> None:
    # ADR-0372: every mutation tool takes flat top-level params, never a nested `request`
    # wrapper. This pins the flat schema for a representative slice of the flattened surface.
    tools = {t.name: t for t in TOOLS}
    flat_fields = {
        "accounting.set_quota": {
            "project",
            "max_concurrent_allocations",
            "max_concurrent_systems",
            "max_pending_allocations",
        },
        "investigations.open": {
            "project",
            "title",
            "description",
            "external_refs",
            "idempotency_key",
        },
        "shapes.set": {"name", "vcpus", "memory_mb", "disk_gb", "pcie_match"},
        "runs.complete_build": {
            "run_id",
            "cmdline",
            "build_id",
            "source_label",
            "source_ref",
        },
    }

    for tool_name, fields in flat_fields.items():
        params = tools[tool_name].parameters
        assert set(params["properties"]) == fields
        assert "request" not in params["properties"]


def test_every_mutation_tool_takes_flat_top_level_params() -> None:
    # ADR-0372: the project convention is that every mutation tool (mutating or destructive,
    # i.e. readOnlyHint is False) exposes its arguments as flat top-level params and never nests
    # them under a `request` wrapper, so a black-box agent can predict a mutation's argument
    # shape without fetching the schema first. Read/query tools may keep a `request` filter
    # wrapper (guarded by test_filtered_list_tools_use_request_payloads). A new wrapped mutation
    # tool must break here.
    offenders = [
        t.name
        for t in TOOLS
        if t.annotations is not None
        and t.annotations.readOnlyHint is False
        and "request" in cast(dict[str, object], t.parameters.get("properties", {}))
    ]
    assert offenders == [], (
        f"mutation tools still nesting args under `request`: {sorted(offenders)}"
    )


def test_platform_auditor_reads_keep_pagination_inside_request_payloads() -> None:
    tools = {t.name: t for t in TOOLS}

    audit_params = tools["audit.query"].parameters
    assert set(audit_params["properties"]) == {"request"}
    audit_choices = audit_params["properties"]["request"]["oneOf"]
    assert isinstance(audit_choices, list)
    for choice in audit_choices:
        assert isinstance(choice, dict)
        properties = cast(dict[str, object], choice["properties"])
        assert {"limit", "cursor"} <= set(properties)

    trail_params = tools["ops.tool_trail"].parameters
    assert set(trail_params["properties"]) == {"request"}
    trail_schema = _object_schema(trail_params["properties"]["request"])
    trail_properties = cast(dict[str, object], trail_schema["properties"])
    assert {"limit", "cursor"} <= set(trail_properties)


def test_run_cmdline_docs_describe_debug_args_only() -> None:
    """The agent-provided cmdline must not document platform-owned boot args."""
    tools = {t.name: t for t in TOOLS}
    for tool_name in ("runs.complete_build",):
        schema = cast(
            dict[str, object],
            tools[tool_name].parameters["properties"]["cmdline"],
        )
        description = schema["description"]
        assert isinstance(description, str)
        assert "dhash_entries=1" in description
        assert "console=ttyS0" not in description
        assert "root=/dev/vda" not in description


def test_complete_build_cmdline_advertises_iteration_without_rebuild() -> None:
    # #1256: an agent that sets cmdline at runs.complete_build reads only that field, and its
    # "fixed at build" mental model is what drives the ask for a phantom boot-time override. The
    # field must tell them the value is NOT locked in — it can be changed against the built kernel
    # via runs.install with no rebuild — so they find the real knob (#988) instead of a rebuild.
    tools = {t.name: t for t in TOOLS}
    schema = cast(
        dict[str, object],
        tools["runs.complete_build"].parameters["properties"]["cmdline"],
    )
    description = schema["description"]
    assert isinstance(description, str)
    lowered = description.lower()
    assert "runs.install" in lowered
    assert "without a rebuild" in lowered or "no rebuild" in lowered


def test_runs_create_build_profile_documents_arch() -> None:
    # The nested build_profile.arch is agent-facing (ADR-0343): its description must name the
    # allowed values. test_every_parameter_has_a_description checks only top-level params, so
    # this guards the nested field explicitly.
    tools = {t.name: t for t in TOOLS}
    params = tools["runs.create"].parameters
    build_profile = _object_schema(cast(dict[str, object], params["properties"]["build_profile"]))
    ref = build_profile.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        defs = cast(dict[str, object], params["$defs"])
        build_profile = cast(dict[str, object], defs[ref.removeprefix("#/$defs/")])
    props = cast(dict[str, object], build_profile["properties"])
    arch = cast(dict[str, object], props["arch"])
    description = arch.get("description")
    assert isinstance(description, str) and description.strip()
    assert "ppc64le" in description and "x86_64" in description


def test_run_lifecycle_tools_cross_reference_real_cmdline_parameters() -> None:
    # Extra kernel cmdline args are set on the real build/finalize tools, not through a
    # phantom subtool. The discovery text must name the actual public parameters.
    tools = {t.name: t for t in TOOLS}

    create = tools["runs.create"]
    create_props = create.parameters["properties"]
    create_text = (create.description or "") + create_props["build_profile"]["description"]
    assert "runs.build.cmdline" not in create_text
    assert "runs.complete_build" in create_text
    assert "cmdline" in create_text

    for tool_name in ("runs.boot", "runs.get"):
        description = tools[tool_name].description or ""
        assert "runs.build.cmdline" not in description
        assert "runs.complete_build" in description
        assert "cmdline" in description


def test_runs_get_documents_build_provenance_shape() -> None:
    # An agent calling runs.get reads only the runs.get wrapper docstring, so the
    # data.build_provenance field must be documented there, not only on runs.create which a
    # runs.get caller never reads. On the upload lane it carries the client-attested claim.
    tools = {t.name: t for t in TOOLS}
    description = tools["runs.get"].description or ""
    assert "build_provenance" in description
    assert "client_attested" in description


def test_jobs_wait_description_conveys_retry_contract() -> None:
    # #941: an agent calling jobs.wait reads only the wrapper docstring + Field text, so the
    # transport-reset retry contract must be surfaced there rather than living only on the inner
    # handler / the guide. The agent must learn that a non-terminal return is normal ("call
    # jobs.wait again", signalled via suggested_next_actions), that a transport drop on a long
    # wait is transient and retryable, and that the short default is preferred over one long hold
    # an intermediary proxy can sever.
    tools = {t.name: t for t in TOOLS}
    wait = tools["jobs.wait"]

    description = (wait.description or "").lower()
    # The non-terminal "still running, call again" signal and the field that carries it.
    assert "jobs.wait" in description
    assert "suggested_next_actions" in description
    assert "terminal" in description
    # A transport drop on a long wait is transient and safe to retry, not a real failure.
    assert "retry" in description or "retryable" in description
    assert "transient" in description or "transport" in description

    timeout_desc = wait.parameters["properties"]["timeout_s"]["description"].lower()
    # The surfaced default and cap are derived from the source constants, so the number an
    # agent reads cannot drift from the value the handler actually enforces.
    from kdive.mcp.tools.jobs import DEFAULT_WAIT_S, MAX_WAIT_S

    assert str(int(DEFAULT_WAIT_S)) in timeout_desc
    assert str(int(MAX_WAIT_S)) in timeout_desc
    # Warns that a large value risks an intermediary proxy severing the held stream, and steers
    # toward repeated short waits over one long hold.
    assert "proxy" in timeout_desc
    assert "short" in timeout_desc


def test_upload_tools_state_deadline_scope_and_non_constraint() -> None:
    # #1336 / ADR-0394: an agent calling the upload tools reads only the wrapper docstring, so
    # the deadline contract must be stated there — the scope (begin the PUT before the per-URL
    # expires_at; in-flight not interrupted), the reference clock, the re-mint recovery, and
    # that chunks are a size mechanism, not a way to beat the clock.
    tools = {t.name: t for t in TOOLS}
    for name in ("artifacts.create_run_upload", "artifacts.create_system_upload"):
        description = (tools[name].description or "").lower()
        assert "expires_at" in description
        assert "in flight" in description  # the not-interrupted scope clause
        assert "server_time" in description  # the reference clock
        assert "manifest_deadline" in description
        assert "on_expiry" in description  # the named recovery action
        assert "beat the clock" in description  # chunks are size, not time


def test_upload_tools_warn_extra_header_breaks_signature() -> None:
    # #1338 / ADR-0395: an agent calling the upload tools reads only the wrapper docstring, so the
    # extra-header footgun must be stated there — the PUT must send exactly required_headers, and
    # any extra header (e.g. a default Content-Type) breaks the SigV4 signature with a 403. Points
    # the agent at data.upload_hint, which restates it on the response itself.
    tools = {t.name: t for t in TOOLS}
    for name in ("artifacts.create_run_upload", "artifacts.create_system_upload"):
        description = (tools[name].description or "").lower()
        assert "required_headers" in description
        assert "content-type" in description  # the concrete extra-header trap
        assert "403 signaturedoesnotmatch" in description
        assert "upload_hint" in description  # points at the on-response restatement


def test_expected_boot_failure_documents_match_contract() -> None:
    # D7 (#763): the expected_boot_failure pattern is matched by
    # security.artifacts.artifact_search.search_text (a case-sensitive literal substring, applied
    # line-by-line against the redacted console log, with `|` as an OR separator — not regex). The
    # schema text must spell out that contract, not just give one example, so a black-box caller
    # writes a matching pattern from the surface alone.
    tools = {t.name: t for t in TOOLS}
    create_props = tools["runs.create"].parameters["properties"]
    description = create_props["expected_boot_failure"]["description"]
    lowered = description.lower()
    assert "substring" in lowered
    assert "case-sensitive" in lowered
    assert "line" in lowered  # line-by-line matching
    assert "redacted" in lowered
    assert "not a regex" in lowered or "not regex" in lowered
    # The `|`-OR alternation and its bounds (<=16 terms, <=256 chars) are named.
    assert "|" in description
    assert "16" in description
    assert "256" in description


def test_allocation_and_estimate_payload_schemas_are_concrete() -> None:
    tools = {t.name: t for t in TOOLS}

    # ADR-0372: allocations.request is flat (project + idempotency_key + the sizing fields at
    # top level). accounting.estimate is a read tool and keeps its request-filter wrapper.
    allocation_params = tools["allocations.request"].parameters["properties"]
    assert set(allocation_params) == {
        "project",
        "idempotency_key",
        "arch",
        "disk_gb",
        "memory_gb",
        "on_capacity",
        "pcie_devices",
        "resource",
        "shape",
        "vcpus",
        "window",
    }

    estimate_request = tools["accounting.estimate"].parameters["properties"]["request"]
    assert set(estimate_request["properties"]) == {
        "accel",
        "cost_class",
        "memory_gb",
        "vcpus",
        "window",
    }


def test_resource_register_tools_are_variant_specific() -> None:
    tools = {t.name: t for t in TOOLS}

    assert "resources.register" not in tools

    common = {
        "concurrent_allocation_cap",
        "cost_class",
        "name",
        "owner_project",
        "secret_refs",
        "vcpus",
        "memory_mb",
    }
    # ADR-0372: each register_* tool exposes its fields flat at top level (no `request` wrapper).
    remote_params = set(tools["resources.register_remote_libvirt"].parameters["properties"])
    assert remote_params == common | {"base_image", "host_uri"}

    local_params = set(tools["resources.register_local_libvirt"].parameters["properties"])
    assert local_params == common | {"host_uri"}

    fault_params = set(tools["resources.register_fault_inject"].parameters["properties"])
    assert fault_params == common


def test_every_tool_has_a_valid_maturity() -> None:
    valid = {"implemented", "partial", "planned"}
    offenders = [t.name for t in TOOLS if (t.meta or {}).get("maturity") not in valid]
    assert not offenders, f"tools with missing/invalid maturity: {offenders}"


def test_tools_have_no_maturity_detail() -> None:
    # A maturity_detail left behind after a tool is promoted would mislead.
    offenders = [t.name for t in TOOLS if "maturity_detail" in (t.meta or {})]
    assert not offenders, f"tools carrying a stale maturity_detail: {offenders}"


# The `debug.*` planes were proven live end-to-end on real KVM (M2.8 B6 #680, ADR-0208
# invariant 5): start_session opened a live gdbstub session, set_breakpoint("schedule") →
# continue → stopped(reason="breakpoint-hit") → read_registers(rip) == the schedule address,
# then end_session detached cleanly. The whole gdb-MI set shares that one attached session, so
# those nine are `implemented`. The later stack/disassembly ops were each re-proven live over the
# same transport: `backtrace`/`read_frame` against a stopped `schedule` (PR#929, #920/ADR-0275),
# and `disassemble` (symbol + address paths, plus the categorized bad-bounds/bad-target/unknown
# failures) against a stopped `schedule` (PR#932, #921/ADR-0276). The three watchpoint ops
# (#922/ADR-0277) were proven live against a stopped `schedule` on real KVM: set_watchpoint on
# `jiffies` (symbol path) and on an explicit address, list_watchpoints (which caught the live
# bare-row `-break-list` shape), clear_watchpoint, the categorized bad_byte_count/bad_target/
# bad_symbol_name failures, and — decisively — `continue` trapped on the watched write
# (reason=watchpoint-trigger in tick_do_update_jiffies64), so a hardware watchpoint does fire over
# this gdbstub. The two module-symbol ops (#923/ADR-0278) were proven live against a real kernel
# (v7.1-rc4 vmlinux, nokaslr) with a dependency-free `.ko` loaded by a `finit_module` init:
# list_modules walked the kernel `modules` list and found the module at its `mem[0].base`
# (single-module walk terminating at `&modules`, decode_errors=0), and load_module_symbols
# resolved the `.ko`, ran `add-symbol-file <ko> <base>`, and a symbol that read "No symbol in
# current context" before the load resolved afterward; identity_verified was False because that
# kernel exposes neither srcversion (no MODVERSIONS) nor build_id (no STACKTRACE_BUILD_ID), the
# disclosed-unverified path. This also re-proved the quoted `-data-evaluate-expression` form the
# walk depends on. `resolve_symbol` (ADR-0248) is unit-tested only and stays out until its
# `-data-evaluate-expression` form is re-proven live.
_LOCAL_PROVEN_DEBUG_TOOLS = frozenset(
    {
        "debug.set_breakpoint",
        "debug.clear_breakpoint",
        "debug.list_breakpoints",
        "debug.read_memory",
        "debug.read_registers",
        "debug.continue",
        "debug.interrupt",
        "debug.start_session",
        "debug.end_session",
        "debug.backtrace",
        "debug.read_frame",
        "debug.disassemble",
        "debug.set_watchpoint",
        "debug.list_watchpoints",
        "debug.clear_watchpoint",
        "debug.list_modules",
        "debug.load_module_symbols",
    }
)


def test_introspect_from_vmcore_promoted_to_implemented() -> None:
    # M2.8 B6 (#680): local offline drgn introspection was proven live on a real host_dump core
    # (sysinfo release 7.0.0, cpus_online=1, modules.decode_errors=0, all_failed=false), so the
    # tool is now `implemented` and carries no maturity_detail.
    by_name = {t.name: t for t in TOOLS}
    tool = by_name["introspect.from_vmcore"]
    assert (tool.meta or {}).get("maturity") == "implemented"
    assert (tool.meta or {}).get("maturity_detail") is None


def test_introspect_run_promoted_to_implemented() -> None:
    # M2.8 B6 (#680/#682): local-libvirt live drgn-over-SSH introspection was proven live on real
    # KVM. The per-Run DWARF vmlinux staged at /usr/lib/debug/lib/modules/<ver>/vmlinux (#728/
    # ADR-0221) let in-guest `drgn -k` resolve typed kernel objects: introspect.run(sysinfo)
    # returned release 7.0.0 with cpus_online=2, and run(tasks)/run(modules) both succeeded
    # (decode_errors=0). So the tool is now `implemented` and — per ADR-0175 — carries no
    # maturity_detail.
    tool = next(t for t in TOOLS if t.name == "introspect.run")
    meta = tool.meta or {}
    assert meta.get("maturity") == "implemented"
    assert meta.get("maturity_detail") is None


def test_local_proven_debug_planes_are_implemented() -> None:
    # B6 (#680): the gdb-MI debug surface was proven live on real KVM (ADR-0208 invariant 5
    # satisfied), so every debug.* op is now `implemented` and — per ADR-0175 — carries no
    # maturity_detail. Guards against a leftover maturity_detail after promotion.
    by_name = {t.name: t for t in TOOLS}
    offenders: list[str] = []
    for name in sorted(_LOCAL_PROVEN_DEBUG_TOOLS):
        tool = by_name.get(name)
        if tool is None:
            offenders.append(f"{name}: tool not registered")
            continue
        meta = tool.meta or {}
        if meta.get("maturity") != "implemented":
            offenders.append(f"{name}: maturity is not implemented ({meta.get('maturity')!r})")
        if meta.get("maturity_detail") is not None:
            offenders.append(f"{name}: implemented tool still carries maturity_detail")
    assert not offenders, f"proven debug planes not promoted to implemented: {offenders}"


def test_vmcore_fetch_implemented_both_methods_proven_live() -> None:
    # B6 (#680): both core-producing methods are now proven live on real KVM — HOST_DUMP
    # (the #716 `<acpi/>` fix) and KDUMP (the #705 `final_action poweroff` fix) — so
    # vmcore.fetch is `implemented` and, per ADR-0175, carries no maturity_detail/pointer.
    tool = next(t for t in TOOLS if t.name == "vmcore.fetch")
    assert (tool.meta or {}).get("maturity") == "implemented"
    assert "maturity_detail" not in (tool.meta or {}), (
        "vmcore.fetch: implemented tool must not carry maturity_detail"
    )


def test_postmortem_crash_triage_promoted_to_implemented() -> None:
    # #816 (ADR-0249): the real crash(8) runner is wired into production (replacing the no-op
    # stub) and live-proven end-to-end — `_real_run_crash` drove the real crash(8) over a real
    # captured core and `run_crash_postmortem` returned a `sys`+`log` transcript. So both tools
    # are now `implemented` and, per ADR-0175, carry no maturity_detail. (crash(8) must support
    # the kernel under test — a host prerequisite documented alongside drgn/libguestfs.)
    offenders = []
    for name in ("postmortem.crash", "postmortem.triage"):
        meta = next(t for t in TOOLS if t.name == name).meta or {}
        if meta.get("maturity") != "implemented":
            offenders.append(f"{name}: maturity is not implemented ({meta.get('maturity')!r})")
        if "maturity_detail" in meta:
            offenders.append(f"{name}: implemented tool still carries maturity_detail")
    assert not offenders, f"postmortem tools not promoted to implemented: {offenders}"


def test_destructive_hint_matches_reviewed_set() -> None:
    hinted = {t.name for t in TOOLS if (t.annotations and t.annotations.destructiveHint)}
    assert hinted == _docmeta.DESTRUCTIVE_TOOLS, (
        f"destructiveHint set {sorted(hinted)} != reviewed set {sorted(_docmeta.DESTRUCTIVE_TOOLS)}"
    )


def _gate_reachers() -> set[str]:
    """Tools whose wrapper reaches ``assert_destructive_allowed`` (through its delegate)."""
    return {t.name for t in TOOLS if _reaches_symbol(t.fn, "assert_destructive_allowed")}


def test_gate_callers_are_in_the_destructive_set() -> None:
    # Backstop: any tool that reaches assert_destructive_allowed must be in the reviewed
    # set (the converse — admin-gated ops — is not asserted). The reach is transitive: the
    # gate lives in the module-level handler the wrapper delegates to, not in the wrapper.
    gate_reachers = _gate_reachers()
    assert gate_reachers <= _docmeta.DESTRUCTIVE_TOOLS, (
        f"gate-calling tools not in the destructive set: "
        f"{sorted(gate_reachers - _docmeta.DESTRUCTIVE_TOOLS)}"
    )


def test_backstop_actually_detects_the_known_gate_callers() -> None:
    # Canary against a vacuous backstop: the gate-reacher set must be EXACTLY the tools
    # that call assert_destructive_allowed today. Equality (not subset) catches both a broken
    # mechanism — the reach analysis stopping at the wrapper body would empty this set — and
    # an unexpected new reacher, which then must be reviewed into DESTRUCTIVE_TOOLS and pinned
    # here. systems.teardown and systems.reprovision are deliberately absent: ADR-0129 dropped
    # teardown to a single require_role(ADMIN) check and ADR-0326 made reprovision contributor
    # leaseholder control, so neither reaches the destructive-op gate.
    assert _gate_reachers() == {"control.force_crash"}


# --- #1367: docstring quality gates -------------------------------------------------------
# Three content guards over the agent-facing tool descriptions, extending the ADR-0047 doc
# guard. They key off the same `_docmeta` classification the destructive-hint guard uses,
# plus a reviewed job-handle set. Each rule is paired with a canary that exercises its
# predicate against synthetic input, so a regression that neuters the rule fails loudly here
# rather than passing silently over a (now-clean) live tree. The `ADR-\d+` gate the issue
# also lists is already enforced by tests/mcp/core/test_no_adr_leak.py (ADR-0270); it is not
# duplicated here.

# A destructive-hinted tool must not ship a bare summary: its description has to name a
# concrete consequence (what it destroys / removes / crashes) or the privileged role/gate it
# demands, so a black-box agent reads the stakes from the surface alone. Substring vocabulary,
# lowercased; `admin` also covers `platform_admin`/`platform-admin`, `permanent` covers
# `permanently`, `delete` covers `deletes`, and so on.
_CONSEQUENCE_OR_ROLE_TERMS = frozenset(
    {
        # a named consequence
        "irreversible",
        "no undo",
        "cannot be undone",
        "permanent",
        "delete",
        "remove",
        "teardown",
        "tear down",
        "prune",
        "evict",
        "destroy",
        "destructive",
        "break-glass",
        "crash",
        "reclaim",
        # a required role / gate
        "admin",
        "platform operator",
        "contributor",
        "leaseholder",
        "rbac",
        "authoriz",
        "gate",
    }
)


def _names_consequence_or_role(description: str) -> bool:
    lowered = description.lower()
    return any(term in lowered for term in _CONSEQUENCE_OR_ROLE_TERMS)


def test_destructive_tools_name_a_consequence_or_role() -> None:
    # Every destructive-hinted tool (the reviewed _docmeta.DESTRUCTIVE_TOOLS set) must name a
    # consequence or the role/gate it requires in its rendered description — a bare
    # "Extend an image catalog entry lease."-style one-liner on a destructive tool is exactly
    # the gap this closes. `tools.invoke` qualifies via its RBAC/authorization wording (its
    # destructive hint is gateway-reach only).
    by_name = {t.name: t for t in TOOLS}
    offenders: list[str] = []
    for name in sorted(_docmeta.DESTRUCTIVE_TOOLS):
        tool = by_name.get(name)
        if tool is None:
            offenders.append(f"{name}: not registered")
            continue
        if not _names_consequence_or_role(tool.description or ""):
            offenders.append(name)
    assert not offenders, (
        f"destructive tools whose description names no consequence/role: {offenders}"
    )


def test_destructive_consequence_guard_bites() -> None:
    # Canary: the predicate must reject a bare summary and accept one that names a consequence
    # or a role, so a regression that broadens the vocabulary to vacuity (or drops the check)
    # fails here rather than passing over the clean tree.
    assert not _names_consequence_or_role("Extend an image catalog entry lease.")
    assert not _names_consequence_or_role("Open an investigation.")
    assert _names_consequence_or_role("Permanently delete the catalog row; irreversible.")
    assert _names_consequence_or_role("Enqueue teardown for a System. Requires admin.")


# Tools whose response is an opaque durable-job handle whose result the caller obtains only by
# polling — the out-of-band async contract (#941/jobs.wait): the description must name
# `jobs.wait` as the poll tool, not merely "poll" or `jobs.get`. This is a reviewed set (like
# DESTRUCTIVE_TOOLS): lifecycle tools that enqueue a job to advance a durable entity
# (images.build, systems.provision/teardown, runs.boot) are tracked via that entity's read
# tool, not jobs.wait, and are deliberately excluded.
_JOB_HANDLE_TOOLS = frozenset(
    {
        "control.capture_traffic",
        "control.diagnostic_sysrq",
        "control.watch_for_crash",
        "systems.authorize_ssh_key",
        "systems.check_ssh_reachable",
        "systems.snapshot",
        "systems.restore",
        "systems.delete_snapshot",
        "vmcore.fetch",
    }
)


def _references_jobs_wait(description: str) -> bool:
    return "jobs.wait" in description


def test_job_handle_tools_reference_jobs_wait() -> None:
    # Every reviewed job-handle tool must point the caller at `jobs.wait` — its result is only
    # reachable by polling, and jobs.wait carries the transport-reset retry contract
    # (test_jobs_wait_description_conveys_retry_contract).
    by_name = {t.name: t for t in TOOLS}
    offenders: list[str] = []
    for name in sorted(_JOB_HANDLE_TOOLS):
        tool = by_name.get(name)
        if tool is None:
            offenders.append(f"{name}: not registered")
            continue
        if not _references_jobs_wait(tool.description or ""):
            offenders.append(name)
    assert not offenders, f"job-handle tools that do not name jobs.wait: {offenders}"


def test_job_handle_set_is_exactly_the_jobs_wait_mentioners() -> None:
    # Anti-vacuity pin (mirrors the gate-caller canary): the reviewed set must equal the
    # registered tools — other than jobs.wait itself — whose description references jobs.wait.
    # Dropping the mention from a reviewed tool, or a new tool claiming the poll contract
    # without being reviewed into the set, breaks this equality and forces a deliberate update.
    mentioners = {
        t.name
        for t in TOOLS
        if t.name != "jobs.wait" and _references_jobs_wait(t.description or "")
    }
    assert mentioners == _JOB_HANDLE_TOOLS, (
        f"jobs.wait mentioners {sorted(mentioners)} != reviewed job-handle set "
        f"{sorted(_JOB_HANDLE_TOOLS)}"
    )


# A consequential tool — one that is destructive or returns a poll-only job handle — must
# document more than a single bare sentence: a one-liner like "Capture and persist a vmcore."
# hides the prerequisites, consequence, and poll contract a black-box agent needs. Simple
# reads/mutations ("Open an investigation.") are exempt; the floor applies only where the
# stakes warrant it.
_CONTENT_FLOOR_TOOLS = _docmeta.DESTRUCTIVE_TOOLS | _JOB_HANDLE_TOOLS
_CODE_SPAN = re.compile(r"`[^`]*`")


def _sentence_count(description: str) -> int:
    """Number of sentence-terminated clauses, ignoring dots inside `code spans`."""
    prose = _CODE_SPAN.sub(" ", description)
    return len(re.findall(r"[.!?]+(?=\s|$)", prose))


def test_consequential_tools_clear_the_content_floor() -> None:
    by_name = {t.name: t for t in TOOLS}
    offenders: list[str] = []
    for name in sorted(_CONTENT_FLOOR_TOOLS):
        tool = by_name.get(name)
        if tool is None:
            offenders.append(f"{name}: not registered")
            continue
        if _sentence_count(tool.description or "") < 2:
            offenders.append(name)
    assert not offenders, (
        f"destructive/job-handle tools whose description is a single bare sentence: {offenders}"
    )


def test_content_floor_guard_bites() -> None:
    # Canary: a one-liner is one sentence (below the floor); a summary plus a specifics
    # sentence clears it. A `. ` inside a code span must not inflate the count.
    assert _sentence_count("Capture and persist a vmcore.") == 1
    assert _sentence_count("Extend an image catalog entry lease.") == 1
    assert _sentence_count("Enqueue teardown for a System. Requires admin.") == 2
    assert _sentence_count("Do `a. b` now.") == 1


def _collect_enums(schema: Any) -> list[list[Any]]:
    """Every ``enum`` value-list anywhere in ``schema`` (the rendered ``anyOf`` nests it)."""
    found: list[list[Any]] = []
    if isinstance(schema, dict):
        enum = schema.get("enum")
        if isinstance(enum, list):
            found.append(enum)
        for value in schema.values():
            found.extend(_collect_enums(value))
    elif isinstance(schema, list):
        for item in schema:
            found.extend(_collect_enums(item))
    return found


def test_systems_list_state_filter_is_enum_constrained() -> None:
    # ADR-0147: the closed-value-set `state` filter advertises the SystemState enum at the
    # schema layer (so an invalid value is a schema error the model sees up front), while the
    # open-value-set `shape`/`pcie` filters stay bare strings (shape is runtime-mutable via
    # shapes.set; pcie is a structured <vendor>:<device> format).
    request_schema = {t.name: t for t in TOOLS}["systems.list"].parameters["properties"]["request"]
    props = request_schema["anyOf"][0]["properties"]

    state_enums = _collect_enums(props["state"])
    assert state_enums, "systems.list `state` advertises no enum; it must carry SystemState"
    assert {value for enum in state_enums for value in enum} == {s.value for s in SystemState}

    for open_filter in ("shape", "pcie"):
        assert not _collect_enums(props[open_filter]), (
            f"systems.list `{open_filter}` must stay an open string filter, not an enum"
        )


_CONFUSABLE_SYSTEMS_ALTERNATIVES = {
    "systems.define": (r"\bsystems\.provision\b(?!_)",),
    "systems.provision": (r"\bsystems\.define\b", r"\bsystems\.provision_defined\b"),
    "systems.provision_defined": (r"\bsystems\.define\b",),
    "systems.reprovision": (r"\bsystems\.provision\b(?!_)",),
}
_NEGATIVE_GUIDANCE = re.compile(r"\b(instead|rather|not)\b", re.IGNORECASE)


def test_confusable_systems_tools_name_their_alternative() -> None:
    # ADR-0147: the mis-sequence-prone systems.* lifecycle tools must name their specific
    # alternative tool and carry a when-NOT-to-use cue. Matching is token-precise so a
    # `provision_defined` mention cannot vacuously satisfy a bare-`provision` requirement,
    # and the negative cue is word-bounded so `cannot`/`annotation` do not satisfy it.
    tools = {t.name: t for t in TOOLS}
    for name, alternative_patterns in _CONFUSABLE_SYSTEMS_ALTERNATIVES.items():
        description = tools[name].description or ""
        for pattern in alternative_patterns:
            assert re.search(pattern, description), (
                f"{name} description must name its alternative tool (/{pattern}/): {description!r}"
            )
        assert _NEGATIVE_GUIDANCE.search(description), (
            f"{name} description must carry a when-not-to-use cue: {description!r}"
        )


_TOOLS_PKG = _REPO_ROOT / "src" / "kdive" / "mcp" / "tools"

# A number stated next to bound/limit vocabulary in agent-facing text (a Field description or
# an `@app.tool` wrapper docstring). Such a number duplicates the source constant that enforces
# it and silently drifts when that constant changes, so it must be interpolated from the
# constant (an f-string `{...}` segment), never hand-typed. Interpolated numbers live in an
# f-string FormattedValue, which `_static_description_text` drops, so they never reach this regex.
_BOUND_LITERAL = re.compile(
    r"(?ix)"
    r"(?: (?: capped \s+ at | up \s+ to | at \s+ most | no \s+ more \s+ than"
    r"      | maximum (?: \s+ of)? | limit (?: ed \s+ to)? | default s? (?: \s+ to)?"
    r"      | <= \s* | \b 1 \s* \.\. \s* ) \s* \d+ )"
    r"| (?: \d+ \s* -? \s* (?: bytes | chars | characters | terms | KiB | MiB | seconds? ) \b )",
)

# Bound-context literals that are genuinely not constant-backed (external protocol facts, not a
# kdive-tunable limit), keyed by module path suffix -> allowed snippets. Keep this minimal and
# justify every entry; a real internal cap belongs in a named constant, interpolated.
_BOUND_LITERAL_ALLOWLIST: dict[str, frozenset[str]] = {}


def _call_callee_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _keyword_value(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _module_string_aliases(tree: ast.Module) -> dict[str, ast.expr]:
    """Map each module-level ``NAME = <string expr>`` to its value node.

    Descriptions are often assigned to a module constant and passed as ``description=NAME``;
    resolving the alias stops a hardcoded bound from hiding behind an indirection.
    """
    aliases: dict[str, ast.expr] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
            value: ast.expr = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                aliases[target.id] = value
    return aliases


def _static_description_text(node: ast.expr, aliases: dict[str, ast.expr] | None = None) -> str:
    """The literal string segments of ``node``, with f-string interpolations dropped.

    Resolves a ``Name`` reference through ``aliases`` (module-level string constants) so a
    bound literal cannot escape the guard by living in a named string.
    """
    aliases = aliases or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_static_description_text(part, aliases) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_description_text(node.left, aliases)
        return left + _static_description_text(node.right, aliases)
    if isinstance(node, ast.Name) and node.id in aliases:
        remaining = {name: value for name, value in aliases.items() if name != node.id}
        return _static_description_text(aliases[node.id], remaining)
    return ""  # FormattedValue (interpolated) or a non-literal reference: nothing static


def _is_app_tool(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in func.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
    return False


def _iter_agent_facing_descriptions() -> list[tuple[Path, str, str]]:
    """Every `Field(description=...)` and `@app.tool` wrapper docstring in the tools package."""
    found: list[tuple[Path, str, str]] = []
    for path in sorted(_TOOLS_PKG.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _module_string_aliases(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_callee_name(node) == "Field":
                desc = _keyword_value(node, "description")
                if desc is not None:
                    text = _static_description_text(desc, aliases)
                    found.append((path, "Field.description", text))
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and _is_app_tool(node):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    found.append((path, f"{node.name} docstring", doc))
    return found


def test_agent_facing_numeric_bounds_are_interpolated_not_hardcoded() -> None:
    # A hand-typed bound (e.g. "capped at 300", "at most 24576 bytes") in a Field description or
    # `@app.tool` docstring is the wrapper-vs-source drift this guard exists to prevent: the number
    # the agent reads must be derived from the constant the handler enforces, so bump the constant
    # and the surface follows. Interpolate `{int(MAX_WAIT_S)}` etc.; do not retype the value.
    offenders: list[str] = []
    for path, kind, text in _iter_agent_facing_descriptions():
        suffix_allow = next(
            (
                snips
                for suffix, snips in _BOUND_LITERAL_ALLOWLIST.items()
                if path.as_posix().endswith(suffix)
            ),
            frozenset(),
        )
        for match in _BOUND_LITERAL.finditer(text):
            snippet = match.group(0)
            if snippet in suffix_allow:
                continue
            rel = path.relative_to(_REPO_ROOT)
            offenders.append(f"{rel} [{kind}]: {snippet!r}")
    assert not offenders, (
        "agent-facing numeric bounds must be interpolated from the enforcing constant, not "
        "hand-typed literals that drift when the constant changes:\n" + "\n".join(sorted(offenders))
    )


def _guard_static_text(expr_src: str, aliases_src: str = "") -> str:
    """Run the guard's detection primitives over a parsed expression (for self-testing)."""
    aliases = _module_string_aliases(ast.parse(aliases_src)) if aliases_src else {}
    node = ast.parse(expr_src, mode="eval").body
    return _static_description_text(node, aliases)


def test_bound_literal_guard_detects_and_ignores() -> None:
    # The guard above only runs over the (now-clean) live tree, so on its own it cannot prove it
    # still catches a violation. Exercise the primitives directly against synthetic inputs so a
    # regression that neuters detection (or the regex) fails here instead of passing silently.

    # A hardcoded bound in a plain literal is caught.
    assert _BOUND_LITERAL.search(_guard_static_text('"rows (capped at 200)."'))
    # Explicit string concatenation is flattened before matching.
    assert _BOUND_LITERAL.search(_guard_static_text('"rows " + "(capped at 200)."'))
    # A bound hidden behind a module-level string alias is resolved and caught.
    assert _BOUND_LITERAL.search(_guard_static_text("DESC", 'DESC = "queue up to 16 terms"'))
    # The broadened vocabulary catches maximum/limit/seconds phrasings too.
    assert _BOUND_LITERAL.search(_guard_static_text('"wait maximum 300 seconds"'))
    assert _BOUND_LITERAL.search(_guard_static_text('"limit 200 rows"'))

    # An interpolated bound is invisible: the number lives in an f-string FormattedValue, which
    # _static_description_text drops, so a correctly-interpolated description passes.
    assert not _BOUND_LITERAL.search(_guard_static_text('f"rows (capped at {CAP})."'))
    # A bound-free description is not flagged.
    assert not _BOUND_LITERAL.search(_guard_static_text('"Opaque continuation cursor."'))

    # Known blind spot: a description sourced from an unresolved (e.g. imported) name yields empty
    # static text and is not inspected, so any bound behind such an indirection must stay
    # interpolated at its definition. This assertion pins the limitation so a future change that
    # closes it updates this test deliberately.
    assert _guard_static_text("SOME_IMPORTED_CONST") == ""


def test_active_tools_have_a_covering_test() -> None:
    covered_maturities = {"implemented"}
    active = {t.name for t in TOOLS if (t.meta or {}).get("maturity") in covered_maturities}
    mapped = set(_BEHAVIOR_TESTS_BY_TOOL)
    assert active == mapped, (
        "active tool behavior-test map is out of date: "
        f"missing {sorted(active - mapped)}, stale {sorted(mapped - active)}"
    )

    missing_files: list[str] = []
    live_only_files: list[str] = []
    for tool, rel_paths in _BEHAVIOR_TESTS_BY_TOOL.items():
        for rel_path in rel_paths:
            path = _REPO_ROOT / rel_path
            if not path.is_file():
                missing_files.append(f"{tool}: {rel_path}")
                continue
            text = path.read_text(encoding="utf-8")
            if any(marker in text for marker in _NON_LIVE_MARKERS):
                live_only_files.append(f"{tool}: {rel_path}")
    assert not missing_files, f"mapped behavior test files do not exist: {missing_files}"
    assert not live_only_files, f"mapped behavior tests must be non-live: {live_only_files}"


def _rendered_detail(spec: dict[str, Any]) -> str:
    """The full rendered detail for a parameter: inline type plus the sub-list lines."""
    return render_schema_type(spec) + "\n" + "\n".join(render_param_detail(spec))


def _semantic_depth(spec: Any) -> int:
    """Deepest semantic recursion the renderer performs: properties / items / union members."""
    if not isinstance(spec, dict):
        return 0
    children: list[Any] = list((spec.get("properties") or {}).values())
    items = spec.get("items")
    if isinstance(items, dict):
        children.append(items)
    for union_key in ("anyOf", "oneOf"):
        members = spec.get(union_key)
        if isinstance(members, list):
            children.extend(members)
    return 1 + max((_semantic_depth(c) for c in children), default=-1)


_UNINFORMATIVE_RENDERS = frozenset({"any", "object", "array", "array<any>", "array<object>"})


def test_structured_params_render_nested_detail() -> None:
    # ADR-0177 docs guard: a structured parameter (one carrying properties / items / enum /
    # anyOf / oneOf) must not collapse to a bare `any`/`object`/`array` with no field
    # sub-list. A render that resolves the shape — a scalar token, `(nullable)`, an enum
    # value list, an `array<string>`, a union with `|`, or a `… fields:` sub-list — is
    # informative and passes. Legitimately-scalar params (`string`/`integer`/...) are not
    # `_is_structured`, so they are exempt and the guard does not false-positive on them.
    offenders: list[str] = []
    for t in TOOLS:
        for name, spec in (t.parameters or {}).get("properties", {}).items():
            if not _is_structured(spec):
                continue
            inline = render_schema_type(spec)
            detail = render_param_detail(spec)
            if inline in _UNINFORMATIVE_RENDERS and not detail:
                offenders.append(f"{t.name}:{name} -> {inline!r}")
    assert not offenders, f"structured params collapsed to a bare type with no detail: {offenders}"


def test_build_profile_examples_are_valid() -> None:
    # ADR-0177 decision 2: every documented runs.create build_profile example must parse, so a
    # schema change that invalidates a documented example fails here rather than drifting silently.
    assert _BUILD_PROFILE_EXAMPLES, "build_profile examples must be non-empty"
    for label, payload in _BUILD_PROFILE_EXAMPLES:
        BuildProfile.parse(payload)  # raises CategorizedError on an invalid example
        assert label.strip(), "each example needs a human label"


def test_schema_renderer_rejects_unresolved_ref() -> None:
    # ADR-0177: an unresolved $ref/$defs is not a walkable shape; the renderer must raise
    # rather than fall through to a silent bare `object`/`any`.
    with pytest.raises(ValueError, match="ref"):
        render_schema_type({"$ref": "#/$defs/Foo"})
    with pytest.raises(ValueError, match="ref"):
        render_schema_type({"$defs": {"Foo": {"type": "string"}}, "type": "object"})


def test_schema_renderer_depth_bound_fails_loud() -> None:
    # ADR-0177: a schema deeper than _MAX_SCHEMA_DEPTH fails loud, not silently truncated.
    # render_param_detail recurses through nested object fields; render_schema_type recurses
    # through array items — exercise each on a chain past the bound.
    object_chain: dict[str, Any] = {"type": "string"}
    array_chain: dict[str, Any] = {"type": "string"}
    for _ in range(_MAX_SCHEMA_DEPTH + 2):
        object_chain = {"type": "object", "properties": {"child": object_chain}}
        array_chain = {"type": "array", "items": array_chain}
    with pytest.raises(ValueError, match="_MAX_SCHEMA_DEPTH"):
        render_param_detail(object_chain)
    with pytest.raises(ValueError, match="_MAX_SCHEMA_DEPTH"):
        render_schema_type(array_chain)


def test_max_schema_depth_clears_live_schemas() -> None:
    # ADR-0177: the bound must exceed the deepest live tool-param schema, so a future deeper
    # schema trips this test before it trips CI doc-gen, and the bound keeps headroom.
    deepest = 0
    deepest_param = ""
    for t in TOOLS:
        for name, spec in (t.parameters or {}).get("properties", {}).items():
            d = _semantic_depth(spec)
            if d > deepest:
                deepest, deepest_param = d, f"{t.name}:{name}"
    assert deepest < _MAX_SCHEMA_DEPTH, (
        f"deepest live param {deepest_param} is {deepest} levels; "
        f"_MAX_SCHEMA_DEPTH={_MAX_SCHEMA_DEPTH} has no headroom — raise the bound"
    )
