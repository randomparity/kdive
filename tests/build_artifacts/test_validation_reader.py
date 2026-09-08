"""Focused contracts for the external-build archive ranged reader (#2317)."""

from __future__ import annotations

import io

import pytest

from kdive.build_artifacts import validation
from kdive.domain.errors import CategorizedError, ErrorCategory


class _ReaderStore:
    def __init__(self, blob: bytes) -> None:
        self.blob = blob
        self.calls: list[tuple[int, int]] = []
        self.max_response_extra = 0
        self.response_limit: int | None = None
        self.failure: Exception | None = None

    def head(self, key: str) -> None:
        del key
        return None

    def get_range(
        self, key: str, *, start: int, length: int, version_id: str | None = None
    ) -> bytes:
        del key, version_id
        self.calls.append((start, length))
        if self.failure is not None:
            raise self.failure
        response_length = length + self.max_response_extra
        if self.response_limit is not None:
            response_length = min(response_length, self.response_limit)
        return self.blob[start : start + response_length]


def _pattern(size: int) -> bytes:
    unit = bytes(range(251))
    return (unit * (size // len(unit) + 1))[:size]


def test_ranged_reader_buffers_sequential_reads() -> None:
    blob = _pattern(64 * 1024)
    store = _ReaderStore(blob)
    reader = validation._RangedReader(store, "kernel", len(blob))

    assert b"".join(reader.read(1024) for _ in range(32)) == blob[: 32 * 1024]
    assert reader.tell() == 32 * 1024
    assert store.calls == [(0, len(blob))]


def test_ranged_reader_crosses_buffer_boundary() -> None:
    window = validation._RANGE_CHUNK_BYTES
    blob = _pattern(window + 32)
    store = _ReaderStore(blob)
    reader = validation._RangedReader(store, "kernel", len(blob))

    assert reader.read(window - 8) == blob[: window - 8]
    assert reader.read(16) == blob[window - 8 : window + 8]
    assert store.calls == [(0, window), (window, 32)]


def test_ranged_reader_seek_modes_and_buffer_reuse() -> None:
    blob = _pattern(4096)
    store = _ReaderStore(blob)
    reader = validation._RangedReader(store, "kernel", len(blob))

    assert reader.seekable()
    assert reader.read(64) == blob[:64]
    assert reader.seek(16) == 16
    assert reader.read(8) == blob[16:24]
    assert reader.seek(10, io.SEEK_CUR) == 34
    assert reader.seek(-6, io.SEEK_END) == len(blob) - 6
    assert reader.read() == blob[-6:]
    assert store.calls == [(0, len(blob))]


def test_ranged_reader_reloads_after_out_of_window_seek() -> None:
    window = validation._RANGE_CHUNK_BYTES
    blob = _pattern(window + 64)
    store = _ReaderStore(blob)
    reader = validation._RangedReader(store, "kernel", len(blob))

    assert reader.read(1) == blob[:1]
    assert reader.seek(window + 1) == window + 1
    assert reader.read(1) == blob[window + 1 : window + 2]
    assert store.calls == [(0, window), (window + 1, 63)]


def test_ranged_reader_default_and_large_reads_do_not_retain_over_cap() -> None:
    window = validation._RANGE_CHUNK_BYTES
    blob = _pattern(window + 32)
    store = _ReaderStore(blob)
    reader = validation._RangedReader(store, "kernel", len(blob))

    assert reader.read() == blob
    assert reader.tell() == len(blob)
    assert store.calls == [(0, len(blob))]
    assert reader.seek(window + 1) == window + 1
    assert reader.read(1) == blob[window + 1 : window + 2]
    assert store.calls[-1] == (window + 1, 31)


def test_ranged_reader_eof_and_invalid_seeks() -> None:
    blob = b"abcdef"
    store = _ReaderStore(blob)
    reader = validation._RangedReader(store, "kernel", len(blob))

    assert reader.read(0) == b""
    assert reader.tell() == 0
    assert store.calls == []
    assert reader.seek(len(blob) + 10) == len(blob) + 10
    assert reader.read(1) == b""
    assert store.calls == []
    with pytest.raises(ValueError, match="negative seek position"):
        reader.seek(-1)
    with pytest.raises(ValueError, match="unsupported whence"):
        reader.seek(0, 99)


def test_ranged_reader_preserves_short_and_empty_responses() -> None:
    blob = b"abcdefgh"
    store = _ReaderStore(blob)
    store.response_limit = 2
    reader = validation._RangedReader(store, "kernel", len(blob))

    assert reader.read(4) == b"ab"
    assert reader.tell() == 2
    store.response_limit = 0
    assert reader.read(4) == b""
    assert reader.tell() == 2


def test_ranged_reader_rejects_response_over_fetch_bound() -> None:
    window = validation._RANGE_CHUNK_BYTES
    store = _ReaderStore(_pattern(window + 1))
    store.max_response_extra = 1
    reader = validation._RangedReader(store, "kernel", len(store.blob))

    with pytest.raises(CategorizedError) as exc:
        reader.read(1)
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert "exceeded the requested bound" in str(exc.value)


def test_ranged_reader_does_not_touch_store_on_hit_and_propagates_miss_failure() -> None:
    window = validation._RANGE_CHUNK_BYTES
    blob = _pattern(window + 8)
    store = _ReaderStore(blob)
    reader = validation._RangedReader(store, "kernel", len(blob))

    assert reader.read(8) == blob[:8]
    failure = CategorizedError("store unavailable", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
    store.failure = failure
    assert reader.seek(4) == 4
    assert reader.read(1) == blob[4:5]
    assert reader.seek(window + 1) == window + 1
    with pytest.raises(CategorizedError) as exc:
        reader.read(1)
    assert exc.value is failure


def test_ranged_reader_cross_window_exception_keeps_cursor_for_retry() -> None:
    window = validation._RANGE_CHUNK_BYTES
    blob = _pattern(window + 16)
    store = _ReaderStore(blob)
    reader = validation._RangedReader(store, "kernel", len(blob))
    start = window - 8

    assert reader.read(start) == blob[:start]
    failure = CategorizedError("store unavailable", category=ErrorCategory.INFRASTRUCTURE_FAILURE)
    store.failure = failure
    with pytest.raises(CategorizedError) as exc:
        reader.read(16)
    assert exc.value is failure
    assert reader.tell() == start

    store.failure = None
    assert reader.read(16) == blob[start : start + 16]


def test_ranged_reader_cross_window_oversized_response_keeps_cursor_for_retry() -> None:
    window = validation._RANGE_CHUNK_BYTES
    blob = _pattern(window + 17)
    store = _ReaderStore(blob)
    reader = validation._RangedReader(store, "kernel", len(blob) - 1)
    start = window - 8

    assert reader.read(start) == blob[:start]
    store.max_response_extra = 1
    with pytest.raises(CategorizedError) as exc:
        reader.read(16)
    assert exc.value.category is ErrorCategory.BUILD_FAILURE
    assert reader.tell() == start

    store.max_response_extra = 0
    assert reader.read(16) == blob[start : start + 16]


def test_ranged_reader_cross_window_empty_response_returns_cached_suffix() -> None:
    window = validation._RANGE_CHUNK_BYTES
    blob = _pattern(window + 16)
    store = _ReaderStore(blob)
    reader = validation._RangedReader(store, "kernel", len(blob))
    start = window - 8

    assert reader.read(start) == blob[:start]
    store.response_limit = 0
    assert reader.read(16) == blob[start:window]
    assert reader.tell() == window
    assert store.calls[-1] == (window, 16)

    store.response_limit = None
    assert reader.read(8) == blob[window : window + 8]
    assert store.calls[-1] == (window, 16)
