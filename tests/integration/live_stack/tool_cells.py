"""Tool-cell frame of the live coverage carriers (#2811, ADR-0722).

A tool cell is one contract cell of a registered MCP tool: a configuration (``default`` or
``recovery``), an exposure (``direct``, ``gateway`` or ``cli``) and a kind (``functional`` or one
rejection boundary). :func:`run_tool_cell` proves the server's configuration from its operator
catalog, skips a cell of the other configuration without writing a record, and otherwise runs the
cell through :func:`~tests.integration.live_stack.scenario.run_cell`. :class:`HttpCaller` reaches a
tool over HTTP and :class:`CliCaller` through its generated ``kdivectl`` verb (ADR-0795);
:func:`caller_for` picks the cell's. :func:`prove_functional` and :func:`prove_rejection` prove the
cell's assertions, with :func:`project_state` as the default protected-state snapshot.

A provider cell runs on its provider's :class:`Lane`: a local-libvirt cell on the catalog lane
image (:data:`LANE_IMAGES`) of this host's architecture, a remote-libvirt cell on the remote
representative (:data:`REMOTE_LANE_FAMILIES`) of a separate provider host, observed through the
operator's ``REMOTE_PROVIDER_SSH`` access. :func:`on_lane_system` is the functional frame: it
provisions the lane image in the cell's project, observes the guest, proves the body's ``effect``
and the owned ``cleanup``. :func:`lane_target` is the rejection target, one torn-down System per
stack and provider.

An authority-owned System has a third frame,
:func:`~tests.integration.live_stack.authority_lane.on_authority_system`, which a carrier selects
explicitly: it observes the System on the authority's private daemon rather than over SSH.

``python -m tests.integration.live_stack.tool_cells bindings --candidate SHA --out FILE`` writes
the expected ``Context`` of every bound service tool cell and every bound local-libvirt tool cell
native to this host; ``--remote`` adds the bound remote-libvirt cells of the provider host;
``--kernel-baseline NAME`` binds a verified kernel fixture to the cells that declare kernel inputs.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
import platform
import re
import secrets
import sys
import tempfile
import xml.etree.ElementTree as ET  # noqa: S405 - libvirt XML of the provider  # nosec B405
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext, redirect_stderr
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, TypeIs, cast, get_args
from uuid import UUID

import httpx
import libvirt
import psycopg
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from psycopg import sql

import kdive.config as config
from kdive.cli.__main__ import build_parser
from kdive.cli.commands._generated_verbs import GENERATED_VERBS
from kdive.cli.commands.generated_args import assemble_generated_payload
from kdive.cli.commands.verb_spec import GeneratedVerb
from kdive.cli.reserved_flags import derive_cli_flag
from kdive.config.cli_settings import CLI_CLIENT_ID
from kdive.domain.errors import ErrorCategory
from kdive.images.rootfs.catalog import RootfsCatalogEntry, load_rootfs_catalog
from kdive.mcp.dev_harness import (
    LiveStackClient,
    LiveStackToolError,
    OidcIssuer,
    make_keypair,
    mint_token,
)
from kdive.mcp.exposure import CORE_TOOLS
from kdive.mcp.responses import ToolResponse
from kdive.providers.ports.traffic import pcap_volume_name
from kdive.providers.shared.runtime_paths import pcap_dir
from scripts.coverage_campaign.contract import FILTERED_LIST_BOUNDARIES, Cell, build_contract
from scripts.coverage_campaign.evidence import Context, InputBindings, Outcome
from tests.integration.live_stack.cleanup import disk_absent
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.deep_lifecycle import (
    FIXTURE_ROOT_ENV,
    InstalledKernel,
    bound_kernel,
    file_sha256,
    load_fixture,
)
from tests.integration.live_stack.evidence import os_identity
from tests.integration.live_stack.image_smoke import (
    PROBE,
    Endpoint,
    GuestIdentity,
    os_matches,
    staged_image,
)
from tests.integration.live_stack.remote_lifecycle import (
    HOST_SSH_ENV,
    REMOTE_REPRESENTATIVES,
    RemoteHost,
    guest_boot_kernel,
    observer,
    on_remote_system,
    remote_host,
    remote_profile,
    remote_volume_absent,
    remote_xml,
    staged_base_volume,
    staged_volume,
    volume_sha256,
)
from tests.integration.live_stack.scenario import (
    CatalogBody,
    CellRun,
    Provision,
    ScenarioStop,
    authorize_ssh,
    catalog_profile,
    domain_xml,
    on_catalog_system,
    provision_catalog,
    run_cell,
    ssh_probe,
)
from tests.integration.live_stack.spine import worker_libvirt_uri

Exposure = Literal["direct", "gateway", "cli"]
Boundary = Literal[
    "authentication", "authorization", "project-isolation", "validation", "capability"
]
Result = ToolResponse | list[ToolResponse] | LiveStackToolError
Snapshot = Callable[[], Awaitable[object]]
Setup = Callable[[], AbstractAsyncContextManager[Mapping[str, object]]]

RECOVERY_TOOLS = frozenset({"ops.build_uses_list", "ops.recover_build_use"})
# Rows a rejected or successful call is expected to write (ADR-0722 §4).
_AUDIT_TABLES = frozenset({"audit_log", "platform_audit_log", "tool_invocation"})
_JWT_STANDARD = frozenset({"sub", "iss", "aud", "exp", "iat", "nbf", "jti"})
_CONFIGURATIONS: dict[str, tuple[str, list[str]]] = {}
# The catalog image every provider cell of a host architecture boots.
LANE_IMAGES = {"x86_64": "fedora-kdive-ready-44"}
# The remote representative family every remote provider cell of a guest architecture boots.
REMOTE_LANE_FAMILIES = {"x86_64": "fedora"}
PUBLISH_TOOL = "images.publish"
# The single-kernel catalog image a functional images.publish cell publishes and boots, per
# (architecture, exposure). One image per exposure: images.publish never recycles a finished
# job of the same name, so two cells of one stack cannot both publish one image.
PUBLISHED_IMAGES = {
    ("x86_64", "direct"): "fedora-kdive-ready-43-cloud",
    ("x86_64", "gateway"): "rocky-kdive-ready-9",
}
# The DMI product UUID a KVM guest reports is its libvirt domain UUID.
IDENTITY_PROBE = PROBE + '; printf "product_uuid=%s\\n" "$(cat /sys/class/dmi/id/product_uuid)"'
_SETTLE_S = 15.0
_SETTLE_ATTEMPTS = 8


def configuration_of(catalog: Iterable[str]) -> str:
    """``recovery`` when both build-use recovery tools are listed, ``default`` when neither."""
    listed = RECOVERY_TOOLS & set(catalog)
    if listed == RECOVERY_TOOLS:
        return "recovery"
    if not listed:
        return "default"
    raise AssertionError(
        f"catalog lists only {sorted(listed)} of the build-use recovery tools; "
        "the server's configuration is neither default nor recovery"
    )


@dataclass(frozen=True)
class Grants:
    """The claims one cell's token carries."""

    subject: str
    projects: tuple[str, ...]
    roles: Mapping[str, str] = field(default_factory=dict)
    platform_roles: tuple[str, ...] = ()


