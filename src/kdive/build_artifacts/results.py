"""Provider-neutral kernel build result containers."""

from __future__ import annotations

from typing import NamedTuple

from kdive.artifacts.storage import HeadResult


class BuildOutput(NamedTuple):
    """Stored kernel build artifacts and the produced kernel build id.

    ``kernel_ref`` is the combined kernel+modules tar (``boot/vmlinuz`` + ``lib/modules/<ver>/``),
    the one artifact shape both providers produce and consume (ADR-0234 §2).
    """

    kernel_ref: str
    debuginfo_ref: str
    build_id: str
    build_provenance: dict[str, str | bool | list[str]] | None = None
    """Client-attested source provenance for the uploaded build, or ``None`` when the caller
    supplied none. On the upload lane KDIVE never clones or verifies a source tree, so this is the
    agent's own freeform claim: ``{client_attested: true, label?: ..., source_ref?: ...}``
    (ADR-0274, #893). Recorded via ``BuildStepResult`` and surfaced verbatim on ``runs.get``.
    Defaulted so the three-positional construction sites stay valid."""


class ValidatedUpload(NamedTuple):
    """Externally uploaded build artifacts after manifest and content validation."""

    output: BuildOutput
    heads: dict[str, HeadResult]
