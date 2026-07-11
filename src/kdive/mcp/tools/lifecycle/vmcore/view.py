"""Response rendering helpers for vmcore and postmortem tools."""

from __future__ import annotations

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._vmcore_targets import (
    CONSOLE_CRASH,
    EXPECTED_CONSOLE_CRASH,
    NO_VMCORE,
)
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.artifacts.listing import RedactedArtifact

# Author-controlled narrative for the early-boot console-crash redirect (#734, ADR-0227). For a
# Run that declared expected_boot_failure=console_crash, the kernel panics before kdump's capture
# kernel is loaded via kexec, so kdump never produces a vmcore — none is expected by design, and
# the console artifact is the evidence source. One shared constant so the wording cannot drift; it
# interpolates no guest output, secret, or caller-supplied identifier.
CONSOLE_CRASH_GUIDANCE = (
    "this run declared an early-boot console_crash: the kernel panicked before the kdump "
    "capture kernel was loaded via kexec, so no vmcore is produced and none is expected. "
    "Read the console artifact instead — fetch its reference with runs.get."
)


def vmcore_collection(run_id: str, artifacts: list[RedactedArtifact]) -> ToolResponse:
    """Render one Run's redacted vmcore artifacts into a collection envelope."""
    items = [_vmcore_item(row) for row in artifacts if _is_redacted_vmcore(row.object_key)]
    return ToolResponse.collection(
        run_id,
        "ok",
        items,
        suggested_next_actions=["artifacts.get", "postmortem.crash"],
    )


def _is_redacted_vmcore(object_key: str) -> bool:
    return "/vmcore-" in object_key and object_key.endswith("-redacted")


def _vmcore_item(artifact: RedactedArtifact) -> ToolResponse:
    return ToolResponse.success(
        artifact.id,
        "available",
        suggested_next_actions=["artifacts.get"],
        refs={"object": artifact.object_key},
    )


def console_crash_redirect(run_id: str, exc: CategorizedError) -> ToolResponse | None:
    """The early-boot console-crash redirect, or ``None`` to fall through (#734, ADR-0227).

    Fires only when the resolver miss is ``no_vmcore`` **and** the Run declared
    ``expected_boot_failure=console_crash`` (carried on the error's ``details`` by the resolver).
    Returns a ``configuration_error`` — not the suppressed ``not_found`` the bare miss would yield
    — so the author-controlled narrative ``detail`` reaches the caller and points it at the
    console artifact via ``runs.get``. Every other miss returns ``None`` (the handler then keeps
    the existing reason-keyed ``vmcore_target_failure`` envelope unchanged).
    """
    if exc.details.get("reason") != NO_VMCORE:
        return None
    if exc.details.get("expected_boot_failure") != CONSOLE_CRASH:
        return None
    return ToolResponse.failure(
        run_id,
        ErrorCategory.CONFIGURATION_ERROR,
        detail=CONSOLE_CRASH_GUIDANCE,
        suggested_next_actions=["runs.get", "artifacts.list"],
        data={"reason": EXPECTED_CONSOLE_CRASH, "expected_boot_failure": CONSOLE_CRASH},
    )


def postmortem_success_response(
    run_id: str,
    *,
    transcript: str,
    truncated: bool,
    secret_registry: SecretRegistry,
) -> ToolResponse:
    """Render a successful crash transcript response with mandatory redaction."""
    redactor = Redactor(registry=secret_registry)
    return ToolResponse.success(
        run_id,
        "succeeded",
        suggested_next_actions=["postmortem.crash", "artifacts.list"],
        data={
            "transcript": redactor.redact_text(transcript),
            "truncated": truncated,
        },
    )


def triage_response(resp: ToolResponse) -> ToolResponse:
    """Relabel a successful crash response as the fixed triage workflow."""
    return resp.model_copy(
        update={"suggested_next_actions": ["postmortem.triage", "artifacts.list"]}
    )
