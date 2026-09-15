"""Finalization measurement instrumentation (#2318).

`runs.complete_build` emits one measurement record per finalization attempt, carrying the
phase attribution #2314 could not make from its reports. These tests pin the record's wire
shape, its per-exit-path outcome, and the two facts that make the attribution honest: the
semaphore wait is separated from the scan it gates, and the object-store counts equal what
validation actually issued.

The payload rides in the log *message* rather than a `logging` ``extra=`` attribute, because
both of this repository's serializers build a closed payload and merge only the
``bind_context`` fields (``log.py`` ``_kdive_ctx``; ``stdout_exporter._domain_context``, which
keeps only ``CONTEXT_FIELDS``), and ``bind_context`` rejects any name outside that audited set.
An attribute would be silently dropped by both.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from datetime import timedelta
from typing import Any

import pytest

from kdive.artifacts.storage import HeadResult
from kdive.artifacts.uploads.uploads import ManifestEntry
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.services.runs import complete_build as complete_build_service
from tests.clock import STORE_MTIME
from tests.mcp import complete_build_support
from tests.mcp.complete_build_support import (
    FakeValidator,
    build_output,
    complete_build,
    seed_external_run_with_manifest,
    valid_combined_kernel_tar,
)

_MEASUREMENT_LOGGER = "kdive.services.runs.complete_build"

_EXPECTED_KEYS = {
    "run_id",
    "prepare_ms",
    "reassemble_ms",
    "queue_wait_ms",
    "scan_ms",
    "publish_ms",
    "total_ms",
    "store_requests",
    "store_bytes",
    "chunked",
    "outcome",
}


def _sha256_b64(blob: bytes) -> str:
    """The base64 SHA-256 the upload contract stores (S3 ``x-amz-checksum-sha256``)."""
    return base64.b64encode(hashlib.sha256(blob).digest()).decode()


def _prefix(run_id: Any) -> str:
    """The object-key prefix `seed_external_run_with_manifest` stamps on the manifest."""
    return f"local/runs/{run_id}/"


def _finalizer(**kwargs: Any) -> complete_build_service.CompleteBuildFinalizer:
    """A finalizer whose store must be injected deliberately.

    Mirrors the wrapper in ``test_complete_build.py``: most tests here never reach the object
    store, so an un-injected factory raises rather than silently returning a usable default.
    """
    kwargs.setdefault("object_store_factory", _unexpected_store)
    return complete_build_service.CompleteBuildFinalizer(**kwargs)


def _unexpected_store() -> Any:
    raise AssertionError("this test did not inject an object store")


def _measurements(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Every measurement payload in ``caplog``, parsed out of the record's message."""
    event = complete_build_service._MEASUREMENT_EVENT
    payloads = []
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith(f"{event} "):
            payloads.append(json.loads(message[len(event) + 1 :]))
    return payloads


