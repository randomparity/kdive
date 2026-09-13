"""Initialize a bundled S3 bucket before KDIVE workloads start (ADR-0647)."""

from __future__ import annotations

import time
from contextlib import suppress

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from kdive import config
from kdive.config.core_settings import S3_BUCKET, S3_ENDPOINT_URL, S3_REGION

_ATTEMPTS = 30
_DELAY_SECONDS = 1.0


def initialize_bucket() -> None:
    """Create the configured bucket and require enabled versioning, or raise."""
    client = boto3.client(
        "s3",
        endpoint_url=config.require(S3_ENDPOINT_URL),
        region_name=config.get(S3_REGION) or "us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    bucket = config.require(S3_BUCKET)
    last_error: Exception | None = None
    for _ in range(_ATTEMPTS):
        try:
            client.list_buckets()
            break
        except (BotoCoreError, ClientError, OSError) as exc:
            last_error = exc
            time.sleep(_DELAY_SECONDS)
    else:
        raise RuntimeError(f"S3 endpoint did not become ready: {last_error}")
    with suppress(client.exceptions.BucketAlreadyOwnedByYou):
        client.create_bucket(Bucket=bucket)
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    status = client.get_bucket_versioning(Bucket=bucket).get("Status")
    if status != "Enabled":
        raise RuntimeError(f"S3 bucket versioning is {status!r}, expected 'Enabled'")


if __name__ == "__main__":
    initialize_bucket()
