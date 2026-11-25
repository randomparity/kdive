from __future__ import annotations

from unittest.mock import Mock

import pytest
from botocore.errorfactory import ClientExceptionsFactory
from botocore.exceptions import BotoCoreError, ClientError
from botocore.session import get_session

from kdive.store import initialize_bucket


@pytest.fixture
def s3_client(monkeypatch: pytest.MonkeyPatch) -> Mock:
    client = Mock()
    client.exceptions = ClientExceptionsFactory().create_client_exceptions(
        get_session().get_service_model("s3")
    )
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://store:8333")
    monkeypatch.setenv("KDIVE_S3_BUCKET", "kdive-artifacts")
    monkeypatch.setattr(initialize_bucket.boto3, "client", lambda *_args, **_kwargs: client)
    return client


@pytest.fixture
def sleep(monkeypatch: pytest.MonkeyPatch) -> Mock:
    pause = Mock()
    monkeypatch.setattr(initialize_bucket.time, "sleep", pause)
    return pause


@pytest.fixture(
    params=[
        OSError("endpoint unavailable"),
        BotoCoreError(),
        ClientError(
            {"Error": {"Code": "ServiceUnavailable", "Message": "endpoint unavailable"}},
            "ListBuckets",
        ),
    ],
    ids=["os-error", "sdk-error", "service-error"],
)
def readiness_error(request: pytest.FixtureRequest) -> Exception:
    return request.param


def test_initialize_bucket_creates_and_enables(s3_client: Mock) -> None:
    initialize_bucket.initialize_bucket()

    s3_client.create_bucket.assert_called_once_with(Bucket="kdive-artifacts")
    s3_client.put_bucket_versioning.assert_called_once_with(
        Bucket="kdive-artifacts", VersioningConfiguration={"Status": "Enabled"}
    )
    s3_client.get_bucket_versioning.assert_called_once_with(Bucket="kdive-artifacts")


def test_initialize_bucket_retries_readiness(
    s3_client: Mock, sleep: Mock, readiness_error: Exception
) -> None:
    s3_client.list_buckets.side_effect = [readiness_error, {}]

    initialize_bucket.initialize_bucket()

    assert s3_client.list_buckets.call_count == 2
    sleep.assert_called_once_with(initialize_bucket._DELAY_SECONDS)
    s3_client.create_bucket.assert_called_once_with(Bucket="kdive-artifacts")


def test_initialize_bucket_refuses_after_bounded_readiness_failures(
    s3_client: Mock, sleep: Mock, readiness_error: Exception
) -> None:
    s3_client.list_buckets.side_effect = readiness_error

    with pytest.raises(RuntimeError, match="S3 endpoint did not become ready"):
        initialize_bucket.initialize_bucket()

    assert s3_client.list_buckets.call_count == initialize_bucket._ATTEMPTS
    assert sleep.call_count == initialize_bucket._ATTEMPTS
    s3_client.create_bucket.assert_not_called()
    s3_client.put_bucket_versioning.assert_not_called()


def test_initialize_bucket_enables_versioning_when_bucket_already_owned(s3_client: Mock) -> None:
    s3_client.create_bucket.side_effect = s3_client.exceptions.BucketAlreadyOwnedByYou(
        {"Error": {"Code": "BucketAlreadyOwnedByYou", "Message": "already owned"}},
        "CreateBucket",
    )

    initialize_bucket.initialize_bucket()

    s3_client.put_bucket_versioning.assert_called_once_with(
        Bucket="kdive-artifacts", VersioningConfiguration={"Status": "Enabled"}
    )
    s3_client.get_bucket_versioning.assert_called_once_with(Bucket="kdive-artifacts")


def test_initialize_bucket_propagates_other_creation_errors(s3_client: Mock) -> None:
    error = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "CreateBucket")
    s3_client.create_bucket.side_effect = error

    with pytest.raises(ClientError) as raised:
        initialize_bucket.initialize_bucket()

    assert raised.value is error
    s3_client.put_bucket_versioning.assert_not_called()


@pytest.mark.parametrize("status", [None, "Suspended", "Disabled"])
def test_initialize_bucket_requires_enabled_versioning(s3_client: Mock, status: str | None) -> None:
    s3_client.get_bucket_versioning.return_value = {} if status is None else {"Status": status}

    with pytest.raises(RuntimeError, match="S3 bucket versioning.*expected 'Enabled'"):
        initialize_bucket.initialize_bucket()


def test_initialize_bucket_propagates_versioning_enable_failure(s3_client: Mock) -> None:
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "PutBucketVersioning"
    )
    s3_client.put_bucket_versioning.side_effect = error

    with pytest.raises(ClientError) as raised:
        initialize_bucket.initialize_bucket()

    assert raised.value is error
    s3_client.get_bucket_versioning.assert_not_called()
