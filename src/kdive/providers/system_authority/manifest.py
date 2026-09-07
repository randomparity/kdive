"""Owner-installed provider topology for authority-owned Systems (ADR-0623)."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from kdive.providers.remote_libvirt.system_authority import RemoteAuthoritySystemManifestEntry

_MAX_MANIFEST_BYTES = 1_048_576
_MAX_ENTRIES = 4_096
type Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
type Architecture = Literal["x86_64", "ppc64le"]


def _bounded(value: str) -> str:
    if not value or not value.strip() or len(value.encode("utf-8")) > 255:
        raise ValueError("authority System manifest identifier is outside its bound")
    return value


class _ClosedManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_by_alias=True)


class LocalAuthoritySystemBaseV1(_ClosedManifest):
    """One verified root identity mapped to a fixed private local base."""

    root_identity: Digest
    architecture: Architecture
    source_kind: Literal["catalog", "local"]
    source_name: str | None = None

    @model_validator(mode="after")
    def _source_shape_is_exact(self) -> LocalAuthoritySystemBaseV1:
        if (self.source_kind == "catalog") != (self.source_name is not None):
            raise ValueError("local authority base source selector is invalid")
        if self.source_name is not None:
            _bounded(self.source_name)
        return self

    @property
    def filename(self) -> str:
        return f"{self.root_identity.removeprefix('sha256:')}.qcow2"


class LocalAuthoritySystemManifestV1(_ClosedManifest):
    schema_: Literal["authority-system-manifest-v1"] = Field(alias="schema")
    provider_kind: Literal["local-libvirt"]
    resource_name: str
    authority_instance: str
    guest_egress: bool = False
    accel: Literal["kvm"] = "kvm"
    emulator: str | None = None
    bases: tuple[LocalAuthoritySystemBaseV1, ...] = Field(min_length=1, max_length=_MAX_ENTRIES)

    @field_validator("resource_name", "authority_instance")
    @classmethod
    def _identifiers_are_bounded(cls, value: str) -> str:
        return _bounded(value)

    @field_validator("emulator")
    @classmethod
    def _emulator_is_absolute(cls, value: str | None) -> str | None:
        if value is not None and (not value.startswith("/") or "\0" in value):
            raise ValueError("local authority emulator must be an absolute path")
        return value

    @model_validator(mode="after")
    def _bases_are_unique(self) -> LocalAuthoritySystemManifestV1:
        keys = {base.root_identity for base in self.bases}
        if len(keys) != len(self.bases):
            raise ValueError("local authority manifest has a duplicate root binding")
        catalog = {
            (base.source_name, base.architecture)
            for base in self.bases
            if base.source_kind == "catalog"
        }
        catalog_count = sum(base.source_kind == "catalog" for base in self.bases)
        if len(catalog) != catalog_count:
            raise ValueError("local authority manifest has an ambiguous catalog binding")
        return self


class RemoteAuthoritySystemEntryV1(_ClosedManifest):
    root_identity: Digest
    architecture: Architecture
    base_volume: str
    network: str
    machine: str
    gdb_addr: str
    gdb_port_min: int
    gdb_port_max: int
    ssh_addr: str
    ssh_port_min: int
    ssh_port_max: int


class RemoteAuthoritySystemManifestV1(_ClosedManifest):
    schema_: Literal["authority-system-manifest-v1"] = Field(alias="schema")
    provider_kind: Literal["remote-libvirt"]
    resource_name: str
    authority_instance: str
    entries: tuple[RemoteAuthoritySystemEntryV1, ...] = Field(min_length=1, max_length=_MAX_ENTRIES)

    @field_validator("resource_name", "authority_instance")
    @classmethod
    def _identifiers_are_bounded(cls, value: str) -> str:
        return _bounded(value)

    def provider_entries(self) -> tuple[RemoteAuthoritySystemManifestEntry, ...]:
        return tuple(
            RemoteAuthoritySystemManifestEntry(
                resource_name=self.resource_name,
                authority_instance=self.authority_instance,
                **entry.model_dump(),
            )
            for entry in self.entries
        )

    @model_validator(mode="after")
    def _topology_is_valid(self) -> RemoteAuthoritySystemManifestV1:
        self.provider_entries()
        return self


type AuthoritySystemManifestV1 = Annotated[
    LocalAuthoritySystemManifestV1 | RemoteAuthoritySystemManifestV1,
    Field(discriminator="provider_kind"),
]
_MANIFEST = TypeAdapter(AuthoritySystemManifestV1)


@dataclass(frozen=True, slots=True)
class AuthoritySystemManifestFile:
    """One parsed manifest and the exact private file that supplied it."""

    manifest: AuthoritySystemManifestV1
    device: int
    inode: int
    size: int
    mtime_ns: int
    digest: str


def _load_authority_system_manifest_file(
    path: Path, *, owner_uid: int, owner_gid: int
) -> AuthoritySystemManifestFile | None:
    """Read one bounded owner-only manifest without following or accepting aliases."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)
            or metadata.st_uid != owner_uid
            or metadata.st_gid != owner_gid
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_nlink != 1
            or not 1 <= metadata.st_size <= _MAX_MANIFEST_BYTES
        ):
            raise ValueError("authority System manifest is unsafe")
        data = b""
        while len(data) <= _MAX_MANIFEST_BYTES:
            block = os.read(descriptor, min(1024 * 1024, _MAX_MANIFEST_BYTES + 1 - len(data)))
            if not block:
                break
            data += block
        if len(data) != metadata.st_size or not data.endswith(b"\n"):
            raise ValueError("authority System manifest framing is invalid")
        manifest = _MANIFEST.validate_json(data[:-1], strict=True)
        canonical = json.dumps(
            manifest.model_dump(mode="json", by_alias=True, exclude_unset=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if canonical != data[:-1]:
            raise ValueError("authority System manifest is not canonical")
        return AuthoritySystemManifestFile(
            manifest=manifest,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            digest=sha256(data).hexdigest(),
        )
    finally:
        os.close(descriptor)


def load_authority_system_manifest(
    path: Path, *, owner_uid: int, owner_gid: int
) -> AuthoritySystemManifestV1 | None:
    """Read one bounded owner-only manifest without following or accepting aliases."""
    loaded = _load_authority_system_manifest_file(path, owner_uid=owner_uid, owner_gid=owner_gid)
    return None if loaded is None else loaded.manifest


def load_authority_system_manifest_file(
    path: Path, *, owner_uid: int, owner_gid: int
) -> AuthoritySystemManifestFile | None:
    """Read a manifest with its immutable file identity for readiness pinning."""
    return _load_authority_system_manifest_file(path, owner_uid=owner_uid, owner_gid=owner_gid)


__all__ = [
    "AuthoritySystemManifestV1",
    "AuthoritySystemManifestFile",
    "LocalAuthoritySystemBaseV1",
    "LocalAuthoritySystemManifestV1",
    "RemoteAuthoritySystemEntryV1",
    "RemoteAuthoritySystemManifestV1",
    "load_authority_system_manifest",
    "load_authority_system_manifest_file",
]
