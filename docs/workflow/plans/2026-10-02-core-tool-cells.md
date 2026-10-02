# Core MCP tool cells (#2811) implementation plan

Goal: move 46 tools of owner group 2811 to #3095-#3098, add the ADR-0722 tool-cell harness, and
prove the 56 cells of the six remaining tools live in both server configurations.

Architecture: `scripts/coverage_campaign/obligations.toml` changes ownership only. A new
`tests/integration/live_stack/tool_cells.py` wraps the existing `run_cell` frame
(`scenario.py`) with a configuration proof, an exposure-aware HTTP caller, and the shared
functional and rejection provers. `tests/integration/test_core_tool_cells_live.py` holds one
parametrized node that the 14 scenarios bind to.

Tech stack: Python 3.14, pytest, fastmcp client, httpx, psycopg 3 (all existing dependencies).

Spec: [2026-10-02-core-tool-cells-design.md](../specs/2026-10-02-core-tool-cells-design.md).
Decision: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md).

Expected implementation size: 1000–1100 changed lines (L) — the code blocks below: the harness
module (~400), its unit tests (~210), the live carrier (~220), the TOML move (~110 moved lines),
contract tests (~70), the `run_cell` change (~10) and the runbook section (~50). This sits at the
L ceiling; the band is unchanged.

## Global Constraints

- Python 3.14; ruff line length 100, lint set `E,F,I,UP,B,SIM`; `ty` strict over src and tests.
- No new dependency: use only `httpx`, `psycopg`, `fastmcp`, `pytest` and the standard library.
- A `KDIVE_*` env read outside `kdive.config` fails `just config-guard`; read config through
  `kdive.config`.
- Prose rule: no "critical", "robust", "comprehensive", "elegant"; use "Milestone".
- Evidence artifacts hold no token, host path, hostname or IP.
- Guardrails while iterating: `just lint`, `just type`, `just test-changed`; records gate
  `git fetch origin main && just records`; full gate `just ci > <file> 2>&1 < /dev/null`.

## File map

| File | Change | Owns after the change |
|---|---|---|
| `scripts/coverage_campaign/obligations.toml` | modify | owner groups 2811, 3095-3098; 14 new `[implementations]` rows |
| `tests/scripts/test_coverage_contract.py` | modify | owner-split and binding assertions |
| `tests/integration/live_stack/tool_cells.py` | create | ADR-0722 harness and `bindings` command |
| `tests/integration/live_stack/scenario.py` | modify | `run_cell` records the outcome its caller proves (`success` or `rejection`) |
| `tests/integration/live_stack/test_tool_cells.py` | create | harness unit tests (ordinary suite) |
| `tests/integration/test_core_tool_cells_live.py` | create | `test_core_tool_cell`, the six tools' scenarios |
| `docs/operating/runbooks/live-testing.md` | modify | how to run both lanes and qualify |

No `contract.py`, `evidence.py` or `demo-up.sh` change: owners are group data, and the server
inherits `KDIVE_WORKER_DEATH_VERIFIER` from the caller (`scripts/live-stack/lib.sh`). `run_cell`
changes because `qualify` requires outcome `rejection` for a rejection cell
(`scripts/coverage_campaign/results.py`, `_cell_verdict`), and `run_cell` records `success` only.

## Task 1: owner split

Files: modify `scripts/coverage_campaign/obligations.toml`,
`tests/scripts/test_coverage_contract.py`.

Interfaces: none consumed; Task 3 adds bindings to the same TOML.

Verification:

- Mode: focused-test. Contract: per-owner tool sets, 2811 cell count 56, role overrides kept.
  Test `tests/scripts/test_coverage_contract.py::test_core_tools_follow_the_approved_split`.
  Red: `assert {...} == {2811}` fails because 46 more tools are owned by 2811. Green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q` passes.

Steps:

1. Add to `tests/scripts/test_coverage_contract.py`:

```python
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
```

2. Run `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`; expect one failure
   in `test_core_tools_follow_the_approved_split`.
3. In `obligations.toml`, keep the first group (owner 2811) with only the six `_CORE_TOOLS`
   entries and no `[groups.role_overrides]`. Directly after it add four groups, moving each tool's
   observation text unchanged, in this order and shape:

```toml
[[groups]]
owner = 3095
execution = "service"
[groups.tools]
# images.delete, images.describe, images.kernel_config, images.list, images.upload,
# shapes.delete, shapes.list, shapes.set, resources.availability, resources.describe,
# resources.list — each with its existing observation string

[[groups]]
owner = 3096
execution = "service"
[groups.tools]
# the eight investigations.* and five artifacts.* entries

[[groups]]
owner = 3097
execution = "service"
[groups.tools]
# runs.bind, runs.complete_build, runs.create, runs.get, runs.list, runs.set,
# systems.get, systems.list, jobs.cancel, jobs.list, jobs.wait

[groups.role_overrides]
"jobs.cancel" = ["server", "worker", "reconciler", "authority"]
"jobs.wait" = ["server", "worker"]