def _segment(token: str, index: int) -> dict[str, object]:
    raw = token.split(".")[index]
    decoded = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert isinstance(decoded, dict), "JWT segment is not an object"
    return decoded


def claims_of(token: str) -> dict[str, object]:
    """The unverified payload of a JWT."""
    return _segment(token, 1)


def forge(token: str) -> str:
    """``token`` re-signed by a fresh key the server does not trust (ADR-0722 §3).

    The claims and the header's ``kid`` are kept, so the verifier selects the trusted key and
    fails on the signature itself rather than on key lookup.
    """
    claims = claims_of(token)
    kid = _segment(token, 0).get("kid")
    return make_keypair().create_token(
        subject=str(claims["sub"]),
        issuer=str(claims["iss"]),
        audience=cast(str | list[str] | None, claims.get("aud")),
        additional_claims={k: v for k, v in claims.items() if k not in _JWT_STANDARD},
        kid=kid if isinstance(kid, str) else None,
    )


class Caller(Protocol):
    """What the rejection harness needs from an exposure."""

    @property
    def exposure(self) -> str: ...
    def token(self, grants: Grants) -> str: ...
    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse | list[ToolResponse]: ...
    async def post(self, tool: str, args: Mapping[str, object], token: str) -> int: ...


def one(result: ToolResponse | list[ToolResponse]) -> ToolResponse:
    """The single successful envelope ``result`` must be."""
    assert isinstance(result, ToolResponse), f"expected one envelope, got {len(result)}"
    assert result.error_category is None, f"{result.error_category}: {result.detail}"
    return result


def matches(env: ToolResponse) -> list[dict[str, object]]:
    """The ``tools.search`` matches of ``env``."""
    raw = env.data.get("matches")
    assert isinstance(raw, list), f"{env.object_id} carries no matches"
    return [cast(dict[str, object], m) for m in raw if isinstance(m, dict)]


@dataclass(frozen=True)
class HttpCaller:
    """Reach a tool over HTTP in one exposure (ADR-0722 §1)."""

    exposure: Exposure
    base_url: str
    issuer: OidcIssuer

    def __post_init__(self) -> None:
        # Over HTTP a `cli` exposure would route through the gateway and record it as `cli`.
        assert (self.exposure == "cli") == isinstance(self, CliCaller), (
            f"exposure {self.exposure!r} needs a CliCaller exactly when it is `cli`"
        )

    def token(self, grants: Grants) -> str:
        """A real-issuer token for ``grants``; all but ``gateway`` carry the ``kdivectl`` id."""
        return mint_token(
            self.issuer,
            subject=grants.subject,
            projects=list(grants.projects),
            roles=dict(grants.roles),
            platform_roles=list(grants.platform_roles),
            agent_session=f"{grants.subject}-sess",
            client_id=config.require(CLI_CLIENT_ID) if self.exposure != "gateway" else None,
        )

    def _route(self, tool: str, args: Mapping[str, object]) -> tuple[str, dict[str, object]]:
        if self.exposure == "direct":
            return tool, dict(args)
        return "tools.invoke", {"name": tool, "arguments": dict(args)}

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse | list[ToolResponse]:
        """Call ``tool``; ``gateway`` with ``discover`` first finds it with ``tools.search``."""
        async with LiveStackClient.over_http(self.base_url, token) as client:
            if self.exposure == "gateway" and discover:
                found = one(await client.call_tool("tools.search", names=[tool]))
                names = [m.get("name") for m in matches(found)]
                assert names == [tool], f"tools.search did not find {tool}: {names}"
            name, arguments = self._route(tool, args)
            return await client.call_tool(name, **arguments)

    async def post(self, tool: str, args: Mapping[str, object], token: str) -> int:
        """Send one raw ``tools/call`` carrying ``token``; return the HTTP status."""
        name, arguments = self._route(tool, args)
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            response = await http.post(self.base_url, json=body, headers=headers)
        return response.status_code


_EXIT_CODES = {  # ADR-0089: the CLI's exit code per error category; any other category is 1.
    "configuration_error": 2,
    "authorization_denied": 3,
    "not_found": 4,
    "conflict": 5,
    "capacity_exhausted": 6,
}
_AUTH_FAILED = "error: authentication failed"
_TIER_REFUSED = "is not positively classified"
_CLI_TIMEOUT_S = 900.0
_VERBS = {verb.tool: verb for verb in GENERATED_VERBS}


class CliUsageError(LiveStackToolError):
    """kdivectl refused the command line itself (exit 2, no envelope): no call was made.

    ``kind`` classes argparse's diagnostic (``invalid-value``, ``required``, ``unrecognized``)
    or is ``handler`` for a verb handler's own refusal; ``refused`` names options, never values.
    """

    def __init__(self, tool: str, kind: str, refused: str) -> None:
        self.kind = kind
        self.refused = refused
        super().__init__(tool, f"kdivectl refused the command line ({kind}): {refused}")


class CliTierRefusal(LiveStackToolError):
    """kdivectl refused a verb whose tool the caller's catalog hides (exit 3, no call)."""


class CliTranslationGap(AssertionError):
    """The call cannot be expressed as this tool's kdivectl command line."""


@dataclass(frozen=True)
class CliRun:
    code: int
    stdout: str
    stderr: str


async def run_kdivectl(argv: list[str], *, token: str, base_url: str) -> CliRun:
    """Run the real ``kdivectl`` entry point as a subprocess, stdin closed."""
    env = {**os.environ, "KDIVE_TOKEN": token, "KDIVE_SERVER_URL": base_url}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kdive.cli",
        *argv,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), _CLI_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise AssertionError(f"kdivectl {argv[0]} {argv[1]} ran past {_CLI_TIMEOUT_S}s") from None
    assert proc.returncode is not None
    return CliRun(proc.returncode, out.decode(), err.decode(errors="replace"))


def _auth_failed(done: CliRun) -> bool:
    return any(line.startswith(_AUTH_FAILED) for line in done.stderr.splitlines())


def _refused_flags(done: CliRun) -> str:
    """The option names argparse's last diagnostic line cites; never their values."""
    last = done.stderr.strip().splitlines()[-1:] or [""]
    return " ".join(re.findall(r"--[a-z0-9-]+", last[0])) or "no option named"


def _refusal_kind(done: CliRun) -> str:
    """The class of a usage refusal, from the last diagnostic line argparse or a handler wrote."""
    last = (done.stderr.strip().splitlines()[-1:] or [""])[0]
    if "arguments are required" in last:
        return "required"
    if "unrecognized arguments" in last:
        return "unrecognized"
    if last.startswith("kdivectl") and ": error: argument " in last:
        return "invalid-value"
    return "handler"


def _text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _usage_body(args: Mapping[str, object]) -> dict[str, object]:
    target = args.get("target")
    if set(args) == {"target"} and isinstance(target, Mapping):
        return {str(k): v for k, v in target.items() if k != "kind"}
    return dict(args)


def _report_body(args: Mapping[str, object]) -> dict[str, object]:
    request = args.get("request")
    if set(args) != {"request"} or not isinstance(request, Mapping):
        return dict(args)
    body = {str(k): v for k, v in request.items()}
    if isinstance(projects := body.get("projects"), list):
        body["projects"] = ",".join(str(p) for p in projects)
    if isinstance(window := body.get("window"), list) and len(window) == 2:
        del body["window"]
        body["since"], body["until"] = window
    return body


