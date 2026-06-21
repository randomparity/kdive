"""End-to-end facade wiring (ADR-0090 §1, §2): bootstrap ordering + stdout floor.

Asserts the acceptance criteria that live at the facade boundary: a registered secret
is redacted in the stdout output the running process actually emits, the stdlib floor
is installed first and then handed over to the OTel bridge (no doubling), and OTLP
stays off by default.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

import kdive.config as config
from kdive.log import _KdiveHandler
from kdive.observability import facade
from kdive.observability.facade import (
    LoggingHandler,
    Telemetry,
    _sampler,
    bootstrap_stdout_floor,
    init_telemetry,
    otlp_enabled,
)
from kdive.security.secrets.secret_registry import SecretRegistry

_SERVICE_NAME_KEY = "service.name"
_SERVICE_NAMESPACE_KEY = "service.namespace"
_RATIO_SAMPLER_DESCRIPTION = (
    "ParentBased{{root:TraceIdRatioBased{{{ratio}}},remoteParentSampled:AlwaysOnSampler,"
    "remoteParentNotSampled:AlwaysOffSampler,localParentSampled:AlwaysOnSampler,"
    "localParentNotSampled:AlwaysOffSampler}}"
)


@pytest.fixture(autouse=True)
def _restore_root() -> Iterator[None]:
    root = logging.getLogger()
    before = list(root.handlers)
    before_level = root.level
    yield
    root.handlers = before
    root.setLevel(before_level)


def test_bootstrap_floor_installs_stdlib_handler_first() -> None:
    config.load({})
    bootstrap_stdout_floor("INFO", secret_registry=SecretRegistry())
    root = logging.getLogger()
    assert any(isinstance(h, _KdiveHandler) for h in root.handlers)


def test_init_telemetry_replaces_floor_with_bridge() -> None:
    config.load({})
    registry = SecretRegistry()
    bootstrap_stdout_floor("INFO", secret_registry=registry)
    init_telemetry("server", secret_registry=registry, level="INFO")
    root = logging.getLogger()
    assert not any(isinstance(h, _KdiveHandler) for h in root.handlers), "floor must be removed"
    assert any(isinstance(h, LoggingHandler) for h in root.handlers), "bridge must be installed"


def _meter_resource_attrs(telemetry: Telemetry) -> dict[str, object]:
    return dict(telemetry.meter_provider._sdk_config.resource.attributes)


def test_init_telemetry_threads_service_name_into_every_provider_resource() -> None:
    config.load({})
    telemetry = init_telemetry("server", secret_registry=SecretRegistry(), level="INFO")

    for attrs in (
        dict(telemetry.tracer_provider.resource.attributes),
        dict(telemetry.logger_provider.resource.attributes),
        _meter_resource_attrs(telemetry),
    ):
        assert attrs[_SERVICE_NAME_KEY] == "server"
        assert attrs[_SERVICE_NAMESPACE_KEY] == "kdive"


def test_init_telemetry_returns_distinct_live_providers() -> None:
    config.load({})
    telemetry = init_telemetry("worker", secret_registry=SecretRegistry(), level="INFO")

    assert telemetry.meter_provider is not None
    assert telemetry.tracer_provider is not None
    assert telemetry.logger_provider is not None
    # The tracer provider carries the configured ratio sampler, not the SDK default (which a
    # dropped/None sampler argument would silently substitute).
    assert telemetry.tracer_provider.sampler.get_description() == _RATIO_SAMPLER_DESCRIPTION.format(
        ratio="0.1"
    )


def test_init_telemetry_attaches_scrape_reader_to_meter_provider() -> None:
    # The aux /metrics endpoint renders the in-memory scrape reader; it must be wired onto the
    # meter provider's readers or a Prometheus pull would see nothing.
    config.load({})
    telemetry = init_telemetry("worker", secret_registry=SecretRegistry(), level="INFO")

    readers = telemetry.meter_provider._sdk_config.metric_readers
    assert telemetry.scrape_reader in readers


def test_init_telemetry_registers_returned_providers_globally(monkeypatch) -> None:
    # init must publish the providers it built (not None) as the process-global providers so
    # library instrumentation resolves the same pipeline.
    config.load({})
    meter_set: list[object] = []
    tracer_set: list[object] = []
    logger_set: list[object] = []
    monkeypatch.setattr(facade.metrics, "set_meter_provider", meter_set.append)
    monkeypatch.setattr(facade.trace, "set_tracer_provider", tracer_set.append)
    monkeypatch.setattr(facade, "set_logger_provider", logger_set.append)

    telemetry = init_telemetry("worker", secret_registry=SecretRegistry(), level="INFO")

    assert meter_set == [telemetry.meter_provider]
    assert tracer_set == [telemetry.tracer_provider]
    assert logger_set == [telemetry.logger_provider]


def test_sampler_uses_configured_ratio() -> None:
    config.load({"KDIVE_OTEL_TRACES_SAMPLER_RATIO": "0.5"})
    assert _sampler().get_description() == _RATIO_SAMPLER_DESCRIPTION.format(ratio="0.5")


def test_sampler_defaults_to_one_tenth_when_unset() -> None:
    config.load({})
    assert _sampler().get_description() == _RATIO_SAMPLER_DESCRIPTION.format(ratio="0.1")


def test_otlp_enabled_true_for_truthy_value() -> None:
    config.load({"KDIVE_OTEL_ENABLED": "true"})
    assert otlp_enabled() is True


def test_otlp_enabled_false_for_non_truthy_value() -> None:
    # A set-but-unrecognized value must read as off, not on (the membership check, not its
    # negation, decides).
    config.load({"KDIVE_OTEL_ENABLED": "garbage"})
    assert otlp_enabled() is False


def test_otlp_enabled_false_when_unset() -> None:
    config.load({})
    assert otlp_enabled() is False


def test_resource_uses_configured_namespace() -> None:
    config.load({"KDIVE_OTEL_SERVICE_NAMESPACE": "acme"})
    telemetry = init_telemetry("server", secret_registry=SecretRegistry(), level="INFO")

    assert dict(telemetry.tracer_provider.resource.attributes)[_SERVICE_NAMESPACE_KEY] == "acme"


def test_bootstrap_floor_applies_requested_level() -> None:
    config.load({})
    bootstrap_stdout_floor("DEBUG", secret_registry=SecretRegistry())

    assert logging.getLogger().level == logging.DEBUG


def test_bridge_root_logger_sets_configured_level() -> None:
    config.load({})
    bootstrap_stdout_floor("INFO", secret_registry=SecretRegistry())
    init_telemetry("server", secret_registry=SecretRegistry(), level="WARNING")

    assert logging.getLogger().level == logging.WARNING


def test_bridge_root_logger_falls_back_to_info_for_unknown_level() -> None:
    # An unrecognized level name must fall back to INFO, not to a None level that breaks the
    # root logger's effective-level resolution.
    config.load({})
    bootstrap_stdout_floor("INFO", secret_registry=SecretRegistry())
    init_telemetry("server", secret_registry=SecretRegistry(), level="NOPE")

    assert logging.getLogger().level == logging.INFO


def test_registered_secret_is_redacted_in_stdout(monkeypatch) -> None:
    config.load({})
    registry = SecretRegistry()
    registry.register("hunter2-prod-token", scope=None)
    stream = io.StringIO()
    # The stdout exporter binds sys.stderr at construction, so patch before init.
    monkeypatch.setattr("sys.stderr", stream)
    telemetry = init_telemetry("worker", secret_registry=registry, level="INFO")
    logging.getLogger("kdive.test.facade").info("using hunter2-prod-token to connect")
    telemetry.logger_provider.force_flush()
    output = stream.getvalue()
    assert output, "expected the stdout exporter to emit a line"
    record = json.loads(output.splitlines()[-1])
    assert "hunter2-prod-token" not in output
    assert "[REDACTED]" in record["msg"]
    assert record["logger"] == "kdive.test.facade"
