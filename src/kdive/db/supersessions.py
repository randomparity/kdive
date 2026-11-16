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


SUPERSESSIONS: tuple[Supersession, ...] = ()


def admits(version: str, recorded_sha256: str, current_sha256: str) -> bool:
    """Whether a recorded/on-disk checksum pair is a reviewed supersession of ``version``."""
    return any(
        entry.version == version
        and entry.superseded_sha256 == recorded_sha256
        and entry.current_sha256 == current_sha256
        for entry in SUPERSESSIONS
    )
