"""Contract tests for the remote-libvirt external-boot activation primitives (#2110)."""

from __future__ import annotations

import xml.etree.ElementTree as ET  # noqa: S405 - test-owned XML
from collections.abc import Callable
from typing import Any
from uuid import UUID

import libvirt
import pytest
from pydantic import ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.ports.external_boot import (
    ActivationOwnership,
    BundleSource,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    InitrdSource,
    MaterializedArtifacts,
    ModuleObligation,
    OpaqueProviderRef,
    PlanOwnership,
    RootSource,
    RootSpecV1,
    RunningKernelObservation,
)
from kdive.providers.remote_libvirt.guest.agent import AgentExecResult
from kdive.providers.remote_libvirt.lifecycle.external_boot import (
    MAX_DEFINITION_BYTES,
    MAX_GUEST_READ_BYTES,
    RemoteExternalBootDefinition,
    activate_definition,
    boot_projection_identity,
    observe_guest_identity,
    prepare_target_definition,
    preserved_definition_identity,
    render_target_xml,
    require_disk_grub_source,
)
from kdive.providers.remote_libvirt.lifecycle.xml import overlay_volume_name, render_domain_xml
from kdive.providers.shared.runtime_paths import domain_name_for

_SYSTEM_ID = UUID("00000000-0000-0000-0000-00000000beef")
_OTHER_SYSTEM_ID = UUID("00000000-0000-0000-0000-0000000000aa")

# ADR-0583's three published golden vectors. These are normative: the two identity algorithms
# exist to reproduce them, and local-libvirt carries a second copy of the same algorithm (#2159).
_GOLDEN_SOURCE = '<domain><os><type arch="x86_64">hvm</type></os></domain>'
_GOLDEN_PRESERVED = "sha256:3e3cde0b5115867e991160f1d361fef3ec0734e8a87e2ab003d62cc0f8af4eea"
_GOLDEN_NULL_BOOT = "sha256:c48b5e5a6e9ac64b1129c1d468ce0de305288a86a6575467fb15f71d3c14b925"
_GOLDEN_UNICODE_BOOT = "sha256:06bf5b2aceb13f19b7debd17181ada54041d883f926c9c5f4c0acae4336f58fb"


def _remote_profile(**section_overrides: Any) -> ProvisioningProfile:
    section: dict[str, Any] = {
        "base_image_volume": "kdive-base-fedora-42.qcow2",
        "crashkernel": "256M",
        **section_overrides,
    }
    return ProvisioningProfile.parse(
        {
            "schema_version": 1,
            "arch": "x86_64",
            "vcpu": 4,
            "memory_mb": 4096,
            "disk_gb": 20,
            "boot_method": "disk-image",
            "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
            "provider": {"remote-libvirt": section},
        }
    )


def _source_xml(
    *, system_id: UUID = _SYSTEM_ID, pool: str = "kdive", volume: str | None = None
) -> str:
    """The provisioned disk/GRUB baseline a remote System actually carries."""
    return render_domain_xml(
        system_id,
        _remote_profile(),
        pool=pool,
        volume=volume if volume is not None else overlay_volume_name(system_id),
        gdb_addr="10.0.0.5",
        gdb_port=1234,
        ssh_addr="10.0.0.5",
        ssh_port=2222,
    )


def test_preserved_identity_matches_the_adr_golden_vector() -> None:
    assert preserved_definition_identity(_GOLDEN_SOURCE) == _GOLDEN_PRESERVED


def test_all_null_boot_projection_matches_the_adr_golden_vector() -> None:
    assert boot_projection_identity(_GOLDEN_SOURCE) == _GOLDEN_NULL_BOOT


def test_non_ascii_boot_projection_matches_the_adr_golden_vector() -> None:
    projected = render_target_xml(
        _GOLDEN_SOURCE,
        kernel="/var/lib/kdive/café",
        initrd=None,
        cmdline="root=LABEL=café",
    )
    assert boot_projection_identity(projected) == _GOLDEN_UNICODE_BOOT


