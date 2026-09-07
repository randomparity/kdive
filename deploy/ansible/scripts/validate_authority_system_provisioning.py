#!/usr/bin/env python3
"""Validate one controller-side authority System manifest and its staged bases."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from typing import Any, NoReturn, cast

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"\S(?:.*\S)?\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}\Z")
_MAX_MANIFEST_BYTES = 1_048_576
_MAX_BASES = 4_096


def _fail() -> NoReturn:
    raise ValueError("invalid or incomplete authority System provisioning inputs")


def _identifier(value: object) -> str:
    if not isinstance(value, str):
        _fail()
    if len(value.encode()) > 255 or not _IDENTIFIER.fullmatch(value):
        _fail()
    return value


def _name(value: object) -> str:
    if not isinstance(value, str):
        _fail()
    if not _NAME.fullmatch(value):
        _fail()
    return value


def _address(value: object) -> str:
    if not isinstance(value, str):
        _fail()
    if (
        not value
        or len(value.encode()) > 255
        or not value.isascii()
        or any(character.isspace() or ord(character) < 0x20 for character in value)
    ):
        _fail()
    return value


def _absolute_path(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        _fail()
    return value


def _read_canonical_manifest(path: object) -> dict[str, Any]:
    manifest_path = _absolute_path(path)
    try:
        descriptor = os.open(manifest_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as manifest:
            metadata = os.fstat(manifest.fileno())
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) not in (
                0o400,
                0o600,
            ):
                _fail()
            data = manifest.read(_MAX_MANIFEST_BYTES + 1)
    except OSError, ValueError:
        _fail()
    if not 1 <= len(data) <= _MAX_MANIFEST_BYTES or not data.endswith(b"\n"):
        _fail()
    try:
        document = json.loads(data[:-1])
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    except TypeError, ValueError:
        _fail()
    if canonical != data[:-1] or not isinstance(document, dict):
        _fail()
    return document


def _base_digest(path: object) -> str:
    base_path = _absolute_path(path)
    try:
        descriptor = os.open(base_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) not in (
                0o400,
                0o600,
            ):
                _fail()
            return hashlib.file_digest(source, "sha256").hexdigest()
    except OSError, ValueError:
        _fail()


def _allowed_keys(
    value: dict[str, Any], required: set[str], optional: set[str] | None = None
) -> None:
    optional = optional or set()
    if not required <= set(value) <= required | optional:
        _fail()


def _manifest_identities(document: dict[str, Any], provider_kind: str) -> set[str]:
    common = {"schema", "provider_kind", "resource_name", "authority_instance"}
    if provider_kind == "local-libvirt":
        _allowed_keys(document, common | {"bases"}, {"guest_egress", "accel", "emulator"})
        if "guest_egress" in document and type(document["guest_egress"]) is not bool:
            _fail()
        if "accel" in document and document["accel"] != "kvm":
            _fail()
        emulator = document.get("emulator")
        if emulator is not None and (
            not isinstance(emulator, str) or not emulator.startswith("/") or "\0" in emulator
        ):
            _fail()
    else:
        _allowed_keys(document, common | {"entries"})
    if document.get("schema") != "authority-system-manifest-v1":
        _fail()
    if document.get("provider_kind") != provider_kind:
        _fail()
    _identifier(document.get("resource_name"))
    _identifier(document.get("authority_instance"))
    entries = document.get("bases" if provider_kind == "local-libvirt" else "entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= _MAX_BASES:
        _fail()
    identities: set[str] = set()
    catalog_bindings: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            _fail()
        if provider_kind == "local-libvirt":
            _allowed_keys(
                entry,
                {"root_identity", "architecture", "source_kind"},
                {"source_name"},
            )
            if entry["architecture"] not in {"x86_64", "ppc64le"}:
                _fail()
            source_kind = entry["source_kind"]
            source_name = entry.get("source_name")
            if source_kind == "catalog":
                name = _identifier(source_name)
                binding = (name, entry["architecture"])
                if binding in catalog_bindings:
                    _fail()
                catalog_bindings.add(binding)
            elif source_kind != "local" or source_name is not None:
                _fail()
        else:
            _allowed_keys(
                entry,
                {
                    "root_identity",
                    "architecture",
                    "base_volume",
                    "network",
                    "machine",
                    "gdb_addr",
                    "gdb_port_min",
                    "gdb_port_max",
                    "ssh_addr",
                    "ssh_port_min",
                    "ssh_port_max",
                },
            )
            if entry["architecture"] not in {"x86_64", "ppc64le"}:
                _fail()
            for field in ("base_volume", "network", "machine"):
                _name(entry[field])
            for field in ("gdb_addr", "ssh_addr"):
                _address(entry[field])
            prefixes = {
                "x86_64": ("pc-i440fx-", "pc-q35-"),
                "ppc64le": ("pseries-",),
            }
            if not entry["machine"].startswith(prefixes[entry["architecture"]]):
                _fail()
            for low, high in (("gdb_port_min", "gdb_port_max"), ("ssh_port_min", "ssh_port_max")):
                if (
                    type(entry[low]) is not int
                    or type(entry[high]) is not int
                    or not 1 <= entry[low] <= entry[high] <= 65535
                ):
                    _fail()
            if entry["gdb_port_min"] == entry["gdb_port_max"] or (
                entry["gdb_addr"] == entry["ssh_addr"]
                and entry["gdb_port_min"] <= entry["ssh_port_max"]
                and entry["ssh_port_min"] <= entry["gdb_port_max"]
            ):
                _fail()
        identity = entry.get("root_identity")
        if not isinstance(identity, str) or not _DIGEST.fullmatch(identity):
            _fail()
        key = identity.removeprefix("sha256:")
        if key in identities:
            _fail()
        identities.add(key)
    return identities


def _validate_local_bases(bases: object, identities: set[str]) -> None:
    if not isinstance(bases, list) or not bases:
        _fail()
    staged: set[str] = set()
    for base in bases:
        if not isinstance(base, dict):
            _fail()
        base = cast(dict[str, object], base)
        if set(base) != {"source", "digest"}:
            _fail()
        digest = base["digest"]
        if not isinstance(digest, str) or not _HEX_DIGEST.fullmatch(digest) or digest in staged:
            _fail()
        if _base_digest(base["source"]) != digest:
            _fail()
        staged.add(digest)
    if staged != identities:
        _fail()


def _validate_remote_bases(bases: object, document: dict[str, Any]) -> None:
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        _fail()
    if not isinstance(bases, list) or not bases:
        _fail()
    expected: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry.get("base_volume"), str):
            _fail()
        name = entry["base_volume"]
        digest = entry["root_identity"].removeprefix("sha256:")
        if name in expected:
            _fail()
        expected[name] = digest
    staged: dict[str, str] = {}
    for base in bases:
        if not isinstance(base, dict):
            _fail()
        base = cast(dict[str, object], base)
        if set(base) != {"source", "name"}:
            _fail()
        name = _name(base["name"])
        if name in staged:
            _fail()
        staged[name] = _base_digest(base["source"])
    if staged != expected:
        _fail()


def main() -> None:
    try:
        value = json.load(sys.stdin)
        if not isinstance(value, dict):
            _fail()
        expected_kind = value.get("expected_provider_kind")
        if expected_kind not in {"local-libvirt", "remote-libvirt"}:
            _fail()
        authority_instance = _identifier(value.get("authority_instance"))
        document = _read_canonical_manifest(value.get("manifest"))
        if document.get("authority_instance") != authority_instance:
            _fail()
        identities = _manifest_identities(document, expected_kind)
        if expected_kind == "local-libvirt":
            if value.get("remote_bases") != []:
                _fail()
            _validate_local_bases(value.get("local_bases"), identities)
        else:
            if value.get("local_bases") != []:
                _fail()
            _validate_remote_bases(value.get("remote_bases"), document)
    except OSError, TypeError, ValueError:
        sys.exit("invalid or incomplete authority System provisioning inputs")


if __name__ == "__main__":
    main()
