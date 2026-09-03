"""Closed provider-private documents for remote module recovery (ADR-0585).

The canonical byte form of a ``remote-module-*-v1`` document is fixed by the ADR-0585 amendment of
2026-09-03: UTF-8 with no ``\\uXXXX`` escape of a character that needs none, members sorted by code
point, ``,`` and ``:`` separators with no other whitespace, absent optional fields absent rather
than null, and -- for a document stored as a file on an appliance volume -- exactly one trailing
newline. ``deploy/remote_module_appliance/appliance.py`` writes and reads the same bytes through
its own ``_canonical_bytes``; before that amendment its writer used the ``json.dumps`` default of
``ensure_ascii=True`` while its manifest digest used ``ensure_ascii=False``, so an ASCII-escaped
document is the one non-conforming form a reader here is most likely to meet and is reported as
that specific mismatch rather than as a bare byte inequality. Only a non-ASCII value can tell the
two apart, and in the two documents that cross the appliance boundary only the volume key can
carry one; the recovery reference's opaque provider references can carry one too, but never reach
the appliance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from kdive.providers.ports.external_boot import (
    Digest,
    KernelRelease,
    OpaqueProviderRef,
    _ClosedValue,
    _nfc,
)

type RemoteModulePhase = Literal[
    "accepted",
    "captured",
    "staging-intent",
    "replacement-ready",
    "installed",
    "restore-ready",
    "restored",
]
type CaptureState = Literal["present", "absent"]
type RemoteModuleErrorCode = Literal[
    "INVALID_DOCUMENT",
    "IDENTITY_MISMATCH",
    "LIMIT_EXCEEDED",
    "SOURCE_INVALID",
    "ROOT_DISCOVERY_FAILED",
    "FILESYSTEM_FAILURE",
    "RECOVERY_CONFLICT",
    "DEPMOD_FAILURE",
    "FLUSH_FAILURE",
    "SHUTDOWN_FAILURE",
]
type _CanonicalUuid = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"),
]
type _OperationNonce = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
# The character bound mirrors the shipped appliance schemas, where `maxLength` is all JSON Schema
# can express; the normative bound is _VOLUME_KEY_MAX_BYTES, applied by _volume_name_key below.
type _BoundedVolumeKey = Annotated[str, Field(min_length=1, max_length=255)]

_VOLUME_KEY_MAX_BYTES = 255
_OPERATION_MAX_BYTES = 16_384
_DOCUMENT_MAX_BYTES = 65_536
# DOCUMENT_LIMIT in deploy/remote_module_appliance/appliance.py, applied there to the whole
# framed file. Every document's field bounds cap its canonical form well under this, so the
# limit bites only on a corrupt or foreign file read back from a volume.
_APPLIANCE_FILE_MAX_BYTES = 16_384
_IDENTITY_FIELDS = (
    "system_id",
    "run_id",
    "plan_identity",
    "operation_nonce",
    "appliance_image_digest",
    "release",
    "root_volume_key",
    "root_volume_identity",
    "source_manifest",
)
_EARLY_FAILURE_CODES = {
    "INVALID_DOCUMENT",
    "ROOT_DISCOVERY_FAILED",
    "FILESYSTEM_FAILURE",
    "FLUSH_FAILURE",
    "SHUTDOWN_FAILURE",
}


class _RemoteModuleDocument(_ClosedValue):
    """A closed document whose absent optional fields are absent on the wire."""

    protocol: str

    def _json(self, *, ensure_ascii: bool) -> bytes:
        # This overrides _ClosedValue.to_canonical_json to add exclude_none and to parameterize
        # ensure_ascii; every other dump argument must keep matching the base, because both
        # serializers answer to the same canonical form. by_alias pairs with the base's
        # validate_by_alias config and is a no-op only while no field here declares an alias.
        return json.dumps(
            self.model_dump(mode="json", by_alias=True, exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=ensure_ascii,
        ).encode()

    def to_canonical_json(self) -> bytes:
        """Return compact sorted UTF-8 JSON without a trailing newline.

        This is the unframed identity form: the bytes ``identity_for`` digests. Bytes written
        to or read from an appliance volume carry one framing newline; use the wire pair below.
        """
        return self._json(ensure_ascii=False)

    @classmethod
    def from_canonical_json(cls, data: bytes) -> Self:
        """Parse a bounded unframed document and reject alternate byte encodings."""
        limit = _OPERATION_MAX_BYTES if cls is RemoteModuleOperationV1 else _DOCUMENT_MAX_BYTES
        if len(data) > limit:
            raise ValueError(f"remote module document exceeds {limit} bytes")
        try:
            value = cls.model_validate_json(data)
        except ValidationError as error:
            # The bytes come off a volume the worker did not write, so the message names where
            # validation failed and never what the document said. A location is itself input on
            # an extra-field error -- the rejected key is the caller's own string -- so only
            # known field names survive. `from None` drops the chained pydantic error, which
            # carries the offending values.
            known = set(cls.model_fields)
            locations = sorted(
                "{}:{}".format(
                    ".".join(str(part) if str(part) in known else "?" for part in item["loc"]),
                    item["type"],
                )
                for item in error.errors()
            )
            raise ValueError(
                f"remote module document failed validation at {', '.join(locations)}"
            ) from None
        if value.to_canonical_json() != data:
            if value._json(ensure_ascii=True) == data:
                raise ValueError(
                    "remote module document is ASCII-escaped JSON; the canonical form is "
                    "unescaped UTF-8 (ADR-0585 amendment 2026-09-03)"
                )
            raise ValueError("remote module document is not canonical JSON")
        return value

    def to_wire_bytes(self) -> bytes:
        """Return the framed on-volume file the appliance reads: canonical JSON plus a newline.

        The appliance rejects an operation file that does not end in a newline and writes every
        result and checkpoint with one, so an unframed document never survives the round trip.
        """
        return self.to_canonical_json() + b"\n"

    @classmethod
    def from_wire_bytes(cls, data: bytes) -> Self:
        """Parse a framed on-volume file, requiring exactly one trailing newline."""
        if len(data) > _APPLIANCE_FILE_MAX_BYTES:
            raise ValueError(
                f"framed remote module document exceeds {_APPLIANCE_FILE_MAX_BYTES} bytes"
            )
        if not data.endswith(b"\n") or data.endswith(b"\n\n"):
            raise ValueError("remote module document is not newline-framed")
        return cls.from_canonical_json(data[:-1])


def identity_for(document: _RemoteModuleDocument) -> str:
    """Return the protocol-separated SHA-256 identity of a canonical document."""
    protocol = document.protocol.encode("ascii")
    return "sha256:" + hashlib.sha256(protocol + b"\0" + document.to_canonical_json()).hexdigest()


def _closed_volume_key(value: str) -> str:
    # The same rule set OpaqueProviderRef._opaque applies in the sibling port: one fence, not a
    # subset of it, because a consumer reaches both through the same recovery reference. It is a
    # copy rather than a call because the port exports no predicate; a rule added there is owed
    # here too, and the test asserting both reject the same keys is what says so.
    value = _nfc(value)
    if (
        value.startswith("/")
        or any(segment in {"", ".", ".."} for segment in value.split("/"))
        or not value.isprintable()
        or any(character in value for character in ("\\", ":", "@"))
    ):
        raise ValueError("volume key must be an opaque provider value")
    return value


def _volume_name_key(value: str) -> str:
    """Refuse a key that could not name a libvirt volume, by bytes rather than by characters.

    A dir pool inherits the filesystem `NAME_MAX`, so the bound is 255 UTF-8 bytes (ADR-0585
    amendment 2026-09-03; #2176). This is the first reader to see an operator-provisioned key, and
    the appliance can only answer with a closed error code, so the byte length and the limit are
    named here.
    """
    value = _closed_volume_key(value)
    encoded_length = len(value.encode())
    if encoded_length > _VOLUME_KEY_MAX_BYTES:
        raise ValueError(
            f"volume key is {encoded_length} UTF-8 bytes; the limit is "
            f"{_VOLUME_KEY_MAX_BYTES} bytes"
        )
    return value


class _RootVolumeV1(_ClosedValue):
    # The pool-scoped volume name resolved by storageVolLookupByName, not the host-path form
    # virStorageVolGetKey returns for file-backed pools: ADR-0585 keeps host paths out of the
    # operation document, so the validator below rejects them.
    key: _BoundedVolumeKey
    identity: Digest

    _key_names_a_volume = field_validator("key")(_volume_name_key)


class RemoteModuleOperationV1(_RemoteModuleDocument):
    """The exact closed operation document consumed by the v1 appliance."""

    protocol: Literal["remote-module-operation-v1"] = "remote-module-operation-v1"
    operation: Literal["capture_install", "restore"]
    system_id: _CanonicalUuid
    run_id: _CanonicalUuid
    plan_identity: Digest
    operation_nonce: _OperationNonce
    release: KernelRelease
    root_volume: _RootVolumeV1
    source_manifest: Digest
    capture_manifest: Digest | None = None
    installed_manifest: Digest | None = None
    capture_absent: Literal[True] | None = None
    appliance_image_digest: Digest

    @model_validator(mode="after")
    def _operation_shape_is_exact(self) -> Self:
        if self.operation == "capture_install":
            if any(
                value is not None
                for value in (self.capture_manifest, self.installed_manifest, self.capture_absent)
            ):
                raise ValueError("capture_install cannot carry restore evidence")
            return self
        if self.installed_manifest is None:
            raise ValueError("restore requires installed_manifest")
        if (self.capture_manifest is None) == (self.capture_absent is not True):
            raise ValueError("restore requires exactly one capture form")
        return self


class RemoteModuleResultV1(_RemoteModuleDocument):
    """A validated durable appliance result with no free-form evidence."""

    protocol: Literal["remote-module-result-v1"] = "remote-module-result-v1"
    status: Literal["success", "failure"]
    phase: RemoteModulePhase
    error_code: RemoteModuleErrorCode | None = None
    system_id: _CanonicalUuid | None = None
    run_id: _CanonicalUuid | None = None
    plan_identity: Digest | None = None
    operation_nonce: _OperationNonce | None = None
    appliance_image_digest: Digest | None = None
    release: KernelRelease | None = None
    root_volume_key: _BoundedVolumeKey | None = None
    root_volume_identity: Digest | None = None
    source_manifest: Digest | None = None
    installed_manifest: Digest | None = None
    capture_manifest: Digest | None = None
    capture_absent: Literal[True] | None = None
    entry_count: Annotated[int, Field(ge=0, le=200_000)] | None = None
    content_bytes: Annotated[int, Field(ge=0, le=8_589_934_592)] | None = None

    _root_key_names_a_volume = field_validator("root_volume_key")(
        lambda value: None if value is None else _volume_name_key(value)
    )

    @property
    def capture_state(self) -> CaptureState | None:
        """Return the normalized captured-tree state when the result contains one."""
        if self.capture_manifest is not None:
            return "present"
        if self.capture_absent is True:
            return "absent"
        return None

    @property
    def is_identity_complete(self) -> bool:
        """Whether every identity field is present, which is what ``validate_for`` can compare.

        A caller that must not accept an identity-absent early-failure result for its own attempt
        checks this first; ``validate_for`` has nothing to compare on that shape and says so.
        """
        return all(getattr(self, field) is not None for field in _IDENTITY_FIELDS)

    @model_validator(mode="after")
    def _result_shape_is_exact(self) -> Self:
        identity_presence = [getattr(self, field) is not None for field in _IDENTITY_FIELDS]
        complete_identity = all(identity_presence)
        no_identity = not any(identity_presence)
        evidence = (
            self.installed_manifest,
            self.capture_manifest,
            self.capture_absent,
            self.entry_count,
            self.content_bytes,
        )
        if self.status == "failure":
            if self.error_code is None:
                raise ValueError("failure requires error_code")
            if no_identity and self.error_code in _EARLY_FAILURE_CODES:
                if self.phase != "accepted" or any(value is not None for value in evidence):
                    raise ValueError(
                        "identity-absent failure requires accepted phase without evidence"
                    )
                return self
            if not complete_identity:
                raise ValueError("failure requires complete identity")
            if not self._phase_evidence_is_compatible():
                raise ValueError("failure phase and evidence are inconsistent")
            return self

        if self.error_code is not None or not complete_identity:
            raise ValueError("success requires complete identity and no error_code")
        if self.phase == "accepted":
            # The result schema's success branch is a oneOf over the six later phases: nothing
            # durable exists yet at "accepted", so it can only ever describe a failure.
            raise ValueError("success cannot report the accepted phase")
        if not self._phase_evidence_is_compatible():
            raise ValueError("success phase and evidence are inconsistent")
        return self

    def _phase_evidence_is_compatible(self) -> bool:
        if self.phase == "accepted":
            return all(
                value is None
                for value in (
                    self.installed_manifest,
                    self.capture_manifest,
                    self.capture_absent,
                    self.entry_count,
                    self.content_bytes,
                )
            )
        capture_is_valid = (self.capture_manifest is not None) != (self.capture_absent is True)
        if not capture_is_valid:
            return False
        counts_present = self.entry_count is not None and self.content_bytes is not None
        counts_absent = self.entry_count is None and self.content_bytes is None
        if self.phase == "captured":
            return self.installed_manifest is None and counts_present
        elif self.phase == "staging-intent":
            return self.installed_manifest is None and counts_absent
        elif self.phase in {"replacement-ready", "installed"}:
            return self.installed_manifest is not None and counts_present
        elif self.phase in {"restore-ready", "restored"}:
            return self.installed_manifest is not None and counts_absent
        return False

    def validate_for(self, operation: RemoteModuleOperationV1) -> None:
        """Reject durable evidence belonging to a different operation attempt.

        An identity-absent early-failure result carries nothing to compare, so it is accepted
        for any operation. The appliance writes that shape when it fails before reading the
        operation document, and binding it to an attempt comes from the scratch-volume
        reference it was read through, never from this method. A caller that needs the
        stronger guarantee checks ``is_identity_complete`` before trusting it.
        """
        allowed_phases = (
            {"accepted", "captured", "staging-intent", "replacement-ready", "installed"}
            if operation.operation == "capture_install"
            else {"accepted", "installed", "restore-ready", "restored"}
        )
        if self.phase not in allowed_phases:
            raise ValueError(f"result phase does not belong to {operation.operation} operation")
        expected = {
            "system_id": operation.system_id,
            "run_id": operation.run_id,
            "plan_identity": operation.plan_identity,
            "operation_nonce": operation.operation_nonce,
            "appliance_image_digest": operation.appliance_image_digest,
            "release": operation.release,
            "root_volume_key": operation.root_volume.key,
            "root_volume_identity": operation.root_volume.identity,
            "source_manifest": operation.source_manifest,
        }
        if operation.operation == "restore":
            expected.update(
                installed_manifest=operation.installed_manifest,
                capture_manifest=operation.capture_manifest,
                capture_absent=operation.capture_absent,
            )
        for field, value in expected.items():
            actual = getattr(self, field)
            if actual is not None and actual != value:
                raise ValueError(f"result {field} does not match operation")


class RemoteModuleRecoveryRefV1(_RemoteModuleDocument):
    """Opaque durable evidence sufficient to reopen one appliance attempt.

    This is the provider-private half of the ADR-0585 recovery point: the ADR has Core store the
    appliance result "and the exact scratch-volume reference" before activation, and this document
    is that pairing. It never crosses the appliance boundary and has no appliance schema.

    Three names sit close together, so read them precisely. The document the appliance writes is
    ``remote-module-result-v1`` in ``deploy/remote_module_appliance/result-v1.schema.json`` and
    ``RemoteModuleResultV1`` here, but ADR-0585 calls that same document
    ``remote-module-recovery-v1``; the ADR and the shipped schema disagree, and the schema is what
    the appliance enforces. This class is neither of those.
    """

    protocol: Literal["remote-module-recovery-ref-v1"] = "remote-module-recovery-ref-v1"
    system_id: _CanonicalUuid
    run_id: _CanonicalUuid
    plan_identity: Digest
    operation_nonce: _OperationNonce
    pool: OpaqueProviderRef
    root_volume: OpaqueProviderRef
    source_volume: OpaqueProviderRef
    scratch_volume: OpaqueProviderRef
    operation_identity: Digest
    result_identity: Digest
    installed_entry_count: Annotated[int, Field(ge=0, le=200_000)] | None = None
    installed_content_bytes: Annotated[int, Field(ge=0, le=8_589_934_592)] | None = None
    appliance_image_digest: Digest
    authority_identity: Digest

    @staticmethod
    def identity_for_authority(authority: OpaqueProviderRef) -> str:
        """Bind the reference to one authority reference so a mismatch is detected.

        The digest is not a secret and does not need to be: ADR-0584 holds that possessing an
        authority reference is not authority, and the references this repository mints are
        structured enough to enumerate. What the digest buys is that the recovery reference
        cannot be re-presented under a different authority without ``validate_authority``
        noticing, while the reference itself stays out of durable storage.
        """
        return (
            "sha256:"
            + hashlib.sha256(
                b"remote-module-authority-v1\0" + authority.to_canonical_json()
            ).hexdigest()
        )

    def validate_authority(self, authority: OpaqueProviderRef) -> None:
        """Reject a recovery reference presented with different mutation authority."""
        if self.identity_for_authority(authority) != self.authority_identity:
            raise ValueError("recovery authority does not match reference")
