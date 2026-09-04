"""Fail-closed remote module attachment inspection."""

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    ExpectedAppliance,
    ExpectedAttachmentState,
    inspect_module_attachments,
)
from kdive.providers.shared.libvirt_xml import KDIVE_METADATA_NS


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
    def __init__(self, domains: list[Domain]) -> None:
        self.domains = domains

    def listAllDomains(self, flags: int = 0) -> list[Domain]:  # noqa: N802
        return self.domains


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
    domains = [
        Domain(system_xml(state.system_id)),
        Domain(
            system_xml(other, volume="unrelated"),
            active=True,
            inactive_xml=system_xml(other, volume=state.root_volume),
        ),
    ]

    with pytest.raises(CategorizedError, match="another domain"):
        inspect_module_attachments(Conn(domains), state)


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
        protected_paths=frozenset({"/var/lib/libvirt/images/systems/root.qcow2"}),
    )
    tenant = foreign_xml(
        "tenant",
        f"<disk type='file'><source {attribute}="
        "'/var/lib/libvirt/images/systems/root.qcow2'/></disk>",
        active=active,
    )
    with pytest.raises(CategorizedError) as raised:
        inspect_module_attachments(Conn([Domain(system_xml(state.system_id)), tenant]), state)
    assert raised.value.category is ErrorCategory.CONFLICT
    assert "by path" in str(raised.value)


def test_unprotected_path_reference_is_ignored() -> None:
    state = expected()
    tenant = foreign_xml("tenant", "<disk type='file'><source file='/srv/other.qcow2'/></disk>")
    result = inspect_module_attachments(Conn([Domain(system_xml(state.system_id)), tenant]), state)
    assert result.exclusive
