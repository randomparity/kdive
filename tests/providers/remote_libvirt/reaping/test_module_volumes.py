"""Attachment-safe remote module volume reaping (ADR-0588, ADR-0603)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    RemoteDeviceIdentity,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    ModuleVolumeOwner,
    render_module_volume_name,
)
from kdive.providers.remote_libvirt.reaping.module_volumes import (
    ModuleVolumeReaperConn,
    list_owned_module_volumes,
    reap_orphaned_module_volumes,
    referenced_volume_paths,
)

POOL = "modules"
SYSTEM = "00000000-0000-0000-0000-000000000001"
RUN = "00000000-0000-0000-0000-000000000002"
NONCE = "3" * 32


def _owner(kind: str = "source.ext4") -> ModuleVolumeOwner:
    return ModuleVolumeOwner(SYSTEM, RUN, NONCE, kind)


def _name(kind: str = "source.ext4") -> str:
    return render_module_volume_name(SYSTEM, RUN, NONCE, kind)


class Volume:
    def __init__(self, name: str, path: str, events: list[str], *, gone: bool = False) -> None:
        self._name = name
        self._path = path
        self.events = events
        self.gone = gone

    def name(self) -> str:
        return self._name

    def path(self) -> str:
        self.events.append(f"path:{self._name}")
        return self._path

    def delete(self, flags: int = 0) -> int:
        self.events.append(f"delete:{self._name}")
        if self.gone:
            error = libvirt.libvirtError("missing")
            error.err = [libvirt.VIR_ERR_NO_STORAGE_VOL] + [None] * 8
            raise error
        return 0


class Pool:
    def __init__(self, name: str, volumes: list[Volume], events: list[str]) -> None:
        self.name = name
        self.volumes = {volume.name(): volume for volume in volumes}
        self.events = events

    def refresh(self, flags: int = 0) -> int:
        self.events.append(f"refresh:{self.name}")
        return 0

    def listAllVolumes(self, flags: int = 0) -> list[Volume]:  # noqa: N802
        self.events.append(f"enumerate:{self.name}")
        return list(self.volumes.values())

    def storageVolLookupByName(self, name: str) -> Volume:  # noqa: N802
        self.events.append(f"lookup:{self.name}:{name}")
        try:
            return self.volumes[name]
        except KeyError:
            raise libvirt.libvirtError("missing") from None


class Domain:
    def __init__(self, live: str, inactive: str | None = None, *, active: bool = True) -> None:
        self.live = live
        self.inactive = inactive
        self.active = active

    def isActive(self) -> int:  # noqa: N802
        return int(self.active)

    def isPersistent(self) -> int:  # noqa: N802
        return int(self.inactive is not None)

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        if flags == libvirt.VIR_DOMAIN_XML_INACTIVE and self.inactive is not None:
            return self.inactive
        return self.live


class Conn:
    def __init__(self, pools: list[Pool], events: list[str], domains: list[Domain] | None = None):
        self.pools = {pool.name: pool for pool in pools}
        self.events = events
        self.domains = domains or []

    def storagePoolLookupByName(self, name: str) -> Pool:  # noqa: N802
        self.events.append(f"pool:{name}")
        try:
            return self.pools[name]
        except KeyError:
            raise libvirt.libvirtError("missing") from None

    def listAllDomains(self, flags: int = 0) -> list[Domain]:  # noqa: N802
        self.events.append("domains")
        return self.domains


class IdentityPort:
    def __init__(
        self,
        aliases: dict[str, RemoteDeviceIdentity | None] | None = None,
        *,
        failure: Exception | None = None,
    ) -> None:
        self.aliases = aliases or {}
        self.failure = failure
        self.calls: list[str] = []

    def identity(self, path: str) -> RemoteDeviceIdentity | None:
        self.calls.append(path)
        if self.failure is not None:
            raise self.failure
        return self.aliases.get(path, RemoteDeviceIdentity("inode", 1, hash(path) & 0xFFFF_FFFF))


def _domain(*sources: str) -> str:
    return (
        "<domain><name>guest</name><devices><disk>"
        + "".join(sources)
        + "</disk></devices></domain>"
    )


def _source(*, path: str | None = None, pool: str | None = None, volume: str | None = None) -> str:
    attrs = []
    if path is not None:
        attrs.append(f"file='{path}'")
    if pool is not None:
        attrs.append(f"pool='{pool}'")
    if volume is not None:
        attrs.append(f"volume='{volume}'")
    return f"<source {' '.join(attrs)}/>"


def _subject(
    *,
    volumes: list[Volume] | None = None,
    domains: list[Domain] | None = None,
    retained: set[ModuleVolumeOwner] | None = None,
    identity: IdentityPort | None = None,
) -> tuple[Callable[[], int], list[str], IdentityPort]:
    events: list[str] = []
    volumes = volumes or [Volume(_name(), "/pool/source", events)]
    for volume in volumes:
        volume.events = events
    pool = Pool(POOL, volumes, events)
    conn = Conn([pool], events, domains)
    identity = identity or IdentityPort()

    def retained_owners() -> set[ModuleVolumeOwner]:
        events.append("retained")
        return retained or set()

    return (
        lambda: reap_orphaned_module_volumes(
            cast("ModuleVolumeReaperConn", conn), POOL, identity, retained_owners=retained_owners
        ),
        events,
        identity,
    )


def test_listing_recognizes_only_complete_owned_names() -> None:
    events: list[str] = []
    owned = Volume(_name(), "/owned", events)
    foreign = Volume("kdive-module-not-an-owner", "/foreign", events)
    conn = Conn([Pool(POOL, [owned, foreign], events)], events)
    assert list_owned_module_volumes(cast("ModuleVolumeReaperConn", conn), POOL) == [
        (_name(), _owner())
    ]


def test_retained_set_is_read_after_enumeration() -> None:
    run, events, _ = _subject(retained={_owner()})
    assert run() == 0
    assert events.index("retained") > events.index(f"enumerate:{POOL}")


def test_unrestored_attempt_keeps_both_volumes() -> None:
    events: list[str] = []
    volumes = [
        Volume(_name("source.ext4"), "/pool/source", events),
        Volume(_name("scratch.ext4"), "/pool/scratch", events),
    ]
    run, events, _ = _subject(volumes=volumes, retained={_owner("source.ext4")})
    assert run() == 0
    assert not any(event.startswith("delete:") for event in events)


def test_in_flight_reap_journal_is_kept() -> None:
    run, events, _ = _subject(
        volumes=[Volume(_name("reaping.journal"), "/pool/reaping", [])],
        retained={_owner("reaped.journal")},
    )
    assert run() == 0
    assert not any(event.startswith("delete:") for event in events)


@pytest.mark.parametrize("kind", ["reaped.journal", "source.ext4"])
def test_discharged_or_torn_down_attempt_is_reclaimed(kind: str) -> None:
    run, events, _ = _subject(volumes=[Volume(_name(kind), f"/pool/{kind}", [])])
    assert run() == 1
    assert any(event == f"delete:{_name(kind)}" for event in events)


def test_missing_on_delete_is_an_achieved_removal() -> None:
    run, _, _ = _subject(volumes=[Volume(_name(), "/pool/source", [], gone=True)])
    assert run() == 1


def test_attached_candidate_is_skipped_but_independent_candidate_is_deleted() -> None:
    events: list[str] = []
    attached = Volume(_name("source.ext4"), "/pool/attached", events)
    free = Volume(_name("scratch.ext4"), "/pool/free", events)
    identity = RemoteDeviceIdentity("inode", 7, 9)
    port = IdentityPort({"/pool/attached": identity, "/alias": identity})
    run, events, _ = _subject(
        volumes=[attached, free], domains=[Domain(_domain(_source(path="/alias")))], identity=port
    )
    assert run() == 1
    assert f"delete:{attached.name()}" not in events
    assert f"delete:{free.name()}" in events


def test_active_and_inactive_nested_disk_graph_references_are_protected() -> None:
    events: list[str] = []
    active = Volume(_name("source.ext4"), "/pool/active", events)
    inactive = Volume(_name("scratch.ext4"), "/pool/inactive", events)
    live_xml = _domain("<backingStore>" + _source(path="/alias-active") + "</backingStore>")
    old_xml = _domain("<dataStore>" + _source(path="/alias-inactive") + "</dataStore>")
    first = RemoteDeviceIdentity("inode", 8, 1)
    second = RemoteDeviceIdentity("inode", 8, 2)
    port = IdentityPort(
        {
            "/pool/active": first,
            "/alias-active": first,
            "/pool/inactive": second,
            "/alias-inactive": second,
        }
    )
    run, events, _ = _subject(
        volumes=[active, inactive], domains=[Domain(live_xml, old_xml)], identity=port
    )
    assert run() == 0
    assert not any(event.startswith("delete:") for event in events)


def test_active_and_legacy_mirror_paths_are_protected() -> None:
    events: list[str] = []
    volume = Volume(_name(), "/pool/source", events)
    ident = RemoteDeviceIdentity("inode", 9, 1)
    xml = _domain("<mirror file='/mirror'/><mirror><source file='/legacy'/></mirror>")
    port = IdentityPort({"/pool/source": ident, "/mirror": ident, "/legacy": ident})
    run, events, _ = _subject(volumes=[volume], domains=[Domain(xml)], identity=port)
    assert run() == 0
    assert not any(event.startswith("delete:") for event in events)


def test_named_volume_reference_is_resolved_and_protected() -> None:
    events: list[str] = []
    volume = Volume(_name(), "/pool/source", events)
    run, events, _ = _subject(
        volumes=[volume], domains=[Domain(_domain(_source(pool=POOL, volume=_name())))]
    )
    assert run() == 0
    assert not any(event.startswith("delete:") for event in events)


def test_named_reference_in_another_pool_protects_a_managed_alias() -> None:
    events: list[str] = []
    candidate = Volume(_name(), "/modules/candidate", events)
    foreign = Volume("root", "/systems/root", events)
    module_pool = Pool(POOL, [candidate], events)
    system_pool = Pool("systems", [foreign], events)
    conn = Conn(
        [module_pool, system_pool],
        events,
        [Domain(_domain(_source(pool="systems", volume="root")))],
    )
    identity = RemoteDeviceIdentity("inode", 10, 1)
    port = IdentityPort({"/modules/candidate": identity, "/systems/root": identity})

    removed = reap_orphaned_module_volumes(
        cast("ModuleVolumeReaperConn", conn), POOL, port, retained_owners=set
    )

    assert removed == 0
    assert f"delete:{candidate.name()}" not in events


def test_unresolvable_disk_reference_suppresses_all_deletions() -> None:
    run, events, _ = _subject(domains=[Domain(_domain(_source(pool="missing", volume="missing")))])
    with pytest.raises(CategorizedError) as caught:
        run()
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert not any(event.startswith("delete:") for event in events)


@pytest.mark.parametrize("returned", [None, object()])
def test_absent_or_malformed_identity_fails_closed_before_delete(returned: Any) -> None:
    port = IdentityPort({"/pool/source": returned})
    run, events, _ = _subject(identity=port)
    with pytest.raises(CategorizedError) as caught:
        run()
    assert caught.value.category is ErrorCategory.CONFLICT
    assert not any(event.startswith("delete:") for event in events)


def test_operational_identity_failure_is_redacted_infrastructure_failure() -> None:
    run, events, _ = _subject(identity=IdentityPort(failure=TimeoutError("host secret")))
    with pytest.raises(CategorizedError) as caught:
        run()
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "host secret" not in str(caught.value)
    assert caught.value.details == {"pool": POOL, "volume": _name()}
    assert not any(event.startswith("delete:") for event in events)


def test_normalized_aliases_consume_one_lookup_and_do_not_delete() -> None:
    run, events, port = _subject(domains=[Domain(_domain(_source(path="/pool/./source")))])
    assert run() == 0
    assert port.calls == ["/pool/source"]
    assert not any(event.startswith("delete:") for event in events)


def test_two_candidates_with_one_normalized_path_are_both_protected() -> None:
    events: list[str] = []
    first = Volume(_name("source.ext4"), "/pool/./shared", events)
    second = Volume(_name("scratch.ext4"), "/pool/shared", events)
    run, events, port = _subject(
        volumes=[first, second], domains=[Domain(_domain(_source(path="/pool/shared")))]
    )
    assert run() == 0
    assert port.calls == ["/pool/shared"]
    assert not any(event.startswith("delete:") for event in events)


@pytest.mark.parametrize("count, succeeds", [(4096, True), (4097, False)])
def test_whole_host_identity_budget(count: int, succeeds: bool) -> None:
    sources = "".join(_source(path=f"/refs/{index}") for index in range(count - 1))
    run, events, port = _subject(domains=[Domain(_domain(sources))])
    if succeeds:
        assert run() == 1
        assert len(port.calls) == count
    else:
        with pytest.raises(CategorizedError) as caught:
            run()
        assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert "refs" not in str(caught.value)
        assert port.calls == []
        assert not any(event.startswith("delete:") for event in events)


def test_referenced_volume_paths_includes_direct_and_named_paths() -> None:
    events: list[str] = []
    named = Volume("named", "/named/path", events)
    conn = Conn(
        [Pool(POOL, [named], events)],
        events,
        [Domain(_domain(_source(path="/direct"), _source(pool=POOL, volume="named")))],
    )
    assert referenced_volume_paths(cast("ModuleVolumeReaperConn", conn)) == frozenset(
        {"/direct", "/named/path"}
    )
