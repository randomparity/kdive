"""Tests for the provisioning-profile schema (`kdive.profiles.provisioning`)."""

from __future__ import annotations

import copy
from typing import Any, cast

import pytest
from pydantic import ValidationError

from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.sizing import AllocationSizing
from kdive.domain.operations.jobs import JobKind
from kdive.profiles.provider_policy import (
    capture_method,
    reject_rootfs_upload_without_window,
    rootfs_upload_window_allowed,
)
from kdive.profiles.provisioning import (
    FADUMP_MIN_MEMORY_MB,
    BootMethod,
    ProvisioningProfile,
    dump_profile,
    profile_digest,
    reconcile_profile_sizing,
    require_concrete_sizing,
)
from kdive.providers.fault_inject.profile_policy import FaultInjectProfilePolicy
from kdive.providers.local_libvirt.profile_policy import LocalLibvirtProfilePolicy
from kdive.providers.remote_libvirt.profile_policy import RemoteLibvirtProfilePolicy

_LOCAL_POLICY = LocalLibvirtProfilePolicy()
_FAULT_POLICY = FaultInjectProfilePolicy()
_REMOTE_POLICY = RemoteLibvirtProfilePolicy()

_VALID: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "pc-q35-9.0"},
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


def _valid() -> dict[str, Any]:
    """A fresh deep copy of the canonical valid profile, safe to mutate."""
    return copy.deepcopy(_VALID)


def test_kernel_source_ref_field_documents_baseline_and_disk_image_exception() -> None:
    # D6 (#763): kernel_source_ref carries a schema Field(description=...) so the MCP surface
    # explains *why* a direct-kernel System needs a baseline kernel label and that disk-image omits
    # it — the rationale must not live only in the model docstring. The validator's terse
    # "required for boot_method 'direct-kernel'" message is otherwise the only on-wire hint.
    description = ProvisioningProfile.model_fields["kernel_source_ref"].description
    assert description is not None
    lowered = description.lower()
    assert "direct-kernel" in lowered
    assert "disk-image" in lowered
    # It names the baseline-kernel role (reaching ready before Runs iterate kernels).
    assert "baseline" in lowered


def test_valid_libvirt_profile_parses() -> None:
    profile = ProvisioningProfile.parse(_valid())

    assert profile.schema_version == 1
    assert profile.arch == "x86_64"
    assert profile.vcpu == 4
    assert profile.memory_mb == 4096
    assert profile.disk_gb == 20
    assert profile.boot_method is BootMethod.DIRECT_KERNEL
    assert profile.kernel_source_ref is not None
    assert profile.kernel_source_ref.startswith("git+https://")
    assert profile.provider.local_libvirt.domain_xml_params == {"machine": "pc-q35-9.0"}
    rootfs = profile.provider.local_libvirt.rootfs
    assert rootfs.kind == "local"
    assert rootfs.path == "/var/lib/kdive/rootfs/fedora-40.qcow2"
    assert profile.provider.kind is ResourceKind.LOCAL_LIBVIRT


def test_local_libvirt_policy_reports_profile_rootfs() -> None:
    profile = ProvisioningProfile.parse(_valid())

    assert _LOCAL_POLICY.rootfs_source(profile) == profile.provider.local_libvirt.rootfs


def test_valid_fault_inject_profile_parses_and_dumps_alias() -> None:
    data = _valid()
    data["provider"] = {
        "fault-inject": {
            "capture_method": "host_dump",
            "destructive_ops": ["force_crash"],
        }
    }

    profile = ProvisioningProfile.parse(data)

    assert profile.provider.fault_inject.capture_method is CaptureMethod.HOST_DUMP
    assert profile.provider.kind is ResourceKind.FAULT_INJECT
    assert _FAULT_POLICY.destructive_opt_in(profile, JobKind.FORCE_CRASH) is True
    assert rootfs_upload_window_allowed(_FAULT_POLICY, profile) is False
    assert dump_profile(profile)["provider"] == {
        "fault-inject": {
            "capture_method": "host_dump",
            "destructive_ops": ["force_crash"],
        }
    }