# These handlers reshape the payload, so their command line is not the payload's.
_INVERSES = {"accounting.usage": _usage_body, "accounting.report": _report_body}


def _body(verb: GeneratedVerb, args: Mapping[str, object]) -> dict[str, object]:
    request = args.get("request")
    if verb.unwrap_request and set(args) == {"request"} and isinstance(request, Mapping):
        return {str(k): v for k, v in request.items()}
    return dict(args)


def _flag(verb: GeneratedVerb, key: str, value: object) -> list[str]:
    if value is None:
        return []
    if key in verb.json_params:
        return [f"{derive_cli_flag(key)}-json", json.dumps(value)]
    flag = next((f for f in verb.flags if f.dest == key), None)
    if flag is None:
        raise CliTranslationGap(f"kdivectl {verb.group} {verb.sub} has no option for {key!r}")
    if flag.action == "store_true" and isinstance(value, bool):
        return [flag.name] if value else []
    if flag.action == "bool_optional" and isinstance(value, bool):
        return [flag.name if value else "--no-" + flag.name.removeprefix("--")]
    if flag.action == "append" and isinstance(value, list):
        return [token for item in value for token in (flag.name, _text(item))]
    return [flag.name, _text(value)]


def cli_argv(tool: str, args: Mapping[str, object]) -> list[str]:
    """The ``kdivectl`` command line that makes ``tool``'s call with ``args`` (ADR-0795)."""
    verb = _VERBS[tool]
    inverse = _INVERSES.get(tool)
    body = inverse(args) if inverse else _body(verb, args)
    argv = [verb.group, verb.sub]
    argv += [_text(body.pop(name)) for name in verb.positionals if body.get(name) is not None]
    for key, value in body.items():
        argv += _flag(verb, key, value)
    return [*argv, *(["--yes"] if verb.confirm_destructive else []), "--json"]


def _check_exact(tool: str, argv: list[str], args: Mapping[str, object]) -> None:
    """A parsed command line reassembles exactly ``args`` (the two reshaping verbs excepted)."""
    verb = _VERBS[tool]
    if tool in _INVERSES:
        return
    try:
        with redirect_stderr(io.StringIO()):
            namespace = build_parser().parse_args(argv)
    except SystemExit:
        return  # The subprocess reports the same refusal as a usage error.
    bools = {f.dest for f in verb.flags if f.action == "store_true"}
    body = {
        k: v
        for k, v in _body(verb, args).items()
        if v is not None and not (k in bools and v is False)
    }
    expected = ({"request": body} if body else {}) if verb.unwrap_request else body
    got = assemble_generated_payload(verb, namespace)
    differs = sorted(k for k in expected.keys() | got.keys() if expected.get(k) != got.get(k))
    if differs:
        raise CliTranslationGap(f"kdivectl {verb.group} {verb.sub} cannot carry {differs}")


def _result(tool: str, argv: list[str], done: CliRun) -> ToolResponse | list[ToolResponse]:
    verb = f"kdivectl {argv[0]} {argv[1]}"
    if done.stdout.strip().startswith("{"):
        payload = json.loads(done.stdout)
        category = payload.get("error_category")
        expected = 0 if category is None else _EXIT_CODES.get(category, 1)
        assert done.code == expected, f"{verb} exited {done.code} for {category!r}, not {expected}"
        inner = payload.get("result")
        if list(payload) == ["result"] and isinstance(inner, list):
            return [ToolResponse.model_validate(item) for item in inner]
        return ToolResponse.model_validate(payload)
    if done.code == 2:
        raise CliUsageError(tool, _refusal_kind(done), _refused_flags(done))
    if done.code == 3 and _TIER_REFUSED in done.stdout:
        raise CliTierRefusal(tool, f"{verb}: the caller's catalog hides {tool}")
    errors = [line for line in done.stderr.splitlines() if line.startswith("error: ")]
    if done.code == 1 and errors:
        raise LiveStackToolError(tool, errors[0].removeprefix("error: "))
    first = done.stdout.strip().splitlines()[:1]
    raise AssertionError(f"{verb} exited {done.code} with no envelope: {first}")


@dataclass(frozen=True)
class CliCaller(HttpCaller):
    """Reach a tool through its generated ``kdivectl`` verb (ADR-0795)."""

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse | list[ToolResponse]:
        argv = cli_argv(tool, args)
        _check_exact(tool, argv, args)
        done = await run_kdivectl(argv, token=token, base_url=self.base_url)
        try:
            return _result(tool, argv, done)
        except CliTierRefusal:
            # UNKNOWN also answers a listed tool with bad annotations; only a hidden one is denied.
            assert tool not in await _catalog(self.base_url, token), (
                f"kdivectl refused listed {tool}"
            )
            raise

    async def authenticate(
        self, tool: str, args: Mapping[str, object], genuine: str
    ) -> dict[str, object]:
        argv = cli_argv(tool, args)
        forged = await run_kdivectl(argv, token=forge(genuine), base_url=self.base_url)
        if forged.code == 2:
            raise CliTranslationGap(f"kdivectl {argv[0]} {argv[1]}: {_refused_flags(forged)}")
        assert forged.code == 1 and _auth_failed(forged), (
            f"forged-token kdivectl {argv[0]} {argv[1]} exited {forged.code} without the diagnostic"
        )
        control = await run_kdivectl(argv, token=genuine, base_url=self.base_url)
        assert not _auth_failed(control), f"the issued token's kdivectl {argv[1]} also failed"
        return {
            "exit": 1,
            "diagnostic": "authentication-failed",
            "token": "foreign-signature",
            "issued_token_exit": control.code,
        }


async def _catalog(base_url: str, token: str) -> set[str]:
    transport = StreamableHttpTransport(url=base_url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport) as client:
        return {tool.name for tool in await client.list_tools()}


def caller_for(exposure: str, base_url: str, issuer: OidcIssuer) -> HttpCaller:
    """The caller of a cell's exposure: ``cli`` drives kdivectl, the others call over HTTP."""
    if exposure == "cli":
        return CliCaller("cli", base_url, issuer)
    return HttpCaller(cast(Exposure, exposure), base_url, issuer)


def http_caller(caller: HttpCaller) -> HttpCaller:
    """``caller``, or a ``direct`` one for a CLI cell's owned-state cleanup."""
    if isinstance(caller, CliCaller):
        return HttpCaller("direct", caller.base_url, caller.issuer)
    return caller


async def operator_catalog(base_url: str, issuer: OidcIssuer, grants: Grants) -> dict[str, object]:
    """Each tool's ``inputSchema`` in the operator-direct catalog for ``grants``.

    Fails unless the catalog is unclipped: a token whose ``azp`` the server does not take for
    ``kdivectl`` gets the agent-gateway profile, clipped to ``CORE_TOOLS`` (ADR-0268), and would
    make every ``direct`` cell name an exposure the run never had.
    """
    token = HttpCaller("direct", base_url, issuer).token(grants)
    transport = StreamableHttpTransport(url=base_url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport) as client:
        catalog = {tool.name: tool.inputSchema for tool in await client.list_tools()}
    assert set(catalog) - CORE_TOOLS, (
        "operator catalog is clipped to CORE_TOOLS: the server does not resolve this process's "
        "KDIVE_CLI_CLIENT_ID as kdivectl; use the same value for the server and the tests"
    )
    return catalog