[[groups]]
owner = 3098
execution = "service"
[groups.tools]
# allocations.list, allocations.release, allocations.renew, allocations.request,
# allocations.wait, accounting.estimate, accounting.report, accounting.set_budget,
# accounting.set_quota, accounting.usage, reports.generate

[groups.role_overrides]
"allocations.release" = ["server", "worker", "reconciler", "authority"]
"allocations.wait" = ["server", "reconciler"]
```

   The comment lines above stand for the moved `"name" = "observation"` lines; the committed
   file contains the lines themselves, not the comments.
4. Run `uv run python -m pytest tests/scripts/test_coverage_contract.py -q` (all pass) and
   `PYTHONPATH=. uv run python -m scripts.coverage_campaign check` (prints `126 tools`, a new
   `Matrix:` digest). Commit: `test(coverage): split core tool ownership into #3095-#3098`.

Acceptance: the per-owner sets match; `check` exits 0; the observation strings are unchanged
(`git diff --stat` shows equal insertions and deletions for moved lines plus group headers).

## Task 2: tool-cell harness

Files: create `tests/integration/live_stack/tool_cells.py`,
`tests/integration/live_stack/test_tool_cells.py`; modify
`tests/integration/live_stack/scenario.py` (`run_cell` gains `proves`).

Interfaces it consumes (existing, confirmed): `run_cell(cell, scenario)`, `CellRun`
(`.cell`, `.writer`, `.artifacts`, `.assertions`, `.prove(name, observation)`) from
`tests/integration/live_stack/scenario.py`; `EvidenceWriter(root)` from `evidence.py`;
`require_stack()`, `require_issuer()` from `conftest.py`; `LiveStackClient.over_http(url, token)`,
`.call_tool(name, **args)`, `.list_tools()`, `LiveStackToolError(.message)`, `mint_token`,
`make_keypair().create_token(subject=, issuer=, audience=, additional_claims=)` from
`kdive.mcp.dev_harness`; `build_contract()`, `Cell` from `scripts.coverage_campaign.contract`;
`Context`, `InputBindings` from `scripts.coverage_campaign.evidence`.

Interfaces it provides to Task 3: `Grants(subject, projects, roles, platform_roles)`,
`HttpCaller(exposure, base_url, issuer)` with `.token(grants) -> str`,
`async .call(tool, args, token, *, discover=False)`, `async .post(tool, args, token) -> int`,
`async operator_catalog(base_url, issuer, grants) -> dict[str, object]` (name → `inputSchema`);
`Rejection(args, grants, categories)`;
`claims_of(token)`, `forge(token)`, `matches(env)`, `one(result)`;
`async prove_functional(run, caller, grants, body, snapshot)`;
`async prove_rejection(run, caller, boundary, rejection, snapshot)`;
`async project_state(db_url, project)`; `boundary_of(cell)`; `tool_cells(tools)`;
`run_tool_cell(cell, scenario)`; and, in `scenario.py`,
`run_cell(cell, scenario, *, proves: Outcome = Outcome.SUCCESS)`.

Verification:

- Mode: focused-test. Contract: catalog → configuration classification, including the partial
  case. Test `test_configuration_follows_the_recovery_tools`. Red: `ImportError` before the
  module exists. Green: `uv run python -m pytest tests/integration/live_stack/test_tool_cells.py -q`.
- Mode: focused-test. Contract: each boundary's rejection rule and the unchanged-state check.
  Tests `test_authentication_needs_401`, `test_validation_rule_per_exposure`,
  `test_category_boundaries_use_the_closed_set`, `test_changed_state_fails_the_rejection`. Red and
  green as above.
- Mode: focused-test. Contract: `forge` keeps claims and the `kid` header and changes the
  signature. Test `test_forge_keeps_claims_and_changes_signature`.
- Mode: focused-test. Contract: `run_cell(..., proves=Outcome.REJECTION)` records outcome
  `rejection`, which `qualify` requires for a rejection cell. Test
  `test_run_cell_records_the_proven_outcome`. Red: `TypeError: unexpected keyword argument
  'proves'` before the `scenario.py` change.
- Mode: focused-test. Contract: `bindings` writes a `none`-accelerator context for every bound
  service tool cell only. Test `test_bindings_cover_bound_tool_cells`.

Steps:

1. Write `tests/integration/live_stack/test_tool_cells.py`:

```python
"""Unit tests of the ADR-0722 tool-cell harness (no stack needed)."""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import LiveStackToolError, make_keypair
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import build_contract
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack import scenario
from tests.integration.live_stack.evidence import EvidenceWriter, RunIdentity
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.tool_cells import (
    RECOVERY_TOOLS,
    Grants,
    Rejection,
    bindings,
    claims_of,
    configuration_of,
    forge,
    prove_rejection,
    rejected_by_validation,
)

_GRANTS = Grants("agent", ("p",), {"p": "viewer"}, ("platform_auditor",))


def forge_source(grants: Grants) -> str:
    """A stand-in for a real-issuer token carrying ``grants``."""
    return make_keypair().create_token(
        subject=grants.subject,
        issuer="https://issuer.test",
        audience="kdive",
        additional_claims={"projects": list(grants.projects)},
        kid="issuer-key-1",
    )


@dataclass
class _Caller:
    exposure: str
    result: object = None
    status: int = 401
    calls: list[tuple[str, object]] = field(default_factory=list)

    def token(self, grants: Grants) -> str:
        return forge_source(grants)

    async def call(self, tool, args, token, *, discover=False):  # noqa: ANN001, ANN202
        self.calls.append((tool, args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def post(self, tool, args, token) -> int:  # noqa: ANN001
        self.calls.append((tool, claims_of(token)))
        return self.status
```

   The test bodies:

