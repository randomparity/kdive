"""persist_build_log PUTs a Run-owned REDACTED build-log object (#770, ADR-0238)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.providers.shared.build_host.publishing.build_log import (
    BUILD_LOG_NAME,
    BUILD_LOG_RETENTION_CLASS,
    persist_build_log,
)

_RUN = UUID("77777777-7777-7777-7777-777777777777")
_TENANT = "local"


@dataclass
class _RecordingStore:
    puts: list[ArtifactWriteRequest] = field(default_factory=list)

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        self.puts.append(request)
        return StoredArtifact(request.key(), "etag", request.sensitivity, request.retention_class)

    def presign_put(self, request: Any) -> Any:  # pragma: no cover - unused here
        raise NotImplementedError


def test_persist_build_log_puts_redacted_run_owned_object() -> None:
    store = _RecordingStore()
    key = persist_build_log(store, _RUN, "ld: undefined reference", tenant=_TENANT)
    assert key == f"{_TENANT}/runs/{_RUN}/{BUILD_LOG_NAME}"
    [request] = store.puts
    assert request.owner_kind == "runs"
    assert request.owner_id == str(_RUN)
    assert request.name == BUILD_LOG_NAME
    assert request.sensitivity is Sensitivity.REDACTED
    assert request.retention_class == BUILD_LOG_RETENTION_CLASS
    assert request.data == b"ld: undefined reference"


def test_persist_build_log_skips_empty_output() -> None:
    store = _RecordingStore()
    assert persist_build_log(store, _RUN, "", tenant=_TENANT) is None
    assert persist_build_log(store, _RUN, "   \n\t ", tenant=_TENANT) is None
    assert store.puts == []
