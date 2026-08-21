"""Tests for the local-libvirt reconciler ``InfraReaper`` adapter (ADR-0111)."""

from __future__ import annotations

import asyncio
from uuid import UUID

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.infra.reaping import CaptureReaper, OrphanedCapture
from kdive.providers.local_libvirt import reaping as reaping_module
from kdive.providers.local_libvirt.reaping import (
    LibvirtInfraReaper,
    LocalLibvirtCaptureReaper,
)
from kdive.providers.ports.handles import OwnedInfra
from kdive.providers.ports.traffic import capture_qom_id


class _FakeDiscovery:
    def __init__(self, owned: list[OwnedInfra]) -> None:
        self._owned = owned

    def list_owned(self) -> list[OwnedInfra]:
        return list(self._owned)


class _FakeProvisioning:
    def __init__(self) -> None:
        self.torn_down: list[str] = []

    def teardown(self, domain_name: str) -> None:
        self.torn_down.append(domain_name)


def _reaper(owned: list[OwnedInfra]) -> tuple[LibvirtInfraReaper, _FakeProvisioning]:
    provisioning = _FakeProvisioning()
    reaper = LibvirtInfraReaper(discovery=_FakeDiscovery(owned), provisioning=provisioning)
    return reaper, provisioning


def test_list_owned_adapts_valid_uuid_tag() -> None:
    reaper, _ = _reaper(
        [{"system_id": "11111111-1111-1111-1111-111111111111", "domain_name": "kdive-1"}]
    )
    owned = asyncio.run(reaper.list_owned())
    assert len(owned) == 1
    assert owned[0].name == "kdive-1"
    assert owned[0].system_id == UUID("11111111-1111-1111-1111-111111111111")


def test_list_owned_maps_empty_system_id_to_none() -> None:
    reaper, _ = _reaper(
        [{"system_id": "", "domain_name": "kdive-22222222-2222-2222-2222-222222222222"}]
    )
    owned = asyncio.run(reaper.list_owned())
    assert owned[0].name == "kdive-22222222-2222-2222-2222-222222222222"
    assert owned[0].system_id is None  # never UUID("") — that would raise


def test_list_owned_maps_unparseable_system_id_to_none() -> None:
    reaper, _ = _reaper([{"system_id": "not-a-uuid", "domain_name": "kdive-x"}])
    owned = asyncio.run(reaper.list_owned())
    assert owned[0].system_id is None


def test_destroy_routes_to_provisioning_teardown() -> None:
    reaper, provisioning = _reaper([])
    asyncio.run(reaper.destroy("kdive-99999999-9999-9999-9999-999999999999"))
    assert provisioning.torn_down == ["kdive-99999999-9999-9999-9999-999999999999"]


# --- LocalLibvirtCaptureReaper (ADR-0556, ADR-0567, #1948) ---

_SID = UUID("00000000-0000-0000-0000-0000000000cc")
_JID = UUID("00000000-0000-0000-0000-0000000000dd")
_DOMAIN = "kdive-x"


def _capture() -> OrphanedCapture:
    return OrphanedCapture(
        provider_kind="local-libvirt",
        resource_id=_SID,
        resource_name="local",
        system_id=_SID,
        domain_name=_DOMAIN,
        job_id=_JID,
    )


class _ReaperConn:
    """Records lookup calls; raises the configured lookup error, if any."""

    def __init__(
        self,
        *,
        domain: object | None = object(),
        lookup_error: libvirt.libvirtError | None = None,
    ) -> None:
        self.lookups: list[str] = []
        self.closed = False
        self._domain = domain
        self._lookup_error = lookup_error

    def lookupByName(self, name: str) -> object:  # noqa: N802 - libvirt binding name
        self.lookups.append(name)
        if self._lookup_error is not None:
            raise self._lookup_error
        return self._domain

    def close(self) -> int:
        self.closed = True
        return 0


def _no_domain() -> libvirt.libvirtError:
    err = libvirt.libvirtError("synthetic")
    err.err = (libvirt.VIR_ERR_NO_DOMAIN, 0, "synthetic", 0, "", None, None, 0, 0)
    return err


def _reaper_for(conn: _ReaperConn, monitor) -> LocalLibvirtCaptureReaper:
    return LocalLibvirtCaptureReaper(connect=lambda: conn, monitor=monitor)


def test_reaper_satisfies_the_capture_reaper_port() -> None:
    assert isinstance(LocalLibvirtCaptureReaper.from_env(), CaptureReaper)