@dataclass(frozen=True)
class FilteredListControl:
    """The positive control of a list tool that filters instead of rejecting (ADR-0756).

    ``expected_item`` names, independently of the list call, a real protected item; ``grants``
    are a viewer's, which must see that item listed by the same arguments and exposure.
    """

    grants: Grants
    expected_item: Callable[[], Awaitable[str]]


@dataclass(frozen=True)
class Rejection:
    """One call a boundary must reject: its arguments, grants and accepted categories.

    ``filtered`` is required exactly for a contract-named filtering list boundary
    (``FILTERED_LIST_BOUNDARIES``): its denied call must answer the canonical empty page after
    the control proved the protected item visible to a viewer.
    ``absent_twin`` holds arguments naming an owner that does not exist: the rejected answer
    must be indistinguishable from the twin's, so a category shared with an unrelated failure
    cannot pass the cell.
    """

    args: Mapping[str, object]
    grants: Grants
    categories: frozenset[str] = frozenset({ErrorCategory.AUTHORIZATION_DENIED.value})
    filtered: FilteredListControl | None = None
    absent_twin: Mapping[str, object] | None = None


def rejected_by_validation(exposure: str, result: Result) -> bool:
    """A schema rejection of the call's arguments (ADR-0722 §3).

    ``direct`` accepts a ``configuration_error`` envelope or a tool error naming validation.
    ``gateway`` accepts only ``tools.invoke``'s argument-binding failure: ``tools.invoke`` also
    answers ``configuration_error`` for an unknown tool, an inner handler's error and a tool body
    re-validating its own data, and only the binding failure carries non-empty
    ``data.field_errors`` beside its "failed schema validation" detail
    (``src/kdive/mcp/tools/gateway.py``). A tool whose binding failure ``BindingErrorMiddleware``
    re-envelopes (``src/kdive/mcp/middleware/binding_errors.py``) never reaches that branch, so
    its ``gateway`` validation cell fails here rather than recording ambiguous evidence.
    ``cli`` follows ``direct`` and also accepts ``kdivectl`` refusing a declared option's value
    before any call (ADR-0795).
    """
    if isinstance(result, CliUsageError):
        return exposure == "cli"
    if isinstance(result, LiveStackToolError):
        return exposure in ("direct", "cli") and "validation error" in result.message.lower()
    if not (
        isinstance(result, ToolResponse)
        and result.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    ):
        return False
    if exposure in ("direct", "cli"):
        return True
    return bool(result.data.get("field_errors")) and "failed schema validation" in (
        result.detail or ""
    )


def refused_for_capability(result: Result) -> TypeIs[ToolResponse]:
    """ADR-0209's capability refusal, which a ``capability`` cell requires (ADR-0722, 2026-10-13).

    ``configuration_error`` alone also answers schema and name failures; only the refusal carries
    ``data.reason`` ``capability_unsupported``. ``tools.invoke`` passes the handler's envelope
    through, so both exposures carry it.
    """
    return (
        isinstance(result, ToolResponse)
        and result.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        and result.data.get("reason") == "capability_unsupported"
    )


def _shape(result: Result) -> dict[str, object]:
    """A message-free description of ``result`` for evidence."""
    if isinstance(result, CliUsageError):
        # Option names only: which declared option kdivectl refused, never its value.
        return {"usage_error": result.kind, "refused": result.refused}
    if isinstance(result, CliTierRefusal):
        return {"catalog_hidden": True, "error_category": ErrorCategory.AUTHORIZATION_DENIED.value}
    if isinstance(result, LiveStackToolError):
        return {
            "tool_error": True,
            "names_validation": "validation error" in result.message.lower(),
        }
    if isinstance(result, list):
        return {"envelopes": len(result)}
    return {"status": result.status, "error_category": result.error_category}


async def _attempt(
    caller: Caller, tool: str, rejection: Rejection, args: Mapping[str, object] | None = None
) -> Result:
    try:
        call = rejection.args if args is None else args
        return await caller.call(tool, call, caller.token(rejection.grants))
    except LiveStackToolError as exc:
        return exc


def _empty_page(tool: str, args: Mapping[str, object]) -> ToolResponse:
    """The page a filtering list tool answers a caller who may see nothing (ADR-0756).

    A named tool whose carrier has not yet added its page fails here rather than borrowing
    another tool's.
    """
    if tool == "investigations.list":
        object_id, actions = "investigations", ["investigations.get", "investigations.open"]
    elif tool == "artifacts.list":
        object_id, actions = str(args["system_id"]), ["artifacts.get"]
    elif tool == "debug.list_sessions":
        object_id, actions = "debug_sessions", ["debug.get_session"]
    elif tool == "runs.list":
        object_id, actions = "runs", ["runs.get", "runs.create"]
    elif tool == "systems.list":
        object_id, actions = "systems", ["systems.get", "runs.create"]
    elif tool == "jobs.list":
        object_id, actions = "jobs", ["jobs.wait", "jobs.cancel"]
    elif tool == "allocations.list":
        # Names the requested project; its next actions are filtered to the caller's roles there.
        request = args.get("request")
        project = request.get("project") if isinstance(request, Mapping) else None
        assert isinstance(project, str) and project, (
            "an allocations.list filtered rejection must name a project"
        )
        return ToolResponse.collection(
            "allocations",
            "ok",
            [],
            suggested_next_actions=[],
            data={"truncated": False, "next_cursor": None, "project": project},
        )
    else:
        raise AssertionError(f"no canonical empty page is configured for {tool}")
    return ToolResponse.collection(
        object_id,
        "ok",
        [],
        suggested_next_actions=actions,
        data={"truncated": False, "next_cursor": None},
    )


async def _observe_filtered(
    caller: Caller, tool: str, rejection: Rejection, control: FilteredListControl
) -> dict[str, object]:
    """A viewer sees the known item, then the denied caller gets exactly the empty page."""
    empty = _empty_page(tool, rejection.args).model_dump()
    expected = await control.expected_item()
    assert expected, f"{tool} has no independently known protected item"
    seen = await _attempt(caller, tool, replace(rejection, grants=control.grants))
    assert isinstance(seen, ToolResponse) and seen.status == "ok", (
        f"{tool} control call failed: {_shape(seen)}"
    )
    assert seen.error_category is None and seen.detail is None, (
        f"{tool} control call failed: {_shape(seen)}"
    )
    listed = [i.object_id for i in seen.items if i.error_category is None]
    assert expected in listed, f"{tool} did not list the known item to a viewer ({len(listed)})"
    result = await _attempt(caller, tool, rejection)
    answer = result.model_dump() if isinstance(result, ToolResponse) else _shape(result)
    differs = sorted(k for k in empty.keys() | answer.keys() if answer.get(k) != empty.get(k))
    assert not differs, f"{tool} denied answer is not the empty page; differs in {differs}"
    return {
        "denied_visibility": {"status": "ok", "canonical_empty_page": True},
        "viewer_control": {
            "listed": len(listed),
            "item_sha256": hashlib.sha256(expected.encode()).hexdigest(),
        },
    }


