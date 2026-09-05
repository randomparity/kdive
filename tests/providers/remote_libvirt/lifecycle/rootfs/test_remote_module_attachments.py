"""Fail-closed remote module attachment inspection."""

import posixpath
import subprocess
import sys
from pathlib import Path

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    ExpectedAppliance,
    ExpectedAttachmentState,
    HostStatDeviceIdentity,
    RemoteDeviceIdentity,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    inspect_module_attachments as _inspect_module_attachments,
)
from kdive.providers.remote_libvirt.lifecycle.storage import render_volume_xml
from kdive.providers.shared.libvirt_xml import KDIVE_METADATA_NS
from tests.providers.remote_libvirt.fakes import FakeStoragePool, libvirt_error


class Domain:
    def __init__(self, xml: str, active: bool = False, *, inactive_xml: str | None = None) -> None:
        self.xml = xml
        self.active = active
        self.inactive_xml = inactive_xml

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        import libvirt

        if flags == libvirt.VIR_DOMAIN_XML_INACTIVE and self.inactive_xml is not None:
            return self.inactive_xml
        return self.xml

    def isActive(self) -> int:  # noqa: N802
        return int(self.active)

    def isPersistent(self) -> int:  # noqa: N802
        return int(self.inactive_xml is not None)


class Conn:
    def __init__(
        self, domains: list[Domain], pools: dict[str, FakeStoragePool] | None = None
    ) -> None:
        self.domains = domains
        self.pools = pools or {"systems": storage_pool()}

    def listAllDomains(self, flags: int = 0) -> list[Domain]:  # noqa: N802
        return self.domains

    def storagePoolLookupByName(self, name: str) -> FakeStoragePool:  # noqa: N802
        try:
            return self.pools[name]
        except KeyError as exc:
            raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_POOL) from exc

    def storageVolLookupByPath(self, path: str):  # noqa: N802
        normalized = posixpath.normpath(path)
        for pool in self.pools.values():
            for name in pool.listVolumes():
                volume = pool.storageVolLookupByName(name)
                if posixpath.normpath(volume.path()) == normalized:
                    return volume
        raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL)


class IdentityPort:
    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self.aliases = aliases or {}
        self.identities: dict[str, RemoteDeviceIdentity] = {}

    def identity(self, path: str) -> RemoteDeviceIdentity | None:
        canonical = self.aliases.get(posixpath.normpath(path), posixpath.normpath(path))
        return self.identities.setdefault(canonical, RemoteDeviceIdentity(1, len(self.identities)))


def inspect_module_attachments(
    conn: Conn,
    state: ExpectedAttachmentState,
    identity_port: IdentityPort | HostStatDeviceIdentity | None = None,
):
    return _inspect_module_attachments(conn, identity_port or IdentityPort(), state)


def storage_pool(*, name: str = "systems", target_path: str = "/pool") -> FakeStoragePool:
    pool = FakeStoragePool(name=name, target_path=target_path)
    for volume in ("root", "source", "scratch"):
        pool.createXML(render_volume_xml(volume, capacity_bytes=1024, backing_path="/base"))
    return pool


def system_xml(system: str, *, volume: str = "root", arch: str = "x86_64") -> str:
    return f"""<domain><name>kdive-{system}</name><os><type arch='{arch}'>hvm</type></os>
      <metadata><system xmlns='{KDIVE_METADATA_NS}'>{system}</system></metadata><devices>
      <disk><source pool='systems' volume='{volume}'/></disk></devices></domain>"""


def expected(arch: str = "x86_64") -> ExpectedAttachmentState:
    return ExpectedAttachmentState(
        system_id="00000000-0000-4000-8000-000000000001",
        pool="systems",
        root_volume="root",
        source_volume="source",
        scratch_volume="scratch",
        appliance=ExpectedAppliance(
            name="kdive-module-appliance-1",
            architecture=arch,
            image_digest="sha256:" + "e" * 64,
            operation_nonce="a" * 32,
        ),
    )


