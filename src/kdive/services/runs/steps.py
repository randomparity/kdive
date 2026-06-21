"""Neutral run-step result and boot-cmdline helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.capture import CaptureMethod
from kdive.domain.lifecycle import Run, System
from kdive.profiles.provider_policy import capture_method
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.runtime import ProfilePolicy

_REQUIRED_CONSOLE = "console=ttyS0"
_KDUMP_CRASHKERNEL = "crashkernel=256M"
_PLATFORM_OWNED_CMDLINE_TOKENS = ("root=", "console=", "crashkernel=")


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class BuildStepResult:
    """Typed boundary for the `run_steps(step='build').result` JSON payload."""

    kernel_ref: str | None
    debuginfo_ref: str | None
    build_id: str | None
    initrd_ref: str | None = None
    modules_ref: str | None = None
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
            modules_ref=_optional_str(result.get("modules_ref")),
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
        if self.modules_ref is not None:
            result["modules_ref"] = self.modules_ref
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
        if self.modules_ref is not None:
            refs["modules"] = self.modules_ref
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

    def steps_map(self) -> dict[str, str]:
        """The fixed-key `runs.get` `data.steps` map; `build` is `succeeded` by construction."""
        return {"build": "succeeded", "install": self.install, "boot": self.boot}


async def step_progress(conn: AsyncConnection, run_id: UUID) -> StepProgress:
    """Read the `install`/`boot` ledger rows for a built Run (ADR-0179).

    A missing row is reported as ``pending`` (the step has not started); a present row
    carries its persisted ``running``/``succeeded`` state verbatim. ``boot_outcome`` is the
    ``boot`` step result's recorded outcome (``None`` when boot is unrecorded or carries no
    outcome), used to route the booted-run next-action.
    """
    states = {step: "pending" for step in _PROGRESS_STEPS}
    boot_outcome: str | None = None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT step, state, result FROM run_steps WHERE run_id = %s AND step = ANY(%s)",
            (run_id, list(_PROGRESS_STEPS)),
        )
        rows = await cur.fetchall()
    for row in rows:
        states[row["step"]] = row["state"]
        if row["step"] == "boot" and isinstance(row["result"], Mapping):
            outcome = cast("Mapping[str, object]", row["result"]).get("boot_outcome")
            boot_outcome = outcome if isinstance(outcome, str) else None
    return StepProgress(install=states["install"], boot=states["boot"], boot_outcome=boot_outcome)


async def installed_initrd_ref(conn: AsyncConnection, run_id: UUID) -> str | None:
    result = await existing_build_result(conn, run_id)
    if result is None:
        return None
    return result.initrd_ref


async def installed_modules_ref(conn: AsyncConnection, run_id: UUID) -> str | None:
    result = await existing_build_result(conn, run_id)
    if result is None:
        return None
    return result.modules_ref


def system_required_cmdline(method: CaptureMethod, root_cmdline: str | None) -> str:
    """Compose the platform-owned kernel cmdline (ADR-0183).

    ``console=ttyS0`` (serial console capture parity) leads; ``root_cmdline`` follows when the
    provider owns the root device (``"root=/dev/vda"`` for local-libvirt's direct-kernel boot,
    ``None`` for remote-libvirt where the in-guest bootloader supplies ``root=UUID=…``); the kdump
    crashkernel reservation is last. Tokens are emitted in this fixed order, dropping ``None``.
    """
    tokens = [_REQUIRED_CONSOLE]
    if root_cmdline:
        tokens.append(root_cmdline)
    if method is CaptureMethod.KDUMP:
        tokens.append(_KDUMP_CRASHKERNEL)
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