def test_projection_preserves_every_remote_device_and_the_preserved_digest() -> None:
    source = _source_xml()
    projected = render_target_xml(
        source, kernel="kernel.img", initrd="initrd.img", cmdline="root=/dev/vda1 console=ttyS0"
    )
    for fragment in (
        '<disk type="volume" device="disk">',
        '<driver name="qemu" type="qcow2" />',
        f'<source pool="kdive" volume="{overlay_volume_name(_SYSTEM_ID)}" />',
        '<target dev="vda" bus="virtio" />',
        '<interface type="network">',
        '<serial type="pty">',
        '<console type="pty">',
        'name="org.qemu.guest_agent.0"',
        '<vmcoreinfo state="on" />',
        'value="-gdb"',
        'value="tcp:10.0.0.5:1234"',
        "hostfwd=tcp:10.0.0.5:2222-:22",
        f"<kdive:system>{_SYSTEM_ID}</kdive:system>",
    ):
        assert fragment in projected, fragment
    assert preserved_definition_identity(projected) == preserved_definition_identity(source)
    assert boot_projection_identity(projected) != boot_projection_identity(source)
    assert "<kernel>kernel.img</kernel>" in projected
    assert "<initrd>initrd.img</initrd>" in projected
    assert "<cmdline>root=/dev/vda1 console=ttyS0</cmdline>" in projected
    assert '<boot dev="hd" />' in projected


def test_projection_omits_the_initrd_element_when_no_initrd_is_supplied() -> None:
    projected = render_target_xml(_GOLDEN_SOURCE, kernel="k", initrd=None, cmdline="c")
    assert "<initrd>" not in projected


def test_projection_replaces_rather_than_duplicates_existing_boot_fields() -> None:
    once = render_target_xml(_GOLDEN_SOURCE, kernel="k1", initrd="i1", cmdline="c1")
    twice = render_target_xml(once, kernel="k2", initrd="i2", cmdline="c2")
    assert twice.count("<kernel>") == 1
    assert twice.count("<initrd>") == 1
    assert twice.count("<cmdline>") == 1
    assert "k1" not in twice


def test_projection_creates_the_os_element_when_the_source_has_none() -> None:
    projected = render_target_xml(
        "<domain><name>d</name></domain>", kernel="k", initrd=None, cmdline="c"
    )
    assert "<os><kernel>k</kernel><cmdline>c</cmdline></os>" in projected


@pytest.mark.parametrize(
    ("source", "category"),
    [
        ("<domain>", ErrorCategory.INFRASTRUCTURE_FAILURE),
        (
            '<!DOCTYPE d [<!ENTITY x "y">]><domain><name>&x;</name></domain>',
            ErrorCategory.INFRASTRUCTURE_FAILURE,
        ),
        ("<not-a-domain />", ErrorCategory.CONFLICT),
        # Deliberately decomposed (e + U+0301), so the NFC guard is what rejects it.
        ("<domain><name>caf\u0065\u0301</name></domain>", ErrorCategory.CONFLICT),
    ],
    ids=["malformed", "entity", "wrong-root", "non-nfc"],
)
def test_projection_rejects_malformed_forbidden_or_non_nfc_sources(
    source: str, category: ErrorCategory
) -> None:
    with pytest.raises(CategorizedError) as caught:
        render_target_xml(source, kernel="k", initrd=None, cmdline="c")
    assert caught.value.category is category


def test_admission_accepts_the_provisioned_disk_grub_baseline() -> None:
    require_disk_grub_source(_source_xml(), system_id=_SYSTEM_ID, pool="kdive")


@pytest.mark.parametrize(
    ("mutate", "rule"),
    [
        (
            lambda xml: render_target_xml(xml, kernel="k", initrd=None, cmdline="c"),
            "boot-projection",
        ),
        (lambda xml: xml.replace(str(_SYSTEM_ID), str(_OTHER_SYSTEM_ID)), "system-metadata"),
        (lambda xml: xml.replace('pool="kdive"', 'pool="other"'), "boot-disk"),
        (lambda xml: xml.replace('dev="vda"', 'dev="sda"'), "boot-disk"),
        (lambda xml: xml.replace('type="qcow2"', 'type="raw"'), "boot-disk"),
        (
            lambda xml: xml.replace('<boot dev="hd" />', '<boot dev="hd" /><boot dev="network" />'),
            "boot-selection",
        ),
        (lambda xml: xml.replace("<os>", '<os firmware="efi">'), "firmware"),
        (lambda xml: xml.replace("<os>", "<os><loader>/x</loader>"), "firmware"),
    ],
    ids=[
        "external-boot-fields",
        "other-system",
        "wrong-pool",
        "wrong-target-dev",
        "wrong-driver-type",
        "extra-boot-selection",
        "firmware-attribute",
        "loader-child",
    ],
)
def test_admission_rejects_a_source_that_is_not_the_owned_baseline(
    mutate: Callable[[str], str], rule: str
) -> None:
    with pytest.raises(CategorizedError) as caught:
        require_disk_grub_source(mutate(_source_xml()), system_id=_SYSTEM_ID, pool="kdive")
    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details["rule"] == rule