def appliance_xml(state: ExpectedAttachmentState, **metadata: str) -> str:
    values = {
        "system": state.system_id,
        "image-digest": state.appliance.image_digest,
        "nonce": state.appliance.operation_nonce,
        **metadata,
    }
    attrs = " ".join(f"{key}='{value}'" for key, value in values.items())
    disks = "".join(
        f"<disk type='volume' device='disk'><source pool='{state.pool}' volume='{volume}'/>"
        f"<target dev='{alias}' bus='virtio'/>"
        f"{'<readonly/>' if alias == 'vdb' else ''}</disk>"
        for volume, alias in (
            (state.root_volume, "vda"),
            (state.source_volume, "vdb"),
            (state.scratch_volume, "vdc"),
        )
    )
    return (
        f"<domain><name>{state.appliance.name}</name><os><type "
        f"arch='{state.appliance.architecture}'>hvm</type></os><metadata>"
        f"<remote-module-appliance {attrs}/></metadata><devices>{disks}"
        "<console type='pty'/></devices></domain>"
    )


@pytest.mark.parametrize("arch", ["x86_64", "ppc64le"])
def test_stopped_exclusive_owner_is_safe(arch: str) -> None:
    state = expected(arch)
    domains = Conn([Domain(system_xml(state.system_id, arch=arch))])
    result = inspect_module_attachments(domains, state)
    assert result.system_shut_off
    assert result.exclusive
    assert not result.appliance_present
    assert (state.pool, state.source_volume) in result.detached_volumes
    assert (state.pool, state.scratch_volume) in result.detached_volumes


@pytest.mark.parametrize(
    "domains",
    [
        [Domain(system_xml(expected().system_id), active=True)],
        [Domain(system_xml(expected().system_id)), Domain(system_xml("other"))],
        [
            Domain(
                system_xml(expected().system_id).replace(
                    "</devices>",
                    "<disk><source pool='systems' volume='root'/></disk></devices>",
                )
            )
        ],
        [Domain("<domain")],
        [Domain(system_xml(expected().system_id)), Domain(system_xml("other", volume="source"))],
        [Domain(system_xml(expected().system_id)), Domain(system_xml("other", volume="scratch"))],
    ],
)
def test_unsafe_or_unreadable_attachment_fails_closed(domains: list[Domain]) -> None:
    with pytest.raises(CategorizedError) as caught:
        inspect_module_attachments(Conn(domains), expected())
    assert caught.value.category is ErrorCategory.CONFLICT


def test_matching_active_appliance_is_the_only_allowed_second_owner() -> None:
    state = expected()
    domains = [
        Domain(system_xml(state.system_id)),
        Domain(appliance_xml(state), active=True),
    ]
    assert inspect_module_attachments(Conn(domains), state).appliance_present


def test_mismatching_resumed_appliance_fails_closed() -> None:
    state = expected()
    domains = [
        Domain(system_xml(state.system_id)),
        Domain(appliance_xml(state, nonce="b" * 32), active=True),
    ]
    with pytest.raises(CategorizedError) as caught:
        inspect_module_attachments(Conn(domains), state)
    assert caught.value.category is ErrorCategory.CONFLICT


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("dev='vdb'", "dev='vdd'"),
        ("<readonly/>", ""),
        ("dev='vda'", "dev='vdb'"),
        ("type='volume'", "type='file'"),
        ("device='disk'", "device='cdrom'"),
        ("bus='virtio'", "bus='sata'"),
    ],
)
def test_resumed_appliance_requires_exact_aliases_and_readonly_source(old: str, new: str) -> None:
    state = expected()
    domains = [
        Domain(system_xml(state.system_id)),
        Domain(appliance_xml(state).replace(old, new), active=True),
    ]
    with pytest.raises(CategorizedError, match="volume set"):
        inspect_module_attachments(Conn(domains), state)


@pytest.mark.parametrize(
    "mutation",
    [
        "<auth username='secret'/>",
        "<backingStore/>",
        "<source pool='extra' volume='extra'/>",
        "<serial>unexpected</serial>",
    ],
)
def test_resumed_appliance_rejects_non_allowlisted_disk_xml(mutation: str) -> None:
    state = expected()
    appliance = appliance_xml(state).replace("<target dev='vda'", mutation + "<target dev='vda'")
    domains = [Domain(system_xml(state.system_id)), Domain(appliance, active=True)]

    with pytest.raises(CategorizedError, match="volume set"):
        inspect_module_attachments(Conn(domains), state)


