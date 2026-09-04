"""Compose an operation's result from persisted rows and the acknowledgement, never from guesses.

Two rules hold everything here together, and both exist because breaking them is silent:

- **Every ``objects`` entry is a reference the commit's ``known_refs`` recursion already knows.**
  That recursion seeds from four activation columns and unions the reservation and release
  ``store_identity``/``owner_key`` (``0122_external_boot_authority.sql:1204-1249``); an unknown ref
  raises SQLSTATE ``22023``. A ``22023`` from the commit is the specification's §8 *third* route to
  a wedged job — it escapes ``_finalize_handler``'s ``try/except`` entirely and surfaces only as a
  lane-level warning with no job attribution. So the refs here are drawn from the rows the handler
  read, which is a deliberate **subset** of what ``known_refs`` accepts: the set whose provenance
  the handler can establish.

- **``composite_state`` is the acknowledgement's ``positive_quiescence_digest``**, not a digest the
  handler computes for itself. That is already what ``0122…sql:1514-1515`` stores as
  ``acknowledged_composite_state`` on a resolved conflict, so the evidence and the acknowledgement
  agree by construction rather than by convention.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from kdive.domain.external_boot_activation import ExternalBootActivation
from kdive.jobs.handlers.external_boot.runner import OperationContext
from kdive.jobs.models import ExternalBootAuthoritySuccessV1

__all__ = [
    "authority_result",
    "known_object_refs",
    "release_identity",
    "terminal_evidence",
]


def known_object_refs(activation: ExternalBootActivation) -> tuple[str, ...]:
    """Every provider reference the activation's persisted evidence already names.

    Sorted and duplicate-free by the canonical JSON of ``{"ref": …}``, which is what
    ``ExternalBootTerminalEvidenceV1``'s validator requires — sorting the bare strings is not the
    same ordering once a ref contains a character that sorts differently inside a JSON document.
    """
    refs: set[str] = set()
    if activation.materialization is not None:
        artifacts = activation.materialization.artifacts
        refs.add(artifacts.kernel.ref)
        refs.add(artifacts.modules.ref)
        if artifacts.initrd is not None:
            refs.add(artifacts.initrd.ref)
    if activation.recovery_point is not None:
        refs.add(activation.recovery_point.recovery_ref.ref)
    return tuple(
        sorted(
            refs, key=lambda ref: json.dumps({"ref": ref}, sort_keys=True, separators=(",", ":"))
        )
    )


def terminal_evidence(context: OperationContext, outcome: str) -> dict[str, Any]:
    """Terminal evidence for ``outcome``, composed only from rows and the acknowledgement."""
    return {
        "schema": "external-boot-terminal-evidence-v1",
        "activation_id": str(context.marker.activation_id),
        "system_id": str(context.marker.system_id),
        "outcome": outcome,
        "composite_state": context.acknowledgement.positive_quiescence_digest,
        "objects": [{"ref": ref} for ref in known_object_refs(context.activation)],
        "observed_at": _now(),
    }


def release_identity(evidence: dict[str, Any]) -> str:
    """``sha256`` over the canonical release evidence, so the identity names that exact evidence."""
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def authority_result(
    context: OperationContext, result: dict[str, Any]
) -> ExternalBootAuthoritySuccessV1:
    """Wrap ``result`` in the binding the worker's ``_authority_binding_matches`` re-checks.

    Every binding field is taken from the marker or the allocation, never recomputed, so a result
    that reaches the commit carries exactly the admission facts the job was claimed under.

    The **subclass** is load-bearing, not decoration. ``_commit_external_result`` dispatches on
    ``isinstance``: an ``ExternalBootAuthoritySuccessV1`` goes to ``queue.complete_external_boot``,
    an ``ExternalBootAuthorityFailureV1`` to ``queue.fail_external_boot``, and anything else — the
    bare base class included — is logged as an "untyped result variant" and **written nowhere**, so
    the job keeps its lease and wedges ``running``. Returning the base class would satisfy every
    type annotation and every binding check and still never commit.
    """
    marker = context.marker
    return ExternalBootAuthoritySuccessV1.model_validate(
        {
            "schema": "external-boot-authority-result-v1",
            "authority_id": context.authority.authority_id,
            "generation": context.authority.generation,
            "activation_id": marker.activation_id,
            "run_id": marker.run_id,
            "system_id": marker.system_id,
            "plan_identity": marker.plan_identity,
            "purpose": marker.purpose,
            "provider_kind": marker.provider_kind,
            "authority_instance": marker.authority_instance,
            "admitted_operation": marker.operation,
            "operation_identity": marker.operation_identity,
            "operation_digest": context.authority.operation_digest,
            "journal_sequence": context.acknowledgement.journal_sequence,
            "journal_digest": context.acknowledgement.journal_digest,
            "result": result,
        }
    )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