```python
def _run(tmp_path: Path, boundary: str) -> CellRun:
    cell = next(
        c
        for c in build_contract().cells
        if c.operation == "tools.search" and c.scenario_id.endswith("/" + boundary)
    )
    return CellRun(cell, EvidenceWriter(tmp_path))


def _snapshot(*states: object):  # noqa: ANN202
    values = iter(states)

    async def snap() -> object:
        return next(values)

    return snap


def test_configuration_follows_the_recovery_tools() -> None:
    assert configuration_of(["tools.search"]) == "default"
    assert configuration_of([*RECOVERY_TOOLS, "tools.search"]) == "recovery"
    with pytest.raises(AssertionError, match="neither default nor recovery"):
        configuration_of(["ops.build_uses_list"])


def test_authentication_needs_401(tmp_path: Path) -> None:
    run = _run(tmp_path, "authentication")
    caller = _Caller("gateway")
    asyncio.run(
        prove_rejection(run, caller, "authentication", Rejection({}, _GRANTS), _snapshot(1, 1))
    )
    assert set(run.assertions) == {"authentication", "unchanged-state", "cleanup"}
    assert caller.calls[0][1]["projects"] == ["p"]
    with pytest.raises(AssertionError, match="HTTP 200"):
        asyncio.run(
            prove_rejection(
                _run(tmp_path, "authentication"),
                _Caller("direct", status=200),
                "authentication",
                Rejection({}, _GRANTS),
                _snapshot(1, 1),
            )
        )


def test_validation_rule_per_exposure() -> None:
    tool_error = LiveStackToolError("tools.search", "1 validation error for call[tools_search]")
    config = ToolResponse.failure("tools.invoke", ErrorCategory.CONFIGURATION_ERROR)
    assert rejected_by_validation("direct", tool_error)
    assert not rejected_by_validation("gateway", tool_error)
    assert rejected_by_validation("gateway", config)
    assert not rejected_by_validation("direct", LiveStackToolError("t", "boom"))
    assert not rejected_by_validation("direct", ToolResponse.success("x", "ok"))


def test_category_boundaries_use_the_closed_set(tmp_path: Path) -> None:
    denied = ToolResponse.failure("x", ErrorCategory.AUTHORIZATION_DENIED)
    for boundary in ("authorization", "project-isolation"):
        run = _run(tmp_path, "validation")
        asyncio.run(
            prove_rejection(
                run, _Caller("direct", denied), boundary, Rejection({}, _GRANTS), _snapshot(1, 1)
            )
        )
        assert boundary in run.assertions
    narrow = Rejection({}, _GRANTS, frozenset({"not_found"}))
    with pytest.raises(AssertionError, match="authorization_denied"):
        asyncio.run(
            prove_rejection(
                _run(tmp_path, "validation"),
                _Caller("direct", denied),
                "project-isolation",
                narrow,
                _snapshot(1, 1),
            )
        )
    with pytest.raises(AssertionError, match="was not rejected"):
        asyncio.run(
            prove_rejection(
                _run(tmp_path, "validation"),
                _Caller("direct", ToolResponse.success("x", "ok")),
                "authorization",
                Rejection({}, _GRANTS),
                _snapshot(1, 1),
            )
        )


def test_changed_state_fails_the_rejection(tmp_path: Path) -> None:
    run = _run(tmp_path, "authentication")
    with pytest.raises(AssertionError, match="protected state changed"):
        asyncio.run(
            prove_rejection(
                run,
                _Caller("direct"),
                "authentication",
                Rejection({}, _GRANTS),
                _snapshot({"t": [0]}, {"t": [1]}),
            )
        )
    assert "unchanged-state" not in run.assertions


def test_forge_keeps_claims_and_changes_signature() -> None:
    token = forge_source(_GRANTS)
    forged = forge(token)
    assert claims_of(forged) | {"iat": 0, "exp": 0} == claims_of(token) | {"iat": 0, "exp": 0}
    assert forged.split(".")[2] != token.split(".")[2]
    header = json.loads(base64.urlsafe_b64decode(forged.split(".")[0] + "=="))
    assert header["kid"] == "issuer-key-1"


def test_bindings_cover_bound_tool_cells() -> None:
    contract = build_contract()
    whoami = [c for c in contract.cells if c.operation == "session.whoami"]
    bound = [replace(c, node_id="tests/x.py::test_x") for c in whoami]
    unbound = replace(whoami[0], node_id=None, id="unbound")
    provider = next(c for c in contract.cells if c.provider == "local-libvirt")
    native = replace(provider, node_id="tests/x.py::test_x")
    inputs = bindings(
        "a" * 40,
        host_os="fedora:44",
        host_arch="x86_64",
        matrix=contract.matrix_sha256,
        cells=[*bound, unbound, native],
    )
    assert set(inputs.cells) == {c.id for c in bound}
    assert {c.accelerator for c in inputs.cells.values()} == {"none"}


def test_run_cell_records_the_proven_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "a" * 40
    identity = RunIdentity(sha, "b" * 64, "fedora:44", "x86_64", True, {"server": sha})
    monkeypatch.setattr(scenario, "require_stack", lambda: "http://stack.test/mcp")
    monkeypatch.setattr(scenario, "run_identity", lambda _url: identity)
    monkeypatch.setattr(scenario, "prerequisites", lambda: (object(), "postgresql://x"))
    monkeypatch.setattr(scenario, "evidence_root", lambda: tmp_path)
    cell = replace(_run(tmp_path, "authentication").cell, node_id="tests/x.py::test_x")

    async def body(run: CellRun, *_: object) -> None:
        for name in cell.assertions:
            run.prove(name, {})

    scenario.run_cell(cell, body, proves=Outcome.REJECTION)
    (record,) = (tmp_path / "records").glob("*.json")
    assert json.loads(record.read_text())["outcome"] == "rejection"
```

   Format with `just format` before committing.