def test_active_domain_persistent_definition_is_also_scanned() -> None:
    state = expected()
    other = "00000000-0000-4000-8000-000000000099"
    pool = storage_pool()
    pool.createXML(render_volume_xml("unrelated", capacity_bytes=1024, backing_path="/base"))
    domains = [
        Domain(system_xml(state.system_id)),
        Domain(
            system_xml(other, volume="unrelated"),
            active=True,
            inactive_xml=system_xml(other, volume=state.root_volume),
        ),
    ]

    with pytest.raises(CategorizedError, match="another domain"):
        inspect_module_attachments(Conn(domains, {"systems": pool}), state)


def test_inactive_owner_definition_still_observes_active_domain() -> None:
    state = expected()
    live_xml = foreign_xml("live-owner", "<disk type='file'><source file='/other'/></disk>").xml
    domain = Domain(live_xml, active=True, inactive_xml=system_xml(state.system_id))

    with pytest.raises(CategorizedError, match="owning System is active"):
        inspect_module_attachments(Conn([domain]), state)


def test_active_appliance_rejects_identical_persistent_definition() -> None:
    state = expected()
    xml = appliance_xml(state)
    domains = [
        Domain(system_xml(state.system_id)),
        Domain(xml, active=True, inactive_xml=xml),
    ]

    with pytest.raises(CategorizedError, match="not active"):
        inspect_module_attachments(Conn(domains), state)


@pytest.mark.parametrize("duplicate", ["same", "foreign"])
def test_duplicate_system_metadata_fails_closed(duplicate: str) -> None:
    state = expected()
    value = state.system_id if duplicate == "same" else "00000000-0000-4000-8000-000000000099"
    xml = system_xml(state.system_id).replace(
        "</metadata>",
        f"<system xmlns='{KDIVE_METADATA_NS}'>{value}</system></metadata>",
    )
    with pytest.raises(CategorizedError, match="duplicate System"):
        inspect_module_attachments(Conn([Domain(xml)]), state)


@pytest.mark.parametrize("duplicate", ["same", "foreign"])
def test_duplicate_appliance_metadata_fails_closed(duplicate: str) -> None:
    state = expected()
    nonce = state.appliance.operation_nonce if duplicate == "same" else "b" * 32
    extra = (
        f"<remote-module-appliance system='{state.system_id}' "
        f"image-digest='{state.appliance.image_digest}' nonce='{nonce}'/>"
    )
    xml = appliance_xml(state).replace("</metadata>", extra + "</metadata>")
    domains = [Domain(system_xml(state.system_id)), Domain(xml, active=True)]
    with pytest.raises(CategorizedError, match="metadata is absent"):
        inspect_module_attachments(Conn(domains), state)


def foreign_xml(name: str, sources: str, *, active: bool = False) -> Domain:
    return Domain(
        f"<domain><name>{name}</name><os><type arch='x86_64'>hvm</type></os>"
        f"<devices>{sources}</devices></domain>",
        active=active,
    )


def test_unrelated_two_disk_vm_does_not_block_inspection() -> None:
    """A file-backed disk carries no pool/volume, so two of them are not a duplicate."""
    state = expected()
    tenant = foreign_xml(
        "tenant",
        "<disk type='file'><source file='/srv/a.qcow2'/></disk>"
        "<disk type='file'><source file='/srv/b.qcow2'/></disk>",
        active=True,
    )
    result = inspect_module_attachments(Conn([Domain(system_xml(state.system_id)), tenant]), state)
    assert result.system_shut_off
    assert result.exclusive


def test_repeated_protected_volume_reference_is_still_a_duplicate() -> None:
    state = expected()
    tenant = foreign_xml(
        "tenant",
        f"<disk><source pool='{state.pool}' volume='{state.root_volume}'/></disk>"
        f"<disk><source pool='{state.pool}' volume='{state.root_volume}'/></disk>",
    )
    with pytest.raises(CategorizedError) as raised:
        inspect_module_attachments(Conn([Domain(system_xml(state.system_id)), tenant]), state)
    assert raised.value.category is ErrorCategory.CONFLICT
    assert "duplicate volume reference" in str(raised.value)