def test_dump_profile_emits_json_native_scalars() -> None:
    # The dump is for JSON persistence, so enum-typed fields must serialize to plain
    # strings (StrEnum compares equal to its value, so an == check would not notice a
    # non-json dump mode — assert the concrete type instead).
    data = _valid()
    data["provider"] = {"fault-inject": {"capture_method": "host_dump"}}
    dumped = dump_profile(ProvisioningProfile.parse(data))

    boot_method = dumped["boot_method"]
    assert type(boot_method) is str
    assert boot_method == "direct-kernel"
    provider = cast("dict[str, dict[str, object]]", dumped["provider"])
    capture = provider["fault-inject"]["capture_method"]
    assert type(capture) is str
    assert capture == "host_dump"


def test_provider_section_rejects_multiple_providers() -> None:
    data = _valid()
    data["provider"]["fault-inject"] = {}
    _expect_configuration_error(data)


def test_fault_inject_capture_method_defaults_to_console() -> None:
    data = _valid()
    data["provider"] = {"fault-inject": {}}
    assert capture_method(_FAULT_POLICY, data) is CaptureMethod.CONSOLE


def test_crashkernel_is_present() -> None:
    # The crashkernel reservation is the kdump prerequisite (acceptance criterion).
    profile = ProvisioningProfile.parse(_valid())

    assert profile.provider.local_libvirt.crashkernel == "256M"


def test_baseline_kernel_defaults_to_none() -> None:
    # The single-kernel common case carries no hint (ADR-0310); selection stays fail-closed.
    profile = ProvisioningProfile.parse(_valid())
    assert profile.provider.local_libvirt.baseline_kernel is None


def test_baseline_kernel_parses_when_present() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["baseline_kernel"] = "vmlinuz-6.18.0-100.fc44.x86_64"
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.baseline_kernel == "vmlinuz-6.18.0-100.fc44.x86_64"


@pytest.mark.parametrize("value", ["", "   "])
def test_baseline_kernel_rejects_blank(value: str) -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["baseline_kernel"] = value
    _expect_configuration_error(data)


def test_local_libvirt_ssh_credential_ref_now_rejected() -> None:
    # The vestigial drgn-live credential ref is retired (ADR-0315); the local-libvirt section is
    # extra="forbid", so a profile still carrying it is rejected at parse — drgn-live now gates on
    # the per-System bootstrap key, not a profile field.
    data = _valid()
    data["provider"]["local-libvirt"]["ssh_credential_ref"] = "ssh/guest-key"
    _expect_configuration_error(data)


def test_profile_digest_is_stable_hex() -> None:
    digest = profile_digest(ProvisioningProfile.parse(_valid()))
    assert len(digest) == 64  # sha256 hex
    assert int(digest, 16) >= 0  # all hex


def test_profile_digest_matches_known_canonical_value() -> None:
    # The digest is computed over the sorted-key, compact-separator JSON encoding of the
    # parsed profile (ADR-0038 §3). Pinning the exact hex guards the canonical encoding:
    # any change to key ordering or the item/key separators changes the digest and so
    # would break dedup-key stability across deploys.
    digest = profile_digest(ProvisioningProfile.parse(_valid()))
    # Repinned when the ADR-0349 `debug.fadump` flag joined the serialized debug block (like the
    # sibling `preserve_on_crash`/`gdbstub` flags, it dumps as `false` by default).
    assert digest == "9852f2ce104e3b547ad585ca9dc55037c8d59d94037876bd6a50f5fcbbfff16a"


