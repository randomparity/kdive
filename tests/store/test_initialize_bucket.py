from __future__ import annotations

from unittest.mock import Mock

from kdive.store import initialize_bucket


def test_initialize_bucket_creates_and_enables(monkeypatch) -> None:
    client = Mock()
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://store:8333")
    monkeypatch.setenv("KDIVE_S3_BUCKET", "kdive-artifacts")
    monkeypatch.setattr(initialize_bucket.boto3, "client", lambda *_args, **_kwargs: client)

    initialize_bucket.initialize_bucket()

    client.create_bucket.assert_called_once_with(Bucket="kdive-artifacts")
    client.put_bucket_versioning.assert_called_once_with(
        Bucket="kdive-artifacts", VersioningConfiguration={"Status": "Enabled"}
    )