_RUN_ID = "00000000-0000-0000-0000-000000000001"
_OTHER_RUN_ID = "00000000-0000-0000-0000-000000009999"
_ACTIVATION_ID = "00000000-0000-0000-0000-000000000a01"
_BUILD_GENERATION = "00000000-0000-0000-0000-000000000b01"
_SHA = "sha256:" + "11" * 32
_INITRD_SHA = "sha256:" + "22" * 32
_MANIFEST = "sha256:" + "33" * 32
_TREE = "sha256:" + "44" * 32
_ROOT_IDENTITY = "sha256:" + "55" * 32
_KERNEL_PATH = "/var/lib/kdive/boot/kernel.img"
_INITRD_PATH = "/var/lib/kdive/boot/initrd.img"


def _observation() -> RunningKernelObservation:
    return RunningKernelObservation(
        architecture="x86_64", release="6.9.0-kdive", gnu_build_id="ab" * 8
    )


def _plan(*, with_initrd: bool = True) -> ExternalBootPlan:
    arguments = ("root=/dev/vda1", "console=ttyS0")
    return ExternalBootPlan(
        schema="external-boot-plan-v1",
        architecture="x86_64",
        ownership=PlanOwnership(
            system_id=str(_SYSTEM_ID), run_id=_RUN_ID, build_generation=_BUILD_GENERATION
        ),
        bundle=BundleSource(
            key="builds/kernel.tar",
            version="v1",
            sha256=_SHA,
            vmlinuz_sha256=_SHA,
            member_count=12,
            uncompressed_bytes=4096,
            vmlinuz_size_bytes=2048,
            decoded_kernel_size_bytes=4096,
            elf_metadata_bytes=512,
            gnu_build_id_size_bytes=8,
        ),
        initrd=(
            InitrdSource(key="builds/initrd.img", version="v1", sha256=_INITRD_SHA, size_bytes=1024)
            if with_initrd
            else None
        ),
        cmdline="root=/dev/vda1 console=ttyS0",
        debug_cmdline=None,
        platform_arguments=arguments,
        module_obligation=ModuleObligation(
            release="6.9.0-kdive", source_manifest=_MANIFEST, member_count=3, uncompressed_bytes=64
        ),
        root=RootSpecV1(
            schema="root-spec-v1",
            architecture="x86_64",
            root="/dev/vda1",
            arguments=("root=/dev/vda1",),
            authority="stage-inspection",
            source=RootSource(kind="staged-image", identity=_ROOT_IDENTITY),
        ),
    )


def _materialization(
    *, plan: ExternalBootPlan | None = None, with_initrd: bool = True, run_id: str = _RUN_ID
) -> ExternalBootMaterialization:
    plan = plan if plan is not None else _plan(with_initrd=with_initrd)
    return ExternalBootMaterialization(
        schema="external-boot-materialization-v1",
        architecture="x86_64",
        provider_kind="remote-libvirt",
        ownership=ActivationOwnership(system_id=str(_SYSTEM_ID), run_id=run_id),
        plan_identity=plan.identity,
        extracted_vmlinuz_sha256=_SHA,
        source_module_manifest=_MANIFEST,
        installed_module_tree=_TREE,
        verified_bundle_sha256=_SHA,
        verified_initrd_sha256=_INITRD_SHA if with_initrd else None,
        kernel_observation=_observation(),
        artifacts=MaterializedArtifacts(
            kernel=OpaqueProviderRef(ref="kernel/abc"),
            modules=OpaqueProviderRef(ref="modules/abc"),
            initrd=OpaqueProviderRef(ref="initrd/abc") if with_initrd else None,
        ),
    )


def _binding(
    *, run_id: str = _RUN_ID, system_id: UUID = _SYSTEM_ID
) -> ExternalBootActivationBinding:
    return ExternalBootActivationBinding(
        system_id=str(system_id), run_id=run_id, activation_id=_ACTIVATION_ID
    )


