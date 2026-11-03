"""The vmcore download deadline is read from the SigV4 presigned URL alone (#3313)."""

from datetime import UTC, datetime

import pytest

from kdive.artifacts.vmcore_retention import signed_get_deadline
from kdive.domain.errors import CategorizedError, ErrorCategory

_BASE = "https://store.example/bucket/vmcore?X-Amz-Algorithm=AWS4-HMAC-SHA256"


def test_deadline_is_signing_time_plus_expiry() -> None:
    url = f"{_BASE}&X-Amz-Date=20261021T004119Z&X-Amz-Expires=900&X-Amz-Signature=ab"

    assert signed_get_deadline(url) == datetime(2026, 10, 21, 0, 56, 19, tzinfo=UTC)


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("AWSAccessKeyId=k&Expires=1792543339&Signature=s", id="sigv2"),
        pytest.param("X-Amz-Expires=900", id="missing-date"),
        pytest.param("X-Amz-Date=20261021T004119Z", id="missing-expires"),
        pytest.param(
            "X-Amz-Date=20261021T004119Z&X-Amz-Date=20261021T004119Z&X-Amz-Expires=900",
            id="duplicate-date",
        ),
        pytest.param(
            "X-Amz-Date=20261021T004119Z&X-Amz-Expires=900&X-Amz-Expires=60",
            id="duplicate-expires",
        ),
        pytest.param("X-Amz-Date=2026-10-21T00:41:19Z&X-Amz-Expires=900", id="malformed-date"),
        pytest.param("X-Amz-Date=20261021T004119Z&X-Amz-Expires=soon", id="malformed-expires"),
        pytest.param("X-Amz-Date=20261021T004119Z&X-Amz-Expires=0", id="zero-expires"),
        pytest.param("X-Amz-Date=20261021T004119Z&X-Amz-Expires=-5", id="negative-expires"),
        pytest.param("X-Amz-Date=99991231T235959Z&X-Amz-Expires=60", id="overflow"),
        pytest.param("X-Amz-Date=&X-Amz-Expires=900", id="blank-date"),
    ],
)
def test_unusable_signing_parameters_fail_closed(query: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        signed_get_deadline(f"https://store.example/bucket/vmcore?{query}")

    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(excinfo.value) == "vmcore download signing deadline could not be established"
