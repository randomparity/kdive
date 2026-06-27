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
    build_provenance: dict[str, str | bool] | None = None
    """What was actually built: ``{remote, ref, resolved_commit, build_host}`` for a git source
    (``remote`` userinfo-stripped), or best-effort ``{label, resolved_commit, dirty, tree_sha?}``
    for a warm tree (``dirty`` a bool; ``tree_sha`` only when dirty with tracked changes), down to
    ``{label}`` when the staged tree is not git; ``None`` when provenance capture was unavailable.
    The warm-tree lane builds **working-tree state, not HEAD**: ``resolved_commit`` is the HEAD the
    tree is based on (decorative when ``dirty``), and ``dirty``/``tree_sha`` cover git-tracked
    state only (#778, #861, ADR-0265). Recorded via ``BuildStepResult`` and surfaced on
    ``runs.get``. Defaulted so the three-positional construction sites stay valid."""


class ValidatedUpload(NamedTuple):
    """Externally uploaded build artifacts after manifest and content validation."""

    output: BuildOutput
    heads: dict[str, HeadResult]
