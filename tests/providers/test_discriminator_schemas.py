"""Pin the validation schema of every model whose discriminator declaration was restructured.

The digests were taken before the base classes were split (#3675); a changed alias, default,
field order, constraint or discriminator literal changes one.
"""

import hashlib
import importlib
import json

import pytest

_DIGESTS: dict[str, dict[str, str]] = {
    "kdive.providers.system_authority.protocol": {
        "_ClosedValue": "ab4cc0ed77f7",  # pragma: allowlist secret
        "AuthoritySystemMarkerV1": "9142a55a9a36",  # pragma: allowlist secret
        "_AuthoritySystemAttemptBinding": "a8c353163bd7",  # pragma: allowlist secret
        "AuthoritySystemTakeoverRequestV1": "e7e7cd564b3d",  # pragma: allowlist secret
        "AuthoritySystemMutationRequestV1": "06fdeca9ccdf",  # pragma: allowlist secret
        "AuthoritySystemCommitContextV1": "5ce8d305368c",  # pragma: allowlist secret
        "AuthoritySystemObservationV1": "f3f0576f923c",  # pragma: allowlist secret
        "AuthoritySystemJournalRecordV1": "c2db6c86f212",  # pragma: allowlist secret
        "AuthoritySystemProvisionFacts": "957486491008",  # pragma: allowlist secret
        "AuthoritySystemAbsenceFacts": "f9593d9b9389",  # pragma: allowlist secret
        "_AuthoritySystemProofBinding": "7bc6adeaabfe",  # pragma: allowlist secret
        "AuthoritySystemProvisionReadyV1": "2b1a504d13d8",  # pragma: allowlist secret
        "AuthoritySystemPreactivationAbsentV1": "2b5c4e1a9c02",  # pragma: allowlist secret
        "AuthoritySystemRetainedQuarantineV1": "91ab337136d3",  # pragma: allowlist secret
        "AuthoritySystemAcknowledgementV1": "c38deeb90d63",  # pragma: allowlist secret
        "AuthoritySystemResponseV1": "18450dd66170",  # pragma: allowlist secret
        "AuthoritySystemExecutionV1": "274edf11a4e3",  # pragma: allowlist secret
    },
    "kdive.providers.external_boot_authority.protocol": {
        "_ClosedValue": "ab4cc0ed77f7",  # pragma: allowlist secret
        "AuthorityInstallRefusalV1": "c683badc74aa",  # pragma: allowlist secret
        "_AuthorityBinding": "32effe01be8f",  # pragma: allowlist secret
        "AuthorityTakeoverRequestV1": "831f97ec2a4d",  # pragma: allowlist secret
        "RecoveryObjectBindingV1": "aecc63691889",  # pragma: allowlist secret
        "AuthorityMutationRequestV1": "0017633a7559",  # pragma: allowlist secret
        "AuthorityTeardownMutationRequestV1": "72d086e9e624",  # pragma: allowlist secret
        "AuthorityConflictResolutionRequestV1": "9f472eae1125",  # pragma: allowlist secret
        "AuthorityPreparationMutationRequestV1": "7bc9bba2672e",  # pragma: allowlist secret
        "AuthorityHealthRequestV1": "d484f3263172",  # pragma: allowlist secret
        "AuthorityHealthAcknowledgementV1": "ea6d46adeae3",  # pragma: allowlist secret
        "AuthorityRecoveryOrphanDispositionRequestV1": "6779a8d98821",  # pragma: allowlist secret
        "AuthorityRecoveryOrphanDispositionResponseV1": "e0c80cbde54b",  # pragma: allowlist secret
        "AuthorityAcknowledgementV1": "281a695ef288",  # pragma: allowlist secret
        "AuthorityObservationV1": "ecf7bdf58388",  # pragma: allowlist secret
        "AuthorityRunningObservationV1": "598f32492d60",  # pragma: allowlist secret
        "AuthorityPreparationResponseV1": "94d306a3e119",  # pragma: allowlist secret
        "AuthorityTeardownCompleteReadyV1": "f855471b6fda",  # pragma: allowlist secret
        "AuthorityTeardownCompletePendingV1": "7f52691126dd",  # pragma: allowlist secret
        "AuthorityTeardownCompleteReleasedV1": "186234ec0d10",  # pragma: allowlist secret
        "AuthorityTeardownRetainedQuarantineV1": "5faa16b4db52",  # pragma: allowlist secret
        "AuthorityTeardownResponseV1": "2649cc6e51fe",  # pragma: allowlist secret
        "AuthorityStrandedObservationRequestV1": "dce11ebb56d6",  # pragma: allowlist secret
        "AuthorityStrandedObservationResponseV1": "5138511197b8",  # pragma: allowlist secret
        "JournalRecordV1": "b223a8b3837a",  # pragma: allowlist secret
        "AuthorityCommitContextV1": "d9af0cd22170",  # pragma: allowlist secret
        "AuthorityCleanupEvidenceContextV1": "f5a9fb94dddc",  # pragma: allowlist secret
        "AuthorityRecoveryObservationContextV1": "9e84793bfe68",  # pragma: allowlist secret
    },
    "kdive.providers.local_libvirt.lifecycle.boot.recovery_store": {
        "_ClosedValue": "ab4cc0ed77f7",  # pragma: allowlist secret
        "LocalRecoveryMetadataV1": "29a42aa9881c",  # pragma: allowlist secret
        "LocalPreStopIntentV1": "bc7dc92cb997",  # pragma: allowlist secret
        "LocalSystemTeardownAnchorV1": "042e8091ddee",  # pragma: allowlist secret
        "LocalSystemTeardownIntentV1": "f7092c65a024",  # pragma: allowlist secret
        "LocalSystemTeardownRecordV1": "232b9c96f027",  # pragma: allowlist secret
        "FinalizeCleanupProof": "746e67d7f525",  # pragma: allowlist secret
        "CleanupTombstoneV1": "4986864bc20f",  # pragma: allowlist secret
        "CleanupQuarantineReceiptV1": "51c23691526f",  # pragma: allowlist secret
        "TargetProjectionV1": "5e751b0b5d00",  # pragma: allowlist secret
        "LocalPartialAbortReceiptV1": "1efa8ca51db4",  # pragma: allowlist secret
        "LocalPreparationReceiptsV1": "a7d76c94cdaa",  # pragma: allowlist secret
    },
    "kdive.providers.remote_libvirt.external_boot_store": {
        "RemoteSystemTeardownAnchorV1": "b22720bd9b82",  # pragma: allowlist secret
        "RemoteSystemTeardownIntentV1": "4967ca4a7b80",  # pragma: allowlist secret
        "RemoteSystemTeardownRecordV1": "0a198acd7969",  # pragma: allowlist secret
        "RemoteModulePreparationCompletionV1": "d1371e7d4e94",  # pragma: allowlist secret
        "_PersistedRemoteModuleLifecycleV1": "74efdf371bba",  # pragma: allowlist secret
        "RemoteModuleLifecycleCompletionV1": "3538b4a80de3",  # pragma: allowlist secret
        "RemoteModuleTerminalRecord": "3d3732382e0e",  # pragma: allowlist secret
        "_PersistedRemoteModulePreparationV1": "07c5106c806e",  # pragma: allowlist secret
        "RemoteCleanupQuarantineV1": "cdd27bc5fc9e",  # pragma: allowlist secret
        "RemoteFinalizedBootArtifactV1": "a66af80533e3",  # pragma: allowlist secret
        "RemoteFinalizedTeardownRefsV1": "40a6be9818d4",  # pragma: allowlist secret
        "RemoteCleanupFinalizedMarkerV1": "365f5b76fb19",  # pragma: allowlist secret
        "RemoteExternalBootRecoveryRecord": "bd592c0909a6",  # pragma: allowlist secret
        "RemoteExternalBootMaterializationRecord": "97fa3a7540eb",  # pragma: allowlist secret
    },
    "kdive.providers.external_boot_authority.module_protocol": {
        "RemotePreparedVolumeV1": "9e28bf7bd2f5",  # pragma: allowlist secret
        "RemoteModuleVolumePreparationRequestV1": "08782e3f4736",  # pragma: allowlist secret
        "RemoteModulePreparationBeginRequestV1": "817445bcdef1",  # pragma: allowlist secret
        "RemoteModulePreparationBeginResponseV1": "fbc79b61f617",  # pragma: allowlist secret
        "RemoteModuleVolumePreparationResponseV1": "58699e7e3001",  # pragma: allowlist secret
        "RemoteModuleTerminalPreparationResponseV1": "a6595c552838",  # pragma: allowlist secret
        "RemoteModuleLifecycleRequestV1": "d38d5e836b04",  # pragma: allowlist secret
        "RemoteModuleLifecycleResponseV1": "73a78015363c",  # pragma: allowlist secret
        "RemoteModuleSystemTeardownRequestV1": "46b0d12b2f0a",  # pragma: allowlist secret
    },
    "kdive.providers.external_boot_authority.module_documents": {
        "_RemoteModuleDocument": "72c56660e4d7",  # pragma: allowlist secret
        "_RootVolumeV1": "26dd8d35da05",  # pragma: allowlist secret
        "RemoteModuleOperationV1": "0ae5f41d469c",  # pragma: allowlist secret
        "RemoteModuleResultV1": "c3e7bd6116e3",  # pragma: allowlist secret
        "RemoteModuleRecoveryRefV1": "8af6f85eb3fe",  # pragma: allowlist secret
        "RemoteModuleRecoveryRefV2": "f974f022ffc0",  # pragma: allowlist secret
    },
    "kdive.processes.lifecycle.systemd.systemd_worker_state": {
        "SlotState": "9fc47984043e",  # pragma: allowlist secret
    },
}
_CASES = [(module, name) for module, names in _DIGESTS.items() for name in names]


@pytest.mark.parametrize(("module_name", "class_name"), _CASES)
def test_model_schema_is_unchanged(module_name: str, class_name: str) -> None:
    model = getattr(importlib.import_module(module_name), class_name)
    schema = model.model_json_schema(by_alias=True)
    digest = hashlib.sha256(json.dumps(schema).encode()).hexdigest()
    assert digest.startswith(_DIGESTS[module_name][class_name])