def test_profile_digest_ignores_input_key_order() -> None:
    # Digest equality must be semantic equality (ADR-0038 dedup correctness): the same
    # profile submitted with a different key order yields the same digest.
    a = _valid()
    reordered = {k: a[k] for k in reversed(list(a))}
    reordered["provider"]["local-libvirt"]["domain_xml_params"] = {
        "machine": a["provider"]["local-libvirt"]["domain_xml_params"]["machine"]
    }
    assert profile_digest(ProvisioningProfile.parse(a)) == profile_digest(
        ProvisioningProfile.parse(reordered)
    )


def test_profile_digest_differs_on_meaningful_change() -> None:
    a = ProvisioningProfile.parse(_valid())
    changed = _valid()
    changed["vcpu"] = 8
    assert profile_digest(a) != profile_digest(ProvisioningProfile.parse(changed))


def _expect_configuration_error(data: dict[str, Any]) -> None:
    """Assert that parsing ``data`` fails as a CONFIGURATION_ERROR."""
    with pytest.raises(CategorizedError) as caught:
        ProvisioningProfile.parse(data)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "arch",
        "boot_method",
        "kernel_source_ref",
        "provider",
    ],
)
def test_missing_core_field_raises_configuration_error(field: str) -> None:
    data = _valid()
    del data[field]
    _expect_configuration_error(data)


@pytest.mark.parametrize("field", ["vcpu", "memory_mb", "disk_gb"])
def test_sizing_fields_are_optional_at_parse(field: str) -> None:
    # ADR-0024 delta (ADR-0067): a shape-sized allocation omits profile sizing;
    # systems.provision constructs it from the resolved snapshot. Parsing is structural,
    # so an omitted sizing field is None, not an error.
    data = _valid()
    del data[field]
    parsed = ProvisioningProfile.parse(data)
    assert getattr(parsed, field) is None


@pytest.mark.parametrize("field", ["vcpu", "memory_mb", "disk_gb"])
def test_present_sizing_must_be_positive(field: str) -> None:
    data = _valid()
    data[field] = 0
    _expect_configuration_error(data)


@pytest.mark.parametrize("field", ["rootfs"])
def test_missing_libvirt_field_raises_configuration_error(field: str) -> None:
    data = _valid()
    del data["provider"]["local-libvirt"][field]
    _expect_configuration_error(data)


def test_unknown_top_level_field_rejected() -> None:
    data = _valid()
    data["unexpected"] = "x"
    _expect_configuration_error(data)


def test_unknown_provider_key_rejected() -> None:
    data = _valid()
    data["provider"]["cloud"] = {}
    _expect_configuration_error(data)


def test_unknown_libvirt_field_rejected() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["extra"] = "x"
    _expect_configuration_error(data)


def test_empty_provider_section_rejected() -> None:
    # The local-libvirt section is required (ADR-0024 decision 1): a profile that
    # names no provider cannot be provisioned.
    data = _valid()
    data["provider"] = {}
    _expect_configuration_error(data)


def test_non_mapping_provider_section_rejected() -> None:
    data = _valid()
    data["provider"]["local-libvirt"] = "not-a-mapping"
    _expect_configuration_error(data)


@pytest.mark.parametrize("payload", [None, [], "not-a-mapping", 42])
def test_non_mapping_input_rejected(payload: Any) -> None:
    # parse() guards the boundary against a caller handing it a non-document.
    with pytest.raises(CategorizedError) as caught:
        ProvisioningProfile.parse(payload)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR


@pytest.mark.parametrize("value", ["", "   "])
@pytest.mark.parametrize("field", ["arch", "kernel_source_ref"])
def test_blank_core_string_rejected(field: str, value: str) -> None:
    data = _valid()
    data[field] = value
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_crashkernel_rejected(value: str) -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["crashkernel"] = value
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_rootfs_path_rejected(value: str) -> None:
    # A local-kind rootfs with a blank file path is as malformed as a blank string field was.
    data = _valid()
    data["provider"]["local-libvirt"]["rootfs"] = {"kind": "local", "path": value}
    _expect_configuration_error(data)


