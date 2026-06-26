"""Computed kdump-capability predicate and its data-driven support matrix (ADR-0253).

A from-source kernel's vmcore can only be filtered into a complete kdump core by a makedumpfile
new enough for that kernel (ADR-0251 / #817: a v7.0-class kernel needs makedumpfile ``>= 1.7.9``).
This module is the single, pure (no I/O) home for that rule and the capability computation an agent
reads from ``images.describe`` before provisioning. It replaces the write-only, kernel-relative
``RootfsCatalogEntry.kdump_capable`` bit.

The relationship is monotonic and the constraint is an *upper* bound: a makedumpfile supports
kernels *up to* some version, so an older makedumpfile never starts supporting a newer kernel. The
predicate is honest about its knowledge boundary — a kernel newer than anything characterized, or an
older kernel whose requirement was never characterized and whose image does not meet the highest
characterized requirement, is reported ``unverified`` (with a ChangeLog pointer), never a confident
``capable``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TRIPLE_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_PAIR_RE = re.compile(r"(\d+)\.(\d+)")
_KERNEL_RE = re.compile(r"\s*(\d+)(?:\.(\d+))?")

MAKEDUMPFILE_CHANGELOG_URL = "https://github.com/makedumpfile/makedumpfile/blob/master/ChangeLog"


@dataclass(frozen=True, slots=True, order=True)
class MakedumpfileVersion:
    """A makedumpfile ``major.minor.patch`` version with total ordering."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: str) -> MakedumpfileVersion:
        """Extract a dotted version from anywhere in ``value`` (e.g. a ``--version`` banner).

        Args:
            value: A version string such as ``"1.7.9"`` or ``"makedumpfile: version 1.7.9 (...)"``.

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
        raise ValueError(f"unrecognized makedumpfile version: {value!r}")

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True, slots=True, order=True)
class KernelVersion:
    """A kernel version compared on ``(major, minor)`` — the dump-format generation.

    A from-source kernel is named ``7.0``, ``7.0.5``, ``7.1.0-rc2``, ``7.0.0-00123-gdeadbee+`` etc.;
    only ``major.minor`` selects the makedumpfile requirement, so a stable point release or a
    ``-rc``/``+localversion`` suffix never crosses a matrix threshold.
    """

    major: int
    minor: int

    @classmethod
    def parse(cls, value: str) -> KernelVersion:
        """Read the leading ``major[.minor]`` of ``value``, ignoring any suffix.

        Args:
            value: A kernel version string; a missing minor is treated as ``0``.

        Returns:
            The parsed ``(major, minor)`` version.

        Raises:
            ValueError: ``value`` has no leading integer.
        """
        match = _KERNEL_RE.match(value)
        if match is None:
            raise ValueError(f"unrecognized kernel version: {value!r}")
        return cls(int(match[1]), int(match[2]) if match[2] is not None else 0)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


# Each row means "a kernel in this row's major.minor line (up to the next row) needs >= the paired
# makedumpfile". Ascending by kernel; verified against the makedumpfile ChangeLog (ADR-0251 / #817).
SUPPORT_MATRIX: tuple[tuple[KernelVersion, MakedumpfileVersion], ...] = (
    (KernelVersion(7, 0), MakedumpfileVersion(1, 7, 9)),
)
KNOWN_THROUGH: KernelVersion = SUPPORT_MATRIX[-1][0]
DEFAULT_KERNEL_BASIS: KernelVersion = KNOWN_THROUGH
MAX_CHARACTERIZED_REQUIREMENT: MakedumpfileVersion = SUPPORT_MATRIX[-1][1]


def required_makedumpfile(kernel: KernelVersion) -> MakedumpfileVersion | None:
    """The minimum makedumpfile a characterized ``kernel`` needs, or ``None`` below the matrix.

    Returns the makedumpfile of the highest matrix row whose kernel ``<= kernel``; ``None`` when
    ``kernel`` predates every characterized row (no floor — an un-characterized older kernel has no
    asserted requirement).
    """
    for row_kernel, makedumpfile in reversed(SUPPORT_MATRIX):
        if row_kernel <= kernel:
            return makedumpfile
    return None


@dataclass(frozen=True, slots=True)
class KdumpCapability:
    """The computed kdump capability of an image for one target kernel.

    Attributes:
        status: ``capable``, ``incapable``, ``unverified``, or ``not_applicable``.
        target_kernel: The kernel basis the answer was computed against (``"7.0"``).
        makedumpfile_version: The image's makedumpfile version as recorded, or ``None`` when absent.
        min_makedumpfile_required: The characterized minimum for ``target_kernel``, or ``None`` when
            the kernel is outside the characterized range.
        note: A human-actionable note for ``unverified`` (with the ChangeLog pointer), else ``""``.
    """

    status: str
    target_kernel: str
    makedumpfile_version: str | None
    min_makedumpfile_required: str | None
    note: str


def _unverified_unknown_version(
    target_kernel: KernelVersion, makedumpfile_version: str | None
) -> KdumpCapability:
    """``unverified`` because the image's makedumpfile version is missing or unparseable."""
    return KdumpCapability(
        status="unverified",
        target_kernel=str(target_kernel),
        makedumpfile_version=makedumpfile_version,
        min_makedumpfile_required=None,
        note="the image's makedumpfile version is not recorded; rebuild the image to capture it",
    )