def _answer(result: Result, args: Mapping[str, object]) -> dict[str, object]:
    """``result`` with every string argument masked, to compare a call with its absent twin."""
    if not isinstance(result, ToolResponse):
        return _shape(result)
    text = json.dumps(
        [result.object_id, result.status, result.error_category, result.detail, result.data],
        sort_keys=True,
        default=str,
    )
    for value in args.values():
        if isinstance(value, str) and value:
            text = text.replace(value, "<arg>")
    return {"answer_sha256": hashlib.sha256(text.encode()).hexdigest(), **_shape(result)}


async def _observe(
    caller: Caller, tool: str, boundary: Boundary, rejection: Rejection
) -> dict[str, object]:
    named = (tool, boundary) in FILTERED_LIST_BOUNDARIES
    assert named == (rejection.filtered is not None), (
        f"{tool} {boundary}: a filtering-list control is required exactly for "
        "the contract's FILTERED_LIST_BOUNDARIES"
    )
    if rejection.filtered is not None:
        return await _observe_filtered(caller, tool, rejection, rejection.filtered)
    if boundary == "authentication":
        genuine = caller.token(rejection.grants)
        if isinstance(caller, CliCaller):
            return await caller.authenticate(tool, rejection.args, genuine)
        status = await caller.post(tool, rejection.args, forge(genuine))
        assert status == 401, f"forged-signature call to {tool} answered HTTP {status}, not 401"
        # The same request with the issued token must pass authentication, so the 401 above is
        # the signature's and not the request's.
        control = await caller.post(tool, rejection.args, genuine)
        assert control != 401, f"the issued token's call to {tool} was also refused with 401"
        return {"http_status": status, "token": "foreign-signature", "issued_token_status": control}
    result = await _attempt(caller, tool, rejection)
    if boundary == "capability":
        assert refused_for_capability(result), (
            f"{tool} was not refused for capability: {_shape(result)}"
        )
        return {"rejected": _shape(result), "capability": result.data.get("capability")}
    if boundary == "validation":
        assert rejected_by_validation(caller.exposure, result), (
            f"{tool} was not rejected by argument validation: {_shape(result)}"
        )
        return {"rejected": _shape(result)}
    # kdivectl's refusal of a tool the server hid from the caller's catalog (ADR-0795).
    category = (
        ErrorCategory.AUTHORIZATION_DENIED.value
        if isinstance(result, CliTierRefusal)
        else result.error_category
        if isinstance(result, ToolResponse)
        else None
    )
    assert category is not None, f"{tool} was not rejected: {_shape(result)}"
    assert category in rejection.categories, (
        f"{tool} rejected with {category}, not one of {sorted(rejection.categories)}"
    )
    observation: dict[str, object] = {"rejected": _shape(result)}
    if rejection.absent_twin is not None:
        twin = await _attempt(caller, tool, rejection, rejection.absent_twin)
        answered, absent = _answer(result, rejection.args), _answer(twin, rejection.absent_twin)
        assert answered == absent, f"{tool} answered {answered}, an absent owner {absent}"
        observation["absent_owner_twin"] = answered
    return observation


def _digest(state: object) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


async def prove_rejection(
    run: CellRun,
    caller: Caller,
    boundary: Boundary,
    rejection: Rejection,
    snapshot: Snapshot,
    *,
    setup: Setup | None = None,
) -> None:
    """Prove ``boundary`` rejects the call and the protected state is unchanged (ADR-0722).

    ``setup`` provides state the rejected call needs and removes it on exit; it yields argument
    overrides. ``unchanged-state`` brackets the call alone, and ``cleanup`` compares the snapshot
    taken before ``setup`` with the one taken after it, so the setup's own state must go.
    """
    operation = run.cell.operation
    outer = await snapshot() if setup is not None else None
    context: AbstractAsyncContextManager[Mapping[str, object]] = (
        setup() if setup is not None else nullcontext({})
    )
    async with context as overrides:
        effective = replace(rejection, args={**rejection.args, **overrides})
        before = await snapshot()
        observation = await _observe(caller, operation, boundary, effective)
        run.prove(boundary, {"exposure": caller.exposure, **observation})
        after = await snapshot()
        assert after == before, f"protected state changed across the rejected {operation}"
        run.prove("unchanged-state", {"snapshot_sha256": _digest(before)})
    final = await snapshot() if setup is not None else after
    start = outer if setup is not None else before
    assert final == start, f"the setup of the rejected {operation} left state behind"
    owned = sorted(str(value) for value in overrides.values())
    run.prove("cleanup", {"owned": owned, "snapshot_sha256": _digest(start)})


async def prove_refusal(
    run: CellRun, caller: Caller, refusal: Rejection, detail: str, snapshot: Snapshot
) -> None:
    """Prove an authorized call is refused as ``not_implemented`` and writes nothing (ADR-0759).

    ``effect`` is the exact refusal envelope: not retryable, the requested object, ``detail``, no
    job reference and no next action. ``cleanup`` is the unchanged snapshot around the call. The
    request is retained before the call, so a failed proof still names what an operator must
    clean up.
    """
    operation = run.cell.operation
    before = await snapshot()
    request = {"cell": run.cell.id, "request": dict(refusal.args), "before": before}
    run.artifacts.append(run.writer.artifact(request))
    result = await caller.call(operation, refusal.args, caller.token(refusal.grants), discover=True)
    assert isinstance(result, ToolResponse), f"expected one envelope, got {len(result)}"
    refused = {
        "error_category": result.error_category,
        "retryable": result.retryable,
        "object_id": result.object_id == refusal.args.get("name"),
        "detail": result.detail == detail,
        "refs": dict(result.refs),
        "suggested_next_actions": result.suggested_next_actions,
    }
    assert refused == {
        "error_category": ErrorCategory.NOT_IMPLEMENTED.value,
        "retryable": False,
        "object_id": True,
        "detail": True,
        "refs": {},
        "suggested_next_actions": [],
    }, f"{operation} {refusal.args} was not refused at admission: {refused}"
    after = await snapshot()
    assert after == before, f"the refused {operation} {refusal.args} changed protected state"
    run.prove("effect", {"exposure": caller.exposure, "refused": refused})
    run.prove("cleanup", {"owned": [], "snapshot_sha256": _digest(before)})


Functional = Callable[[HttpCaller, Grants], Awaitable[dict[str, object]]]


async def prove_functional(
    run: CellRun, caller: HttpCaller, grants: Grants, body: Functional, snapshot: Snapshot
) -> None:
    """Prove ``effect`` with ``body``, then ``cleanup`` as an unchanged project snapshot.

    A body that writes state removes it and names it under ``owned``.
    """
    before = await snapshot()
    observed = await body(caller, grants)
    owned = observed.pop("owned", [])
    run.prove("effect", {"exposure": caller.exposure, **observed})
    after = await snapshot()
    assert after == before, f"{run.cell.operation} left durable state in the cell's project"
    run.prove("cleanup", {"owned": owned, "snapshot_sha256": _digest(before)})


