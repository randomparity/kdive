"""Computed live-drgn-introspection capability predicate and its BTF-capability threshold.

In-guest drgn introspects the running kernel using the guest's own BTF
(``/sys/kernel/btf/vmlinux``, from ``CONFIG_DEBUG_INFO_BTF``) rather than uploaded DWARF; whether
that works end to end depends on the drgn build shipped in the image. The path of least resistance
lands on single-kernel distro images installing drgn unpinned from distro repos, so the shipped
version varies sharply by image family — an agent cannot tell before provisioning whether that
drgn can actually introspect the kernel it will boot.

This module is the single, pure (no I/O) home for the BTF-capability rule and the capability an
agent reads from ``images.describe`` before provisioning. It mirrors the kdump-capability
predicate (:mod:`kdive.images.kdump_support`): a build-recorded drgn version is the per-image
operand, and the predicate degrades to a non-confident ``unverified`` when the operand is absent or
unparseable, so metadata that predates the signal never reports a confident-but-wrong answer.

The threshold is a curated *lower* bound: drgn's ability to debug the running kernel without full
DWARF (kallsyms symbol index, ORC-from-core-dump unwinding, and the module API for BTF-backed
finders) reached practical usability at 0.0.31. It is a policy floor, not upstream truth, and may
be raised as drgn's BTF support matures — the release highlights are the human reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TRIPLE_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_PAIR_RE = re.compile(r"(\d+)\.(\d+)")

DRGN_RELEASE_HIGHLIGHTS_URL = "https://drgn.readthedocs.io/en/latest/release_highlights.html"


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


#: The curated minimum drgn that can introspect a live kernel from its in-guest BTF without
#: uploaded DWARF (drgn 0.0.31, the DWARFless-kernel-debugging milestone). A policy floor, revised
#: as drgn's BTF support matures; the release highlights are the human reference.
BTF_CAPABLE_DRGN: DrgnVersion = DrgnVersion(0, 0, 31)


@dataclass(frozen=True, slots=True)
class LiveDrgnCapability:
    """The computed live-introspection capability of an image's shipped drgn.

    Attributes:
        status: ``capable``, ``incapable``, ``unverified``, or ``not_applicable``.
        drgn_version: The image's recorded drgn version as stored, or ``None`` when absent.
        min_drgn_required: The BTF-capability threshold (``"0.0.31"``), or ``None`` when the
            image has no drgn tooling or the version is unknown.
        note: A human-actionable note (with the release-highlights pointer) for a non-``capable``
            status, else ``""``.
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
        when the version is unknown or unparseable; otherwise ``capable`` (the shipped drgn is at
        or above the BTF-capability threshold) or ``incapable`` (below it — it cannot introspect
        the live kernel from in-guest BTF alone).
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
        drgn = DrgnVersion.parse(drgn_version)
    except ValueError:
        return LiveDrgnCapability(
            status="unverified",
            drgn_version=drgn_version,
            min_drgn_required=None,
            note=f"stored drgn version {drgn_version!r} is unrecognized",
        )
    if drgn >= BTF_CAPABLE_DRGN:
        return LiveDrgnCapability(
            status="capable",
            drgn_version=drgn_version,
            min_drgn_required=str(BTF_CAPABLE_DRGN),
            note="",
        )
    return LiveDrgnCapability(
        status="incapable",
        drgn_version=drgn_version,
        min_drgn_required=str(BTF_CAPABLE_DRGN),
        note=(
            f"drgn {drgn} predates BTF-based live kernel introspection (needs "
            f">= {BTF_CAPABLE_DRGN}); this image cannot introspect a booted kernel from its "
            f"in-guest BTF without uploaded debuginfo — see {DRGN_RELEASE_HIGHLIGHTS_URL}"
        ),
    )
