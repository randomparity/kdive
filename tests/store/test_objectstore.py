"""Behavior and edge tests for the object-store client (ADR-0017).

The MinIO-backed tests use the session ``minio_store`` fixture and gate on Docker
exactly as the db tests do; the pure tests (key validation, etag normalization,
``register_artifact_row``, env config) run without a container.
"""

from __future__ import annotations

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.models import Sensitivity
from kdive.store.objectstore import ObjectStore, _normalize_etag


def test_normalize_etag_strips_surrounding_quotes() -> None:
    assert _normalize_etag('"abc123"') == "abc123"
    assert _normalize_etag("abc123") == "abc123"


@pytest.mark.parametrize(
    ("tenant", "kind", "object_id", "name"),
    [
        ("", "vmcore", "oid", "core"),
        ("t", "vmcore", "oid", ""),
        ("t", "with/slash", "oid", "core"),
        ("t", "vmcore", "oid", "bad\nname"),
    ],
)
def test_put_artifact_rejects_invalid_key_component(
    tenant: str, kind: str, object_id: str, name: str
) -> None:
    store = ObjectStore(object(), "bucket")  # client never touched: validation precedes it
    with pytest.raises(CategorizedError) as excinfo:
        store.put_artifact(
            tenant,
            kind,
            object_id,
            name,
            data=b"x",
            sensitivity=Sensitivity.REDACTED,
            retention_class="vmcore",
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
