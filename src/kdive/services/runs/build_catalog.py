"""Immutable, content-addressed Investigation build generations (ADR-0531)."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import cast
from uuid import UUID, uuid4

from psycopg import AsyncConnection

from kdive.artifacts.storage import HeadResult
from kdive.db.repositories import INVESTIGATION_BUILDS
from kdive.domain.lifecycle.records import InvestigationBuild, Run
from kdive.serialization import JsonValue, ensure_json_value
from kdive.services.runs.steps import BuildStepResult

_BUILD_REF_RE = re.compile(
    r"^(?P<digest>[0-9a-f]{64})\."
    r"(?P<generation>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)
_CANONICAL_DOCUMENT_VERSION = 1


@dataclass(frozen=True, slots=True)
class BuildPublication:
    """The selected build generation and whether this caller created it."""

    build: InvestigationBuild
    created: bool


def canonical_build_document(
    run: Run, result: BuildStepResult, heads: Mapping[str, HeadResult]
) -> dict[str, JsonValue]:
    """Build the versioned content identity document from validated artifact HEADs."""
    checksums: dict[str, JsonValue] = {}
    for name, key in sorted(result.refs().items()):
        head = heads.get(key)
        if head is None or head.checksum_sha256 is None:
            raise ValueError(f"validated HEAD checksum is required for {name}")
        checksums[name] = {"checksum_sha256": head.checksum_sha256}
    document = {
        "version": _CANONICAL_DOCUMENT_VERSION,
        "target_kind": run.target_kind.value,
        "build_profile": dict(run.build_profile),
        "artifacts": checksums,
        "build_id": result.build_id,
        "cmdline": result.cmdline,
        "provenance": result.build_provenance,
    }
    return cast("dict[str, JsonValue]", ensure_json_value(document, path="canonical build"))


def parse_build_ref(value: str) -> tuple[str, UUID]:
    """Parse a canonical ``<sha256>.<lowercase UUID>`` build reference."""
    match = _BUILD_REF_RE.fullmatch(value)
    if match is None:
        raise ValueError("build_ref must be <64 lowercase hex digest>.<lowercase UUID>")
    return match["digest"], UUID(match["generation"])


async def publish_or_reuse_build(
    conn: AsyncConnection,
    *,
    run: Run,
    result: BuildStepResult,
    heads: Mapping[str, HeadResult],
    retention: timedelta,
) -> BuildPublication:
    """Select or insert one immutable build generation under the caller's Investigation lock."""
    canonical_document = canonical_build_document(run, result, heads)
    encoded_document = json.dumps(canonical_document, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded_document.encode("utf-8")).hexdigest()
    artifact_versions = _artifact_versions(result, heads)

    async with conn.cursor() as cur:
        await cur.execute("SELECT clock_timestamp()")
        row = await cur.fetchone()
    if row is None:  # Invariant: SELECT clock_timestamp() returns exactly one row.
        raise RuntimeError("SELECT clock_timestamp() returned no row")
    now = row[0]

    existing = await INVESTIGATION_BUILDS.active_by_digest(conn, run.investigation_id, digest, now)
    if existing is not None:
        if existing.canonical_document != canonical_document:
            raise RuntimeError("matching build digest has a different canonical document")
        return BuildPublication(build=existing, created=False)

    generation = uuid4()
    build = InvestigationBuild(
        investigation_id=run.investigation_id,
        generation=generation,
        build_ref=f"{digest}.{generation}",
        content_digest=digest,
        canonical_document=canonical_document,
        build_result=result.dump(),
        artifacts=artifact_versions,
        target_kind=run.target_kind,
        build_profile=run.build_profile,
        state="active",
        expires_at=now + retention,
        created_at=now,
        updated_at=now,
    )
    return BuildPublication(build=await INVESTIGATION_BUILDS.insert(conn, build), created=True)


async def resolve_build(
    conn: AsyncConnection, investigation_id: UUID, build_ref: str
) -> InvestigationBuild | None:
    """Return a build generation only when it belongs to ``investigation_id``."""
    return await INVESTIGATION_BUILDS.get(conn, investigation_id, build_ref)


def _artifact_versions(
    result: BuildStepResult, heads: Mapping[str, HeadResult]
) -> dict[str, dict[str, str]]:
    versions: dict[str, dict[str, str]] = {}
    for name, key in sorted(result.refs().items()):
        head = heads.get(key)
        if head is None:
            raise ValueError(f"validated HEAD is required for {name}")
        versions[name] = {"key": key, "version_id": head.version_id}
    return versions
