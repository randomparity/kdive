"""Real server bootstrap-key registration for secrets.list functional cells (#3155)."""

from __future__ import annotations

import contextlib
import json
import os
import platform
import secrets
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import cast

import psycopg

import kdive.config as config
from kdive.config.core_settings import SECRETS_ROOT
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.responses import ToolResponse
from kdive.security.secrets.secrets import read_secret_file
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack.deep_lifecycle import (
    FIXTURE_ROOT_ENV,
    kernel_inputs,
    load_fixture,
)
from tests.integration.live_stack.scenario import (
    CellRun,
    ScenarioStop,
    domain_xml,
    on_catalog_system,
)
from tests.integration.live_stack.spine import (
    build_and_upload_kernel,
    build_profile,
    drain_job,
    ok,
    scalar,
)
from tests.integration.live_stack.tool_cells import (
    Exposure,
    Grants,
    HttpCaller,
    lane_image,
    one,
)


def lane_secrets() -> set[str]:
    """Configured values are private comparison inputs, never evidence artifacts."""
    root = Path(config.require(SECRETS_ROOT))
    found: set[str] = set()
    for setting in config.all_settings():
        value = config.get(setting) if setting.secret else None
        if not value:
            continue
        found.add(value)
        with contextlib.suppress(OSError, ValueError):
            found.add(read_secret_file(root, value))
    return {value for value in found if value.strip()}


def verify_presence(answer: ToolResponse, known: set[str]) -> dict[str, object]:
    """Fail without disclosing compared values, including when a label contains one."""
    served = answer.model_dump_json()
    leaked = sum(json.dumps(value, ensure_ascii=False)[1:-1] in served for value in known)
    assert leaked == 0, f"{leaked} known secret value(s) were served"
    expected = answer.data.get("secrets") == ["<process-global>"]
    assert expected, "expected exact process-global presence"
    return {"labels": ["<process-global>"], "known_value_leaks": leaked}


async def _source_key(db_url: str, system_id: str) -> str | None:
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT private_key FROM system_bootstrap_keys WHERE system_id = %s", (system_id,)
        )
        row = await cursor.fetchone()
        return str(row[0]) if row else None


async def _booted_run(op: LiveStackClient, investigation: str, system_id: str, run: CellRun) -> str:
    root = os.environ.get(FIXTURE_ROOT_ENV)
    if not root:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{FIXTURE_ROOT_ENV} must name verified kernel fixtures"
        )
    tree, manifest = load_fixture(Path(root), "longterm", platform.machine())
    run.observed.update(kernel_inputs(tree, manifest))
    run_id = ok(
        await scalar(
            op,
            "runs.create",
            investigation_id=investigation,
            system_id=system_id,
            build_profile=build_profile(manifest["arch"]),
        ),
        "create-run",
    ).object_id
    with tempfile.TemporaryDirectory(prefix="secret-presence-") as scratch:
        await build_and_upload_kernel(
            op,
            run_id=run_id,
            arch=manifest["arch"],
            kernel_tree=tree,
            evidence_dir=Path(scratch) / "upload",
            with_vmlinux=True,
            require_network=True,
            root_fs="ext4",
        )
    for step in ("install", "boot"):
        job = ok(await scalar(op, f"runs.{step}", run_id=run_id), step)
        await drain_job(op, step, job.object_id)
    return run_id


async def prove_secret_presence(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
) -> None:
    """Own a real debug source; global registry cleanup requires subsequent process exit."""
    project = f"cov-{secrets.token_hex(6)}"
    caller = HttpCaller(cast(Exposure, run.cell.exposure), base_url, issuer)
    grants = Grants(project, (project,), {project: "viewer"}, ("platform_operator",))
    token = caller.token(grants)

    async def listed() -> ToolResponse:
        return one(await caller.call("secrets.list", {}, token, discover=True))

    baseline = await listed()
    if baseline.data.get("secrets") != []:
        raise ScenarioStop(
            Outcome.BLOCKED, "secrets.list requires an idle fresh server; restart before this cell"
        )
    fixture = CellRun(run.cell, run.writer)
    known = lane_secrets()
    owned_system: str | None = None

    async def body(op: LiveStackClient, system_id: str, owned: list[str]) -> None:
        nonlocal owned_system
        owned_system = system_id
        investigation = ok(
            await scalar(op, "investigations.open", project=project, title="secret presence"),
            "open",
        ).object_id
        session: str | None = None
        try:
            run_id = await _booted_run(op, investigation, system_id, fixture)
            xml = ET.fromstring(domain_xml(system_id))  # noqa: S314  # nosec B314
            owned.extend(
                path
                for name in ("kernel", "initrd")
                if (path := xml.findtext(f"./os/{name}")) is not None
            )
            clean = (await listed()).data.get("secrets") == []
            assert clean, "server registered an unexpected source before debug attach"
            attached = ok(
                await scalar(op, "debug.start_session", run_id=run_id, transport="drgn-live"),
                "attach-drgn-live",
            )
            session = attached.object_id
            key = await _source_key(db_url, system_id)
            assert key, "owned System bootstrap key is absent"
            known.add(key)
            observation = verify_presence(await listed(), known)
            run.prove(
                "effect",
                {
                    "exposure": caller.exposure,
                    "source": "system-bootstrap-key",
                    "system_id": system_id,
                    "session_id": session,
                    **observation,
                },
            )
        finally:
            try:
                if session is not None:
                    ok(await scalar(op, "debug.end_session", session_id=session), "end-session")
                    ended = ok(
                        await scalar(op, "debug.get_session", session_id=session), "read-session"
                    )
                    assert ended.status == "detached", "owned debug session did not detach"
            finally:
                closed = await scalar(
                    op,
                    "investigations.close",
                    investigation_id=investigation,
                    summary="secret presence finished",
                )
                assert closed.status == "closed", "owned investigation did not close"

    try:
        await on_catalog_system(
            fixture,
            base_url,
            issuer,
            db_url,
            project=project,
            image=lane_image()[0],
            body=body,
        )
    finally:
        run.artifacts.extend(fixture.artifacts)
        run.artifacts.extend(fixture.assertions.values())
    assert owned_system is not None, "source System was not created"
    absent = await _source_key(db_url, owned_system) is None
    assert absent, "owned bootstrap key remained after System cleanup"
    retained = verify_presence(await listed(), known)
    run.prove(
        "cleanup",
        {
            "fixture_cleanup": fixture.assertions["cleanup"],
            "fixture_context": fixture.observed,
            "bootstrap_key_absent": absent,
            "session_detached": True,
            "registry_lifetime": "process; stop server after this cell",
            **retained,
        },
    )
