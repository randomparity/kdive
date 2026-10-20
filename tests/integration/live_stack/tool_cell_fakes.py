"""In-memory fakes for exercising the shared tool-cell harness without a live stack."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from kdive.mcp.dev_harness import make_keypair
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import build_contract
from tests.integration.live_stack.evidence import EvidenceWriter
from tests.integration.live_stack.scenario import CellRun
from tests.integration.live_stack.tool_cells import (
    FilteredListControl,
    Grants,
    claims_of,
)

_OK = ToolResponse.success("x", "ok")


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
    result: ToolResponse | Exception = field(default_factory=lambda: _OK)
    status: int = 401
    calls: list[tuple[str, Mapping[str, object]]] = field(default_factory=list)
    issued: set[str] = field(default_factory=set)
    refuse_issued: bool = False

    def token(self, grants: Grants) -> str:
        token = forge_source(grants)
        self.issued.add(token)
        return token

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse | list[ToolResponse]:
        self.calls.append((tool, args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def post(self, tool: str, args: Mapping[str, object], token: str) -> int:
        self.calls.append((tool, claims_of(token)))
        return 200 if token in self.issued and not self.refuse_issued else self.status


def _snapshot(*states: object) -> Callable[[], Awaitable[object]]:
    values = iter(states)

    async def snap() -> object:
        return next(values)

    return snap


_VIEWER = Grants("viewer", ("p",), {"p": "viewer"})


_ITEM = "inv-1"


def _listed(*ids: str) -> ToolResponse:
    items = [ToolResponse.success(i, "open") for i in ids]
    return ToolResponse.collection(
        "investigations",
        "ok",
        items,
        suggested_next_actions=["investigations.get", "investigations.open"],
        data={"truncated": False, "next_cursor": None},
    )


@dataclass
class _Filtering(_Caller):
    """Answers the viewer's token with ``control``, every other token with ``result``."""

    control: ToolResponse = field(default_factory=lambda: _listed(_ITEM))

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse | list[ToolResponse]:
        subject = str(claims_of(token)["sub"])
        self.calls.append((tool, {**args, "subject": subject}))
        return self.control if subject == _VIEWER.subject else cast(ToolResponse, self.result)


def _list_run(
    tmp_path: Path, tool: str = "investigations.list", boundary: str = "authorization"
) -> CellRun:
    cell = next(
        c
        for c in build_contract().cells
        if c.operation == tool and c.kind == "rejection" and c.scenario_id.endswith("/" + boundary)
    )
    return CellRun(cell, EvidenceWriter(tmp_path))


def _control(item: str = _ITEM) -> FilteredListControl:
    async def expected() -> str:
        return item

    return FilteredListControl(_VIEWER, expected)
