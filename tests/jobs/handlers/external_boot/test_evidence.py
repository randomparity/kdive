"""Evidence composition, in isolation from the database and the provider.

These are the rules the commit enforces in SQL and would otherwise only be checked there, where a
violation raises SQLSTATE ``22023`` from inside ``_commit_external_result`` — the specification's
§8 *third* route to a wedged job, which surfaces as a lane-level warning carrying no job id. Pinning
them here turns that into an ordinary red.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from kdive.domain.external_boot_activation import ExternalBootActivation
from kdive.jobs.handlers.external_boot.evidence import (
    evidence_digest,
    known_object_refs,
    terminal_evidence,
)
from kdive.jobs.handlers.external_boot.runner import OperationContext
from tests.jobs.handlers.external_boot.vehicle import build_vehicle

DIGEST = "sha256:" + "c" * 64


def _activation() -> ExternalBootActivation:
    """Just the two evidence columns ``known_object_refs`` reads, from a real driven port."""
    vehicle = build_vehicle()
    return cast(
        ExternalBootActivation,
        SimpleNamespace(
            materialization=vehicle.materialization,
            recovery_point=vehicle.recovery_point,
        ),
    )


def test_known_object_refs_names_every_persisted_artifact_and_the_recovery_ref() -> None:
    activation = _activation()
    materialization = activation.materialization
    assert materialization is not None
    recovery = activation.recovery_point
    assert recovery is not None

    refs = known_object_refs(activation)

    expected = {
        materialization.artifacts.kernel.ref,
        materialization.artifacts.modules.ref,
        recovery.recovery_ref.ref,
    }
    if materialization.artifacts.initrd is not None:
        expected.add(materialization.artifacts.initrd.ref)
    assert set(refs) == expected


def test_known_object_refs_are_sorted_by_canonical_bytes_and_duplicate_free() -> None:
    """``ExternalBootTerminalEvidenceV1``'s validator rejects any other ordering."""
    refs = known_object_refs(_activation())

    encoded = [json.dumps({"ref": ref}, sort_keys=True, separators=(",", ":")) for ref in refs]
    assert encoded == sorted(encoded)
    assert len(encoded) == len(set(encoded))


def test_known_object_refs_is_empty_when_nothing_is_persisted() -> None:
    """A NULL column contributes no ref rather than a fabricated one."""
    bare = cast(ExternalBootActivation, SimpleNamespace(materialization=None, recovery_point=None))

    assert known_object_refs(bare) == ()


def test_terminal_evidence_takes_composite_state_from_the_acknowledgement() -> None:
    """Not a digest the handler computed: the commit stores the acknowledgement's on a conflict."""
    activation_id, system_id = uuid4(), uuid4()
    context = cast(
        OperationContext,
        SimpleNamespace(
            marker=SimpleNamespace(activation_id=activation_id, system_id=system_id),
            activation=_activation(),
            acknowledgement=SimpleNamespace(positive_quiescence_digest=DIGEST),
        ),
    )

    evidence = terminal_evidence(context, "active")

    assert evidence["composite_state"] == DIGEST
    assert evidence["activation_id"] == str(activation_id)
    assert evidence["system_id"] == str(system_id)
    assert evidence["outcome"] == "active"
    assert evidence["schema"] == "external-boot-terminal-evidence-v1"


def test_evidence_digest_is_canonical_and_key_order_independent() -> None:
    """The identity must name the evidence's *content*, not a dict's incidental key order."""
    one = {"b": 2, "a": 1}
    other = {"a": 1, "b": 2}

    assert evidence_digest(one) == evidence_digest(other)
    assert evidence_digest(one).startswith("sha256:")
    assert len(evidence_digest(one)) == 71
    assert evidence_digest({"a": 1}) != evidence_digest({"a": 2})
