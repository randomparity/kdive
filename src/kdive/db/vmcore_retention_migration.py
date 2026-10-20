"""Offline operator-only vmcore baseline import (ADR-0786).

Receipt values are operator attestations, never inferred runtime quiescence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from functools import partial
from typing import Any, NamedTuple
from uuid import UUID, uuid4

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.catalog.artifacts import Sensitivity
from kdive.store.objectstore import ObjectStore


class PreparedVmcore(NamedTuple):
    id: UUID
    run_id: UUID
    method: str
    raw_key: str
    redacted_key: str
    captured_at: datetime
    raw_version: str
    redacted_version: str


async def import_vmcore_baseline(
    conn: AsyncConnection, store: ObjectStore, receipt: Mapping[str, object]
) -> int:
    mode = receipt.get("mode")
    if mode not in {"fresh", "legacy_verified"}:
        raise ValueError("vmcore baseline mode must be fresh or legacy_verified")
    required = {
        "all_old_producers_stopped",
        "all_old_store_requests_terminated",
        "old_signers_permanently_revoked",
        "identity_inventory_verified",
        "anonymous_write_denied",
        "new_signer_verified",
    }
    if mode == "fresh":
        required = {
            "fresh_namespace_verified",
            "conditional_publication_verified",
            "all_old_producers_stopped",
        }
    if any(receipt.get(name) is not True for name in required):
        raise ValueError("vmcore baseline requires every offline authority attestation")
    if mode == "legacy_verified" and receipt.get("backend") != "bundled-single-process":
        raise ValueError("external legacy backend needs its separately reviewed procedure")
    if (
        mode == "legacy_verified"
        # Public pinned source commit, not a credential.
        # pragma: allowlist nextline secret
        and receipt.get("backend_commit") != "d997fba1575583a89cf0cc50dc0150642286c86d"
    ):
        raise ValueError("vmcore baseline backend does not match the reviewed bundled image")
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT mode FROM vmcore_retention_baseline WHERE singleton")
        existing = await cur.fetchone()
        if existing is not None:
            if existing["mode"] != mode:
                raise ValueError("vmcore baseline already exists with another mode")
            return 0
        await cur.execute(
            "SELECT id,owner_id,object_key,sensitivity,created_at,etag FROM artifacts "
            "WHERE owner_kind='runs' AND retention_class='vmcore' ORDER BY owner_id,id"
        )
        rows = await cur.fetchall()
    if mode == "fresh" and rows:
        raise ValueError("fresh vmcore baseline requires an empty vmcore catalog")
    groups: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["owner_id"], []).append(row)
    known_keys = {str(row["object_key"]) for row in rows}
    for root in ("local/runs/", "remote-libvirt/runs/", "fault-inject/runs/"):
        pages = store.iter_prefix_version_pages(root)
        while (page := await asyncio.to_thread(partial(next, pages, None))) is not None:
            for version in page.entries:
                name = version.key.rsplit("/", 1)[-1]
                if name.startswith("vmcore-") and version.key not in known_keys:
                    raise ValueError(
                        "unregistered legacy vmcore key requires explicit reconciliation"
                    )
    prepared: list[PreparedVmcore] = []
    for run_id, pair in groups.items():
        if len(pair) != 2:
            raise ValueError("legacy vmcore Run must have exactly one raw/redacted pair")
        by_sensitivity = {str(row["sensitivity"]): row for row in pair}
        if set(by_sensitivity) != {"sensitive", "redacted"}:
            raise ValueError("legacy vmcore pair has ambiguous sensitivity")
        raw = by_sensitivity["sensitive"]
        redacted = by_sensitivity["redacted"]
        raw_key, redacted_key = str(raw["object_key"]), str(redacted["object_key"])
        parts = raw_key.split("/")
        if (
            len(parts) != 4
            or parts[0] not in {"local", "remote-libvirt", "fault-inject"}
            or parts[1] != "runs"
            or parts[2] != str(run_id)
            or parts[3] not in {"vmcore-kdump", "vmcore-fadump", "vmcore-host_dump"}
            or redacted_key != raw_key + "-redacted"
        ):
            raise ValueError("legacy vmcore key ownership or pair shape is not supported")
        raw_head = await asyncio.to_thread(store.head, raw_key)
        redacted_head = await asyncio.to_thread(store.head, redacted_key)
        if (
            raw_head is None
            or redacted_head is None
            or raw_head.sensitivity != Sensitivity.SENSITIVE
            or redacted_head.sensitivity != Sensitivity.REDACTED
            or raw_head.etag != raw["etag"]
            or redacted_head.etag != redacted["etag"]
        ):
            raise ValueError("legacy vmcore pair lacks verified current immutable identities")
        prepared.append(
            PreparedVmcore(
                uuid4(),
                run_id,
                parts[3][len("vmcore-") :],
                raw_key,
                redacted_key,
                max(raw["created_at"], redacted["created_at"]),
                raw_head.version_id,
                redacted_head.version_id,
            )
        )
    async with conn.transaction(), conn.cursor() as cur:
        await cur.execute("LOCK TABLE vmcore_retention_baseline IN EXCLUSIVE MODE")
        await cur.execute("SELECT 1 FROM vmcore_retention_baseline WHERE singleton")
        if await cur.fetchone() is not None:
            raise ValueError("another baseline completed; verify its receipt before retrying")
        for (
            execution_id,
            run_id,
            method,
            raw_key,
            redacted_key,
            captured_at,
            raw_version,
            red_version,
        ) in prepared:
            await cur.execute(
                "INSERT INTO vmcore_capture_executions "
                "(id,run_id,method,raw_key,redacted_key,state,legacy,published_at,captured_at,"
                "raw_version_id,redacted_version_id) VALUES (%s,%s,%s,%s,%s,'published',true,"
                "%s,%s,%s,%s)",
                (
                    execution_id,
                    run_id,
                    method,
                    raw_key,
                    redacted_key,
                    captured_at,
                    captured_at,
                    raw_version,
                    red_version,
                ),
            )
        await cur.execute("INSERT INTO vmcore_retention_baseline(mode) VALUES (%s)", (mode,))
    return len(prepared)
