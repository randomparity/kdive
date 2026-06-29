"""Pure console-part rotation core (issue #892, ADR-0095 carry mechanism).

Slices a System's growing console into redacted, threshold-sized parts. The seam
geometry is the hold-back-overlap carry proven in the remote-libvirt console
collector (``providers/remote_libvirt/console/collector.py`` ``_rotate``/
``_flush_tail``/``_carry``): a trailing ``SEAM_OVERLAP`` window is held back from
each rotation and emitted (redacted) prepended to the next part, so the overlap is
emitted exactly once and a secret straddling a boundary is redacted contiguously.

Unlike the streaming collector — which redacts each emitted slice — this pure core
redacts the whole pending delta **before** slicing. A part boundary is a hard byte
cut; redacting only the emitted side of the cut would split a secret that straddles
the cut across two independent redaction calls and store its halves raw. Redacting
the full ``carry + delta`` first replaces every secret fully contained in the delta
(secrets are bounded by ``SEAM_OVERLAP``) before any cut, so no part and no carry
ever holds a secret raw. A secret straddling the delta's trailing edge stays raw in
the held-back carry and is rejoined and redacted on the next feed.

The job is stateless across invocations, so the carry and the monotonic per-boot
part index live in :class:`RotationState` (persisted by the sidecar). Keying parts
by a monotonic ``index`` — not a plaintext byte offset — is required because the
carry means a part's logical start is not a clean file offset.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from kdive.providers.remote_libvirt.console.collector import (
    DEFAULT_ROTATION_THRESHOLD,
    DEFAULT_SEAM_OVERLAP,
)

# The steady-state rotation size and the trailing overlap re-scanned across each seam, reused
# from the collector so the two implementations share one geometry (Task 8 dedupes the collector
# onto this module).
ROTATION_THRESHOLD = DEFAULT_ROTATION_THRESHOLD
SEAM_OVERLAP = DEFAULT_SEAM_OVERLAP

Redact = Callable[[bytes], bytes]


@dataclass(frozen=True, slots=True)
class RotationState:
    """Resumable rotation cursor for one System (persisted by the sidecar).

    Attributes:
        plaintext_offset: Plaintext bytes consumed from the file so far; the next feed
            processes only ``file_bytes[plaintext_offset:]``.
        carry: Held-back overlap not yet sealed into a part, emitted (redacted) with the
            next part. Reproduces the collector's in-memory ``_carry`` across stateless jobs.
        next_index: Monotonic per-generation part index; the key component for the next part.
        boot_gen: Monotonic generation, bumped when a new boot is detected.
        boot_id: The boot identity this cursor belongs to (``None`` before the first feed).
    """

    plaintext_offset: int
    carry: bytes
    next_index: int
    boot_gen: int
    boot_id: str | None


@dataclass(frozen=True, slots=True)
class SealedPart:
    """One redacted console part, keyed by ``(gen, index)``.

    Attributes:
        gen: The boot generation the part belongs to.
        index: Monotonic index within the generation; the key component.
        redacted: The redacted part bytes — a contiguous redaction, never split.
    """

    gen: int
    index: int
    redacted: bytes


@dataclass(frozen=True, slots=True)
class RotationResult:
    """The parts sealed by one :func:`rotate` call plus the advanced cursor."""

    parts: list[SealedPart]
    next_state: RotationState


def part_object_name(gen: int, index: int) -> str:
    """Return the object key for a console part, e.g. ``console-part-0-000001``."""
    return f"console-part-{gen}-{index:06d}"


class SeamRotator:
    """Stateful hold-back-overlap rotator (collector ``_rotate`` carry, collector.py:213-241).

    Mirrors the collector's seam arithmetic: a part is ``data[:split]`` where
    ``data = held_overlap + window`` and ``split = len(data) - seam_overlap``, leaving the last
    ``seam_overlap`` bytes as the carry that leads the next part. The pending region is redacted
    in :meth:`drain_parts` before any cut, so a secret straddling an emit split is replaced
    before it can be split across two parts.
    """

    def __init__(
        self,
        redact: Redact,
        threshold: int,
        seam_overlap: int,
        carry: bytes = b"",
    ) -> None:
        self._redact = redact
        self._threshold = threshold
        self._seam_overlap = seam_overlap
        self._carry = carry
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        """Append a console delta to the pending buffer."""
        self._buffer.extend(data)

    def drain_parts(self) -> list[bytes]:
        """Seal as many full parts as the pending region yields, holding back the overlap.

        The pending ``carry + buffer`` is redacted once, then sliced with the collector's
        window carry: while a threshold of bytes remains, ``data = held + window`` and the part
        is ``data[:len(data) - seam_overlap]`` with ``data[split:]`` carried forward. The
        un-emitted overlap plus any sub-threshold remainder becomes the new :attr:`carry`.
        """
        redacted = self._redact(self._carry + bytes(self._buffer))
        self._buffer.clear()
        work = bytearray(redacted)
        held = b""
        parts: list[bytes] = []
        while len(work) >= self._threshold:
            window = bytes(work[: self._threshold])
            del work[: self._threshold]
            data = held + window
            if len(data) > self._seam_overlap:
                split = len(data) - self._seam_overlap
                emit, held = data[:split], data[split:]
            else:
                emit, held = b"", data
            if emit:
                parts.append(emit)
        self._carry = held + bytes(work)
        return parts

    @property
    def carry(self) -> bytes:
        """The held-back overlap plus sub-threshold remainder, emitted with the next part."""
        return self._carry


def _detect_new_boot(
    state: RotationState, file_bytes: bytes, boot_id: str
) -> tuple[int, int, bytes, int]:
    """Resolve the generation, read offset, carry, and starting index for this feed.

    A new boot — a changed ``boot_id`` or a file shorter than the prior offset (truncate/regrow)
    — restarts the cursor: a fresh generation (bumped only past a real prior boot), offset and
    index back to zero, and an empty carry. Otherwise the prior cursor is carried forward.
    """
    is_new_boot = boot_id != state.boot_id or len(file_bytes) < state.plaintext_offset
    if is_new_boot:
        gen = state.boot_gen + 1 if state.boot_id is not None else state.boot_gen
        return gen, 0, b"", 0
    return state.boot_gen, state.plaintext_offset, state.carry, state.next_index


def rotate(
    state: RotationState,
    file_bytes: bytes,
    boot_id: str,
    redact: Redact,
) -> RotationResult:
    """Seal new console parts from a growing file, mirroring the collector's seam carry.

    Pure: no I/O. Processes only the unconsumed delta ``file_bytes[offset:]`` through a
    :class:`SeamRotator`, so per-job work is bounded by the new bytes and the held-back tail
    lives in the carry rather than being re-read. A re-run from the same (un-advanced) state
    reproduces the identical ``(gen, index)`` parts, so a retry after a crash-before-sidecar is a
    no-op.

    Args:
        state: The resumable cursor from the prior run (the sidecar's persisted state).
        file_bytes: The full current console file contents.
        boot_id: The boot identity of ``file_bytes``; a change starts a new generation.
        redact: A pure ``bytes -> bytes`` redaction applied before any part is sealed.

    Returns:
        The parts sealed by this call and the advanced cursor.
    """
    gen, offset, carry, index = _detect_new_boot(state, file_bytes, boot_id)
    rotator = SeamRotator(redact, ROTATION_THRESHOLD, SEAM_OVERLAP, carry=carry)
    rotator.feed(file_bytes[offset:])
    parts: list[SealedPart] = []
    for blob in rotator.drain_parts():
        parts.append(SealedPart(gen=gen, index=index, redacted=blob))
        index += 1
    next_state = RotationState(
        plaintext_offset=len(file_bytes),
        carry=rotator.carry,
        next_index=index,
        boot_gen=gen,
        boot_id=boot_id,
    )
    return RotationResult(parts=parts, next_state=next_state)