2. Run `uv run python -m pytest tests/integration/live_stack/test_tool_cells.py -q`; expect a
   collection `ImportError` for `tool_cells`.
3. Create `tests/integration/live_stack/tool_cells.py` with this content:

```python
"""Tool-cell frame of the live coverage carriers (#2811, ADR-0722).

A tool cell is one contract cell of a registered MCP tool: a configuration (``default`` or
``recovery``), an exposure (``direct`` or ``gateway``) and a kind (``functional`` or one rejection
boundary). :func:`run_tool_cell` proves the server's configuration from its operator catalog,
skips a cell of the other configuration without writing a record, and otherwise runs the cell
through :func:`~tests.integration.live_stack.scenario.run_cell`. :class:`HttpCaller` reaches a tool
in the cell's exposure; :func:`prove_functional` and :func:`prove_rejection` prove the cell's
assertions, with :func:`project_state` as the default protected-state snapshot.

``python -m tests.integration.live_stack.tool_cells bindings --candidate SHA --out FILE`` writes
the expected ``Context`` of every bound service tool cell.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import platform
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast, get_args

import httpx
import psycopg
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from psycopg import sql

import kdive.config as config
from kdive.config.cli_settings import CLI_CLIENT_ID
from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import (
    LiveStackClient,
    LiveStackToolError,
    OidcIssuer,
    make_keypair,
    mint_token,
)
from kdive.mcp.exposure import CORE_TOOLS
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell, build_contract
from scripts.coverage_campaign.evidence import Context, InputBindings, Outcome
from tests.integration.live_stack.conftest import require_issuer, require_stack
from tests.integration.live_stack.evidence import os_identity
from tests.integration.live_stack.scenario import CellRun, run_cell

Exposure = Literal["direct", "gateway"]
Boundary = Literal["authentication", "authorization", "project-isolation", "validation"]
Result = ToolResponse | list[ToolResponse] | LiveStackToolError
Snapshot = Callable[[], Awaitable[object]]

RECOVERY_TOOLS = frozenset({"ops.build_uses_list", "ops.recover_build_use"})
# Rows a rejected or successful call is expected to write (ADR-0722 §4).
_AUDIT_TABLES = frozenset({"audit_log", "platform_audit_log", "tool_invocation"})
_JWT_STANDARD = frozenset({"sub", "iss", "aud", "exp", "iat", "nbf", "jti"})
_CONFIGURATIONS: dict[str, tuple[str, list[str]]] = {}


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

    def token(self, grants: Grants) -> str:
        """A real-issuer token for ``grants``; ``direct`` carries the ``kdivectl`` client id."""
        return mint_token(
            self.issuer,
            subject=grants.subject,
            projects=list(grants.projects),
            roles=dict(grants.roles),
            platform_roles=list(grants.platform_roles),
            agent_session=f"{grants.subject}-sess",
            client_id=config.require(CLI_CLIENT_ID) if self.exposure == "direct" else None,
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
class Rejection:
    """One call a boundary must reject: its arguments, grants and accepted categories."""

    args: Mapping[str, object]
    grants: Grants
    categories: frozenset[str] = frozenset({ErrorCategory.AUTHORIZATION_DENIED.value})


def rejected_by_validation(exposure: str, result: Result) -> bool:
    """A ``configuration_error`` envelope, or a ``direct`` tool error naming validation."""
    if isinstance(result, LiveStackToolError):
        return exposure == "direct" and "validation error" in result.message.lower()
    return (
        isinstance(result, ToolResponse)
        and result.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    )


def _shape(result: Result) -> dict[str, object]:
    """A message-free description of ``result`` for evidence."""
    if isinstance(result, LiveStackToolError):
        return {
            "tool_error": True,
            "names_validation": "validation error" in result.message.lower(),
        }
    if isinstance(result, list):
        return {"envelopes": len(result)}
    return {"status": result.status, "error_category": result.error_category}


async def _attempt(caller: Caller, tool: str, rejection: Rejection) -> Result:
    try:
        return await caller.call(tool, rejection.args, caller.token(rejection.grants))
    except LiveStackToolError as exc:
        return exc


async def _observe(
    caller: Caller, tool: str, boundary: Boundary, rejection: Rejection
) -> dict[str, object]:
    if boundary == "authentication":
        token = forge(caller.token(rejection.grants))
        status = await caller.post(tool, rejection.args, token)
        assert status == 401, f"forged-signature call to {tool} answered HTTP {status}, not 401"
        return {"http_status": status, "token": "foreign-signature"}
    result = await _attempt(caller, tool, rejection)
    if boundary == "validation":
        assert rejected_by_validation(caller.exposure, result), (
            f"{tool} accepted invalid arguments: {_shape(result)}"
        )
    else:
        category = result.error_category if isinstance(result, ToolResponse) else None
        assert category is not None, f"{tool} was not rejected: {_shape(result)}"
        assert category in rejection.categories, (
            f"{tool} rejected with {category}, not one of {sorted(rejection.categories)}"
        )
    return {"rejected": _shape(result)}


def _digest(state: object) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


async def prove_rejection(
    run: CellRun, caller: Caller, boundary: Boundary, rejection: Rejection, snapshot: Snapshot
) -> None:
    """Prove ``boundary`` rejects the call and the protected state is unchanged (ADR-0722)."""
    before = await snapshot()
    observation = await _observe(caller, run.cell.operation, boundary, rejection)
    run.prove(boundary, {"exposure": caller.exposure, **observation})
    after = await snapshot()
    assert after == before, f"protected state changed across the rejected {run.cell.operation}"
    state = _digest(before)
    run.prove("unchanged-state", {"snapshot_sha256": state})
    run.prove("cleanup", {"owned": [], "snapshot_sha256": state})


Functional = Callable[[HttpCaller, Grants], Awaitable[dict[str, object]]]


async def prove_functional(
    run: CellRun, caller: HttpCaller, grants: Grants, body: Functional, snapshot: Snapshot
) -> None:
    """Prove ``effect`` with ``body``, then ``cleanup`` as an unchanged project snapshot."""
    before = await snapshot()
    run.prove("effect", {"exposure": caller.exposure, **await body(caller, grants)})
    after = await snapshot()
    assert after == before, f"{run.cell.operation} left durable state in the cell's project"
    run.prove("cleanup", {"owned": [], "snapshot_sha256": _digest(before)})


async def project_state(db_url: str, project: str) -> dict[str, list[object]]:
    """Row count and row-text SHA-256 of ``project`` in every public table with a project column.

    Tables come from ``pg_catalog``, which lists every table whatever the DSN may read, so a
    table the DSN cannot read fails the snapshot instead of leaving it.
    """
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
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

    A completed rejection cell records ``rejection``, a functional one ``success``.
    """
    base_url = require_stack()
    configuration, listed = server_configuration(base_url, require_issuer())
    if configuration != cell.configuration:
        pytest.skip(f"stack runs {configuration}; {cell.id} needs {cell.configuration}")

    async def proven(run: CellRun, url: str, issuer: OidcIssuer, db_url: str) -> None:
        proof = {"cell": run.cell.id, "configuration": configuration, "recovery_tools": listed}
        run.artifacts.append(run.writer.artifact(proof))
        await scenario(run, url, issuer, db_url)

    run_cell(
        cell, proven, proves=Outcome.REJECTION if cell.kind == "rejection" else Outcome.SUCCESS
    )


def bindings(
    candidate: str,
    *,
    host_os: str,
    host_arch: str,
    matrix: str,
    cells: Iterable[Cell],
) -> InputBindings:
    """The expected ``Context`` of every bound service tool cell in ``cells``."""
    contexts = {
        cell.id: Context.model_validate(
            {"host_os": host_os, "host_arch": host_arch, "accelerator": "none"}
        )
        for cell in cells
        if cell.provider == "service" and cell.scenario_id.startswith("tool/") and cell.node_id
    }
    return InputBindings(version=1, candidate_sha=candidate, matrix_sha256=matrix, cells=contexts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write tool-cell input bindings.")
    commands = parser.add_subparsers(dest="command", required=True)
    write = commands.add_parser("bindings")
    write.add_argument("--candidate", required=True)
    write.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    contract = build_contract()
    inputs = bindings(
        args.candidate,
        host_os=os_identity(Path("/etc/os-release").read_text(encoding="utf-8")),
        host_arch=platform.machine(),
        matrix=contract.matrix_sha256,
        cells=contract.cells,
    )
    args.out.write_text(inputs.model_dump_json(indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(inputs.cells)} tool-cell binding(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

4. In `tests/integration/live_stack/scenario.py`, change `run_cell` to take the outcome a
   completed scenario proves; only these lines change:

```python
def run_cell(cell: Cell, scenario: Scenario, *, proves: Outcome = Outcome.SUCCESS) -> None:
    """Identity → prerequisites → ``scenario`` → one record; fail pytest unless ``proves``.

    ``proves`` is what a completed scenario records: ``success`` for a functional or native cell,
    ``rejection`` for a rejection cell, the outcome ``qualify`` requires of its kind (ADR-0722).
    """
    ...
        asyncio.run(scenario(run, base_url, issuer, db_url))
        run.outcome, run.reason = proves, ""
    ...
    assert run.outcome is proves, f"{cell.id}: {run.outcome.value}: {run.reason}"