def _only_measurement(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    """The single measurement payload, asserting exactly one attempt was recorded."""
    found = _measurements(caplog)
    assert len(found) == 1, f"expected exactly one measurement record, got {len(found)}"
    return found[0]


class _CountingValidationStore:
    """A head + get_range store fake that keeps its own tally of what validation asked for.

    The tally is the point: the test compares the emitted record against *this* count rather
    than a literal, so the assertion cannot drift out of step with the validator's real read
    pattern when `_RANGE_CHUNK_BYTES` or the scan changes.
    """

    def __init__(self, blob: bytes, key: str) -> None:
        self._blob = blob
        self._key = key
        self.requests = 0
        self.bytes_served = 0

    def head(self, key: str, *, version_id: str | None = None) -> HeadResult | None:
        if key != self._key:
            return None
        self.requests += 1
        return HeadResult(
            size_bytes=len(self._blob),
            checksum_sha256=_sha256_b64(self._blob),
            etag="e",
            last_modified=STORE_MTIME,
            version_id="test-version",
        )

    def get_range(
        self, key: str, *, start: int, length: int, version_id: str | None = None
    ) -> bytes:
        chunk = self._blob[start : start + length]
        self.requests += 1
        self.bytes_served += len(chunk)
        return chunk

    def delete_version(self, key: str, version_id: str) -> None:
        raise AssertionError("single-PUT measurement path must not delete")

    def create_multipart_upload(
        self, key: str, *, sensitivity: object, retention_class: str
    ) -> str:
        raise AssertionError("single-PUT measurement path must not reassemble")


def test_success_emits_measurement_record(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A finalization that succeeds records one measurement carrying every field."""

    async def _run() -> None:
        async with complete_build_support.pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, ttl=timedelta(hours=1))
            with caplog.at_level(logging.INFO, logger=_MEASUREMENT_LOGGER):
                await complete_build(
                    pool,
                    run_id,
                    _finalizer(validate_complete_build=FakeValidator(build_output(run_id))),
                )

        payload = _only_measurement(caplog)
        assert payload["run_id"] == str(run_id)
        assert payload["outcome"] == "succeeded"
        assert set(payload) == _EXPECTED_KEYS

    asyncio.run(_run())


def test_validation_failure_records_its_category(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected artifact set records its ErrorCategory, not `succeeded`.

    `_validate_uploads` wraps every raised CategorizedError in CompleteBuildValidationError,
    which is a plain Exception — so an `except CategorizedError` arm in `complete` never fires
    and would leave the attempt recorded as a success.
    """

    async def _run() -> None:
        async with complete_build_support.pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, ttl=timedelta(hours=1))
            rejection = CategorizedError("bundle rejected", category=ErrorCategory.BUILD_FAILURE)
            with (
                caplog.at_level(logging.INFO, logger=_MEASUREMENT_LOGGER),
                pytest.raises(complete_build_service.CompleteBuildValidationError),
            ):
                await complete_build(
                    pool,
                    run_id,
                    _finalizer(validate_complete_build=FakeValidator(rejection)),
                )

        payload = _only_measurement(caplog)
        assert payload["outcome"] == ErrorCategory.BUILD_FAILURE.value

    asyncio.run(_run())


def test_configuration_failure_records_its_reason(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing upload manifest records its reason. A different `except` arm from validation."""

    async def _run() -> None:
        async with complete_build_support.pool(migrated_url) as pool:
            run_id = await complete_build_support.seed_external_run(pool)
            with (
                caplog.at_level(logging.INFO, logger=_MEASUREMENT_LOGGER),
                pytest.raises(complete_build_service.CompleteBuildConfigurationError),
            ):
                await complete_build(
                    pool,
                    run_id,
                    _finalizer(validate_complete_build=FakeValidator(build_output(run_id))),
                )

        payload = _only_measurement(caplog)
        assert payload["outcome"] == complete_build_service.NO_UPLOAD_MANIFEST

    asyncio.run(_run())


def test_unexpected_exception_records_unexpected(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing uncategorized is recorded as a success.

    A crashed or cancelled attempt recorded as `succeeded` is worse than one not recorded at
    all: the row would be read as measured evidence for the completion-contract decision.
    """

    async def _run() -> None:
        async with complete_build_support.pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, ttl=timedelta(hours=1))

            def _explode(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("publication blew up")

            with (
                caplog.at_level(logging.INFO, logger=_MEASUREMENT_LOGGER),
                pytest.raises(RuntimeError),
            ):
                await complete_build(
                    pool,
                    run_id,
                    _finalizer(validate_complete_build=_explode),
                )

        payload = _only_measurement(caplog)
        assert payload["outcome"] == "unexpected"

    asyncio.run(_run())


def test_store_counts_match_issued_requests(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The recorded counts equal what the real validator asked the store for.

    This one cannot use the injected-validator fixtures the rest of the module uses:
    `_validate_complete_build` returns from that injection *before* it builds the store, so a
    reused fixture would report 0 requests whether or not the counter works.
    """

    async def _run() -> None:
        blob = valid_combined_kernel_tar()
        async with complete_build_support.pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(
                pool,
                entries=[ManifestEntry("kernel", _sha256_b64(blob), len(blob))],
                ttl=timedelta(hours=1),
            )
            key = f"{_prefix(run_id)}kernel"
            store = _CountingValidationStore(blob, key)
            with caplog.at_level(logging.INFO, logger=_MEASUREMENT_LOGGER):
                await complete_build(
                    pool,
                    run_id,
                    _finalizer(object_store_factory=lambda: store),
                )

        payload = _only_measurement(caplog)
        assert store.requests > 0, "the validator issued no store requests; fixture is wrong"
        assert payload["store_requests"] == store.requests
        assert payload["store_bytes"] == store.bytes_served

    asyncio.run(_run())


def test_queue_wait_excludes_scan(
    migrated_url: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The semaphore wait is recorded apart from the scan it gates.

    This is the one attribution #2314 could not make: under `async with` the wait and the held
    region are one span, so a finalization slow because another was ahead of it is
    indistinguishable from one slow because its own archive is large.
    """
    held_for_s = 0.30

    async def _run() -> None:
        # Contend a semaphore created inside *this* event loop, never the module-level one.
        # `Semaphore.acquire` binds a loop only on the contended path (`_get_loop()` is not
        # reached when the counter is positive), so the shared global is normally unbound —
        # and a test that contends it would bind it to this test's loop and fail every later
        # test in the same process with "bound to a different event loop". That is a real
        # cross-test hazard, not a hypothetical: it took out the adversarial concurrency suite.
        slots = asyncio.Semaphore(1)
        monkeypatch.setattr(
            complete_build_service, "_EXTERNAL_BUILD_VALIDATION_SLOTS", slots, raising=True
        )
        async with complete_build_support.pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, ttl=timedelta(hours=1))

            async def _hold() -> None:
                async with slots:
                    await asyncio.sleep(held_for_s)

            holder = asyncio.create_task(_hold())
            await asyncio.sleep(0.05)
            with caplog.at_level(logging.INFO, logger=_MEASUREMENT_LOGGER):
                await complete_build(
                    pool,
                    run_id,
                    _finalizer(validate_complete_build=FakeValidator(build_output(run_id))),
                )
            await holder

        payload = _only_measurement(caplog)
        floor_ms = (held_for_s - 0.05) * 1000 * 0.5
        assert payload["queue_wait_ms"] >= floor_ms, (
            f"queue_wait_ms {payload['queue_wait_ms']} did not capture the held semaphore"
        )
        assert payload["scan_ms"] < payload["queue_wait_ms"], (
            "the held interval leaked into scan_ms; the two phases are not separated"
        )

    asyncio.run(_run())


def test_record_fields_are_closed_vocabulary(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The payload carries only a run id, numbers, a bool, and a fixed outcome string.

    Freezing the shape here is what keeps a later field from carrying a store key, an object
    path, or an operator-supplied string into the log, where the redactor's key pattern
    (`password|passwd|token|api[_-]?key|secret`) would not catch it.
    """

    async def _run() -> None:
        async with complete_build_support.pool(migrated_url) as pool:
            run_id = await seed_external_run_with_manifest(pool, ttl=timedelta(hours=1))
            with caplog.at_level(logging.INFO, logger=_MEASUREMENT_LOGGER):
                await complete_build(
                    pool,
                    run_id,
                    _finalizer(validate_complete_build=FakeValidator(build_output(run_id))),
                )

        payload = _only_measurement(caplog)
        assert set(payload) == _EXPECTED_KEYS
        assert isinstance(payload["run_id"], str)
        assert isinstance(payload["outcome"], str)
        assert isinstance(payload["chunked"], bool)
        for name in (
            "prepare_ms",
            "reassemble_ms",
            "queue_wait_ms",
            "scan_ms",
            "publish_ms",
            "total_ms",
        ):
            assert isinstance(payload[name], float), f"{name} is not a float"
        for name in ("store_requests", "store_bytes"):
            assert isinstance(payload[name], int), f"{name} is not an int"

    asyncio.run(_run())