def _prepare(**overrides: Any) -> RemoteExternalBootDefinition:
    plan = overrides.pop("plan", None) or _plan()
    kwargs: dict[str, Any] = {
        "plan": plan,
        "materialization": overrides.pop("materialization", None) or _materialization(plan=plan),
        "binding": overrides.pop("binding", None) or _binding(),
        "pool": "kdive",
        "kernel_path": _KERNEL_PATH,
        "initrd_path": _INITRD_PATH,
    }
    kwargs.update(overrides)
    source = kwargs.pop("source_xml", None) or _source_xml()
    return prepare_target_definition(source, **kwargs)


def test_prepare_records_both_definitions_and_the_expected_identity() -> None:
    definition = _prepare()
    source = _source_xml()
    assert definition.source_xml == source
    assert definition.source_definition == preserved_definition_identity(source)
    assert definition.source_boot == boot_projection_identity(source)
    assert definition.target_definition == preserved_definition_identity(definition.target_xml)
    assert definition.target_boot == boot_projection_identity(definition.target_xml)
    assert definition.source_definition == definition.target_definition
    assert definition.source_boot != definition.target_boot
    assert f"<kernel>{_KERNEL_PATH}</kernel>" in definition.target_xml
    assert f"<initrd>{_INITRD_PATH}</initrd>" in definition.target_xml
    assert definition.expected_cmdline == "root=/dev/vda1 console=ttyS0"
    assert definition.expected_running == _observation()


def test_prepare_round_trips_through_pydantic_json() -> None:
    definition = _prepare()
    assert RemoteExternalBootDefinition.model_validate_json(definition.model_dump_json()) == (
        definition
    )


@pytest.mark.parametrize(
    ("overrides", "rule"),
    [
        ({"binding": _binding(system_id=_OTHER_SYSTEM_ID)}, "ownership"),
        ({"binding": _binding(run_id=_OTHER_RUN_ID)}, "ownership"),
        (
            {"materialization": _materialization(run_id=_OTHER_RUN_ID)},
            "ownership",
        ),
    ],
    ids=["binding-system", "binding-run", "materialization-run"],
)
def test_prepare_rejects_ownership_disagreement(overrides: dict[str, Any], rule: str) -> None:
    with pytest.raises(CategorizedError) as caught:
        _prepare(**overrides)
    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details["rule"] == rule


def test_prepare_rejects_a_materialization_describing_another_plan() -> None:
    with pytest.raises(CategorizedError) as caught:
        _prepare(materialization=_materialization(plan=_plan(with_initrd=False)))
    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details["rule"] == "plan-identity"


@pytest.mark.parametrize(
    ("with_initrd", "initrd_path"),
    [(True, None), (False, _INITRD_PATH)],
    ids=["path-missing", "path-unexpected"],
)
def test_prepare_rejects_initrd_presence_disagreement(
    with_initrd: bool, initrd_path: str | None
) -> None:
    plan = _plan(with_initrd=with_initrd)
    with pytest.raises(CategorizedError) as caught:
        _prepare(
            plan=plan,
            materialization=_materialization(plan=plan, with_initrd=with_initrd),
            initrd_path=initrd_path,
        )
    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details["rule"] == "initrd-presence"


@pytest.mark.parametrize(
    "kernel_path",
    ["", "relative/kernel.img", "/var/lib/kdive/../../etc/shadow", "/a\x00b", "/" + "x" * 1025],
    ids=["empty", "relative", "traversal", "nul", "oversized"],
)
def test_prepare_rejects_an_ill_shaped_artifact_path(kernel_path: str) -> None:
    with pytest.raises(CategorizedError) as caught:
        _prepare(kernel_path=kernel_path)
    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details["rule"] == "artifact-path"


def test_prepare_rejects_a_non_nfc_artifact_path() -> None:
    with pytest.raises(CategorizedError) as caught:
        _prepare(kernel_path="/var/lib/kdive/café")
    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details["rule"] == "artifact-path"


def test_definition_rejects_a_digest_that_does_not_recompute() -> None:
    payload = _prepare().model_dump()
    payload["target_definition"] = "sha256:" + "99" * 32
    with pytest.raises(ValidationError):
        RemoteExternalBootDefinition.model_validate(payload)


