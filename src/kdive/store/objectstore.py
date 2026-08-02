"""S3-compatible artifact storage for kdive (ADR-0013, ADR-0017).

Writes bulk artifacts under the key scheme ``{tenant}/{kind}/{object_id}/{name}``
with their sensitivity/retention recorded as object metadata, and reads them back
with an etag-consistency check. The client is synchronous (boto3); async callers
offload via ``asyncio.to_thread``. It is policy-neutral — it never decides whether a
fetched object may reach a response (the handler's redaction gate does, using the
returned sensitivity).
"""

from __future__ import annotations

import io
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Any, cast

import boto3
from botocore.exceptions import BotoCoreError, ClientError

import kdive.config as config
from kdive.artifacts import storage as artifact_types
from kdive.artifacts.storage import (
    artifact_key as artifact_key,
)
from kdive.artifacts.storage import (
    chunk_key as chunk_key,
)
from kdive.artifacts.storage import (
    owner_prefix as owner_prefix,
)
from kdive.config.core_settings import S3_BUCKET, S3_ENDPOINT_URL, S3_REGION
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory

# boto3 ships no inline types and boto3-stubs is not a dependency; alias the S3
# client type to Any at this single site rather than add a stubs package.
S3Client = Any

_DEFAULT_REGION = "us-east-1"

# The image catalog's object namespace; the one prefix ``list_image_objects`` may list.
_IMAGE_PREFIX = "images/"

#: How many keys one ``list_objects_v2`` page carries. It is the S3 maximum and boto3's own
#: default, stated here rather than inherited because it is the bound a caller streaming
#: :meth:`ObjectStore.iter_prefix_pages_with_mtime` relies on (ADR-0498): the page is what bounds
#: that caller's peak memory and its per-statement parameter width, so the number has to be a
#: property of this module rather than of whatever boto3 defaults to next.
_LIST_PAGE_SIZE = 1000

# A missing object (404) and an etag mismatch (412) are the one stale_handle case.
_STALE_STATUSES = frozenset({404, 412})


def _normalize_etag(raw: str) -> str:
    return raw.strip('"')


def _infrastructure_error(op: str, key: str, err: BotoCoreError | ClientError) -> CategorizedError:
    """Map an S3 client or transport error to a typed infrastructure failure.

    ``ClientError`` carries an S3 error code in its ``response``; a ``BotoCoreError``
    (connection refused, DNS failure, connect/read timeout) has no response, so its
    exception class name stands in for the code.
    """
    if isinstance(err, ClientError):
        code = err.response.get("Error", {}).get("Code", "unknown")
    else:
        code = type(err).__name__
    return CategorizedError(
        f"object-store {op} for {key!r} failed: {code}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"key": key, "s3_error_code": code},
    )


def _local_stream_error(key: str, path: str, err: OSError) -> CategorizedError:
    return CategorizedError(
        f"object-store put_stream for {key!r} could not read {path!r}: {err.strerror}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details={"op": "put_stream", "key": key, "path": path},
    )


