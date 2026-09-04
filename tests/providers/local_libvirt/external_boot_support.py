"""Shared external-boot recovery fixtures for the local-libvirt provider tests.

A domain support module, not a collected test module: `test_test_module_dependencies.py`
forbids a test importing helpers defined inside another test, so anything two test modules
need lives here and is defined exactly once.
"""

from __future__ import annotations

import hashlib

from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    AbsentComponentState,
    LocalPreStopIntentV1,
    LocalRecoveryMetadataV1,
    PresentComponentState,
    ProviderStateIdentity,
    RecoveryPhase,
    RecoveryPoint,
)
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    KernelIdentity,
    OpaqueProviderRef,
)

_SOURCE_XML = """<domain xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0">
  <name>kdive-system</name>
  <metadata><owner system="00000000-0000-0000-0000-000000000001" /></metadata>
  <memory unit="MiB">2048</memory>
  <os firmware="efi"><type arch="x86_64">hvm</type><kernel>/old</kernel>
    <initrd>/old-i</initrd><cmdline>old</cmdline></os>
  <devices><disk type="file"><target dev="vda" /></disk></devices>
  <qemu:commandline><qemu:arg value="-S" /></qemu:commandline>
</domain>"""


_BINDING = ExternalBootActivationBinding(
    system_id="00000000-0000-0000-0000-000000000001",
    run_id="00000000-0000-0000-0000-000000000002",
    activation_id="00000000-0000-0000-0000-000000000003",
)


def _metadata(phase: RecoveryPhase = "pre-stop-intent") -> LocalRecoveryMetadataV1:
    absent = AbsentComponentState()
    source_state = ProviderStateIdentity(definition="sha256:" + "b" * 64, modules=absent)
    target_state = ProviderStateIdentity(
        definition="sha256:" + "c" * 64,
        modules=PresentComponentState(manifest="sha256:" + "3" * 64),
    )

    return LocalRecoveryMetadataV1(
        binding=_BINDING,
        plan_identity="sha256:" + "6" * 64,
        materialization_identity="sha256:" + "7" * 64,
        release="6.12.0",
        materialized_modules=OpaqueProviderRef(ref="artifacts/system/run/modules"),
        materialized_modules_sha256="sha256:" + "8" * 64,
        materialized_modules_bytes=123,
        source_xml_sha256="sha256:" + hashlib.sha256(_SOURCE_XML.encode()).hexdigest(),
        source_xml=_SOURCE_XML,
        source_definition="sha256:" + "a" * 64,
        source_boot="sha256:" + "b" * 64,
        target_boot="sha256:" + "c" * 64,
        target_projection_sha256="sha256:" + "d" * 64,
        target_xml_sha256="sha256:"
        + hashlib.sha256(_SOURCE_XML.replace("/old", "/new").encode()).hexdigest(),
        target_xml=_SOURCE_XML.replace("/old", "/new"),
        expected_running=KernelIdentity(
            architecture="x86_64",
            release="6.12.0",
            gnu_build_id="01020304",
        ),
        source_state=source_state,
        target_state=target_state,
        prior_power="running",
        capture={"state": "absent"},
        phase=phase,
    )


def _pre_stop(metadata: LocalRecoveryMetadataV1) -> LocalPreStopIntentV1:
    return LocalPreStopIntentV1.model_validate(
        metadata.model_dump(
            exclude={"schema_", "source_state", "target_state", "capture", "phase"},
            by_alias=True,
        )
    )


def _point(metadata: LocalRecoveryMetadataV1) -> RecoveryPoint:
    return RecoveryPoint(
        binding=metadata.binding,
        plan_identity=metadata.plan_identity,
        materialization_identity=metadata.materialization_identity,
        recovery_ref=OpaqueProviderRef(
            ref=f"local-recovery-v1/{_BINDING.system_id}/{_BINDING.activation_id}"
        ),
        source_state=metadata.source_state,
        target_state=metadata.target_state,
    )