def test_definition_rejects_xml_over_the_byte_bound_a_character_bound_would_admit() -> None:
    payload = _prepare().model_dump()
    # Under MAX_DEFINITION_BYTES characters, over it in UTF-8 bytes.
    payload["source_xml"] = "<domain><name>" + "é" * 40_000 + "</name></domain>"
    assert len(payload["source_xml"]) < MAX_DEFINITION_BYTES
    assert len(payload["source_xml"].encode()) > MAX_DEFINITION_BYTES
    with pytest.raises(ValidationError):
        RemoteExternalBootDefinition.model_validate(payload)


def test_definition_surfaces_unparseable_xml_as_validation_error() -> None:
    payload = _prepare().model_dump()
    payload["source_xml"] = "<domain>"
    with pytest.raises(ValidationError):
        RemoteExternalBootDefinition.model_validate(payload)


class _FakeDomain:
    """Models libvirt: it stores what it was defined with and regenerates on read.

    A double that echoed its input verbatim would let a byte comparison pass here and fail
    against a real libvirt, which is exactly the defect the digest comparison exists to prevent.
    """

    def __init__(self, xml: str, *, active: bool) -> None:
        self._xml = xml
        self._active = active
        self.calls: list[str] = []
        self.defined: list[str] = []
        self.create_error: BaseException | None = None
        self.xmldesc_error: BaseException | None = None

    def isActive(self) -> int:  # noqa: N802 - libvirt binding name
        self.calls.append("isActive")
        return 1 if self._active else 0

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - libvirt binding name
        self.calls.append("XMLDesc")
        if self.xmldesc_error is not None:
            raise self.xmldesc_error
        root = ET.fromstring(self._xml)  # noqa: S314 - test-owned XML
        ET.indent(root, space="    ")
        return ET.tostring(root, encoding="unicode")

    def create(self) -> int:
        self.calls.append("create")
        if self.create_error is not None:
            raise self.create_error
        self._active = True
        return 0

    def _replace(self, xml: str) -> None:
        self._xml = xml


class _FakeConn:
    def __init__(self, domain: _FakeDomain | None, *, lookup_error: BaseException | None = None):
        self._domain = domain
        self._lookup_error = lookup_error
        self.define_error: BaseException | None = None
        # What the domain reads back as after a define, when that must differ from what was
        # written — the broken fixed-point case.
        self.readback_xml: str | None = None
        self.calls: list[str] = []

    def lookupByName(self, name: str) -> _FakeDomain:  # noqa: N802 - libvirt binding name
        self.calls.append(f"lookupByName:{name}")
        if self._lookup_error is not None:
            raise self._lookup_error
        assert self._domain is not None
        return self._domain

    def defineXML(self, xml: str) -> _FakeDomain:  # noqa: N802 - libvirt binding name
        self.calls.append("defineXML")
        if self.define_error is not None:
            raise self.define_error
        assert self._domain is not None
        self._domain.defined.append(xml)
        self._domain._replace(self.readback_xml if self.readback_xml is not None else xml)
        return self._domain


def _libvirt_error(code: int) -> libvirt.libvirtError:
    error = libvirt.libvirtError("boom")
    error.err = (code, 0, "boom", 0, "", "", "", 0, 0)
    return error


_OTHER_XML = '<domain><name>kdive-other</name><os><type arch="x86_64">hvm</type></os></domain>'


@pytest.mark.parametrize(
    ("which", "active", "defines", "creates"),
    [
        ("source", False, 1, 1),
        ("source", True, 0, 0),
        ("target", False, 0, 1),
        ("target", True, 0, 0),
        ("other", False, 0, 0),
        ("other", True, 0, 0),
    ],
    ids=[
        "source-inactive-writes",
        "source-active-conflicts",
        "target-inactive-starts",
        "target-active-is-a-noop",
        "other-inactive-conflicts",
        "other-active-conflicts",
    ],
)
def test_activation_matrix(which: str, active: bool, defines: int, creates: int) -> None:
    definition = _prepare()
    observed = {
        "source": definition.source_xml,
        "target": definition.target_xml,
        "other": _OTHER_XML,
    }[which]
    domain = _FakeDomain(observed, active=active)
    conn = _FakeConn(domain)
    expected_conflict = which == "other" or (which == "source" and active)
    if expected_conflict:
        with pytest.raises(CategorizedError) as caught:
            activate_definition(conn, definition)
        assert caught.value.category is ErrorCategory.CONFLICT
        assert caught.value.details["system_id"] == str(_SYSTEM_ID)
        assert "<domain" not in str(caught.value.details)
    else:
        activate_definition(conn, definition)
    assert len(domain.defined) == defines
    assert domain.calls.count("create") == creates
    if defines:
        assert domain.defined[0] == definition.target_xml