@pytest.mark.parametrize(("field", "value"), [("vcpu", 0), ("memory_mb", -1), ("disk_gb", 0)])
def test_non_positive_int_rejected(field: str, value: int) -> None:
    data = _valid()
    data[field] = value
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", ["4", True, 2.0])
@pytest.mark.parametrize("field", ["vcpu", "memory_mb", "disk_gb"])
def test_non_int_value_rejected(field: str, value: object) -> None:
    # strict=True: a malformed externally-authored value must not silently coerce.
    data = _valid()
    data[field] = value
    _expect_configuration_error(data)


def test_empty_domain_xml_param_value_rejected() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["domain_xml_params"] = {"machine": ""}
    _expect_configuration_error(data)


@pytest.mark.parametrize("key", ["", "   "])
def test_empty_domain_xml_param_key_rejected(key: str) -> None:
    # An empty param name is as malformed as an empty value (ADR-0024 decision 2c).
    data = _valid()
    data["provider"]["local-libvirt"]["domain_xml_params"] = {key: "q35"}
    _expect_configuration_error(data)


def test_domain_xml_params_defaults_to_empty_map() -> None:
    data = _valid()
    del data["provider"]["local-libvirt"]["domain_xml_params"]

    profile = ProvisioningProfile.parse(data)

    assert profile.provider.local_libvirt.domain_xml_params == {}


def test_unknown_boot_method_rejected() -> None:
    data = _valid()
    data["boot_method"] = "iso"
    _expect_configuration_error(data)


def test_unreadable_schema_version_rejected() -> None:
    data = _valid()
    data["schema_version"] = 2
    _expect_configuration_error(data)


@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_non_int_schema_version_rejected(value: object) -> None:
    # A bool/str/float must not coerce to version 1 (consistent with strict ints).
    data = _valid()
    data["schema_version"] = value
    _expect_configuration_error(data)


def test_error_details_do_not_leak_submitted_values() -> None:
    data = _valid()
    data["memory_mb"] = "S3CRET-LOOKING-VALUE"  # wrong type carrying a sentinel

    with pytest.raises(CategorizedError) as caught:
        ProvisioningProfile.parse(data)

    assert "S3CRET-LOOKING-VALUE" not in str(caught.value.details)


def test_parse_error_carries_scrubbed_pydantic_errors() -> None:
    # The parse boundary maps Pydantic's ValidationError onto the wire taxonomy with a
    # fixed message and a details["errors"] list, scrubbing the submitted values, URLs,
    # and pydantic context out so a profile referencing secret material cannot leak it
    # (ADR-0024 decision 3).
    # A blank ``arch`` yields a pydantic ``string_too_short`` error whose context
    # (``min_length``) would surface unless ``include_context=False`` strips it, and the
    # sentinel exercises the input-scrubbing flag.
    data = _valid()
    data["arch"] = "   "
    data["memory_mb"] = "S3CRET-LOOKING-VALUE"

    with pytest.raises(CategorizedError) as caught:
        ProvisioningProfile.parse(data)

    assert str(caught.value) == "invalid provisioning profile"
    details = caught.value.details
    assert details is not None
    errors = cast(list[dict[str, Any]], details["errors"])
    assert isinstance(errors, list)
    assert errors  # at least the arch and memory_mb failures
    for entry in errors:
        # Scrubbing flags: include_url/include_input/include_context are all False, so the
        # per-error dicts carry no documentation URL, no submitted input, and no context.
        assert "url" not in entry
        assert "input" not in entry
        assert "ctx" not in entry


def test_profile_is_frozen() -> None:
    profile = ProvisioningProfile.parse(_valid())

    with pytest.raises(ValidationError):
        profile.arch = "aarch64"


def test_direct_construction_bypasses_configuration_error_mapping() -> None:
    # model_validate is not the sanctioned door; it surfaces the raw ValidationError.
    with pytest.raises(ValidationError):
        ProvisioningProfile.model_validate({"schema_version": 1})


def test_destructive_ops_defaults_empty() -> None:
    profile = ProvisioningProfile.parse(_valid())
    assert profile.provider.local_libvirt.destructive_ops == []