```

   The `...` lines are the existing body, unchanged; the existing callers keep the default.
5. Run the focused tests (all pass), `uv run python -m pytest tests/integration/live_stack -q`
   (the existing harness unit tests still pass), `just lint`, `just type`. Commit:
   `test(live-stack): add the ADR-0722 tool-cell harness`.

Acceptance: unit tests green; `ty` clean; no import of a package outside the manifest.

## Task 3: the six tools' live carrier and bindings

Files: create `tests/integration/test_core_tool_cells_live.py`; modify `obligations.toml`
`[implementations]`; modify `tests/scripts/test_coverage_contract.py`.

Interfaces consumed: everything Task 2 lists as provided; `load_fixture_catalog(path)` and
`fixture_catalog_path_from_env()` from `kdive.components.catalog`; `ProvisioningProfile.parse`
from `kdive.profiles.provisioning`; `validate_profile_for_provider(profile, policy, capabilities)`
from `kdive.services.systems.validation`; `LocalLibvirtProfilePolicy` from
`kdive.providers.local_libvirt.profile_policy`; `_component_sources()` from
`kdive.providers.local_libvirt.composition`; `load_inventory_optional(path)` from
`kdive.inventory.loader`; `systems_toml_path()` from `kdive.inventory.path`; `ImageVisibility`
from `kdive.domain.catalog.images`.

Verification:

- Mode: focused-test. Contract: the 14 scenarios bind `test_core_tool_cell`, and no other tool
  scenario is bound. Test: the updated
  `tests/scripts/test_coverage_contract.py::test_pending_cells_have_owned_assertions_but_no_invented_nodes`.
  Red: after adding the assertion and before the TOML rows, the core cells' `node_id` is `None`.
  Green: `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.
