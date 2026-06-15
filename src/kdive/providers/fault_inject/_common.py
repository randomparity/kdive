"""Shared constants for the fault-inject provider planes."""

from __future__ import annotations

from typing import Protocol

from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact

TENANT = "fault-inject"
SYNTHETIC_BUILD_ID = "fa017" + "0" * 35


class StorePort(Protocol):
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...