def test_destructive_ops_accepts_force_crash() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["destructive_ops"] = ["force_crash"]
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.destructive_ops == ["force_crash"]


def test_destructive_opt_in_reports_profile_gate() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["destructive_ops"] = ["force_crash"]
    profile = ProvisioningProfile.parse(data)

    assert _LOCAL_POLICY.destructive_opt_in(profile, JobKind.FORCE_CRASH) is True
    assert _LOCAL_POLICY.destructive_opt_in(profile, JobKind.REPROVISION) is False


def test_destructive_ops_rejects_blank_entry() -> None:
    from kdive.domain.errors import CategorizedError

    data = _valid()
    data["provider"]["local-libvirt"]["destructive_ops"] = [" "]
    with pytest.raises(CategorizedError):
        ProvisioningProfile.parse(data)


def test_debug_block_defaults_to_disabled() -> None:
    profile = ProvisioningProfile.parse(_valid())
    debug = profile.provider.local_libvirt.debug
    assert debug.preserve_on_crash is False
    assert debug.gdbstub is False


def test_debug_flags_parse_when_present() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["debug"] = {"preserve_on_crash": True, "gdbstub": True}
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.debug.preserve_on_crash is True
    assert profile.provider.local_libvirt.debug.gdbstub is True


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ({"crashkernel": "256M"}, CaptureMethod.KDUMP),
        ({"debug": {"gdbstub": True}}, CaptureMethod.GDBSTUB),
        ({"debug": {"preserve_on_crash": True}}, CaptureMethod.HOST_DUMP),
        ({}, CaptureMethod.CONSOLE),
    ],
)
def test_capture_method_reports_profile_capture_tier(
    section: dict[str, Any], expected: CaptureMethod
) -> None:
    data = _valid()
    data["provider"]["local-libvirt"].pop("crashkernel")
    data["provider"]["local-libvirt"].update(section)

    assert capture_method(_LOCAL_POLICY, ProvisioningProfile.parse(data)) is expected


def test_capture_method_rejects_malformed_stored_mapping() -> None:
    with pytest.raises(CategorizedError) as exc:
        capture_method(_LOCAL_POLICY, {"schema_version": 1})

    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_debug_block_rejects_unknown_key() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["debug"] = {"bogus": True}
    with pytest.raises(CategorizedError) as exc:
        ProvisioningProfile.parse(data)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_crashkernel_is_optional() -> None:
    data = _valid()
    del data["provider"]["local-libvirt"]["crashkernel"]
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.crashkernel is None


def _fadump_profile() -> dict[str, Any]:
    """A ppc64le fadump profile: fadump=on + a crashkernel reservation (ADR-0349)."""
    data = _valid()
    data["arch"] = "ppc64le"
    section = data["provider"]["local-libvirt"]
    section["crashkernel"] = "512M"
    section["debug"] = {"fadump": True}
    return data


def test_fadump_flag_defaults_disabled() -> None:
    profile = ProvisioningProfile.parse(_valid())
    assert profile.provider.local_libvirt.debug.fadump is False


def test_fadump_profile_parses_on_ppc64le_with_reservation() -> None:
    # fadump=on requires arch=ppc64le + a crashkernel reservation (ADR-0349 §2).
    profile = ProvisioningProfile.parse(_fadump_profile())
    assert profile.provider.local_libvirt.debug.fadump is True
    assert profile.provider.local_libvirt.crashkernel == "512M"
    # fadump resolves to a distinct capture method, nested under the crashkernel signal.
    assert capture_method(_LOCAL_POLICY, profile) is CaptureMethod.FADUMP


