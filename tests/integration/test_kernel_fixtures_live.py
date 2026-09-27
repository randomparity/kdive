"""Required pinned-fixture upload proofs; explicitly selected missing inputs fail."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.artifacts.catalog.read_model import effective_config_key
from kdive.mcp.dev_harness import LiveStackClient, oidc_issuer_from_env
from kdive.store.objectstore import object_store_from_env
from scripts.kernel_fixtures import verify
from tests.integration.live_stack.skew import repo_facts
from tests.integration.live_stack.spine import (
    build_and_upload_kernel,
    build_profile,
    mint_role_token,
    ok,
    scalar,
)


def require_revision(
    candidate: str, reported: str | None, resolve: Callable[[str], str | None]
) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", candidate):
        raise ValueError("candidate must be a full commit SHA")
    if reported is None or not re.fullmatch(r"[0-9a-f]{7,40}", reported):
        raise ValueError("server must report a known commit")
    if resolve(reported) != candidate:
        raise ValueError("server revision differs from candidate; restart the candidate server")
    return candidate


def deployed(candidate: str) -> str:
    facts = repo_facts()
    assert facts is not None, "test checkout revision unavailable"
    assert facts.head == candidate, "candidate differs from the test checkout"
    assert facts.newest_modified_source_mtime() is None, "test checkout has modified source"
    with urlopen(os.environ["KDIVE_FIXTURE_HEALTH_URL"], timeout=10) as response:
        health = json.load(response)
    assert health["ready"] is True, "candidate server is not ready"
    return require_revision(candidate, health.get("version", {}).get("commit"), facts.resolve)


async def persisted_bytes(run_id: str, build_ref: str, evidence: Path) -> dict[str, str]:
    upload = json.loads((evidence / "upload.json").read_text())
    declarations = {item["name"]: item["sha256"] for item in upload["artifacts"]}
    async with await psycopg.AsyncConnection.connect(
        os.environ["KDIVE_FIXTURE_DATABASE_URL"]
    ) as conn:
        cursor = await conn.execute(
            "SELECT canonical_document, artifacts FROM investigation_builds WHERE build_ref = %s",
            (build_ref,),
        )
        row = await cursor.fetchone()
        assert row is not None, "completed build was not persisted"
        assert row[0]["build_id"] == upload["build_id"]
        config_key = await effective_config_key(conn, UUID(run_id))
        assert config_key, "completed build lost effective config"
        artifacts = dict(row[1])
        artifacts["effective_config"] = {"key": config_key, "version_id": None}
        cursor = await conn.execute(
            "SELECT state FROM run_steps WHERE run_id = %s AND step = 'build'", (run_id,)
        )
        step = await cursor.fetchone()
        assert step is not None and step[0] == "succeeded"
    assert set(artifacts) == set(declarations)
    store = object_store_from_env()
    digests = {}
    for name, ref in artifacts.items():
        checksum = hashlib.sha256()
        with store.get_artifact_stream(ref["key"], None, version_id=ref["version_id"]) as blob:
            while chunk := blob.reader.read(1024 * 1024):
                checksum.update(chunk)
        assert base64.b64encode(checksum.digest()).decode() == declarations[name], name
        digests[name] = checksum.hexdigest()
    return digests


async def upload_fixture(tree: Path, evidence: Path, row: dict[str, Any]) -> None:
    project = "fixture-" + uuid4().hex
    token = mint_role_token(
        oidc_issuer_from_env(), project=project, agent_session="fixture-proof", role="operator"
    )
    async with LiveStackClient.over_http(os.environ["KDIVE_STACK_BASE_URL"], token) as client:
        investigation = ok(
            await scalar(
                client, "investigations.open", project=project, title="Pinned kernel fixture proof"
            ),
            "open",
        )
        try:
            run = ok(
                await scalar(
                    client,
                    "runs.create",
                    investigation_id=investigation.object_id,
                    build_profile=build_profile(arch="x86_64"),
                    target_kind="local-libvirt",
                ),
                "create",
            )
            row["run_id"] = run.object_id
            await build_and_upload_kernel(
                client,
                run_id=run.object_id,
                kernel_tree=tree,
                evidence_dir=evidence / "upload",
                with_vmlinux=True,
                require_live_debug=True,
            )
            result = ok(await scalar(client, "runs.get", run_id=run.object_id), "get")
            assert result.status == "succeeded"
            build_ref = result.data["build_ref"]
            assert isinstance(build_ref, str)
            row["uploaded_sha256"] = await persisted_bytes(
                run.object_id, build_ref, evidence / "upload"
            )
            row["build_ref"] = build_ref
        finally:
            closed = ok(
                await scalar(
                    client,
                    "investigations.close",
                    investigation_id=investigation.object_id,
                    summary="Pinned fixture proof finished; evidence retained",
                ),
                "close",
            )
            assert closed.status == "closed"
            row["cleanup"] = "investigation closed; unbound run created no provider resources"


@pytest.mark.live_stack
@pytest.mark.parametrize("baseline", ["longterm", "stable"])
def test_real_pinned_fixture_upload(baseline: str) -> None:
    root = Path(os.environ["KDIVE_FIXTURE_ROOT"])
    evidence = Path(os.environ["KDIVE_FIXTURE_EVIDENCE"]) / baseline
    evidence.mkdir(parents=True, exist_ok=False)
    row: dict[str, Any] = {
        "scenario": "real-kernel-upload",
        "baseline": baseline,
        "host_arch": platform.machine(),
        "host_os": platform.system(),
        "guest": None,
        "accelerator": None,
        "not_applicable": "unbound build upload invokes only the server role",
        "outcome": "blocked",
        "cleanup": "no resources created",
    }
    started = time.monotonic()
    try:
        candidate = os.environ["KDIVE_FIXTURE_CANDIDATE"]
        row["candidate"] = candidate
        row["server_before"] = deployed(candidate)
        tree = root / baseline
        manifest = verify(tree, baseline=baseline, arch="x86_64")
        row["fixture_id"] = manifest["fixture_id"]
        row["source"] = manifest["source"]
        row["build_id"] = manifest["build_id"]
        row["outcome"] = "failed"
        asyncio.run(upload_fixture(tree, evidence, row))
        assert verify(tree, baseline=baseline, arch="x86_64") == manifest
        row["server_after"] = deployed(candidate)
        row["outcome"] = "passed"
    finally:
        row["elapsed_seconds"] = time.monotonic() - started
        (evidence / "result.json").write_text(json.dumps(row, sort_keys=True, indent=2) + "\n")


@pytest.mark.parametrize("reported", [None, "unknown", "main", "b" * 40])
def test_revision_rejects_unknown_or_different(reported: str | None) -> None:
    with pytest.raises(ValueError):
        require_revision("a" * 40, reported, lambda value: value)


def test_revision_accepts_resolved_abbreviation() -> None:
    assert require_revision("a" * 40, "a" * 7, lambda _: "a" * 40) == "a" * 40


def test_revision_rejects_abbreviated_candidate() -> None:
    with pytest.raises(ValueError):
        require_revision("a" * 7, "a" * 7, lambda _: "a" * 40)