def test_activation_conflicts_when_the_readback_matches_neither_pair() -> None:
    definition = _prepare()
    domain = _FakeDomain(definition.source_xml, active=False)
    conn = _FakeConn(domain)
    conn.readback_xml = _OTHER_XML
    with pytest.raises(CategorizedError) as caught:
        activate_definition(conn, definition)
    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details["phase"] == "readback"
    assert domain.calls.count("create") == 0


def test_activation_conflicts_when_the_start_fails_after_a_successful_define() -> None:
    definition = _prepare()
    domain = _FakeDomain(definition.source_xml, active=False)
    domain.create_error = _libvirt_error(libvirt.VIR_ERR_OPERATION_INVALID)
    with pytest.raises(CategorizedError) as caught:
        activate_definition(_FakeConn(domain), definition)
    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details["phase"] == "start"
    assert caught.value.terminal is False
    assert len(domain.defined) == 1


def test_activation_reports_a_missing_domain_as_not_found() -> None:
    conn = _FakeConn(None, lookup_error=_libvirt_error(libvirt.VIR_ERR_NO_DOMAIN))
    with pytest.raises(CategorizedError) as caught:
        activate_definition(conn, _prepare())
    assert caught.value.category is ErrorCategory.NOT_FOUND


def test_activation_reports_another_lookup_error_as_infrastructure_failure() -> None:
    conn = _FakeConn(None, lookup_error=_libvirt_error(libvirt.VIR_ERR_INTERNAL_ERROR))
    with pytest.raises(CategorizedError) as caught:
        activate_definition(conn, _prepare())
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_activation_reports_an_xml_rejection_as_conflict() -> None:
    definition = _prepare()
    domain = _FakeDomain(definition.source_xml, active=False)
    conn = _FakeConn(domain)
    conn.define_error = _libvirt_error(libvirt.VIR_ERR_XML_ERROR)
    with pytest.raises(CategorizedError) as caught:
        activate_definition(conn, definition)
    assert caught.value.category is ErrorCategory.CONFLICT
    assert domain.calls.count("create") == 0


def test_activation_reports_an_xmldesc_failure_as_infrastructure_failure() -> None:
    definition = _prepare()
    domain = _FakeDomain(definition.source_xml, active=False)
    domain.xmldesc_error = _libvirt_error(libvirt.VIR_ERR_OPERATION_INVALID)
    with pytest.raises(CategorizedError) as caught:
        activate_definition(_FakeConn(domain), definition)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


_NOTES = bytes.fromhex("040000000800000003000000474e5500") + bytes.fromhex("ab" * 8)
_CMDLINE = b"root=/dev/vda1 console=ttyS0\n"


class _FakeGuestDomain:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _FakeAgentExec:
    """Answers an exact argv with a canned result; an unconfigured argv is a hard failure."""

    def __init__(self, replies: dict[tuple[str, ...], AgentExecResult]) -> None:
        self._replies = replies
        self.argvs: list[list[str]] = []
        self.error: BaseException | None = None

    def run(
        self, domain: Any, argv: list[str], *, input_data: str | None = None
    ) -> AgentExecResult:
        self.argvs.append(list(argv))
        if self.error is not None:
            raise self.error
        key = tuple(argv)
        if key not in self._replies:
            raise AssertionError(f"unconfigured argv: {argv}")
        return self._replies[key]


def _replies(
    *,
    release: bytes = b"6.9.0-kdive\n",
    machine: bytes = b"x86_64\n",
    cmdline: bytes = _CMDLINE,
    notes: bytes = _NOTES,
    exits: dict[str, int] | None = None,
) -> dict[tuple[str, ...], AgentExecResult]:
    codes = exits or {}
    return {
        ("/usr/bin/uname", "-r"): AgentExecResult(codes.get("release", 0), release, b""),
        ("/usr/bin/uname", "-m"): AgentExecResult(codes.get("machine", 0), machine, b""),
        ("/usr/bin/cat", "/proc/cmdline"): AgentExecResult(codes.get("cmdline", 0), cmdline, b""),
        ("/usr/bin/cat", "/sys/kernel/notes"): AgentExecResult(codes.get("notes", 0), notes, b""),
    }