def test_fadump_rejected_on_non_ppc64le_arch() -> None:
    # fadump is POWER-specific (the RTAS is pseries-only); x86_64 + fadump is a config error.
    data = _fadump_profile()
    data["arch"] = "x86_64"
    with pytest.raises(CategorizedError) as exc:
        ProvisioningProfile.parse(data)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_fadump_rejected_without_crashkernel_reservation() -> None:
    # A fadump System is defined by its reservation token; fadump with no crashkernel would
    # resolve to a non-capture method and silently drop the flag (ADR-0349 §2).
    data = _fadump_profile()
    del data["provider"]["local-libvirt"]["crashkernel"]
    with pytest.raises(CategorizedError) as exc:
        ProvisioningProfile.parse(data)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_fadump_rejected_below_memory_floor() -> None:
    # fadump reserves a boot-memory region on top of crashkernel; below the floor the guest
    # cannot reach readiness (ADR-0363, #1181). A concrete under-floor size is a config error.
    data = _fadump_profile()
    data["memory_mb"] = FADUMP_MIN_MEMORY_MB - 1024
    with pytest.raises(CategorizedError) as exc:
        ProvisioningProfile.parse(data)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_fadump_accepts_memory_at_floor() -> None:
    # Exactly the floor is admitted (the boundary is inclusive).
    data = _fadump_profile()
    data["memory_mb"] = FADUMP_MIN_MEMORY_MB
    profile = ProvisioningProfile.parse(data)
    assert profile.memory_mb == FADUMP_MIN_MEMORY_MB


def test_fadump_memory_floor_deferred_when_sizing_omitted() -> None:
    # A shape-sized allocation omits memory_mb; the floor cannot fire before reconciliation
    # fills it (the sizing fields stay optional at parse, ADR-0067/0024 delta).
    data = _fadump_profile()
    del data["memory_mb"]
    profile = ProvisioningProfile.parse(data)
    assert profile.memory_mb is None


def test_kdump_profile_unaffected_by_fadump_default() -> None:
    # A crashkernel-only ppc64le System stays KDUMP (fadump defaults off).
    data = _valid()
    data["arch"] = "ppc64le"
    data["provider"]["local-libvirt"]["crashkernel"] = "512M"
    profile = ProvisioningProfile.parse(data)
    assert capture_method(_LOCAL_POLICY, profile) is CaptureMethod.KDUMP


def test_rootfs_upload_window_helpers_report_and_reject_upload_profiles() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["rootfs"] = {"kind": "upload"}
    profile = ProvisioningProfile.parse(data)

    assert rootfs_upload_window_allowed(_LOCAL_POLICY, profile) is True
    with pytest.raises(CategorizedError) as exc:
        reject_rootfs_upload_without_window(_LOCAL_POLICY, profile)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert str(exc.value) == "upload-kind rootfs requires systems.define upload window"


def test_rootfs_upload_window_helpers_allow_non_upload_profiles() -> None:
    profile = ProvisioningProfile.parse(_valid())

    assert rootfs_upload_window_allowed(_LOCAL_POLICY, profile) is False
    reject_rootfs_upload_without_window(_LOCAL_POLICY, profile)


# --- ADR-0024 sizing reconciliation (#161) --------------------------------------------

_SNAPSHOT = AllocationSizing(vcpu=2, memory_mb=4096, disk_gb=20)


def test_reconcile_fills_omitted_sizing_from_snapshot() -> None:
    data = _valid()
    for field in ("vcpu", "memory_mb", "disk_gb"):
        del data[field]
    reconciled = reconcile_profile_sizing(data, _SNAPSHOT)
    parsed = ProvisioningProfile.parse(reconciled)
    assert (parsed.vcpu, parsed.memory_mb, parsed.disk_gb) == (2, 4096, 20)


def test_reconcile_accepts_matching_restatement() -> None:
    data = _valid()
    data["vcpu"], data["memory_mb"], data["disk_gb"] = 2, 4096, 20
    reconciled = reconcile_profile_sizing(data, _SNAPSHOT)
    assert (reconciled["vcpu"], reconciled["memory_mb"], reconciled["disk_gb"]) == (2, 4096, 20)