@dataclass(frozen=True, slots=True)
class _StoreReply:
    """A **successful** store response, read through a contract that says what it must carry.

    Every field this module reads out of a reply is one boto3 parsed against the S3 service model,
    so on a store that implements the API it is present and correctly typed. A store that omits one
    — or returns it as some other type — must not surface as a bare ``KeyError`` or
    ``AttributeError``, because that is not the ``CategorizedError`` this module's callers handle.
    The ADR-0455 upload orphan sweep is the case that makes it matter: its per-key and per-root
    fault handlers catch ``CategorizedError`` alone and *deliberately* let anything else abort the
    whole pass, so that a real bug in the sweep stays loud (#1685). Converting a malformed reply
    here — at the boundary that produced it, and to the category the callers already skip and count
    — is what lets one unreadable reply cost one key instead of the pass, without widening any
    caller's ``except`` to swallow its own bugs.

    The policy is decided once for every field rather than per field, so no reply read in this
    module can be the inconsistent one. An *optional* field gets the same treatment when it is
    present (:meth:`optional`): absent is normal and yields the caller's default, but a present
    value of the wrong type is as malformed as a missing required one, and in the case of
    ``Metadata`` it is the shape that actually raises — a non-mapping there makes the sensitivity
    read a ``TypeError`` that no ``except`` on this path catches.

    It applies to the reads only; the write and multipart calls subscript their replies too, but
    each of those is one request's own result with no per-item fault handler above it, so a
    malformed reply there already fails exactly the one operation it belongs to.
    """

    op: str
    bucket: str
    #: The request's subject — an object key, or the prefix for a listing entry, which has no key
    #: of its own to name in a failure message until ``Key`` is itself read.
    subject: str
    body: Mapping[str, Any]

    def required[T](self, field: str, expected: type[T]) -> T:
        """Return ``field``'s value, or raise if the store omitted it or gave it another type.

        Args:
            field: The response field name, as the S3 API spells it.
            expected: The type the API promises. Checked rather than coerced: a coercion would
                turn a nonsense value into a plausible one, and the point here is to name the
                field that is wrong.

        Returns:
            The field's value, narrowed to ``expected``.

        Raises:
            CategorizedError: the field is absent or is not an ``expected``
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            value = self.body[field]
        except KeyError as err:
            raise self._malformed(field, f"omitted the required {field!r}") from err
        if not isinstance(value, expected):
            raise self._malformed(
                field, f"returned {field!r} as {type(value).__name__}, not {expected.__name__}"
            )
        return value

    def optional[T](self, field: str, expected: type[T]) -> T | None:
        """Return ``field``'s value, ``None`` if the store omitted it, or raise if it is ill-typed.

        The absent case is normal — an object written before checksums were requested carries no
        ``ChecksumSHA256``, and one with no user metadata may carry no ``Metadata`` — so only a
        *present* value is held to the contract.

        Args:
            field: The response field name, as the S3 API spells it.
            expected: The type the API promises when the field is present.

        Returns:
            The field's value, or ``None`` when the store omitted it.

        Raises:
            CategorizedError: the field is present and is not an ``expected``
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        if field not in self.body:
            return None
        return self.required(field, expected)

    def required_nonempty_string(self, field: str) -> str:
        """Return a required nonempty string field from a successful store response."""
        value = self.required(field, str)
        if not value:
            raise self._malformed(field, f"returned {field!r} as an empty string")
        return value

    def _malformed(self, field: str, problem: str) -> CategorizedError:
        return CategorizedError(
            f"object-store {self.op} for {self.subject!r} in bucket {self.bucket!r} {problem}; "
            f"the endpoint is not returning S3-compatible {self.op} replies",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={
                "op": self.op,
                "bucket": self.bucket,
                "key": self.subject,
                "field": field,
            },
        )


class _StreamingBodyReader(io.RawIOBase):
    """A blocking ``RawIOBase`` over a boto ``StreamingBody`` that maps transport faults to the
    same typed infrastructure error the buffered read raises (ADR-0400).

    ``readinto`` returns the number of bytes copied — a short read (fewer bytes than requested)
    is *not* end-of-stream — and returns ``0`` only when the wrapped read returns ``b""`` (true
    EOF), so a partial chunk is never mistaken for a truncated archive. A mid-stream
    ``BotoCoreError``/``ClientError`` becomes a ``CategorizedError`` (a non-``OSError``) that
    propagates cleanly out through ``tarfile``'s stream/io buffering to the caller.
    """

    def __init__(self, body: Any, key: str) -> None:
        self._body = body
        self._key = key

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any, /) -> int:
        try:
            chunk = self._body.read(len(buffer))
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("get_object", self._key, err) from err
        count = len(chunk)
        buffer[:count] = chunk
        return count


