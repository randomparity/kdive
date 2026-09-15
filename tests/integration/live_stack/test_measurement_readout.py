"""The finalization measurement readout (#2318).

Unmarked on purpose, so `just test` runs these: the `live_stack` driver they support is the one
piece of this change the ordinary gate cannot exercise, and its two separable parts — parsing a
record back out of the server log, and the ppc64le preflight — are exactly the parts that can be
proven without a stack.

The fixtures are produced by `format_log_record_json`, the serializer the `server` process
actually runs, rather than hand-written. A hand-written envelope would test the parser against
this test's idea of the log format instead of the log format, which is the defect that made an
earlier draft aim the parser at `JsonFormatter` — a handler `init_telemetry` detaches at startup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared._httpx_utils import create_mcp_http_client
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs._internal import LogRecord
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.util.instrumentation import InstrumentationScope

from kdive.observability.stdout_exporter import format_log_record_json
from kdive.services.runs.complete_build import _MEASUREMENT_EVENT
from tests.integration.live_stack.measurement import (
    PPC64LE_BUNDLE_ENV,
    SUPPORTED_BUDGET_S,
    ppc64le_bundle_preflight,
    read_measurement_record,
)

_RUN_ID = "11111111-2222-3333-4444-555555555555"
_OTHER_RUN_ID = "99999999-8888-7777-6666-555555555555"


def _payload(run_id: str, **overrides: Any) -> dict[str, Any]:
    payload = {
        "run_id": run_id,
        "prepare_ms": 1.5,
        "reassemble_ms": 0.0,
        "queue_wait_ms": 2.25,
        "scan_ms": 1200.75,
        "publish_ms": 8.125,
        "total_ms": 1215.0,
        "store_requests": 42,
        "store_bytes": 4_196_910,
        "store_wait_ms": 91.25,
        "chunked": False,
        "outcome": "succeeded",
    }
    payload.update(overrides)
    return payload


def _server_log_line(payload: dict[str, Any]) -> str:
    """One `server.log` line, rendered by the exporter the server process runs."""
    record = LogRecord(
        timestamp=1_700_000_000_000_000_000,
        observed_timestamp=None,
        severity_text="INFO",
        body=f"{_MEASUREMENT_EVENT} {json.dumps(payload, sort_keys=True)}",
        trace_id=0x0123456789ABCDEF0123456789ABCDEF,
        span_id=0xFEDCBA9876543210,
        attributes={},
    )
    readable = ReadableLogRecord(
        log_record=record,
        resource=Resource.create({}),
        # The OTel scope name, not `kdive.services.runs.complete_build` — which is exactly why
        # the parser must not key on the `logger` field.
        instrumentation_scope=InstrumentationScope("opentelemetry.sdk._logs._internal"),
    )
    return format_log_record_json(readable)


def test_reads_row_from_otel_exporter(tmp_path: Path) -> None:
    """A row is recovered from a line the server's own serializer produced."""
    log = tmp_path / "server.log"
    log.write_text(
        '{"not":"json-with-msg"}\n'
        + "this line is not JSON at all\n"
        + _server_log_line(_payload(_RUN_ID))
        + "\n",
        encoding="utf-8",
    )

    row = read_measurement_record(log, _RUN_ID)

    assert row is not None
    assert row.run_id == _RUN_ID
    assert row.scan_ms == 1200.75
    assert row.store_requests == 42
    assert row.store_bytes == 4_196_910
    assert row.store_wait_ms == 91.25
    assert row.chunked is False
    assert row.outcome == "succeeded"
    assert row.accounted_ms <= row.total_ms


def test_ignores_other_run_ids(tmp_path: Path) -> None:
    """A concurrent finalization's record is not mistaken for this run's."""
    log = tmp_path / "server.log"
    log.write_text(
        _server_log_line(_payload(_OTHER_RUN_ID, scan_ms=9999.0)) + "\n",
        encoding="utf-8",
    )

    assert read_measurement_record(log, _RUN_ID) is None


def test_returns_the_last_attempt_for_a_run(tmp_path: Path) -> None:
    """A retried finalization reports its final attempt, not the first failure."""
    log = tmp_path / "server.log"
    log.write_text(
        _server_log_line(_payload(_RUN_ID, outcome="build_failure", scan_ms=5.0))
        + "\n"
        + _server_log_line(_payload(_RUN_ID, outcome="succeeded", scan_ms=1200.75))
        + "\n",
        encoding="utf-8",
    )

    row = read_measurement_record(log, _RUN_ID)

    assert row is not None
    assert row.outcome == "succeeded"
    assert row.scan_ms == 1200.75


def test_supported_budget_matches_the_shipped_client() -> None:
    """The threshold is read off a client built the way `over_http` builds one.

    An earlier version of this test asserted `SUPPORTED_BUDGET_S == 30.0`: a literal compared
    to itself, which passed no matter what fastmcp or the MCP SDK did. It therefore could not
    falsify the one claim ADR-0656's decision rests on — and did not, when the constant was
    30 s and the client's actual bound was 300 s. This builds the client the way
    `LiveStackClient.over_http` does, confirms it leaves the read timeout unset, and takes the
    budget from what the SDK then applies.
    """
    client = Client(
        StreamableHttpTransport(url="http://127.0.0.1:1/mcp", headers={"Authorization": "x"})
    )
    assert client._session_kwargs.get("read_timeout_seconds") is None, (  # noqa: SLF001
        "over_http passes no timeout; if that changed, the budget below is not what binds"
    )

    effective = create_mcp_http_client(headers={}).timeout
    assert effective.read == SUPPORTED_BUDGET_S, (
        f"the shipped client's read timeout is {effective.read}s, but the recorded supported "
        f"budget is {SUPPORTED_BUDGET_S}s; ADR-0656's decision is graded against this number"
    )
    connect = cast("float", effective.connect)
    assert connect < SUPPORTED_BUDGET_S, (
        "the connect timeout is not the request bound; a finalization this long has already "
        "connected, which is the confusion that produced the original 30 s figure"
    )


def test_ppc64le_arm_skips_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset means "not asked for": skip cleanly, as #1146 does.

    Caught as `pytest.skip.Exception` specifically. `pytest.skip` raises `Skipped`, which
    derives from `BaseException`, so `pytest.raises(Exception)` does not catch it — the skip
    would propagate and this test would *itself* skip, asserting nothing.
    """
    monkeypatch.delenv(PPC64LE_BUNDLE_ENV, raising=False)

    with pytest.raises(pytest.skip.Exception, match=PPC64LE_BUNDLE_ENV):
        ppc64le_bundle_preflight()


def test_ppc64le_arm_raises_when_the_path_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Set but absent is a broken request, not a skip — a measurement must not vanish."""
    monkeypatch.setenv(PPC64LE_BUNDLE_ENV, str(tmp_path / "nope"))

    with pytest.raises(RuntimeError, match="does not contain kernel.tar.gz"):
        ppc64le_bundle_preflight()


def test_ppc64le_arm_raises_without_a_bundle_tar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A directory lacking kernel.tar.gz likewise raises rather than skipping."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    monkeypatch.setenv(PPC64LE_BUNDLE_ENV, str(bundle))

    with pytest.raises(RuntimeError, match="does not contain kernel.tar.gz"):
        ppc64le_bundle_preflight()