def _capability_for_known_version(
    target_kernel: KernelVersion, makedumpfile: MakedumpfileVersion, raw_version: str
) -> KdumpCapability:
    """Capability for a parseable image version against a target kernel (the §1 tail branches)."""
    if target_kernel > KNOWN_THROUGH:
        return KdumpCapability(
            status="unverified",
            target_kernel=str(target_kernel),
            makedumpfile_version=raw_version,
            min_makedumpfile_required=None,
            note=(
                f"makedumpfile {makedumpfile} shipped; the minimum for kernel {target_kernel} is "
                f"unverified — check the makedumpfile ChangeLog: {MAKEDUMPFILE_CHANGELOG_URL}"
            ),
        )
    required = required_makedumpfile(target_kernel)
    if required is not None:
        return KdumpCapability(
            status="capable" if makedumpfile >= required else "incapable",
            target_kernel=str(target_kernel),
            makedumpfile_version=raw_version,
            min_makedumpfile_required=str(required),
            note="",
        )
    if makedumpfile >= MAX_CHARACTERIZED_REQUIREMENT:
        return KdumpCapability(
            status="capable",
            target_kernel=str(target_kernel),
            makedumpfile_version=raw_version,
            min_makedumpfile_required=None,
            note="",
        )
    return KdumpCapability(
        status="unverified",
        target_kernel=str(target_kernel),
        makedumpfile_version=raw_version,
        min_makedumpfile_required=None,
        note=(
            f"the minimum makedumpfile for kernel {target_kernel} is not characterized — check the "
            f"makedumpfile ChangeLog: {MAKEDUMPFILE_CHANGELOG_URL}"
        ),
    )


def kdump_capability(
    *, makedumpfile_version: str | None, target_kernel: KernelVersion, kdump_tooling: bool
) -> KdumpCapability:
    """Compute an image's kdump capability for ``target_kernel`` (ADR-0253).

    Args:
        makedumpfile_version: The image's recorded makedumpfile version (``None`` when absent).
        target_kernel: The kernel the capability is computed against.
        kdump_tooling: Whether the image carries the ``"kdump"`` tooling tag.

    Returns:
        The capability with its kernel basis disclosed. ``not_applicable`` when the image has no
        kdump tooling; ``unverified`` when the version is unknown/unparseable, the kernel is newer
        than the characterized range, or an older un-characterized kernel's image does not meet the
        highest characterized requirement; otherwise ``capable``/``incapable``.
    """
    if not kdump_tooling:
        return KdumpCapability(
            status="not_applicable",
            target_kernel=str(target_kernel),
            makedumpfile_version=makedumpfile_version,
            min_makedumpfile_required=None,
            note="",
        )
    if not makedumpfile_version:
        return _unverified_unknown_version(target_kernel, makedumpfile_version)
    try:
        makedumpfile = MakedumpfileVersion.parse(makedumpfile_version)
    except ValueError:
        return KdumpCapability(
            status="unverified",
            target_kernel=str(target_kernel),
            makedumpfile_version=makedumpfile_version,
            min_makedumpfile_required=None,
            note=f"stored makedumpfile version {makedumpfile_version!r} is unrecognized",
        )
    return _capability_for_known_version(target_kernel, makedumpfile, makedumpfile_version)