@pytest.mark.parametrize("attribute", ["file", "dev"])
@pytest.mark.parametrize("active", [True, False])
def test_path_referenced_protected_volume_is_rejected(attribute: str, active: bool) -> None:
    """ADR-0585 root exclusivity holds against a co-tenant naming the image by path."""
    state = ExpectedAttachmentState(
        system_id="00000000-0000-4000-8000-000000000001",
        pool="systems",
        root_volume="root",
        source_volume="source",
        scratch_volume="scratch",
        appliance=ExpectedAppliance(
            name="kdive-module-appliance-1",
            architecture="x86_64",
            image_digest="sha256:" + "e" * 64,
            operation_nonce="a" * 32,
        ),
    )
    tenant = foreign_xml(
        "tenant",
        f"<disk type='file'><source {attribute}='/pool/root'/></disk>",
        active=active,
    )
    with pytest.raises(CategorizedError) as raised:
        inspect_module_attachments(Conn([Domain(system_xml(state.system_id)), tenant]), state)
    assert raised.value.category is ErrorCategory.CONFLICT
    assert "by path" in str(raised.value)


@pytest.mark.parametrize("attribute", ["file", "dev"])
@pytest.mark.parametrize("active", [True, False])
def test_lexical_path_alias_of_protected_volume_is_rejected(attribute: str, active: bool) -> None:
    state = expected()
    tenant = foreign_xml(
        "tenant",
        f"<disk type='file'><source {attribute}='/pool/../pool/root'/></disk>",
        active=active,
    )

    with pytest.raises(CategorizedError, match="by path"):
        inspect_module_attachments(Conn([Domain(system_xml(state.system_id)), tenant]), state)


def test_managed_volume_lookup_error_is_infrastructure_failure() -> None:
    class FailingConn(Conn):
        def storagePoolLookupByName(self, name: str) -> FakeStoragePool:  # noqa: N802
            raise libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR)

    state = expected()
    tenant = foreign_xml("tenant", "<disk type='file'><source file='/srv/unmanaged'/></disk>")

    with pytest.raises(CategorizedError, match="could not resolve") as raised:
        inspect_module_attachments(
            FailingConn([Domain(system_xml(state.system_id)), tenant]), state
        )
    assert raised.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_unprotected_path_reference_is_ignored() -> None:
    state = expected()
    tenant = foreign_xml("tenant", "<disk type='file'><source file='/srv/other.qcow2'/></disk>")
    result = inspect_module_attachments(Conn([Domain(system_xml(state.system_id)), tenant]), state)
    assert result.exclusive


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_real_host_alias_has_protected_device_identity(tmp_path: Path, alias_kind: str) -> None:
    pool_path = tmp_path / "pool"
    pool_path.mkdir()
    for volume in ("root", "source", "scratch"):
        (pool_path / volume).write_bytes(volume.encode())
    protected = pool_path / "root"
    alias = tmp_path / "alias"
    if alias_kind == "symlink":
        alias.symlink_to(protected)
    else:
        alias.hardlink_to(protected)
    state = expected()
    tenant = foreign_xml("tenant", f"<disk type='file'><source file='{alias}'/></disk>")

    with pytest.raises(CategorizedError, match="by path"):
        inspect_module_attachments(
            Conn(
                [Domain(system_xml(state.system_id)), tenant],
                {state.pool: storage_pool(target_path=str(pool_path))},
            ),
            state,
            HostStatDeviceIdentity(),
        )


