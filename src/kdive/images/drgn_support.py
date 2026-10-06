"""Computed live-drgn-introspection capability predicate (#3121).

In-guest drgn reads the Run's uploaded DWARF vmlinux staged in the guest; no released drgn reads
kernel BTF, so the shipped drgn version does not gate the capability. An image that carries the
drgn tooling and records a parseable drgn version is ``capable``, and the note tells the agent the
matching vmlinux must be uploaded with the build.

This module is the single, pure (no I/O) home for that rule and the capability an agent reads from
``images.describe`` before provisioning. It mirrors the kdump-capability predicate
(:mod:`kdive.images.kdump_support`): the build-recorded drgn version is the
per-image operand, and the predicate degrades to a non-confident ``unverified`` when the operand is
absent or unparseable, so metadata that predates the signal never reports a confident-but-wrong
answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TRIPLE_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_PAIR_RE = re.compile(r"(\d+)\.(\d+)")

_VMLINUX_NOTE = (
    "drgn-live reads the Run's uploaded DWARF vmlinux staged in the guest; upload the "
    "kernel's matching vmlinux with the build (drgn does not read kernel BTF)"
)


@dataclass(frozen=True, slots=True, order=True)
class DrgnVersion:
    """A drgn ``major.minor.patch`` version with total ordering."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> DrgnVersion:
        """Extract a dotted version from anywhere in ``value`` (e.g. a ``drgn --version`` banner).

        Args:
            value: A version string such as ``"0.0.31"``, ``"drgn 0.0.31"``, or a package
                stamp like ``"python-drgn-0.0.31-4.el10"``.

        Returns:
            The parsed version; a bare ``major.minor`` defaults ``patch`` to ``0``.

        Raises:
            ValueError: ``value`` contains no dotted version.
        """
        triple = _TRIPLE_RE.search(value)
        if triple is not None:
            return cls(int(triple[1]), int(triple[2]), int(triple[3]))
        pair = _PAIR_RE.search(value)
        if pair is not None:
            return cls(int(pair[1]), int(pair[2]), 0)
        raise ValueError(f"unrecognized drgn version: {value!r}")

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True, slots=True)
class LiveDrgnCapability:
    """The computed live-introspection capability of an image's shipped drgn.

    Attributes:
        status: ``capable``, ``incapable``, ``unverified``, or ``not_applicable``.
        drgn_version: The image's recorded drgn version as stored, or ``None`` when absent.
        min_drgn_required: Always ``None``; kept so the signal's keys stay stable now that no
            drgn version floor exists.
        note: A human-actionable note: the vmlinux upload requirement for ``capable``, the
            remedy for ``unverified``, else ``""``.
    """

    status: str
    drgn_version: str | None
    min_drgn_required: str | None
    note: str


def live_drgn_capability(*, drgn_version: str | None, drgn_tooling: bool) -> LiveDrgnCapability:
    """Compute an image's live-drgn introspection capability from its shipped drgn version.

    Args:
        drgn_version: The image's recorded drgn version (``None`` when absent).
        drgn_tooling: Whether the image carries the ``"drgn"`` tooling tag.

    Returns:
        The capability. ``not_applicable`` when the image has no drgn tooling; ``unverified``
        when the version is unknown or unparseable; otherwise ``capable``. ``incapable`` remains
        a valid status value but is never returned.
    """
    if not drgn_tooling:
        return LiveDrgnCapability(
            status="not_applicable",
            drgn_version=drgn_version,
            min_drgn_required=None,
            note="",
        )
    if not drgn_version:
        return LiveDrgnCapability(
            status="unverified",
            drgn_version=drgn_version,
            min_drgn_required=None,
            note="the image's drgn version is not recorded; rebuild the image to capture it",
        )
    try:
        DrgnVersion.parse(drgn_version)
    except ValueError:
        return LiveDrgnCapability(
            status="unverified",
            drgn_version=drgn_version,
            min_drgn_required=None,
            note=f"stored drgn version {drgn_version!r} is unrecognized",
        )
    return LiveDrgnCapability(
        status="capable", drgn_version=drgn_version, min_drgn_required=None, note=_VMLINUX_NOTE
    )
