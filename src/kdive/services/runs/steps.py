"""Neutral run-step result and boot-cmdline helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.capacity.state import JobState
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle import Run, System
from kdive.jobs import queue
from kdive.profiles.provider_policy import capture_method
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.runtime import ProfilePolicy
from kdive.serialization import JsonValue

_REQUIRED_CONSOLE = "console=ttyS0"
_KDUMP_CRASHKERNEL = "crashkernel=256M"
# Disable KASLR on a gdbstub-debug boot so the running kernel's base matches the fetched
# vmlinux's link-time symbol addresses. With CONFIG_RANDOMIZE_BASE=y (the kdump fragment
# default) the kernel relocates to a random base, so a breakpoint set by symbol resolves to the
# wrong address over the gdbstub and never fires (#711).
_GDBSTUB_NOKASLR = "nokaslr"
_PLATFORM_OWNED_CMDLINE_TOKENS = ("root=", "console=", "crashkernel=")


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_str_list(value: object) -> list[str] | None:
    """Coerce a persisted JSON value to a ``list[str]``; ``None`` on any non-string-list (#760)."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return [item for item in value if isinstance(item, str)]


@dataclass(frozen=True, slots=True)
class BuildStepResult:
    """Typed boundary for the `run_steps(step='build').result` JSON payload."""

    kernel_ref: str | None
    debuginfo_ref: str | None
    build_id: str | None
    initrd_ref: str | None = None
    cmdline: str | None = None

    @classmethod
    def load(cls, value: object) -> BuildStepResult | None:
        if not isinstance(value, Mapping):
            return None
        result = cast("Mapping[str, object]", value)
        return cls(
            kernel_ref=_optional_str(result.get("kernel_ref")),
            debuginfo_ref=_optional_str(result.get("debuginfo_ref")),
            build_id=_optional_str(result.get("build_id")),
            initrd_ref=_optional_str(result.get("initrd_ref")),
            cmdline=_optional_str(result.get("cmdline")),
        )

    def dump(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if self.kernel_ref is not None:
            result["kernel_ref"] = self.kernel_ref
        if self.debuginfo_ref is not None:
            result["debuginfo_ref"] = self.debuginfo_ref
        if self.initrd_ref is not None:
            result["initrd_ref"] = self.initrd_ref
        if self.build_id is not None:
            result["build_id"] = self.build_id
        if self.cmdline is not None:
            result["cmdline"] = self.cmdline
        return result

    def refs(self) -> dict[str, str]:
        refs: dict[str, str] = {}
        if self.kernel_ref is not None:
            refs["kernel"] = self.kernel_ref
        if self.debuginfo_ref is not None:
            refs["vmlinux"] = self.debuginfo_ref
        if self.initrd_ref is not None:
            refs["initrd"] = self.initrd_ref
        return refs


async def existing_build_result(conn: AsyncConnection, run_id: UUID) -> BuildStepResult | None:
    async with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT result FROM run_steps WHERE run_id = %s AND step = 'build'", (run_id,)
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return BuildStepResult.load(row["result"])


_PROGRESS_STEPS = ("install", "boot")


@dataclass(frozen=True, slots=True)
class StepProgress:
    """Install/boot progress for a built Run, read from the `run_steps` ledger (ADR-0179)."""

    install: str
    boot: str
    boot_outcome: str | None
    console_evidence_artifact_id: str | None = None
    available_capture: list[str] | None = None
    inert_capture: list[str] | None = None

    def steps_map(self) -> dict[str, str]:
        """The fixed-key `runs.get` `data.steps` map; `build` is `succeeded` by construction."""
        return {"build": "succeeded", "install": self.install, "boot": self.boot}


async def step_progress(conn: AsyncConnection, run_id: UUID) -> StepProgress:
    """Read the `install`/`boot` ledger rows for a built Run (ADR-0179).

    A missing row is reported as ``pending`` (the step has not started); a present row
    carries its persisted ``running``/``succeeded`` state verbatim. ``boot_outcome`` is the
    ``boot`` step result's recorded outcome (``None`` when boot is unrecorded or carries no
    outcome), used to route the booted-run next-action. ``console_evidence_artifact_id`` is the
    console artifact id the boot handler recorded in the same ``boot`` result (ADR-0226), used to
    surface ``refs.console`` on ``runs.get``; ``None`` when boot is unrecorded or captured no
    console evidence. ``available_capture`` / ``inert_capture`` are the capture-disclosure lists the
    boot handler recorded for a crash outcome (ADR-0239); ``None`` when the boot result carries
    neither.
    """
    states = {step: "pending" for step in _PROGRESS_STEPS}
    boot_outcome: str | None = None
    console_evidence_artifact_id: str | None = None
    available_capture: list[str] | None = None
    inert_capture: list[str] | None = None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT step, state, result FROM run_steps WHERE run_id = %s AND step = ANY(%s)",
            (run_id, list(_PROGRESS_STEPS)),
        )
        rows = await cur.fetchall()
    for row in rows:
        states[row["step"]] = row["state"]
        if row["step"] == "boot" and isinstance(row["result"], Mapping):
            boot_result = cast("Mapping[str, object]", row["result"])
            outcome = boot_result.get("boot_outcome")
            boot_outcome = outcome if isinstance(outcome, str) else None
            console_evidence_artifact_id = _optional_str(boot_result.get("evidence_artifact_id"))
            available_capture = _optional_str_list(boot_result.get("available_capture"))
            inert_capture = _optional_str_list(boot_result.get("inert_capture"))
    return StepProgress(
        install=states["install"],
        boot=states["boot"],
        boot_outcome=boot_outcome,
        console_evidence_artifact_id=console_evidence_artifact_id,
        available_capture=available_capture,
        inert_capture=inert_capture,
    )


@dataclass(frozen=True, slots=True)
class BootAttempt:
    """A terminally-failed boot job behind a deleted boot step (#750, ADR-0230).

    The boot ``run_steps`` row is deleted on failure so a retry can recycle it (ADR-0185), but
    the boot job survives under its deterministic ``dedup_key`` carrying the terminal failure.
    This is the evidence ``runs.get`` surfaces so a caller can tell a failed boot from a
    never-attempted one.
    """

    job_id: UUID
    error_category: ErrorCategory | None

    def as_data(self) -> dict[str, JsonValue]:
        """The fixed-key ``data.boot_readiness`` payload; ``status`` is always ``"failed"``."""
        return {
            "job_id": str(self.job_id),
            "status": "failed",
            "error_category": self.error_category.value if self.error_category else None,
        }


async def failed_boot_attempt(conn: AsyncConnection, run_id: UUID) -> BootAttempt | None:
    """Return the Run's boot job iff it is terminally ``failed`` (#750, ADR-0230).

    Looks the boot job up by its deterministic ``dedup_key`` (``f"{run_id}:boot"``, matching
    ``_enqueue_step``). Returns ``None`` when no boot job exists (never attempted) or the job is
    ``queued``/``running``/``succeeded`` (an attempt in flight or already done) — only a terminal
    ``failed`` job is reportable boot-failure evidence.
    """
    job = await queue.get_by_dedup_key(conn, f"{run_id}:boot")
    if job is None or job.state is not JobState.FAILED:
        return None
    return BootAttempt(job_id=job.id, error_category=job.error_category)


async def installed_initrd_ref(conn: AsyncConnection, run_id: UUID) -> str | None:
    result = await existing_build_result(conn, run_id)
    if result is None:
        return None
    return result.initrd_ref


async def installed_debuginfo_ref(conn: AsyncConnection, run_id: UUID) -> str | None:
    """The Run's published DWARF vmlinux ref (ADR-0221), or ``None`` if it built none.

    Threaded into install so the local provider can stage the vmlinux in-guest for live drgn;
    other providers ignore it.
    """
    result = await existing_build_result(conn, run_id)
    if result is None:
        return None
    return result.debuginfo_ref


def system_required_cmdline(method: CaptureMethod, root_cmdline: str | None) -> str:
    """Compose the platform-owned kernel cmdline (ADR-0183).

    ``console=ttyS0`` (serial console capture parity) leads; ``root_cmdline`` follows when the
    provider owns the root device (``"root=/dev/vda"`` for local-libvirt's direct-kernel boot,
    ``None`` for remote-libvirt where the in-guest bootloader supplies ``root=UUID=…``); the kdump
    crashkernel reservation, or the gdbstub ``nokaslr`` pin (#711), is last. The trailing token is
    keyed off the resolved ``method``, so a System that sets both ``crashkernel`` and
    ``debug.gdbstub`` resolves to ``KDUMP`` (crashkernel wins in ``capture_method``) and gets
    crashkernel, not ``nokaslr`` — a live gdb symbol breakpoint over such a System would still miss
    the running KASLR base. Tokens are emitted in this fixed order, dropping ``None``.
    """
    tokens = [_REQUIRED_CONSOLE]
    if root_cmdline:
        tokens.append(root_cmdline)
    if method is CaptureMethod.KDUMP:
        tokens.append(_KDUMP_CRASHKERNEL)
    elif method is CaptureMethod.GDBSTUB:
        tokens.append(_GDBSTUB_NOKASLR)
    return " ".join(tokens)


def platform_owned_cmdline_token(cmdline: str | None) -> str | None:
    if not cmdline:
        return None
    return next((tok for tok in _PLATFORM_OWNED_CMDLINE_TOKENS if tok in cmdline), None)


async def cmdline_for(
    conn: AsyncConnection, run: Run, method: CaptureMethod, *, root_cmdline: str | None
) -> str:
    required = system_required_cmdline(method, root_cmdline)
    result = await existing_build_result(conn, run.id)
    if result is not None and result.cmdline is not None and result.cmdline.strip():
        return f"{required} {result.cmdline.strip()}"
    return required


def install_method_for(system: System, profile_policy: ProfilePolicy) -> CaptureMethod:
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    return capture_method(profile_policy, profile)
