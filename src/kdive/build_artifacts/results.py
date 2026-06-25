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
    build_provenance: dict[str, str] | None = None
    """What was actually built: ``{remote, ref, resolved_commit, build_host}`` for a git source
    (``remote`` userinfo-stripped), or best-effort ``{label, resolved_commit?}`` for a warm tree;
    ``None`` when provenance capture was unavailable. Recorded via ``BuildStepResult`` and surfaced
    on ``runs.get`` (#778). Defaulted so the three-positional construction sites stay valid."""


class ValidatedUpload(NamedTuple):
    """Externally uploaded build artifacts after manifest and content validation."""

    output: BuildOutput
    heads: dict[str, HeadResult]