def _guest(system_id: UUID = _SYSTEM_ID) -> _FakeGuestDomain:
    return _FakeGuestDomain(domain_name_for(system_id))


def test_observation_returns_the_running_identity_and_the_command_line() -> None:
    definition = _prepare()
    agent = _FakeAgentExec(_replies())
    identity = observe_guest_identity(agent, _guest(), definition)
    assert identity.running == _observation()
    assert identity.cmdline == b"root=/dev/vda1 console=ttyS0"
    assert agent.argvs == [
        ["/usr/bin/uname", "-r"],
        ["/usr/bin/uname", "-m"],
        ["/usr/bin/cat", "/proc/cmdline"],
        ["/usr/bin/cat", "/sys/kernel/notes"],
    ]


def test_observation_refuses_a_domain_handle_for_another_system() -> None:
    agent = _FakeAgentExec(_replies())
    with pytest.raises(CategorizedError) as caught:
        observe_guest_identity(agent, _guest(_OTHER_SYSTEM_ID), _prepare())
    assert caught.value.category is ErrorCategory.CONFLICT
    assert caught.value.details["rule"] == "domain-binding"
    assert agent.argvs == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"release": b"6.9.0-other\n"},
        {"machine": b"aarch64\n"},
        {"machine": b"ppc64le\n"},
        {"notes": b""},
        {"notes": b"\x00\x01\x02"},
        {"cmdline": b"root=/dev/vda1 console=ttyS1\n"},
        {"cmdline": b"root=/dev/vda1 console=ttyS0"},
        {"cmdline": b"root=/dev/vda1 console=ttyS0\n\n"},
        # The shape a combined `uname -r -m` would have produced: one space-separated line.
        {"release": b"6.9.0-kdive x86_64\n"},
    ],
    ids=[
        "wrong-release",
        "wrong-machine",
        "unnamed-architecture",
        "empty-notes",
        "malformed-notes",
        "cmdline-one-byte-differs",
        "cmdline-no-trailing-newline",
        "cmdline-two-trailing-newlines",
        "uname-combined-output",
    ],
)
def test_observation_fails_closed_and_terminal_on_an_identity_mismatch(kwargs: Any) -> None:
    definition = _prepare()
    with pytest.raises(CategorizedError) as caught:
        observe_guest_identity(_FakeAgentExec(_replies(**kwargs)), _guest(), definition)
    assert caught.value.category is ErrorCategory.READINESS_FAILURE
    assert caught.value.terminal is True
    rendered = str(caught.value.details) + str(caught.value)
    for leak in (b"aarch64", b"ttyS1", b"6.9.0-other"):
        assert leak.decode() not in rendered


@pytest.mark.parametrize(
    "which",
    ["release", "machine", "cmdline", "notes"],
    ids=["release", "machine", "cmdline", "notes"],
)
def test_observation_treats_a_non_zero_exit_as_a_terminal_identity_failure(which: str) -> None:
    with pytest.raises(CategorizedError) as caught:
        observe_guest_identity(_FakeAgentExec(_replies(exits={which: 1})), _guest(), _prepare())
    assert caught.value.category is ErrorCategory.READINESS_FAILURE
    assert caught.value.terminal is True


def test_observation_rejects_a_capture_larger_than_the_read_bound() -> None:
    oversized = b"x" * (MAX_GUEST_READ_BYTES + 1)
    with pytest.raises(CategorizedError) as caught:
        observe_guest_identity(_FakeAgentExec(_replies(cmdline=oversized)), _guest(), _prepare())
    assert caught.value.category is ErrorCategory.READINESS_FAILURE


@pytest.mark.parametrize(
    "category",
    [
        ErrorCategory.TRANSPORT_FAILURE,
        ErrorCategory.CONFIGURATION_ERROR,
        ErrorCategory.INFRASTRUCTURE_FAILURE,
    ],
    ids=["unreachable-agent", "denied-rpc", "malformed-reply"],
)
def test_observation_propagates_the_agent_seams_own_failures_unchanged(
    category: ErrorCategory,
) -> None:
    agent = _FakeAgentExec(_replies())
    agent.error = CategorizedError("seam", category=category)
    with pytest.raises(CategorizedError) as caught:
        observe_guest_identity(agent, _guest(), _prepare())
    assert caught.value.category is category
    assert caught.value.terminal is False
