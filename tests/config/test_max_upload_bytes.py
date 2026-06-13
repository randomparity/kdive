"""The per-artifact upload cap default (ADR-0104 §6)."""

from __future__ import annotations

from kdive.config.core_settings import MAX_UPLOAD_BYTES


def test_max_upload_default_is_50_gib() -> None:
    assert MAX_UPLOAD_BYTES.default == str(50 * 1024 * 1024 * 1024)