async def require_snapshot_read_access(
    conn: psycopg.AsyncConnection, tables: Sequence[str]
) -> None:
    """Fail before the snapshot reads when the DSN cannot SELECT a table, naming each one."""
    cursor = await conn.execute(
        "SELECT name FROM unnest(%s::text[]) WITH ORDINALITY AS t(name, position) "
        "WHERE NOT has_table_privilege(format('public.%%I', name), 'SELECT') ORDER BY position",
        (list(tables),),
    )
    denied = [str(row[0]) for row in await cursor.fetchall()]
    if denied:
        raise PermissionError(
            f"KDIVE_DATABASE_URL cannot SELECT {', '.join(denied)}; the protected-state snapshot "
            "reads every table with a project column, some of them authority-private. Set "
            'KDIVE_DATABASE_URL="$KDIVE_HARNESS_DATABASE_URL" (the least-privilege harness '
            "login); if it is already set, rerun the role bootstrap, which re-grants new tables: "
            "scripts/live-stack/stack-services.sh, or after `just stack-backends` "
            "`env -u KDIVE_MIGRATION_DATABASE_URL docker compose run --rm --no-deps role-bootstrap`"
        )


async def project_state(db_url: str, project: str) -> dict[str, list[object]]:
    """Row count and row-text SHA-256 of ``project`` in every public table with a project column.

    Tables come from ``pg_catalog``, which lists every table whatever the DSN may read, so a
    table the DSN cannot read fails the snapshot instead of leaving it.
    """
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        # The harness login can write its seeding tables; a read-only session keeps this a read.
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT c.relname FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
            "AND a.attname = 'project' AND NOT a.attisdropped ORDER BY c.relname"
        )
        tables = [str(row[0]) for row in await cursor.fetchall()]
        tables = [t for t in tables if t not in _AUDIT_TABLES]
        assert tables, "no table carries a project column; the snapshot would observe nothing"
        await require_snapshot_read_access(conn, tables)
        state: dict[str, list[object]] = {}
        for table in tables:
            query = sql.SQL(
                "SELECT count(*), encode(sha256(convert_to(coalesce("
                "string_agg(t::text, ',' ORDER BY t::text), ''), 'UTF8')), 'hex') "
                "FROM {} t WHERE t.project = %s"
            ).format(sql.Identifier(table))
            row = await (await conn.execute(query, (project,))).fetchone()
            assert row is not None
            state[table] = [row[0], row[1]]
    return state


def boundary_of(cell: Cell) -> Boundary:
    """The rejection boundary a rejection cell's scenario names."""
    boundary = cell.scenario_id.rsplit("/", 1)[1]
    assert boundary in get_args(Boundary), f"{cell.scenario_id} names no rejection boundary"
    return cast(Boundary, boundary)


def admission_refusal(cell: Cell) -> bool:
    """A server-only admission refusal (ADR-0759): an unsupported cell observing no guest."""
    return cell.kind == "unsupported" and cell.guest_arch is None


def tool_cells(tools: Sequence[str]) -> list[Cell]:
    """The contract's cells of ``tools``."""
    return [cell for cell in build_contract().cells if cell.operation in tools]


def server_configuration(base_url: str, issuer: OidcIssuer) -> tuple[str, list[str]]:
    """The proven configuration and the recovery tools listed; read once per stack."""
    if base_url not in _CONFIGURATIONS:
        grants = Grants("cov-configuration", ("cov-configuration",), {}, ("platform_operator",))
        catalog = asyncio.run(operator_catalog(base_url, issuer, grants))
        listed = sorted(RECOVERY_TOOLS & set(catalog))
        _CONFIGURATIONS[base_url] = (configuration_of(catalog), listed)
    return _CONFIGURATIONS[base_url]


ToolScenario = Callable[[CellRun, str, OidcIssuer, str], Awaitable[None]]


def run_tool_cell(cell: Cell, scenario: ToolScenario) -> None:
    """Prove the configuration, skip another configuration's cell, else run and record it.

    A completed rejection cell records ``rejection``, an unsupported one ``unsupported`` and a
    functional one ``success``. A local provider cell, or a server-only admission refusal, of
    another host architecture is skipped before the stack is read; any other remote cell's host
    is the provider host, which its frame observes.
    """
    on_server = cell.provider != "remote-libvirt" or admission_refusal(cell)
    if on_server and cell.host_arch not in (None, platform.machine()):
        pytest.skip(f"{cell.id} runs on a {cell.host_arch} host")
    base_url = require_stack()
    try:
        issuer = require_issuer()
    except pytest.skip.Exception as exc:
        # Without the issuer the configuration cannot be read, so no cell could be placed.
        pytest.fail(f"cannot prove the stack's configuration: {exc}; set KDIVE_OIDC_ISSUER")
    configuration, listed = server_configuration(base_url, issuer)
    if configuration != cell.configuration:
        pytest.skip(f"stack runs {configuration}; {cell.id} needs {cell.configuration}")

    async def proven(run: CellRun, url: str, issuer: OidcIssuer, db_url: str) -> None:
        proof = {"cell": run.cell.id, "configuration": configuration, "recovery_tools": listed}
        run.artifacts.append(run.writer.artifact(proof))
        await scenario(run, url, issuer, db_url)

    proves = {"rejection": Outcome.REJECTION, "unsupported": Outcome.UNSUPPORTED}
    run_cell(cell, proven, proves=proves.get(cell.kind, Outcome.SUCCESS))


def bindings(
    candidate: str,
    *,
    host_os: str,
    host_arch: str,
    matrix: str,
    cells: Iterable[Cell],
    staged: Callable[[str], Path | None] = staged_image,
    kernel: Mapping[str, str] | None = None,
    remote: RemoteHost | None = None,
    digest: Callable[[str, str, str], str] = volume_sha256,
    volumes: Callable[[str], str | None] = staged_volume,
) -> InputBindings:
    """The expected ``Context`` of every bound service, native local and remote tool cell.

    A native cell boots ``LANE_IMAGES[host_arch]``: its ``image_sha256`` is the staged bytes'
    digest, null when unstaged. With ``remote``, a remote cell of the provider host's
    architecture boots that architecture's remote representative there: its host is the provider
    host and its ``image_sha256`` the staged base volume's digest, null when unstaged. ``kernel``
    fills only the kernel fields a cell declares. A functional ``images.publish`` cell boots
    ``PUBLISHED_IMAGES[(host_arch, exposure)]``, the image it publishes, with a null
    ``image_sha256``. An unsupported provider cell without a guest is a server-only admission
    refusal (ADR-0759): like a service cell it binds this host alone, on its own architecture.
    """
    bound = [c for c in cells if c.scenario_id.startswith("tool/") and c.node_id]
    contexts = {
        c.id: Context.model_validate(
            {"host_os": host_os, "host_arch": host_arch, "accelerator": "none"}
        )
        for c in bound
        if c.provider == "service" or (admission_refusal(c) and c.host_arch == host_arch)
    }
    native = [c for c in bound if c.provider == "local-libvirt" and c.guest_arch == host_arch]
    published = [c for c in native if c.operation == PUBLISH_TOOL and c.kind == "functional"]
    native = [c for c in native if c not in published]
    for cell in published:
        product = PUBLISHED_IMAGES.get((host_arch, cell.exposure))
        if product is not None:
            # The published image is the cell's output, so no input digest binds it.
            entry = load_rootfs_catalog()[product]
            contexts |= _lane_contexts([cell], host_os, host_arch, entry, None, kernel=kernel)
    name = LANE_IMAGES.get(host_arch)
    if native and name is not None:
        image = staged(name)
        sha = file_sha256(image) if image is not None else None
        entry = load_rootfs_catalog()[name]
        contexts |= _lane_contexts(native, host_os, host_arch, entry, sha, kernel=kernel)
    family = REMOTE_LANE_FAMILIES.get(remote.host_arch) if remote is not None else None
    if remote is not None and family is not None:
        hosted = [
            c for c in bound if c.provider == "remote-libvirt" and c.guest_arch == remote.host_arch
        ]
        base = REMOTE_REPRESENTATIVES[family]
        volume = volumes(base.name) if hosted else None
        sha = digest(remote.dest, remote.pool, volume) if volume else None
        contexts |= _lane_contexts(
            hosted, remote.host_os, remote.host_arch, base, sha, kernel=kernel
        )
    return InputBindings(version=1, candidate_sha=candidate, matrix_sha256=matrix, cells=contexts)