class ObjectStore:
    """A synchronous S3-compatible artifact store bound to one bucket."""

    def __init__(self, client: S3Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def put_artifact(
        self, request: artifact_types.ArtifactWriteRequest
    ) -> artifact_types.StoredArtifact:
        """Write ``data`` under the key scheme; return its key, etag, class, and VersionId.

        The object carries the request's ``sensitivity`` and ``retention_class`` as user metadata.
        Async callers must offload this call via ``asyncio.to_thread``.

        Raises:
            CategorizedError: a key component is invalid
                (:attr:`ErrorCategory.CONFIGURATION_ERROR`) or the put fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        key = request.key()
        metadata: dict[str, str] = {
            "sensitivity": request.sensitivity.value,
            "retention-class": request.retention_class,
        }
        if request.content_encoding is not None:
            metadata["content-encoding"] = request.content_encoding
        put_kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": request.data,
            "Metadata": metadata,
        }
        if request.sha256_b64 is not None:
            put_kwargs["ChecksumSHA256"] = request.sha256_b64
        try:
            resp = self._client.put_object(**put_kwargs)
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("put_object", key, err) from err
        reply = _StoreReply("put_object", self._bucket, key, resp)
        return artifact_types.StoredArtifact(
            key,
            _normalize_etag(reply.required_nonempty_string("ETag")),
            request.sensitivity,
            request.retention_class,
            reply.required_nonempty_string("VersionId"),
        )

    def put_stream(
        self, request: artifact_types.ArtifactStreamRequest
    ) -> artifact_types.StoredArtifact:
        """Write ``request.path``'s bytes under the key scheme, streaming from disk.

        Used by callers holding a large artifact on local disk (the spooled host_dump core,
        ADR-0094): the open file handle is the PUT body, so boto3 streams it in chunks rather
        than the whole object being read into RAM. The returned value carries the observed
        immutable VersionId. The object carries the request's
        ``sensitivity``/``retention_class`` as user metadata, matching :meth:`put_artifact`,
        and ``request.sha256_b64`` is sent as ``ChecksumSHA256`` so S3 rejects the PUT if the
        streamed body does not hash to it (the end-to-end integrity binding) and a later
        ``head`` returns it for the caller's post-put verification. Async callers must offload
        this call via ``asyncio.to_thread``.

        Raises:
            CategorizedError: a key component is invalid
                (:attr:`ErrorCategory.CONFIGURATION_ERROR`) or the put fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        key = request.key()
        try:
            with request.path.open("rb") as body:
                resp = self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=body,
                    ChecksumSHA256=request.sha256_b64,
                    Metadata={
                        "sensitivity": request.sensitivity.value,
                        "retention-class": request.retention_class,
                    },
                )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("put_object", key, err) from err
        except OSError as err:
            raise _local_stream_error(key, str(request.path), err) from err
        reply = _StoreReply("put_object", self._bucket, key, resp)
        return artifact_types.StoredArtifact(
            key,
            _normalize_etag(reply.required_nonempty_string("ETag")),
            request.sensitivity,
            request.retention_class,
            reply.required_nonempty_string("VersionId"),
        )

    def get_artifact(
        self, key: str, etag: str | None, *, version_id: str | None = None
    ) -> artifact_types.FetchedArtifact:
        """Fetch the object at ``key``, optionally guarded by an ``If-Match`` on ``etag``.

        When ``etag`` is a bare value (from :class:`StoredArtifact`), the GET is
        conditional — the client-serving path's stale-handle check (ADR-0017 §3): a 412
        mismatch raises ``STALE_HANDLE``. When ``etag`` is ``None`` the GET is
        unconditional, for callers that hold a key the system itself produced and no
        client handle to validate (the install staging fetch and the symbolization
        fetches, ADR-0054); a 404 still raises ``STALE_HANDLE``. Async callers must
        offload via ``asyncio.to_thread``.

        Raises:
            CategorizedError: the object is missing or (with an ``etag``) no longer
                matches (:attr:`ErrorCategory.STALE_HANDLE`); the object lacks
                interpretable sensitivity metadata, or the get otherwise fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        resp, sensitivity, retention_class = self._open_get(key, etag, version_id=version_id)
        try:
            data = resp["Body"].read()
        except (BotoCoreError, ClientError) as err:
            # The download streams here, after the headers; a mid-stream timeout or
            # dropped connection raises a BotoCoreError that must stay typed too.
            raise _infrastructure_error("get_object", key, err) from err
        return artifact_types.FetchedArtifact(data, sensitivity, retention_class)

    def _open_get(
        self, key: str, etag: str | None, *, version_id: str | None = None
    ) -> tuple[Any, Sensitivity, str]:
        """Issue the GET and parse the sensitivity metadata, shared by the buffered and
        streaming reads so their error taxonomy cannot drift (ADR-0400, refining ADR-0054).

        Returns the raw ``get_object`` response, the object's ``Sensitivity``, and its
        retention class. The body is left unread so the caller chooses whether to buffer it
        (:meth:`get_artifact`) or stream it (:meth:`get_artifact_stream`).

        Raises:
            CategorizedError: a 404/412 maps to ``STALE_HANDLE``; any other client/transport
                error and absent/invalid sensitivity metadata map to ``INFRASTRUCTURE_FAILURE``.
        """
        get_kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if etag is not None:
            get_kwargs["IfMatch"] = f'"{etag}"'
        if version_id is not None:
            get_kwargs["VersionId"] = version_id
        try:
            resp = self._client.get_object(**get_kwargs)
        except ClientError as err:
            status = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status in _STALE_STATUSES:
                raise CategorizedError(
                    f"artifact {key!r} is gone or its etag no longer matches",
                    category=ErrorCategory.STALE_HANDLE,
                    details={"key": key, "http_status": status},
                ) from err
            raise _infrastructure_error("get_object", key, err) from err
        except BotoCoreError as err:
            raise _infrastructure_error("get_object", key, err) from err
        metadata = resp["Metadata"]
        try:
            sensitivity = Sensitivity(metadata["sensitivity"])
            retention_class = metadata["retention-class"]
        except (KeyError, ValueError) as err:
            raise CategorizedError(
                f"artifact {key!r} has absent or invalid sensitivity metadata",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"key": key},
            ) from err
        return resp, sensitivity, retention_class

    @contextmanager
    def get_artifact_stream(
        self, key: str, etag: str | None, *, version_id: str | None = None
    ) -> Iterator[artifact_types.StreamedArtifact]:
        """Yield a streaming reader over the object at ``key`` plus its sensitivity class.

        Same GET/error/metadata contract as :meth:`get_artifact` (they share :meth:`_open_get`),
        but the body is not materialized: the yielded ``reader`` streams it and maps a mid-stream
        transport fault to ``INFRASTRUCTURE_FAILURE``. The body is closed on ``with``-exit,
        aborting a partially-read download. Async callers offload the whole ``with`` block via
        ``asyncio.to_thread`` (ADR-0400).

        Raises:
            CategorizedError: the object is missing or (with an ``etag``) no longer matches
                (``STALE_HANDLE``); the object lacks interpretable sensitivity metadata, the get
                otherwise fails, or the body read fails mid-stream (``INFRASTRUCTURE_FAILURE``).
        """
        resp, sensitivity, retention_class = self._open_get(key, etag, version_id=version_id)
        body = resp["Body"]
        try:
            # RawIOBase provides the read interface tarfile uses but is not a nominal
            # IO[bytes] in typeshed; cast at this one construction site.
            reader = cast(IO[bytes], _StreamingBodyReader(body, key))
            yield artifact_types.StreamedArtifact(reader, sensitivity, retention_class)
        finally:
            body.close()

    def ping(self) -> None:
        """Probe the bucket's reachability with a ``HEAD`` (ADR-0090 §5 readiness check).

        Raises:
            CategorizedError: the bucket is unreachable or absent
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`), or its versioning is unsuitable
                for immutable deletion (:attr:`ErrorCategory.CONFIGURATION_ERROR`). Async callers
                offload via ``asyncio.to_thread``.
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("head_bucket", self._bucket, err) from err
        self.validate_versioning()

    def validate_versioning(self) -> None:
        """Require a bucket that supports unattended immutable-version deletion."""
        try:
            response = self._client.get_bucket_versioning(Bucket=self._bucket)
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("get_bucket_versioning", self._bucket, err) from err

        reply = _StoreReply("get_bucket_versioning", self._bucket, self._bucket, response)
        status = reply.optional("Status", str)
        if status != "Enabled":
            observed = status if status is not None else "missing"
            raise CategorizedError(
                f"object-store bucket {self._bucket!r} reports versioning {observed!r}; "
                "KDIVE requires Status='Enabled'. Enable versioning on a dedicated compatible "
                "bucket before starting KDIVE.",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "bucket": self._bucket,
                    "observed_status": observed,
                    "required_status": "Enabled",
                },
            )
        mfa_delete = reply.optional("MFADelete", str)
        if mfa_delete == "Enabled":
            raise CategorizedError(
                f"object-store bucket {self._bucket!r} has MFA Delete enabled; KDIVE requires "
                "MFA Delete disabled because unattended version deletion cannot provide an MFA "
                "proof. Use a dedicated bucket without MFA Delete.",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"bucket": self._bucket, "observed_mfa_delete": mfa_delete},
            )
        if mfa_delete not in {None, "Disabled"}:
            raise reply._malformed(
                "MFADelete", f"returned unsupported MFADelete state {mfa_delete!r}"
            )

    def head(self, key: str, *, version_id: str | None = None) -> artifact_types.HeadResult | None:
        """Return the object's size/checksum/etag/mtime/sensitivity, or ``None`` if it is absent.

        Requests ``ChecksumMode="ENABLED"`` so a checksum written at PUT is returned. The
        ``sensitivity`` is read from object metadata (``None`` when absent or
        uninterpretable), so a caller can gate on the object's own class without fetching
        the body. ``last_modified`` makes this the single-object stat the ADR-0455 orphan
        sweep re-reads a candidate's mtime with, in one round trip whatever else sits under
        that key's prefix (#1575). The result also includes the observed immutable VersionId,
        including the legacy literal ``"null"``.

        Raises:
            CategorizedError: any non-404 store error, or a reply that omits or mistypes one of the
                fields ``HeadObject`` is required to return
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`; see :class:`_StoreReply`).
        """
        try:
            request = {"Bucket": self._bucket, "Key": key, "ChecksumMode": "ENABLED"}
            if version_id is not None:
                request["VersionId"] = version_id
            resp = self._client.head_object(**request)
        except ClientError as err:
            status = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                return None
            raise _infrastructure_error("head_object", key, err) from err
        except BotoCoreError as err:
            raise _infrastructure_error("head_object", key, err) from err
        reply = _StoreReply("head_object", self._bucket, key, resp)
        # An absent ``Metadata`` is normal, but a present non-mapping one would make the
        # sensitivity read below a ``TypeError`` that neither this ``except`` nor any caller's
        # catches — the same escape as a missing required field, by way of an optional one.
        metadata: Mapping[str, Any] = reply.optional("Metadata", dict) or {}
        try:
            sensitivity = Sensitivity(metadata["sensitivity"])
        except (KeyError, ValueError) as _exc:
            sensitivity = None
        return artifact_types.HeadResult(
            size_bytes=reply.required("ContentLength", int),
            checksum_sha256=reply.optional("ChecksumSHA256", str),
            etag=_normalize_etag(reply.required("ETag", str)),
            last_modified=reply.required("LastModified", datetime),
            version_id=reply.required_nonempty_string("VersionId"),
            sensitivity=sensitivity,
            content_encoding=metadata.get("content-encoding"),
        )

    def get_range(
        self, key: str, *, start: int, length: int, version_id: str | None = None
    ) -> bytes:
        """Return ``length`` bytes of ``key`` starting at ``start`` (an HTTP ranged GET).

        Raises:
            CategorizedError: the ranged read fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        end = start + length - 1
        try:
            request = {"Bucket": self._bucket, "Key": key, "Range": f"bytes={start}-{end}"}
            if version_id is not None:
                request["VersionId"] = version_id
            resp = self._client.get_object(**request)
            return resp["Body"].read()
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("get_range", key, err) from err

    def presign_put(
        self, request: artifact_types.PresignPutRequest
    ) -> artifact_types.PresignedUpload:
        """Mint a presigned PUT that signs the checksum + object metadata into the URL.

        The agent must send the returned ``required_headers`` (the signed
        ``x-amz-checksum-sha256`` and ``x-amz-meta-*`` metadata) **and nothing else**: the URL
        is SigV4-signed over exactly this header set, so any extra header the client adds — most
        often an implicit ``Content-Type`` (e.g. ``curl --data-binary``) — changes the signed
        request and S3 rejects the PUT with ``403 SignatureDoesNotMatch``. S3 also rejects a PUT
        whose body checksum disagrees with the signed ``x-amz-checksum-sha256`` value, and the
        metadata lands on the object so the later install fetch (`get_artifact`) reads its
        sensitivity. This mints a single PUT (the 5 GiB single-object ceiling on real S3);
        ``size_bytes`` is recorded by the caller's manifest and capped to that ceiling before
        this is called. The `live_stack` test asserts the **checksum** binding, not the upload
        length (ADR-0048 §2).

        Raises:
            CategorizedError: presigning fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        metadata = {
            "sensitivity": request.sensitivity.value,
            "retention-class": request.retention_class,
        }
        try:
            url = self._client.generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": request.key,
                    "ChecksumSHA256": request.sha256,
                    "Metadata": metadata,
                },
                ExpiresIn=request.expires_in,
                HttpMethod="PUT",
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("presign_put", request.key, err) from err
        headers = {
            "x-amz-checksum-sha256": request.sha256,
            "x-amz-meta-sensitivity": request.sensitivity.value,
            "x-amz-meta-retention-class": request.retention_class,
        }
        return artifact_types.PresignedUpload(url=url, required_headers=headers)

    def presign_get(self, key: str, *, expires_in: int, version_id: str | None = None) -> str:
        """Mint a time-boxed presigned GET URL for one object (ADR-0076, ADR-0078).

        The URL is a bearer capability scoped to ``key`` alone, expiring after
        ``expires_in`` seconds. Callers that hand it across a trust boundary must
        register it in the redaction registry before it leaves the worker
        (ADR-0078 §2 — the in-target seam).

        Raises:
            CategorizedError: ``expires_in`` is not positive
                (:attr:`ErrorCategory.CONFIGURATION_ERROR`), or presigning fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        if expires_in <= 0:
            raise CategorizedError(
                f"presign_get for {key!r} needs a positive expiry, got {expires_in}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"key": key},
            )
        try:
            params = {"Bucket": self._bucket, "Key": key}
            if version_id is not None:
                params["VersionId"] = version_id
            return self._client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=expires_in,
                HttpMethod="GET",
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("presign_get", key, err) from err

    def create_multipart_upload(
        self, key: str, *, sensitivity: Sensitivity, retention_class: str
    ) -> str:
        """Initiate a multipart upload for ``key``, setting object metadata at create time.

        Metadata cannot be attached at completion, so the sensitivity/retention-class are set
        here and ride onto the reassembled object (ADR-0104 §4). No checksum algorithm is set,
        so the final object carries an ETag but no whole-object checksum. Returns the upload id.

        Raises:
            CategorizedError: the call fails (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            resp = self._client.create_multipart_upload(
                Bucket=self._bucket,
                Key=key,
                Metadata={"sensitivity": sensitivity.value, "retention-class": retention_class},
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("create_multipart_upload", key, err) from err
        return resp["UploadId"]

    def upload_part_copy(
        self,
        key: str,
        upload_id: str,
        *,
        part_number: int,
        source_key: str,
        source_version_id: str,
    ) -> str:
        """Copy ``source_key`` into part ``part_number`` of ``key``'s multipart upload.

        A server-side copy — no bytes transit the process. Returns the part ETag.

        Raises:
            CategorizedError: the copy fails (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            resp = self._client.upload_part_copy(
                Bucket=self._bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=part_number,
                CopySource={
                    "Bucket": self._bucket,
                    "Key": source_key,
                    "VersionId": source_version_id,
                },
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("upload_part_copy", key, err) from err
        return _normalize_etag(resp["CopyPartResult"]["ETag"])

    def complete_multipart_upload(
        self, key: str, upload_id: str, parts: Sequence[tuple[int, str]]
    ) -> artifact_types.MultipartCompletion:
        """Complete ``key``'s multipart upload with the ordered ``(part_number, etag)`` list.

        Returns the final object ETag (a multipart ``-N`` form).

        Raises:
            CategorizedError: completion fails (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        multipart = {"Parts": [{"PartNumber": n, "ETag": etag} for n, etag in parts]}
        try:
            resp = self._client.complete_multipart_upload(
                Bucket=self._bucket, Key=key, UploadId=upload_id, MultipartUpload=multipart
            )
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("complete_multipart_upload", key, err) from err
        reply = _StoreReply("complete_multipart_upload", self._bucket, key, resp)
        return artifact_types.MultipartCompletion(
            _normalize_etag(reply.required_nonempty_string("ETag")),
            reply.required_nonempty_string("VersionId"),
        )

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        """Abort ``key``'s multipart upload (best-effort cleanup of a failed reassembly).

        Raises:
            CategorizedError: the abort fails (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        try:
            self._client.abort_multipart_upload(Bucket=self._bucket, Key=key, UploadId=upload_id)
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("abort_multipart_upload", key, err) from err

    def list_version_page(
        self,
        prefix: str,
        *,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
        max_keys: int = _LIST_PAGE_SIZE,
    ) -> artifact_types.VersionPage:
        """Return exactly one bounded page of data versions and delete markers under ``prefix``.

        ``max_keys`` is a count of returned version entries or markers, bounded inclusively from
        1 through 1,000. A continuation names both S3 markers when advancing within a key; callers
        resuming after a key's complete history pass only ``key_marker``. A malformed or
        non-advancing truncated response is an infrastructure failure because a caller cannot
        safely guess whether it skipped or repeated immutable identities.
        """
        if type(max_keys) is not int or not 1 <= max_keys <= _LIST_PAGE_SIZE:
            raise ValueError(
                f"max_keys must be an integer from 1 to {_LIST_PAGE_SIZE}, got {max_keys!r}"
            )
        request: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": prefix,
            "MaxKeys": max_keys,
        }
        if key_marker is not None:
            request["KeyMarker"] = key_marker
        if version_id_marker is not None:
            request["VersionIdMarker"] = version_id_marker
        try:
            response = self._client.list_object_versions(**request)
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("list_object_versions", prefix, err) from err
        if not isinstance(response, Mapping):
            malformed = _StoreReply("list_object_versions", self._bucket, prefix, {})
            raise malformed._malformed(
                "response", f"returned a {type(response).__name__}, not an object"
            )

        reply = _StoreReply("list_object_versions", self._bucket, prefix, response)
        versions = self._version_entries(reply, "Versions", is_delete_marker=False)
        markers = self._version_entries(reply, "DeleteMarkers", is_delete_marker=True)
        is_truncated = reply.required("IsTruncated", bool)
        if not is_truncated:
            return artifact_types.VersionPage(
                entries=tuple(sorted([*versions, *markers], key=self._version_sort_key)),
                is_truncated=False,
                next_key_marker=None,
                next_version_id_marker=None,
            )

        next_key_marker = reply.required_nonempty_string("NextKeyMarker")
        next_version_id_marker = reply.required_nonempty_string("NextVersionIdMarker")
        if (next_key_marker, next_version_id_marker) == (key_marker, version_id_marker):
            raise reply._malformed("NextKeyMarker", "returned a non-advancing truncated page")
        return artifact_types.VersionPage(
            entries=tuple(sorted([*versions, *markers], key=self._version_sort_key)),
            is_truncated=True,
            next_key_marker=next_key_marker,
            next_version_id_marker=next_version_id_marker,
        )

    def _version_entries(
        self, reply: _StoreReply, field: str, *, is_delete_marker: bool
    ) -> list[artifact_types.ObjectVersion]:
        """Parse one version-list collection through the same strict store boundary as HEAD."""
        raw_entries = reply.optional(field, list) or []
        entries: list[artifact_types.ObjectVersion] = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, Mapping):
                raise reply._malformed(
                    field, f"returned a non-object entry of type {type(raw_entry).__name__}"
                )
            entry = _StoreReply("list_object_versions", self._bucket, reply.subject, raw_entry)
            etag = None
            if not is_delete_marker:
                etag = _normalize_etag(entry.required_nonempty_string("ETag"))
            entries.append(
                artifact_types.ObjectVersion(
                    key=entry.required_nonempty_string("Key"),
                    version_id=entry.required_nonempty_string("VersionId"),
                    last_modified=entry.required("LastModified", datetime),
                    etag=etag,
                    is_latest=entry.required("IsLatest", bool),
                    is_delete_marker=is_delete_marker,
                )
            )
        return entries

    @staticmethod
    def _version_sort_key(entry: artifact_types.ObjectVersion) -> tuple[str, datetime, str]:
        return entry.key, entry.last_modified, entry.version_id

    def iter_prefix_version_pages(
        self,
        prefix: str,
        *,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
    ) -> Iterator[artifact_types.VersionPage]:
        """Yield ``prefix`` version pages lazily, resuming only with validated markers."""
        while True:
            page = self.list_version_page(
                prefix, key_marker=key_marker, version_id_marker=version_id_marker
            )
            yield page
            if not page.is_truncated:
                return
            key_marker = page.next_key_marker
            version_id_marker = page.next_version_id_marker

    def capture_exact_versions(self, key: str, limit: int) -> artifact_types.VersionBatch:
        """Capture at most ``limit`` exact-key identities and whether their history is complete."""
        if type(limit) is not int or limit < 1:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")

        targets: list[artifact_types.ObjectVersion] = []
        for page in self.iter_prefix_version_pages(key):
            exact_entries = [entry for entry in page.entries if entry.key == key]
            remaining = limit - len(targets)
            history_complete = not (
                len(exact_entries) > remaining
                or (page.is_truncated and page.next_key_marker == key)
            )
            capture_order = exact_entries
            if not history_complete:
                capture_order = [entry for entry in exact_entries if entry.is_latest]
                capture_order.extend(entry for entry in exact_entries if not entry.is_latest)
            targets.extend(capture_order[:remaining])
            if len(targets) == limit:
                return artifact_types.VersionBatch(key, tuple(targets), history_complete)
            if not page.is_truncated or page.next_key_marker != key:
                return artifact_types.VersionBatch(key, tuple(targets), True)
        raise AssertionError("version iterator must return or yield a page")

    def delete_version(self, key: str, version_id: str) -> None:
        """Permanently delete exactly one observed S3 version identity, including ``"null"``."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key, VersionId=version_id)
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("delete_object", key, err) from err

    def delete_batch(self, batch: artifact_types.VersionBatch) -> bool:
        """Delete a bounded retired-key batch, retaining latest until history completes."""
        if any(target.key != batch.key for target in batch.targets):
            raise ValueError("a version batch may contain only its declared key")
        latest = [target for target in batch.targets if target.is_latest]
        if len(latest) > 1:
            raise ValueError("a version batch may contain at most one latest entry")
        for target in batch.targets:
            if not target.is_latest:
                self.delete_version(target.key, target.version_id)
        if not batch.history_complete:
            return False
        if latest:
            target = latest[0]
            self.delete_version(target.key, target.version_id)
        return True

    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        """Capture and delete one bounded retired-key batch without a key-only delete."""
        return self.delete_batch(self.capture_exact_versions(key, limit))

    def list_prefix(self, prefix: str) -> list[str]:
        """Return every object key under ``prefix`` (paginated), or ``[]``.

        Raises:
            CategorizedError: the listing fails, or an entry omits its ``Key``
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`; see :class:`_StoreReply`).
        """
        keys: list[str] = []
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    reply = _StoreReply("list_objects_v2", self._bucket, prefix, obj)
                    keys.append(reply.required("Key", str))
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("list_objects_v2", prefix, err) from err
        return keys

    def iter_prefix_pages_with_mtime(
        self, prefix: str
    ) -> Iterator[list[artifact_types.ObjectListing]]:
        """Yield ``prefix``'s objects and their mtimes, one ``list_objects_v2`` page at a time.

        This is the streaming primitive; :meth:`list_prefix_with_mtime` is what flattens it. It
        exists because a caller
        sweeping an unbounded prefix must not hold the whole listing: `local/runs/` accumulates a
        vmcore per crashing run, a pcap per capture, and every chunked upload's parts for the life
        of the deployment, and the upload orphan sweep walks it every 30 seconds (ADR-0498). A page
        bounds that caller's peak memory *and* the width of any array parameter it derives from a
        page, which is the half a `LIMIT` could not have bounded.

        Pages arrive in store order — S3 lists lexicographically by key and boto3's paginator
        preserves that — so a caller acting page by page acts in the same order it would have acting
        on the flattened list. Each page is one round trip, so a fault surfaces from the iterator
        mid-listing rather than from the call: a caller that has acted on earlier pages keeps those
        effects, which is why the sweep counts a mid-listing fault as a partial root.

        An empty prefix still yields one empty page, mirroring ``list_objects_v2``'s own reply for a
        prefix that matches nothing; a caller counting pages must not read that as no request made.

        Raises:
            CategorizedError: the listing fails, or an entry omits its ``Key`` or ``LastModified``,
                raised from the iterator at the page that failed
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`; see :class:`_StoreReply`).
        """
        try:
            paginator = self._client.get_paginator("list_objects_v2")
            pages = paginator.paginate(
                Bucket=self._bucket,
                Prefix=prefix,
                PaginationConfig={"PageSize": _LIST_PAGE_SIZE},
            )
            for page in pages:
                yield [self._listed_object(obj, prefix) for obj in page.get("Contents", [])]
        except (BotoCoreError, ClientError) as err:
            raise _infrastructure_error("list_objects_v2", prefix, err) from err

    def _listed_object(self, obj: Mapping[str, Any], prefix: str) -> artifact_types.ObjectListing:
        """Read one ``list_objects_v2`` entry, or raise if the store's entry is malformed.

        The same boundary contract :meth:`head` applies to its reply, applied to the sweep's *other*
        read: this iterator is the orphan sweep's per-root listing, and ``_next_page_or_fault``
        catches ``CategorizedError`` alone. A bare ``KeyError`` out of an entry would escape it and
        end the pass — worse than the ``head`` case, because it would also leave the sibling root
        unswept (#1685).
        """
        reply = _StoreReply("list_objects_v2", self._bucket, prefix, obj)
        return artifact_types.ObjectListing(
            key=reply.required("Key", str),
            last_modified=reply.required("LastModified", datetime),
        )

    def list_prefix_with_mtime(self, prefix: str) -> list[artifact_types.ObjectListing]:
        """Return every object under ``prefix`` with its store mtime (paginated), or ``[]``.

        The mtime-bearing twin of :meth:`list_prefix`, and the flattening delegate over
        :meth:`iter_prefix_pages_with_mtime` so the pagination loop and its error mapping exist
        once (ADR-0455 §7's rule, now applied to the paged primitive). It backs the leaked-image
        sweep (ADR-0092), whose prefix is bounded by the image catalog and which classifies per
        object anyway; a caller over an unbounded prefix wants the iterator instead.

        Raises:
            CategorizedError: the listing fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        return [listing for page in self.iter_prefix_pages_with_mtime(prefix) for listing in page]

    def list_image_objects(self) -> list[artifact_types.ObjectListing]:
        """Return every object under the ``images/`` prefix with its store mtime.

        The prefix is bound here rather than passed by the caller: ``ImageSweepStore`` is a port
        for the image sweeps, and an image sweep has no business being able to list an arbitrary
        prefix.

        Raises:
            CategorizedError: the listing fails
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        return self.list_prefix_with_mtime(_IMAGE_PREFIX)

    def head_present(self, key: str) -> bool:
        """Return whether an object exists at ``key`` (a HEAD presence check).

        Raises:
            CategorizedError: any non-404 store error
                (:attr:`ErrorCategory.INFRASTRUCTURE_FAILURE`).
        """
        return self.head(key) is not None


def object_store_from_env() -> ObjectStore:
    """Build an :class:`ObjectStore` from the ``KDIVE_S3_*`` environment.

    Reads ``KDIVE_S3_ENDPOINT_URL``, ``KDIVE_S3_BUCKET``, and ``KDIVE_S3_REGION``
    (default ``us-east-1`` — boto3 signs with SigV4 and needs a region). Credentials
    come from boto3's default chain (the standard ``AWS_*`` vars).

    Raises:
        CategorizedError: ``KDIVE_S3_ENDPOINT_URL`` or ``KDIVE_S3_BUCKET`` is unset, or the
            configured bucket lacks compatible versioning
            (:attr:`ErrorCategory.CONFIGURATION_ERROR`).
    """
    endpoint_url = config.get(S3_ENDPOINT_URL)
    if not endpoint_url:
        raise CategorizedError(
            f"{S3_ENDPOINT_URL.name} is not set; cannot reach the object store",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    bucket = config.get(S3_BUCKET)
    if not bucket:
        raise CategorizedError(
            f"{S3_BUCKET.name} is not set; cannot reach the object store",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )
    region = config.get(S3_REGION) or _DEFAULT_REGION
    client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)
    store = ObjectStore(client, bucket)
    store.validate_versioning()
    return store
