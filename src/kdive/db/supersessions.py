"""Reviewed exceptions to migration byte-immutability (ADR-0015, ADR-0818).

Each entry admits exactly one replacement of one applied migration file: a database that
recorded ``superseded_sha256`` for ``version`` keeps verifying once the file on disk hashes to
``current_sha256``. Nothing else is admitted, and recorded rows are never rewritten.

Standard library only: ``scripts/guards/schema_immutable_guard.py`` imports this module under a
bare ``python3`` with no venv.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Supersession:
    """One permitted, exact byte replacement of an applied migration file."""

    version: str
    filename: str
    superseded_sha256: str
    current_sha256: str
    decision: str


SUPERSESSIONS: tuple[Supersession, ...] = (
    Supersession(
        version="0112",
        filename="0112_capture_operation_supervision.sql",
        superseded_sha256="ea69a7d2e4dde25195a7735c33f953a74b43adcf51aa9f7a981557e14b2ac73a",  # noqa: E501  # pragma: allowlist secret — file-content SHA-256, not a credential
        current_sha256="7c3e31ea512bb8a030ca66e40f4591f916f61dbd15137155ba93943f381526d3",  # noqa: E501  # pragma: allowlist secret — file-content SHA-256, not a credential
        decision="ADR-0818",
    ),
)


def admits(version: str, recorded_sha256: str, current_sha256: str) -> bool:
    """Whether a recorded/on-disk checksum pair is a reviewed supersession of ``version``."""
    return any(
        entry.version == version
        and entry.superseded_sha256 == recorded_sha256
        and entry.current_sha256 == current_sha256
        for entry in SUPERSESSIONS
    )
