"""Local-libvirt host-dump capture boundary tests (ADR-0211)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from kdive.providers.local_libvirt.retrieve import host_dump_capture


def test_inactive_domain_has_no_live_host_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Domain:
        def isActive(self) -> bool:
            return False

    class _Connection:
        closed = False

        def lookupByName(self, _name: str) -> _Domain:
            return _Domain()

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    monkeypatch.setattr(host_dump_capture.libvirt, "open", lambda _uri: connection)

    assert host_dump_capture._real_host_dump_capture(uuid4()) is None
    assert connection.closed is True