- Mode: task-test-not-applicable. Surface: the live functional bodies in
  `test_core_tool_cells_live.py`. Reason: they assert against a running server and lab database;
  their proof is the live run in Task 4, and a mocked server would test the mock.

Steps:

1. In `test_pending_cells_have_owned_assertions_but_no_invented_nodes` add `_CORE_NODE =
   "tests/integration/test_core_tool_cells_live.py::test_core_tool_cell"` at module level and, in
   the body, replace the `bound` line and the assertion after it with:

```python
    core = [c for c in contract.cells if c.operation in _CORE_TOOLS]
    assert len(core) == 56 and {c.node_id for c in core} == {_CORE_NODE}
    bound = {"image-smoke", "deep-lifecycle", "host-install", *_CORE_TOOLS}
```

   and keep `assert all(c.node_id is None for c in contract.cells if c.operation not in bound)`.
2. Run the contract tests; expect the new assertion to fail (`{None} != {_CORE_NODE}`) — the node
   file does not exist yet, so create it in step 3 before adding the TOML rows.
3. Create `tests/integration/test_core_tool_cells_live.py`:

```python
"""Prove the core MCP tool cells over HTTP and record coverage evidence (#2811, ADR-0722).

``live_stack``-marked. One parameter per contract cell of the six tools #2811 owns: each
configuration (proven from the operator catalog; the other configuration's cells skip), each
exposure (operator direct, agent gateway) and each kind. A functional cell compares the tool's
result with a source the server did not produce: the token's own claims, the fixture catalog and
``systems.toml`` read by this process, or the operator-direct catalog. A rejection cell proves its
boundary and an unchanged project snapshot. ``docs/operating/runbooks/live-testing.md`` covers the
two lanes, bindings, assembly and qualification.
"""

from __future__ import annotations

import secrets
from functools import partial
from typing import cast

import pytest

from kdive.components.catalog import fixture_catalog_path_from_env, load_fixture_catalog
from kdive.domain.catalog.images import ImageVisibility
from kdive.inventory.loader import load_inventory_optional
from kdive.inventory.path import systems_toml_path
from kdive.mcp.dev_harness import OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.local_libvirt.composition import _component_sources
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.services.systems.validation import validate_profile_for_provider
from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.tool_cells import (
    Exposure,
    Functional,
    Grants,
    HttpCaller,
    Rejection,
    boundary_of,
    claims_of,
    matches,
    one,
    operator_catalog,
    project_state,
    prove_functional,
    prove_rejection,
    run_tool_cell,
    tool_cells,
)

pytestmark = pytest.mark.live_stack

TOOLS = (
    "fixtures.validate",
    "projects.list",
    "session.whoami",
    "systems.profile_examples",
    "tools.invoke",
    "tools.search",
)
_SCHEMA_TOOLS = ["session.whoami", "tools.invoke"]
_CONCEPT = "granted projects roles"
_VALID: dict[str, dict[str, object]] = {
    "tools.invoke": {"name": "projects.list", "arguments": {}},
    "tools.search": {"names": ["session.whoami"]},
}
_INVALID: dict[str, dict[str, object]] = {
    "tools.invoke": {"arguments": {}},
    "tools.search": {"limit": 0},
}


def _grants(project: str) -> Grants:
    """Distinctive claims: two projects, a role on one, and a platform role."""
    return Grants(
        subject=f"{project}-agent",
        projects=(project, f"{project}-b"),
        roles={project: "viewer"},
        platform_roles=("platform_auditor",),
    )


def _identity(claims: dict[str, object]) -> dict[str, object]:
    """What ``session.whoami`` must report for a token carrying ``claims``."""
    roles = cast(dict[str, str], claims.get("roles") or {})
    return {
        "principal": claims["sub"],
        "client_id": claims.get("azp") or claims.get("client_id"),
        "projects": sorted(set(cast(list[str], claims.get("projects") or []))),
        "roles": dict(sorted(roles.items())),
        "platform_roles": sorted(cast(list[str], claims.get("platform_roles") or [])),
    }


def _assert_projects(env: ToolResponse, claims: dict[str, object]) -> None:
    identity = _identity(claims)
    roles = cast(dict[str, str], identity["roles"])
    expected = [
        {"project": p, "role": roles.get(p, "")} for p in cast(list[str], identity["projects"])
    ]
    assert env.object_id == "projects", f"envelope {env.object_id} is not projects.list's"
    assert [dict(item.data) for item in env.items] == expected
    assert env.data.get("principal") == identity["principal"]
    assert env.data.get("platform_roles") == identity["platform_roles"]


async def _whoami(caller: HttpCaller, grants: Grants) -> dict[str, object]:
    token = caller.token(grants)
    env = one(await caller.call("session.whoami", {}, token, discover=True))
    expected = _identity(claims_of(token))
    assert dict(env.data) == expected, f"whoami {env.data} != token claims {expected}"
    return {"fields_equal_claims": sorted(expected)}


async def _projects(caller: HttpCaller, grants: Grants) -> dict[str, object]:
    token = caller.token(grants)
    env = one(await caller.call("projects.list", {}, token, discover=True))
    _assert_projects(env, claims_of(token))
    return {"items": len(env.items)}


async def _invoke(caller: HttpCaller, grants: Grants) -> dict[str, object]:
    token = caller.token(grants)
    env = one(await caller.call("tools.invoke", _VALID["tools.invoke"], token, discover=True))
    _assert_projects(env, claims_of(token))
    return {"inner": "projects.list", "items": len(env.items)}


async def _search(caller: HttpCaller, grants: Grants) -> dict[str, object]:
    token = caller.token(grants)
    named = one(await caller.call("tools.search", {"names": _SCHEMA_TOOLS}, token, discover=True))
    schemas = {str(m["name"]): m.get("input_schema") for m in matches(named)}
    direct = await operator_catalog(caller.base_url, caller.issuer, grants)
    assert list(schemas) == _SCHEMA_TOOLS, f"names mode returned {list(schemas)}"
    for name in _SCHEMA_TOOLS:
        assert schemas[name] == direct[name], f"{name} schema differs from direct exposure"
    # A test-side anchor: tools.invoke's own signature, independent of both catalogs.
    invoke = cast(dict[str, object], schemas["tools.invoke"])
    assert set(cast(dict[str, object], invoke["properties"])) == {"name", "arguments"}
    assert invoke.get("required") == ["name"], f"tools.invoke requires {invoke.get('required')}"
    concept = one(await caller.call("tools.search", {"query": _CONCEPT}, token))
    found = [str(m["name"]) for m in matches(concept)]
    assert "projects.list" in found, f"{_CONCEPT!r} found {found}"
    return {"named": _SCHEMA_TOOLS, "schemas_equal_direct": True, "concept_hits": found}


async def _fixtures(caller: HttpCaller, grants: Grants) -> dict[str, object]:
    env = one(await caller.call("fixtures.validate", {}, caller.token(grants), discover=True))
    path = fixture_catalog_path_from_env()
    profiles = sorted(
        load_fixture_catalog(path).profiles, key=lambda p: (p.provider, p.name, p.arch)
    )
    expected = [{"provider": p.provider, "name": p.name, "arch": p.arch} for p in profiles]
    assert env.status == "valid", f"fixtures.validate reported {env.status}"
    assert env.data.get("path") == str(path), "server resolved another fixture catalog"
    assert env.data.get("profiles") == expected, "profiles differ from the fixture catalog"
    return {"profiles": len(expected)}


def _expected_local_rootfs() -> dict[str, object]:
    doc = load_inventory_optional(systems_toml_path())
    public = [
        image.name
        for image in (doc.image if doc is not None else [])
        if image.provider == "local-libvirt" and image.visibility == ImageVisibility.PUBLIC
    ]
    if public:
        return {"kind": "catalog", "provider": "local-libvirt", "name": public[0]}
    return {"kind": "local", "path": "/REPLACE_ME/rootfs.img"}


async def _examples(caller: HttpCaller, grants: Grants) -> dict[str, object]:
    env = one(
        await caller.call("systems.profile_examples", {}, caller.token(grants), discover=True)
    )
    providers = []
    for item in env.items:
        provider = str(item.data.get("provider"))
        raw = cast(dict[str, object], item.data.get("profile"))
        profile = ProvisioningProfile.parse(raw)
        if provider == "local-libvirt":
            validate_profile_for_provider(
                profile, LocalLibvirtProfilePolicy(), _component_sources()
            )
            section = cast(dict[str, dict[str, object]], raw["provider"])["local-libvirt"]
            assert section["rootfs"] == _expected_local_rootfs(), "rootfs is not the inventory's"
        providers.append(provider)
    assert "local-libvirt" in providers, f"no local-libvirt example among {providers}"
    return {"providers": sorted(providers), "parsed": len(providers)}


_FUNCTIONAL: dict[str, Functional] = {
    "fixtures.validate": _fixtures,
    "projects.list": _projects,
    "session.whoami": _whoami,
    "systems.profile_examples": _examples,
    "tools.invoke": _invoke,
    "tools.search": _search,
}


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    cell = run.cell
    caller = HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)
    project = f"cov-{secrets.token_hex(4)}"
    grants = _grants(project)
    snapshot = partial(project_state, db_url, project)
    if cell.kind == "functional":
        await prove_functional(run, caller, grants, _FUNCTIONAL[cell.operation], snapshot)
        return
    boundary = boundary_of(cell)
    args = _INVALID if boundary == "validation" else _VALID
    rejection = Rejection(args.get(cell.operation, {}), grants)
    await prove_rejection(run, caller, boundary, rejection, snapshot)


@pytest.mark.parametrize("cell", tool_cells(TOOLS), ids=lambda cell: cell.id)
def test_core_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a core tool and record it."""
    run_tool_cell(cell, _scenario)
```

