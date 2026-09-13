#!/usr/bin/env python3
"""Prove the ADR-0647 SeaweedFS image contract against one Docker platform."""

from __future__ import annotations

import argparse
import base64
import hashlib
import socket
import subprocess
import time
import urllib.request
from contextlib import suppress

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _client(endpoint: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="kdive-proof",
        aws_secret_access_key="kdive-proof-secret",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform", required=True)
    args = parser.parse_args()
    port = _port()
    container = subprocess.check_output(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--platform",
            args.platform,
            "--publish",
            f"127.0.0.1:{port}:8333",
            "--env",
            "AWS_ACCESS_KEY_ID=kdive-proof",
            "--env",
            "AWS_SECRET_ACCESS_KEY=kdive-proof-secret",
            "--env",
            "S3_BUCKET=kdive-proof",
            args.image,
        ],
        text=True,
    ).strip()
    try:
        endpoint = f"http://127.0.0.1:{port}"
        client = _client(endpoint)
        deadline = time.monotonic() + 90
        while True:
            try:
                client.list_buckets()
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise RuntimeError("SeaweedFS S3 endpoint did not become ready") from None
                time.sleep(1)
        bucket = "kdive-proof"
        try:
            client.create_bucket(Bucket=bucket)
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                raise
        client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
        assert client.get_bucket_versioning(Bucket=bucket)["Status"] == "Enabled"
        first = client.put_object(Bucket=bucket, Key="exact", Body=b"first")["VersionId"]
        second = client.put_object(Bucket=bucket, Key="exact", Body=b"second")["VersionId"]
        assert (
            client.get_object(Bucket=bucket, Key="exact", VersionId=first)["Body"].read()
            == b"first"
        )
        client.delete_object(Bucket=bucket, Key="exact", VersionId=second)
        upload = client.create_multipart_upload(Bucket=bucket, Key="multipart")["UploadId"]
        part = client.upload_part(
            Bucket=bucket, Key="multipart", UploadId=upload, PartNumber=1, Body=b"part"
        )
        client.complete_multipart_upload(
            Bucket=bucket,
            Key="multipart",
            UploadId=upload,
            MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": part["ETag"]}]},
        )
        assert client.get_object(Bucket=bucket, Key="multipart")["Body"].read() == b"part"
        client.put_object(Bucket=bucket, Key="presigned-get", Body=b"get")
        with urllib.request.urlopen(
            client.generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": "presigned-get"}
            ),
            timeout=20,
        ) as response:
            assert response.read() == b"get"
        payload = b"put"
        checksum = base64.b64encode(hashlib.sha256(payload).digest()).decode()
        url = client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": "presigned-put", "ChecksumSHA256": checksum},
        )
        request = urllib.request.Request(
            url, data=payload, method="PUT", headers={"x-amz-checksum-sha256": checksum}
        )
        with urllib.request.urlopen(request, timeout=20):
            pass
        assert client.get_object(Bucket=bucket, Key="presigned-put")["Body"].read() == payload
    finally:
        with suppress(subprocess.CalledProcessError):
            subprocess.run(["docker", "stop", container], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
