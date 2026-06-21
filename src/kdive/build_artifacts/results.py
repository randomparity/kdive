"""Provider-neutral kernel build result containers."""

from __future__ import annotations

from typing import NamedTuple

from kdive.artifacts.storage import HeadResult


class BuildOutput(NamedTuple):
    """Stored kernel build artifacts and the produced kernel build id."""

    kernel_ref: str
    debuginfo_ref: str
    build_id: str
    modules_ref: str | None = None


class ValidatedUpload(NamedTuple):
    """Externally uploaded build artifacts after manifest and content validation."""

    output: BuildOutput
    heads: dict[str, HeadResult]
