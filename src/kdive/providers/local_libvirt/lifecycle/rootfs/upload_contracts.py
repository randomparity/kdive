"""Object-store contract for uploaded-rootfs acquisition and staging."""

from contextlib import AbstractContextManager
from typing import Protocol

from kdive.artifacts import storage as artifact_types


class UploadObjectStore(Protocol):
    """Streaming object reads required by the uploaded-rootfs pipeline."""

    def head(self, key: str) -> artifact_types.HeadResult | None: ...

    def get_artifact_stream(
        self, key: str, etag: str | None
    ) -> AbstractContextManager[artifact_types.StreamedArtifact]: ...

    def get_range(self, key: str, *, start: int, length: int) -> bytes: ...