def _lane_contexts(
    cells: Iterable[Cell],
    host_os: str,
    host_arch: str,
    entry: GuestIdentity,
    digest: str | None,
    *,
    kernel: Mapping[str, str] | None,
) -> dict[str, Context]:
    return {
        cell.id: Context.model_validate(
            {
                "host_os": host_os,
                "host_arch": host_arch,
                "guest_os": f"{entry.distro}:{entry.version}",
                "guest_arch": entry.arch,
                "accelerator": cell.accelerator,
                "image_sha256": digest,
                **{k: v for k, v in (kernel or {}).items() if k in cell.inputs},
            }
        )
        for cell in cells
    }


def lane_image() -> tuple[str, RootfsCatalogEntry]:
    """The catalog image this host's provider cells boot; blocked on an unlisted architecture."""
    name = LANE_IMAGES.get(platform.machine())
    if name is None:
        raise ScenarioStop(Outcome.BLOCKED, f"no lane image for {platform.machine()}")
    return name, load_rootfs_catalog()[name]


Connect = Callable[[], libvirt.virConnect]


def worker_connect() -> libvirt.virConnect:
    """The worker's own libvirt: the local lane's provider."""
    return libvirt.open(worker_libvirt_uri())


def domain_kernel(
    system_id: str, _endpoint: Endpoint, _key: Path, _release: str
) -> tuple[str, str]:
    """The ``<os><kernel>`` file the domain boots (the install's staged kernel) and its digest."""
    kernel = ET.fromstring(domain_xml(system_id)).findtext("./os/kernel")  # noqa: S314  # nosec B314
    assert kernel, "the installed domain names no direct kernel"
    return file_sha256(kernel), kernel


def _local_spool(system_id: str) -> Callable[[str], str]:
    """The System's capture directory, whichever job writes into it."""
    path = str(pcap_dir(UUID(system_id)))
    return lambda _job_id: path


def remote_pcap_spool(dest: str, pool: str, system_id: str) -> Callable[[str], str]:
    """Name the provider-host pcap volume a capture job of ``system_id`` writes into ``pool``.

    The remote capturer writes a per-job volume into the configured storage pool (ADR-0432), so
    the System owns it until the job reclaims it. The pool's target is read here, before any job
    exists; the returned namer does no I/O.
    """
    conn = observer(dest)
    try:
        xml = conn.storagePoolLookupByName(pool).XMLDesc(0)
    finally:
        conn.close()
    target = ET.fromstring(xml).findtext("./target/path")  # noqa: S314  # nosec B314
    assert target, f"storage pool {pool} has no target path"
    directory = PurePosixPath(target)
    return lambda job_id: str(directory / pcap_volume_name(UUID(system_id), UUID(job_id)))


class LaneFrame(Protocol):
    """A provider's System frame: provision, run ``body``, then prove owned cleanup."""

    def __call__(
        self,
        run: CellRun,
        base_url: str,
        issuer: OidcIssuer,
        db_url: str,
        *,
        project: str,
        body: CatalogBody,
        provision: Provision,
    ) -> Awaitable[None]: ...


@dataclass(frozen=True)
class Lane:
    """How one provider frames, profiles and observes a provider cell's System.

    ``profile`` takes the profile's unread ``kernel_source_ref``. ``xml`` and ``absent`` read the
    provider's own libvirt: the worker's for local-libvirt, the provider host's through the
    operator's observer for remote-libvirt. ``connect`` opens that libvirt for a native read.
    ``spool(system)`` names the provider-host path each capture job of the System writes (the
    System's directory locally, the job's pool volume remotely), reading any provider state
    before a job exists. ``installed_kernel`` observes the kernel an install put in place (the
    domain's direct kernel locally, the guest's own ``/boot`` remotely).
    """

    provider: str
    entry: GuestIdentity
    profile: Callable[[str], dict[str, object]]
    xml: Callable[[str], str]
    absent: Callable[[str], bool]
    frame: LaneFrame
    connect: Connect
    spool: Callable[[str], Callable[[str], str]]
    installed_kernel: InstalledKernel


def _local_lane() -> Lane:
    name, entry = lane_image()
    return Lane(
        "local-libvirt",
        entry,
        partial(catalog_profile, entry, name),
        domain_xml,
        disk_absent,
        partial(on_catalog_system, image=name),
        worker_connect,
        _local_spool,
        domain_kernel,
    )


def _remote_lane(arch: str) -> Lane:
    family = REMOTE_LANE_FAMILIES.get(arch)
    if family is None:
        raise ScenarioStop(Outcome.BLOCKED, f"no remote lane image for {arch}")
    image = REMOTE_REPRESENTATIVES[family]
    host = remote_host()
    volume = staged_base_volume(image)

    def profile(_unread_ref: str) -> dict[str, object]:
        return remote_profile(image.arch, volume)

    return Lane(
        "remote-libvirt",
        image,
        profile,
        partial(remote_xml, host.dest),
        partial(remote_volume_absent, host.dest),
        partial(on_remote_system, family=family),
        partial(observer, host.dest),
        partial(remote_pcap_spool, host.dest, host.pool),
        partial(guest_boot_kernel, family=family),
    )


def lane_for(cell: Cell) -> Lane:
    """The lane of provider cell ``cell``: its provider host when remote, else this host."""
    if cell.provider == "remote-libvirt":
        return _remote_lane(str(cell.guest_arch))
    return _local_lane()


async def observe_guest(
    run: CellRun, op: LiveStackClient, system_id: str, scratch: Path, entry: GuestIdentity
) -> tuple[Endpoint, Path, dict[str, str]]:
    """Authorize a frame key, probe the guest as root and record its identity on ``run``."""
    scratch.mkdir(parents=True, exist_ok=True)
    endpoint, key = await asyncio.wait_for(
        authorize_ssh(op, system_id, scratch, "tool-cell"), timeout=900
    )
    probe = await asyncio.to_thread(ssh_probe, endpoint, key, IDENTITY_PROBE)
    assert probe.get("uid") == "0", f"ssh as root reported uid {probe.get('uid')!r}"
    assert os_matches(entry, probe), f"guest {probe.get('ID')} is not catalog {entry.distro}"
    run.observed |= {"guest_os": f"{entry.distro}:{entry.version}", "guest_arch": entry.arch}
    return endpoint, key, probe


@dataclass(frozen=True)
class Guest:
    """A ``ready`` lane System as a provider cell's body sees it.

    ``owned`` is the frame's list of provider-host paths the cleanup proves absent; a body that
    makes the System own more appends to it. ``observed`` is the cell's recorded context: a body
    that boots a kernel writes the kernel fields it observed there.
    """

    op: LiveStackClient
    project: str
    system_id: str
    lane: Lane
    endpoint: Endpoint
    key: Path
    probe: dict[str, str]
    owned: list[str]
    observed: dict[str, object]
    scratch: Path