4. Append to `[implementations]` in `obligations.toml`, one line per scenario, value
   `"tests/integration/test_core_tool_cells_live.py::test_core_tool_cell"`, for:
   `tool/fixtures.validate/default/{authentication,functional}`,
   `tool/projects.list/default/{authentication,functional}`,
   `tool/session.whoami/default/{authentication,functional}`,
   `tool/systems.profile_examples/default/{authentication,functional}`,
   `tool/tools.invoke/default/{authentication,functional,validation}`,
   `tool/tools.search/default/{authentication,functional,validation}` (14 lines, each scenario
   written out in full).
5. Run `uv run python -m pytest tests/scripts/test_coverage_contract.py
   tests/integration/live_stack/test_tool_cells.py -q` (pass) and
   `uv run python -m pytest tests/integration/test_core_tool_cells_live.py -q` without a stack
   (expect `56 skipped`, `KDIVE_STACK_BASE_URL unset`). `just lint`, `just type`. Commit:
   `test(live-stack): prove the six core tool cells over HTTP (#2811)`.

Acceptance: 14 bindings; the module collects 56 parameters; skips cleanly without a stack.

## Task 4: runbook and live proof

Files: modify `docs/operating/runbooks/live-testing.md` (new subsection after "Deep lifecycle
across representative guests (#2809)").

Verification:

- Mode: task-test-not-applicable. Surface: runbook prose. Reason: no executable consumer reads the
  section; it is checked by following it in the live run below.

Steps:

1. Add the subsection `#### Core tool cells and server configurations (#2811)`: what
   `test_core_tool_cell` covers; that a run proves its configuration from the operator catalog and
   skips the other configuration's cells; the two-lane procedure:

```bash
sha=$(git rev-parse HEAD)
uv run python -m tests.integration.live_stack.tool_cells bindings --candidate "$sha" \
  --out inputs.json
export KDIVE_ARTIFACT_DIR=$(mktemp -d)        # one evidence root for both lanes
examples/local-libvirt/demo-up.sh             # default configuration
uv run python -m pytest -m live_stack tests/integration/test_core_tool_cells_live.py
KDIVE_WORKER_DEATH_VERIFIER=local examples/local-libvirt/demo-up.sh   # recovery
uv run python -m pytest -m live_stack tests/integration/test_core_tool_cells_live.py
uv run python -m tests.integration.live_stack.evidence assemble \
  "$KDIVE_ARTIFACT_DIR/coverage-evidence" --candidate "$sha" --out results.json
uv run python -m scripts.coverage_campaign qualify --inputs inputs.json --results results.json
```

   and that `qualify` exits 1 because other owners' cells have no result, so read the 56 rows.
2. Live run on a disposable lab host at the PR head: both lanes; confirm `/readyz` revisions equal
   `HEAD`; record the outcome (candidate, lanes, per-cell result) as a "Last run" paragraph. A
   product defect becomes a separate `status:needs-triage` issue and its cells stay failed.
3. Tear down: `scripts/live-stack/stack-down.sh`; nothing else is created (read-only tools).
4. Commit: `docs(live-testing): run the core tool cells in both configurations`.

Acceptance: 56 rows qualified, or each unqualified row explained by a filed defect.
