"""Shared provider-port models for supervised traffic capture (ADR-0558)."""

from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.capture_operations.protocol import CaptureRequest
from kdive.providers.assembly import composition
from kdive.providers.ports.traffic import (
    LocalCaptureConfiguration,
    QuiescenceEvidence,
    RemoteCaptureConfiguration,
    TrafficCaptureExecutor,
)


def test_local_child_configuration_is_resource_bound_and_uri_allowlisted() -> None:
    resource_id = uuid4()
    configuration = LocalCaptureConfiguration(
        resource_id=resource_id,
        uri="qemu:///session",
    )

    assert (
        LocalCaptureConfiguration.from_canonical_json(configuration.to_canonical_json())
        == configuration
    )
    assert configuration.resource_id == resource_id

    with pytest.raises(ValidationError, match="local libvirt URI"):
        LocalCaptureConfiguration(resource_id=resource_id, uri="qemu+tcp://host/system")


def test_remote_child_configuration_carries_only_exact_resource_bound_references() -> None:
    resource_id = uuid4()
    configuration = RemoteCaptureConfiguration(
        resource_id=resource_id,
        uri="qemu+tls://host.example/system",
        client_cert_ref="client.pem",
        client_key_ref="client-key.pem",  # pragma: allowlist secret
        ca_cert_ref="ca.pem",
        secrets_root="/run/secrets/kdive",
        storage_pool="kdive",
    )

    encoded = configuration.to_canonical_json()
    assert RemoteCaptureConfiguration.from_canonical_json(encoded) == configuration
    assert b"postgres" not in encoded
    assert b"BEGIN PRIVATE KEY" not in encoded  # pragma: allowlist secret


def test_quiescence_evidence_is_bounded_and_contains_no_transport_details() -> None:
    evidence = QuiescenceEvidence(
        provider_kind="remote-libvirt",
        resource_id=uuid4(),
        domain_name="kdive-domain",
        qom_id="kdive-dump-job",
        result="absent",
        ordering="fresh-qmp-connection",
    )

    payload = evidence.as_dict()
    assert payload["result"] == "absent"
    assert "uri" not in payload
    assert "credential" not in payload
    assert len(evidence.to_canonical_json()) <= 4096


def test_capture_executor_dispatch_validates_resource_before_provider_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = CaptureRequest(
        job_id=uuid4(),
        provider_kind="local-libvirt",
        resource_id=uuid4(),
        system_id=uuid4(),
        domain_name="kdive-local",
        snaplen=128,
        max_bytes=1_048_576,
        max_polls=1,
    )
    configuration = LocalCaptureConfiguration(
        resource_id=request.resource_id,
        uri="qemu:///system",
    )
    expected = cast("TrafficCaptureExecutor", object())
    monkeypatch.setattr(
        composition.local_composition,
        "build_capture_executor",
        lambda observed: expected if observed == configuration else None,
    )

    assert (
        composition.build_capture_executor(request, configuration.to_canonical_json()) is expected
    )

    mismatched = configuration.model_copy(update={"resource_id": uuid4()})
    with pytest.raises(CategorizedError) as excinfo:
        composition.build_capture_executor(request, mismatched.to_canonical_json())
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
