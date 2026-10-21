"""Presigned-URL TTL knobs stay within SigV4's 7-day expiry ceiling (#3313)."""

import pytest

import kdive.config as config
from kdive.config.core_settings import ARTIFACT_DOWNLOAD_TTL_SECONDS
from kdive.domain.errors import CategorizedError, ErrorCategory

_SIGV4_MAX_SECONDS = 7 * 24 * 60 * 60


def _load(ttl: str) -> None:
    config.load(
        {
            "KDIVE_DATABASE_URL": "postgresql://db/kdive",
            "KDIVE_S3_ENDPOINT_URL": "http://minio:9000",
            "KDIVE_S3_BUCKET": "kdive",
            ARTIFACT_DOWNLOAD_TTL_SECONDS.name: ttl,
        }
    )


def test_download_ttl_accepts_the_sigv4_ceiling() -> None:
    _load(str(_SIGV4_MAX_SECONDS))

    assert config.require(ARTIFACT_DOWNLOAD_TTL_SECONDS) == _SIGV4_MAX_SECONDS


@pytest.mark.parametrize("ttl", [str(_SIGV4_MAX_SECONDS + 1), "-1"])
def test_download_ttl_outside_the_sigv4_window_fails_server_validation(ttl: str) -> None:
    _load(ttl)

    with pytest.raises(CategorizedError) as excinfo:
        config.require(ARTIFACT_DOWNLOAD_TTL_SECONDS)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert ARTIFACT_DOWNLOAD_TTL_SECONDS.name in str(excinfo.value)
    with pytest.raises(CategorizedError):
        config.validate("server")
