"""Store identities written by fake vmcore captures (ADR-0786)."""

from __future__ import annotations

from uuid import UUID, uuid4

from psycopg import AsyncConnection

from kdive.artifacts.storage import HeadResult, StoredArtifact
from kdive.artifacts.vmcore_retention import capture_metadata
from kdive.domain.capture import CaptureMethod
from kdive.providers.ports.retrieve import CaptureOutput
from tests.clock import STORE_MTIME


class WrittenObjects:
    """The keys and etags a capture double claims to have written, answerable through ``head``."""

    def __init__(self) -> None:
        self._written: dict[str, tuple[StoredArtifact, UUID]] = {}
        self.headed_keys: list[str] = []

    def record(self, output: CaptureOutput, *, execution_id: UUID) -> CaptureOutput:
        """Remember both of ``output``'s objects as written, and return it unchanged."""
        for stored in (output.raw, output.redacted):
            self._written[stored.key] = (stored, execution_id)
        return output

    def head(self, key: str) -> HeadResult | None:
        """Stat a recorded object; ``last_modified`` is fixed — no capture test is about it."""
        self.headed_keys.append(key)
        recorded = self._written.get(key)
        if recorded is None:
            return None
        stored, execution_id = recorded
        return HeadResult(
            size_bytes=1,
            checksum_sha256=None,
            etag=stored.etag,
            last_modified=STORE_MTIME,
            version_id=stored.version_id,
            sensitivity=stored.sensitivity,
            metadata=capture_metadata(execution_id),
        )


async def journal_existing_test_pair(
    conn: AsyncConnection, run_id: str, raw_key: str, *, method: CaptureMethod
) -> None:
    """Model an offline-imported historical pair; caller supplies its known capture method."""
    await conn.execute(
        "INSERT INTO artifacts(owner_kind,owner_id,object_key,etag,sensitivity,retention_class) "
        "SELECT 'runs',%s,%s,'e','redacted','vmcore' WHERE NOT EXISTS "
        "(SELECT 1 FROM artifacts WHERE object_key=%s)",
        (run_id, raw_key + "-redacted", raw_key + "-redacted"),
    )
    await conn.execute(
        "INSERT INTO vmcore_capture_executions "
        "(id,run_id,method,raw_key,redacted_key,state,legacy,raw_version_id,"
        "redacted_version_id,captured_at,published_at) "
        "VALUES (%s,%s,%s,%s,%s,'published',true,'test-version','test-version',now(),now())",
        (uuid4(), run_id, method.value, raw_key, raw_key + "-redacted"),
    )
