"""Unit tests for the build-config seed (ADR-0096).

The seed's publish / idempotency / source-aware behavior is exercised DB-backed in
``test_seed_db.py``: the source-guarded seed upsert acquires a real ``pg_advisory_xact_lock``
(ADR-0119) that a connection double cannot satisfy, so the former fake-conn seed tests were
retired in favor of the real-connection ones. This module keeps only the connection-free
packaged-fragment check.
"""

from __future__ import annotations

from kdive.build_configs.seed import KDUMP_FRAGMENT_PATH


def test_kdump_fragment_is_packaged_and_nonempty() -> None:
    data = KDUMP_FRAGMENT_PATH.read_bytes()
    assert data.strip()
    assert b"CONFIG_CRASH_DUMP=y" in data