@pytest.mark.parametrize(
    ("field", "bad"),
    [("vcpu", 99), ("memory_mb", 8192), ("disk_gb", 40)],
)
def test_reconcile_rejects_conflicting_restatement(field: str, bad: int) -> None:
    data = _valid()
    data["vcpu"], data["memory_mb"], data["disk_gb"] = 2, 4096, 20
    data[field] = bad
    with pytest.raises(CategorizedError) as caught:
        reconcile_profile_sizing(data, _SNAPSHOT)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    resolved = {"vcpu": 2, "memory_mb": 4096, "disk_gb": 20}[field]
    assert str(caught.value) == (
        f"provisioning profile {field}={bad!r} conflicts with the "
        f"allocation's resolved size {resolved}"
    )
    assert caught.value.details == {"field": field, "resolved": str(resolved)}


def test_reconcile_does_not_mutate_input() -> None:
    data = _valid()
    del data["vcpu"]
    snapshot_before = copy.deepcopy(data)
    reconcile_profile_sizing(data, _SNAPSHOT)
    assert data == snapshot_before


def test_require_concrete_sizing_rejects_missing() -> None:
    data = _valid()
    del data["disk_gb"]
    parsed = ProvisioningProfile.parse(data)
    with pytest.raises(CategorizedError) as caught:
        require_concrete_sizing(parsed)
    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    missing = cast(list[str], caught.value.details["missing"])
    assert "disk_gb" in missing


def test_require_concrete_sizing_message_lists_every_missing_field() -> None:
    # Two omitted fields exercise the comma-space join of the human-readable message.
    data = _valid()
    del data["vcpu"]
    del data["disk_gb"]
    parsed = ProvisioningProfile.parse(data)
    with pytest.raises(CategorizedError) as caught:
        require_concrete_sizing(parsed)
    assert str(caught.value) == "provisioning profile is missing required sizing: vcpu, disk_gb"
    assert caught.value.details == {"missing": ["vcpu", "disk_gb"]}


def test_require_concrete_sizing_accepts_full() -> None:
    require_concrete_sizing(ProvisioningProfile.parse(_valid()))


_VALID_REMOTE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "disk-image",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "remote-libvirt": {
            "base_image_volume": "kdive-base-fedora-42.qcow2",
            "crashkernel": "256M",
            "destructive_ops": ["force_crash"],
        }
    },
}


def _valid_remote() -> dict[str, Any]:
    """A fresh deep copy of the canonical valid remote profile, safe to mutate."""
    return copy.deepcopy(_VALID_REMOTE)


def test_valid_remote_profile_parses() -> None:
    profile = ProvisioningProfile.parse(_valid_remote())

    assert profile.provider.kind is ResourceKind.REMOTE_LIBVIRT
    assert profile.boot_method is BootMethod.DISK_IMAGE
    section = profile.provider.remote_libvirt
    assert section.base_image_volume == "kdive-base-fedora-42.qcow2"
    assert section.crashkernel == "256M"


def test_disk_image_profile_parses_without_kernel_source_ref() -> None:
    # #472: a disk-image (remote-libvirt) provision boots the base image's own kernel and never
    # reads kernel_source_ref, so it is optional on this lane — the VM-only flow must not be forced
    # to invent a kernel source.
    data = _valid_remote()
    del data["kernel_source_ref"]

    profile = ProvisioningProfile.parse(data)

    assert profile.kernel_source_ref is None
    assert profile.boot_method is BootMethod.DISK_IMAGE


def test_disk_image_profile_still_accepts_kernel_source_ref() -> None:
    # #472: relaxing the requirement is backward compatible — a present value is still accepted
    # (and ignored downstream, as it always was).
    profile = ProvisioningProfile.parse(_valid_remote())

    assert profile.kernel_source_ref is not None