LaneBody = Callable[[Guest], Awaitable[dict[str, object]]]


async def on_lane_system(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    *,
    project: str,
    body: LaneBody,
    provision: Provision = provision_catalog,
) -> None:
    """Provision the lane image in ``project``, observe its guest, prove ``effect``, then cleanup.

    ``body`` returns the ``effect`` observation; the frame adds the exposure.
    """
    lane = lane_for(run.cell)

    async def framed(op: LiveStackClient, system_id: str, owned: list[str]) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            frame = root / "frame"
            endpoint, key, probe = await observe_guest(run, op, system_id, frame, lane.entry)
            guest = Guest(
                op, project, system_id, lane, endpoint, key, probe, owned, run.observed, root
            )
            observed = await body(guest)
        run.prove("effect", {"exposure": run.cell.exposure, **observed})

    await lane.frame(
        run, base_url, issuer, db_url, project=project, body=framed, provision=provision
    )


@dataclass(frozen=True)
class LaneTarget:
    """A torn-down lane System and its released Allocation: what rejection cells aim at.

    ``observed`` is the target's host, guest, accelerator and image identity, which each
    rejection record carries; ``artifacts`` hold its own evidence and cleanup proof. ``profile``
    is the lane's provisioning profile, for the calls that name one.
    """

    project: str
    allocation_id: str
    system_id: str
    observed: dict[str, object]
    artifacts: tuple[str, ...]
    profile: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _Failed:
    """A lane target preparation that raised, and the artifacts its cleanup attempt left."""

    error: Exception
    artifacts: tuple[str, ...]


_TARGETS: dict[tuple[str, str], LaneTarget | _Failed] = {}


async def _allocation_of(db_url: str, system_id: str) -> str:
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute("SELECT allocation_id FROM systems WHERE id = %s", (system_id,))
        row = await cursor.fetchone()
    assert row is not None, f"System {system_id} has no row"
    return str(row[0])


async def settled(db_url: str, project: str) -> None:
    """Wait until ``project``'s snapshot stops changing (release bookkeeping runs after it)."""
    for _ in range(_SETTLE_ATTEMPTS):
        before = await project_state(db_url, project)
        await asyncio.sleep(_SETTLE_S)
        if await project_state(db_url, project) == before:
            return
    raise AssertionError(f"project {project} kept changing after its System was released")


async def _provision_target(
    run: CellRun, target: CellRun, base_url: str, issuer: OidcIssuer, db_url: str
) -> LaneTarget:
    """Provision, observe and reclaim the target; ``target`` collects its own evidence."""
    lane = lane_for(run.cell)
    project = f"cov-{secrets.token_hex(4)}"
    seen: list[str] = []

    async def observe(op: LiveStackClient, system_id: str, _owned: list[str]) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            await observe_guest(target, op, system_id, Path(scratch), lane.entry)
        seen.append(system_id)

    await lane.frame(
        target,
        base_url,
        issuer,
        db_url,
        project=project,
        body=observe,
        provision=provision_catalog,
    )
    allocation = await _allocation_of(db_url, seen[0])
    await settled(db_url, project)
    cleanup = target.assertions["cleanup"]
    summary = run.writer.artifact(
        {"lane_target": {"system": "torn_down", "allocation": "released"}, "cleanup": cleanup}
    )
    return LaneTarget(
        project,
        allocation,
        seen[0],
        dict(target.observed),
        (*target.artifacts, cleanup, summary),
        lane.profile(f"{project}-unread"),
    )


async def lane_target(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> LaneTarget:
    """The stack's rejection target, provisioned, observed and reclaimed on first use.

    Every role check of the System tools runs before any state check, so a torn-down System and
    a released Allocation are valid targets that no background work changes. A failed or
    blocked preparation is remembered and replayed, with its cleanup-attempt artifacts, for every
    later cell rather than retried. Each provider of a stack has its own target.
    """
    key = (base_url, run.cell.provider)
    cached = _TARGETS.get(key)
    if cached is None:
        target = CellRun(run.cell, run.writer)
        try:
            cached = await _provision_target(run, target, base_url, issuer, db_url)
        except Exception as exc:  # noqa: BLE001 - remembered and replayed for every cell
            # A cleanup the target already proved is evidence too, beside any cleanup attempt.
            cached = _Failed(exc, (*target.artifacts, *target.assertions.values()))
        _TARGETS[key] = cached
    if isinstance(cached, _Failed):
        run.artifacts.extend(cached.artifacts)
        if isinstance(cached.error, ScenarioStop):
            raise ScenarioStop(cached.error.outcome, str(cached.error))
        raise AssertionError(f"the lane target could not be prepared: {cached.error!r}")
    run.observed |= cached.observed
    run.artifacts.extend(cached.artifacts)
    return cached


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write tool-cell input bindings.")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("bindings")
    write.add_argument("--candidate", required=True)
    write.add_argument("--out", type=Path, required=True)
    write.add_argument(
        "--kernel-baseline",
        help=f"bind the verified ${FIXTURE_ROOT_ENV}/NAME kernel to cells declaring kernel inputs",
    )
    write.add_argument(
        "--remote",
        action="store_true",
        help=f"also bind the remote-libvirt cells of the provider host ${HOST_SSH_ENV} reaches",
    )
    args = parser.parse_args(argv)
    remote: RemoteHost | None = None
    if args.remote:
        try:
            remote = remote_host()
        except ScenarioStop as stop:
            print(f"cannot bind the remote cells: {stop}")
            return 2
        if remote.host_arch not in REMOTE_LANE_FAMILIES:
            print(f"cannot bind the remote cells: no remote lane image for {remote.host_arch}")
            return 2
    host_arch = platform.machine()
    kernel: dict[str, str] = {}
    if args.kernel_baseline:
        root = os.environ.get(FIXTURE_ROOT_ENV)
        if not root:
            parser.error(f"--kernel-baseline needs {FIXTURE_ROOT_ENV}")
        kernel = bound_kernel(Path(root), args.kernel_baseline, host_arch, load_fixture)
        if not kernel:
            print("no verified kernel fixture; kernel fields stay null")
    contract = build_contract()
    inputs = bindings(
        args.candidate,
        host_os=os_identity(Path("/etc/os-release").read_text(encoding="utf-8")),
        host_arch=host_arch,
        matrix=contract.matrix_sha256,
        cells=contract.cells,
        kernel=kernel,
        remote=remote,
    )
    args.out.write_text(inputs.model_dump_json(indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(inputs.cells)} tool-cell binding(s)")
    published = {
        c.id for c in contract.cells if c.operation == PUBLISH_TOOL and c.kind == "functional"
    }
    unstaged = sum(
        c.guest_arch is not None and c.image_sha256 is None
        for cell_id, c in inputs.cells.items()
        if cell_id not in published
    )
    if unstaged:
        print(
            f"{unstaged} provider binding(s) have no image digest: stage the lane image "
            f"({LANE_IMAGES.get(host_arch)} locally, the remote representative remotely) and "
            "declare it in KDIVE_SYSTEMS_TOML first"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