def test_real_host_bind_alias_has_protected_device_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    alias = tmp_path / "alias"
    source.mkdir()
    alias.mkdir()
    probe = subprocess.run(  # noqa: S603
        ["unshare", "--user", "--map-root-user", "--mount", "true"],  # noqa: S607
        check=False,
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip("unprivileged mount namespaces are unavailable on this host")
    script = (
        "import sys; from kdive.providers.remote_libvirt.lifecycle.rootfs."
        "remote_module_attachments import HostStatDeviceIdentity; "
        "p=HostStatDeviceIdentity(); assert p.identity(sys.argv[1]) == p.identity(sys.argv[2])"
    )
    subprocess.run(  # noqa: S603
        [
            "unshare",
            "--user",
            "--map-root-user",
            "--mount",
            "sh",
            "-c",
            'mount --bind "$1" "$2" && exec "$3" -c "$4" "$1" "$2"',
            "bind-identity-test",
            str(source),
            str(alias),
            sys.executable,
            script,
        ],
        check=True,
    )


def test_physical_alias_of_protected_volume_is_rejected() -> None:
    state = expected()
    tenant = foreign_xml("tenant", "<disk type='file'><source file='/alias/root'/></disk>")
    identities = IdentityPort({"/alias/root": "/pool/root"})

    with pytest.raises(CategorizedError, match="by path"):
        inspect_module_attachments(
            Conn([Domain(system_xml(state.system_id)), tenant]), state, identities
        )


def test_unavailable_device_identity_fails_closed_without_path_detail() -> None:
    class MissingIdentity(IdentityPort):
        def identity(self, path: str) -> None:
            return None

    state = expected()
    with pytest.raises(CategorizedError, match="identity is unavailable") as raised:
        inspect_module_attachments(
            Conn([Domain(system_xml(state.system_id))]), state, MissingIdentity()
        )
    assert raised.value.category is ErrorCategory.CONFLICT
    assert "/pool" not in str(raised.value)


def test_identity_operational_failure_is_redacted_infrastructure_failure() -> None:
    class FailingIdentity(IdentityPort):
        def identity(self, path: str) -> RemoteDeviceIdentity:
            raise OSError("private-host-path")

    state = expected()
    with pytest.raises(CategorizedError, match="identity lookup failed") as raised:
        inspect_module_attachments(
            Conn([Domain(system_xml(state.system_id))]), state, FailingIdentity()
        )
    assert raised.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert "private-host-path" not in str(raised.value)


def test_volume_reference_through_alias_pool_is_rejected() -> None:
    state = expected()
    pools = {
        state.pool: storage_pool(name=state.pool),
        "alias": storage_pool(name="alias"),
    }
    tenant = foreign_xml(
        "tenant",
        f"<disk type='volume'><source pool='alias' volume='{state.root_volume}'/></disk>",
    )

    with pytest.raises(CategorizedError, match="by path"):
        inspect_module_attachments(
            Conn([Domain(system_xml(state.system_id)), tenant], pools), state
        )


def test_unresolvable_volume_reference_fails_closed() -> None:
    state = expected()
    tenant = foreign_xml(
        "tenant",
        "<disk type='volume'><source pool='missing' volume='root'/></disk>",
    )

    with pytest.raises(CategorizedError, match="could not resolve") as raised:
        inspect_module_attachments(Conn([Domain(system_xml(state.system_id)), tenant]), state)
    assert raised.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


@pytest.mark.parametrize(("attribute", "volume"), [("file", "source"), ("dev", "scratch")])
def test_owning_system_path_reference_to_attempt_volume_is_rejected(
    attribute: str, volume: str
) -> None:
    state = expected()
    xml = system_xml(state.system_id).replace(
        "</devices>",
        f"<disk type='file'><source {attribute}='/pool/{volume}'/></disk></devices>",
    )

    with pytest.raises(CategorizedError, match="attempt-scoped"):
        inspect_module_attachments(Conn([Domain(xml)]), state)


@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize(
    "disk",
    [
        "<disk type='file'><source file='/overlay'/>"
        "<backingStore type='file'><source file='/pool/root'/><format type='raw'/>"
        "<backingStore/></backingStore></disk>",
        "<disk type='file'><source file='/overlay'><dataStore>"
        "<format type='raw'/><source file='/pool/root'/></dataStore></source></disk>",
        "<disk type='file'><source file='/overlay'/><mirror job='copy' type='file'>"
        "<source file='/pool/root'/><format type='raw'/><backingStore/></mirror></disk>",
        "<disk type='file'><source file='/overlay'/><mirror file='/pool/root' job='copy'/></disk>",
    ],
)
def test_nested_protected_storage_source_is_rejected(disk: str, active: bool) -> None:
    state = expected()
    tenant = foreign_xml("tenant", disk, active=active)

    with pytest.raises(CategorizedError, match="by path"):
        inspect_module_attachments(Conn([Domain(system_xml(state.system_id)), tenant]), state)


@pytest.mark.parametrize(
    "nested",
    [
        "<backingStore type='volume'><source pool='systems' volume='root'/>"
        "<format type='raw'/><backingStore/></backingStore>",
        "<source file='/overlay'><dataStore><format type='raw'/>"
        "<source pool='systems' volume='root'/></dataStore></source>",
        "<mirror job='copy' type='volume'><source pool='systems' volume='root'/>"
        "<format type='raw'/><backingStore/></mirror>",
    ],
)
def test_nested_root_volume_does_not_satisfy_owning_root_identity(nested: str) -> None:
    state = expected()
    xml = system_xml(state.system_id).replace(
        "<disk><source pool='systems' volume='root'/></disk>",
        f"<disk type='file'><source file='/overlay'/>{nested}</disk>",
    )

    with pytest.raises(CategorizedError, match="different root volume"):
        inspect_module_attachments(Conn([Domain(xml)]), state)
