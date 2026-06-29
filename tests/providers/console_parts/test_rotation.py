"""Tests for the pure console-part rotation core (issue #892).

These are pure unit tests: no I/O, no DB, no Docker. They drive the extracted
``SeamRotator`` carry geometry and assert that a secret straddling any boundary
(internal emit split or job/feed boundary) is redacted contiguously and never
stored raw.
"""

from __future__ import annotations

from kdive.providers.console_parts.rotation import (
    ROTATION_THRESHOLD,
    SEAM_OVERLAP,
    RotationState,
    part_object_name,
    rotate,
)


def _ident(b: bytes) -> bytes:
    return b


S0 = RotationState(plaintext_offset=0, carry=b"", next_index=0, boot_gen=0, boot_id=None)


def test_seals_full_threshold_parts_indexed_monotonically():
    data = b"A" * (ROTATION_THRESHOLD * 2 + 10)
    r = rotate(S0, data, "id1", _ident)
    assert [(p.gen, p.index) for p in r.parts] == [(0, 0), (0, 1)]
    assert r.next_state.next_index == 2
    # the trailing < threshold remainder (plus the held-back overlap) is carried, not sealed
    assert len(r.next_state.carry) >= 10
    assert part_object_name(0, 1) == "console-part-0-000001"


def test_secret_split_across_internal_part_boundary_is_redacted():
    # place the secret straddling the EMIT split (ROTATION_THRESHOLD - SEAM_OVERLAP) so the test
    # proves the carry geometry, not just a threshold-aligned boundary
    sensitive = b"ZZ-INTERNAL-BOUNDARY-MARKER-ZZ"
    split = ROTATION_THRESHOLD - SEAM_OVERLAP
    data = b"a" * (split - len(sensitive) // 2) + sensitive + b"b" * (ROTATION_THRESHOLD * 2)
    redact = lambda b: b.replace(sensitive, b"[REDACTED]")  # noqa: E731
    r = rotate(S0, data, "id1", redact)
    joined = b"".join(p.redacted for p in r.parts)
    assert sensitive not in joined and sensitive not in r.next_state.carry


def test_no_new_bytes_yields_no_parts_and_same_state():
    data = b"B" * ROTATION_THRESHOLD
    r1 = rotate(S0, data, "id1", _ident)
    r2 = rotate(r1.next_state, data, "id1", _ident)
    assert r2.parts == [] and r2.next_state.next_index == r1.next_state.next_index


def test_retry_same_delta_produces_same_keys_idempotent():
    data = b"C" * (ROTATION_THRESHOLD * 2 + 5)
    first = rotate(S0, data, "id1", _ident)
    # crash before sidecar write: re-run from the SAME (un-advanced) state
    retry = rotate(S0, data, "id1", _ident)
    assert [(p.gen, p.index) for p in first.parts] == [(p.gen, p.index) for p in retry.parts]


def test_boot_id_change_resets_and_bumps_generation():
    prior = RotationState(ROTATION_THRESHOLD * 3, b"leftover", 5, 0, "old")
    new = rotate(prior, b"D" * (ROTATION_THRESHOLD + 4), "new", _ident)
    assert new.next_state.boot_gen == 1 and new.next_state.boot_id == "new"
    assert new.next_state.next_index >= 1 and all(p.gen == 1 for p in new.parts)
    assert new.parts[0].index == 0  # new generation re-indexes from 0


def test_truncate_regrow_past_old_offset_detected_via_boot_id():
    # file already grew past old offset; size-only check would miss it, boot_id catches it
    prior = RotationState(ROTATION_THRESHOLD * 2, b"", 9, 0, "old")
    r = rotate(prior, b"E" * (ROTATION_THRESHOLD * 4), "new", _ident)
    assert r.next_state.boot_gen == 1 and r.parts[0].index == 0  # new boot's early console captured


def test_secret_split_across_job_boundary_is_redacted():
    sensitive = b"ZZ-REDACT-ME-MARKER-ZZ"
    full = b"x" * (ROTATION_THRESHOLD - 5) + sensitive + b"y" * (ROTATION_THRESHOLD)
    redact = lambda b: b.replace(sensitive, b"[REDACTED]")  # noqa: E731
    first = rotate(S0, full, "id1", redact)  # job 1 holds back the straddling region in carry
    second = rotate(first.next_state, full, "id1", redact)  # job 2 emits it, redacted
    joined = b"".join(p.redacted for p in first.parts + second.parts)
    assert sensitive not in joined  # never stored raw on either side of the seam