def test_detach_happens_before_the_unlink(monkeypatch) -> None:
    """The ordering is the whole control (ADR-0556): unlink-first orphans a live inode."""
    order: list[str] = []
    conn = _ReaperConn()

    def monitor(domain, cmd, flags):
        order.append("object-del")
        return "{}"

    reaper = _reaper_for(conn, monitor)

    def _record_unlink(capture: OrphanedCapture) -> None:
        order.append("unlink")

    monkeypatch.setattr(reaper, "_unlink_pcap", _record_unlink)
    assert asyncio.run(reaper.reclaim_capture(_capture())) is True
    assert order == ["object-del", "unlink"]


def test_a_missing_domain_is_tolerated_and_the_pcap_still_unlinks(monkeypatch, tmp_path) -> None:
    """The domain stopped (or was reaped); there is no filter to detach (spec R2)."""
    pcap = tmp_path / f"{_JID}.pcap"
    pcap.write_bytes(b"stale")
    monkeypatch.setattr(reaping_module, "pcap_path", lambda sid, jid: tmp_path / f"{jid}.pcap")
    qmp_calls: list[str] = []
    conn = _ReaperConn(domain=None, lookup_error=_no_domain())

    def monitor(domain, cmd, flags):  # pragma: no cover - must never run
        qmp_calls.append("object-del")
        return "{}"

    assert asyncio.run(_reaper_for(conn, monitor).reclaim_capture(_capture())) is True
    assert conn.lookups == [_DOMAIN]
    assert qmp_calls == []  # no domain to address: no QMP call attempted
    assert not pcap.exists()


def test_a_missing_domain_and_missing_pcap_reclaim_cleanly(tmp_path) -> None:
    """Concurrent absences: no QMP call, unlink attempted, True (spec R2, test 4b)."""
    conn = _ReaperConn(domain=None, lookup_error=_no_domain())

    def monitor(domain, cmd, flags):  # pragma: no cover - must never run
        raise AssertionError("no QMP call without a domain")

    assert asyncio.run(_reaper_for(conn, monitor).reclaim_capture(_capture())) is True


def test_a_missing_filter_is_tolerated_and_the_pcap_still_unlinks(monkeypatch, tmp_path) -> None:
    """A QMP object-del on an absent id must not fail the reclaim (spec R2)."""
    pcap = tmp_path / f"{_JID}.pcap"
    pcap.write_bytes(b"stale")
    monkeypatch.setattr(reaping_module, "pcap_path", lambda sid, jid: tmp_path / f"{jid}.pcap")
    conn = _ReaperConn()

    def monitor(domain, cmd, flags):
        raise libvirt.libvirtError(f"Device '{capture_qom_id(_JID)}' not found")

    assert asyncio.run(_reaper_for(conn, monitor).reclaim_capture(_capture())) is True
    assert not pcap.exists()


def test_a_missing_pcap_is_tolerated(tmp_path) -> None:
    """The in-job reclaim already removed it; the sweep still marks completion."""
    conn = _ReaperConn()

    def monitor(domain, cmd, flags):
        return "{}"

    assert asyncio.run(_reaper_for(conn, monitor).reclaim_capture(_capture())) is True


def test_a_non_not_found_monitor_error_aborts_before_the_unlink(monkeypatch, tmp_path) -> None:
    """A genuine monitor failure is CONTROL_FAILURE and never deletes on unknown state."""
    pcap = tmp_path / f"{_JID}.pcap"
    pcap.write_bytes(b"stale")
    monkeypatch.setattr(reaping_module, "pcap_path", lambda sid, jid: tmp_path / f"{jid}.pcap")
    conn = _ReaperConn()

    def monitor(domain, cmd, flags):
        raise libvirt.libvirtError("monitor locked")

    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(_reaper_for(conn, monitor).reclaim_capture(_capture()))
    assert excinfo.value.category is ErrorCategory.CONTROL_FAILURE
    assert excinfo.value.details == {"domain": _DOMAIN}
    assert pcap.exists()  # abort-before-unlink
    assert conn.closed  # the connection is closed on the raise path too


def test_a_non_absence_unlink_failure_is_infrastructure_failure(monkeypatch) -> None:
    """A swallowed unlink failure would mark the row complete with the file on disk."""

    class _Unlinkable:
        def unlink(self, missing_ok: bool = False) -> None:
            raise PermissionError(1, "operation not permitted")

    monkeypatch.setattr(reaping_module, "pcap_path", lambda sid, jid: _Unlinkable())
    conn = _ReaperConn()

    def monitor(domain, cmd, flags):
        return "{}"

    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(_reaper_for(conn, monitor).reclaim_capture(_capture()))
    assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_from_env_builds_without_connecting() -> None:
    """Lazy construction: no libvirt connection is opened until reclaim."""
    reaper = LocalLibvirtCaptureReaper.from_env()
    assert isinstance(reaper, LocalLibvirtCaptureReaper)