def test_direct_kernel_profile_requires_kernel_source_ref() -> None:
    # #472: direct-kernel stays the build-iterating lane; omitting the source is rejected.
    data = _valid()
    del data["kernel_source_ref"]

    with pytest.raises(CategorizedError) as exc_info:
        ProvisioningProfile.parse(data)

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_remote_section_requires_disk_image_boot() -> None:
    data = _valid_remote()
    data["boot_method"] = "direct-kernel"

    with pytest.raises(CategorizedError) as exc_info:
        ProvisioningProfile.parse(data)

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_disk_image_boot_requires_remote_section() -> None:
    data = _valid()
    data["boot_method"] = "disk-image"

    with pytest.raises(CategorizedError) as exc_info:
        ProvisioningProfile.parse(data)

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_remote_profile_capture_method_kdump_with_crashkernel() -> None:
    assert (
        capture_method(_REMOTE_POLICY, ProvisioningProfile.parse(_valid_remote()))
        is CaptureMethod.KDUMP
    )


def test_remote_profile_capture_method_gdbstub_without_crashkernel() -> None:
    data = _valid_remote()
    del data["provider"]["remote-libvirt"]["crashkernel"]

    assert capture_method(_REMOTE_POLICY, ProvisioningProfile.parse(data)) is CaptureMethod.GDBSTUB


def test_remote_profile_destructive_opt_in() -> None:
    profile = ProvisioningProfile.parse(_valid_remote())

    assert _REMOTE_POLICY.destructive_opt_in(profile, JobKind.FORCE_CRASH) is True
    assert _REMOTE_POLICY.destructive_opt_in(profile, JobKind.REPROVISION) is False


def test_remote_profile_rootfs_is_none() -> None:
    profile = ProvisioningProfile.parse(_valid_remote())

    assert _REMOTE_POLICY.rootfs_source(profile) is None


def test_remote_profile_rejects_unknown_fields() -> None:
    data = _valid_remote()
    data["provider"]["remote-libvirt"]["bogus"] = "x"

    with pytest.raises(CategorizedError) as exc_info:
        ProvisioningProfile.parse(data)

    assert exc_info.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_remote_profile_validate_profile_accepts_remote_section() -> None:
    _REMOTE_POLICY.validate_profile(ProvisioningProfile.parse(_valid_remote()))


def test_drgn_live_seeds_bootstrap_key_true_for_local_section() -> None:
    profile = ProvisioningProfile.parse(_valid())
    assert _LOCAL_POLICY.drgn_live_seeds_bootstrap_key(profile) is True


def test_drgn_live_seeds_bootstrap_key_false_for_remote_section() -> None:
    profile = ProvisioningProfile.parse(_valid_remote())
    assert _REMOTE_POLICY.drgn_live_seeds_bootstrap_key(profile) is False


def test_drgn_live_seeds_bootstrap_key_false_for_fault_inject_section() -> None:
    data = _valid()
    data["provider"] = {"fault-inject": {}}
    profile = ProvisioningProfile.parse(data)
    assert _FAULT_POLICY.drgn_live_seeds_bootstrap_key(profile) is False


def test_cpu_pin_parsed() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["cpu"] = {"model": "x86-64-v2"}
    profile = ProvisioningProfile.parse(data)
    assert profile.provider.local_libvirt.cpu is not None
    assert profile.provider.local_libvirt.cpu.model == "x86-64-v2"


def test_cpu_pin_defaults_none() -> None:
    profile = ProvisioningProfile.parse(_valid())
    assert profile.provider.local_libvirt.cpu is None


def test_cpu_pin_rejects_empty_model() -> None:
    data = _valid()
    data["provider"]["local-libvirt"]["cpu"] = {"model": ""}
    with pytest.raises(CategorizedError):
        ProvisioningProfile.parse(data)


def test_cpu_pin_field_documents_isa_floor() -> None:
    from kdive.profiles.provisioning import LibvirtCpuPin

    description = LibvirtCpuPin.model_fields["model"].description
    assert description is not None
    lowered = description.lower()
    assert "selectable_cpus" in lowered
    assert "non-booting" in lowered or "not boot" in lowered
