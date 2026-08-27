"""Synchronous local capture child execution and independent quiescence (ADR-0558)."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol
from uuid import UUID, uuid4

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.traffic import (
    QuiescenceEvidence,
)


class _ProbeConnection(Protocol):
    def lookupByName(self, name: str) -> object: ...  # noqa: N802
    def close(self) -> object: ...


type Connect = Callable[[], _ProbeConnection]
type Monitor = Callable[[object, str, int], str]


def _ordered_reply(raw: str, expected_id: str) -> object:
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CategorizedError(
            "local QMP quiescence response was malformed",
            category=ErrorCategory.CONTROL_FAILURE,
        ) from error
    if not isinstance(response, dict) or response.get("id") != expected_id:
        raise CategorizedError(
            "local QMP transport did not correlate the ordered response",
            category=ErrorCategory.CONTROL_FAILURE,
        )
    if "return" not in response:
        raise CategorizedError(
            "local QMP quiescence response was inconclusive",
            category=ErrorCategory.CONTROL_FAILURE,
        )
    return response["return"]


class LocalLibvirtCaptureQuiescence:
    """Cross a fresh local QMP connection and prove the exact QOM object absent."""

    def __init__(self, *, resource_id: UUID, connect: Connect, monitor: Monitor) -> None:
        self._resource_id = resource_id
        self._connect = connect
        self._monitor = monitor

    def prove_absent(self, resource_id: UUID, domain_name: str, qom_id: str) -> QuiescenceEvidence:
        """Detach idempotently, then issue a correlated QOM query on one new connection."""
        if resource_id != self._resource_id:
            raise CategorizedError(
                "local capture quiescence Resource identity mismatch",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        try:
            connection = self._connect()
        except libvirt.libvirtError as error:
            raise CategorizedError(
                "local libvirt is unreachable for capture quiescence",
                category=ErrorCategory.TRANSPORT_FAILURE,
            ) from error
        try:
            try:
                domain = connection.lookupByName(domain_name)
            except libvirt.libvirtError as error:
                raise CategorizedError(
                    "local domain lookup failed during capture quiescence",
                    category=ErrorCategory.CONTROL_FAILURE,
                ) from error
            self._detach(domain, qom_id)
            self._query_absence(domain, qom_id)
        finally:
            with suppress(libvirt.libvirtError):
                connection.close()
        return QuiescenceEvidence(
            provider_kind="local-libvirt",
            resource_id=resource_id,
            domain_name=domain_name,
            qom_id=qom_id,
            result="absent",
            ordering="fresh-qmp-connection",
        )

    def _detach(self, domain: object, qom_id: str) -> None:
        command_id = f"kdive-detach-{uuid4()}"
        command = {"execute": "object-del", "arguments": {"id": qom_id}, "id": command_id}
        try:
            raw = self._monitor(domain, json.dumps(command), 0)
        except libvirt.libvirtError as error:
            message = str(error).lower()
            if "not found" in message or "devicenotfound" in message:
                return
            raise CategorizedError(
                "local capture detach failed during quiescence",
                category=ErrorCategory.CONTROL_FAILURE,
            ) from error
        _ordered_reply(raw, command_id)

    def _query_absence(self, domain: object, qom_id: str) -> None:
        command_id = f"kdive-query-{uuid4()}"
        command = {
            "execute": "qom-list",
            "arguments": {"path": "/objects"},
            "id": command_id,
        }
        try:
            raw = self._monitor(domain, json.dumps(command), 0)
        except libvirt.libvirtError as error:
            raise CategorizedError(
                "local capture QOM query failed during quiescence",
                category=ErrorCategory.CONTROL_FAILURE,
            ) from error
        members = _ordered_reply(raw, command_id)
        if not isinstance(members, list):
            raise CategorizedError(
                "local capture QOM query returned an inconclusive shape",
                category=ErrorCategory.CONTROL_FAILURE,
            )
        member_names: list[str] = []
        for item in members:
            name = item.get("name") if isinstance(item, dict) else None
            member_type = item.get("type") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or not isinstance(name, str)
                or not name
                or not isinstance(member_type, str)
                or not member_type
            ):
                raise CategorizedError(
                    "local capture QOM query returned an inconclusive shape",
                    category=ErrorCategory.CONTROL_FAILURE,
                )
            member_names.append(name)
        if qom_id in member_names:
            raise CategorizedError(
                "local capture QOM object is still present",
                category=ErrorCategory.CONTROL_FAILURE,
            )
