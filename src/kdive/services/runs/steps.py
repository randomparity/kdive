"""Neutral run-step result and boot-cmdline helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import LiteralString, cast
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.capacity.state import JobState
from kdive.domain.capture import CaptureMethod
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Run, System
from kdive.domain.lifecycle.run_steps import (
    RUN_STEP_PENDING,
    RUN_STEP_SUCCEEDED,
    RunStepState,
    parse_persisted_run_step_state,
)
from kdive.domain.platform.arch_traits import arch_traits
from kdive.images.families._fedora_customize import READINESS_MARKER
from kdive.jobs import queue
from kdive.profiles.provider_policy import ProfilePolicy, capture_method
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.serialization import JsonValue

DEFAULT_CRASHKERNEL = "256M"
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


def _is_provenance_value(value: object) -> bool:
    """Whether ``value`` is an admissible provenance value: ``str``, ``bool``, or a ``list[str]``.

    ``bool`` for warm-tree flags (``dirty``/``untracked``, ADR-0265/0282), ``list[str]`` for the
    ``dirty_files`` manifest (ADR-0282), ``str`` for every other field. ``bool`` subclasses ``int``
    in Python, so the scalar check is ``str | bool`` explicitly (never ``int``) to keep a stray
    numeric value (e.g. ``123``) rejected; a list is admitted only when every element is a ``str``.
    """
    if isinstance(value, str | bool):
        return True
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _optional_provenance_map(value: object) -> dict[str, str | bool | list[str]] | None:
    """Coerce a persisted JSON value to ``dict[str, str | bool | list[str]]``; else ``None``.

    Accepts only a mapping whose every key is a string and every value satisfies
    :func:`_is_provenance_value` (#778, #861, #938). A malformed persisted ``build_provenance``
    (a non-mapping, a non-string key, a numeric value, a nested dict, or a list with a non-string
    element) degrades the whole map to ``None`` rather than carrying mistyped fields forward.
    """
    if not isinstance(value, Mapping):
        return None
    items = cast("Mapping[object, object]", value).items()
    if not all(isinstance(k, str) and _is_provenance_value(v) for k, v in items):
        return None
    return {k: cast("str | bool | list[str]", v) for k, v in items if isinstance(k, str)}


@dataclass(frozen=True, slots=True)
class BuildStepResult:
    """Typed boundary for the `run_steps(step='build').result` JSON payload."""

    kernel_ref: str | None
    debuginfo_ref: str | None
    build_id: str | None
    initrd_ref: str | None = None
    cmdline: str | None = None
    build_provenance: dict[str, str | bool | list[str]] | None = None

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
            build_provenance=_optional_provenance_map(result.get("build_provenance")),
        )

    def dump(self) -> dict[str, str | dict[str, str | bool | list[str]]]:
        result: dict[str, str | dict[str, str | bool | list[str]]] = {}
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
        if self.build_provenance is not None:
            result["build_provenance"] = dict(self.build_provenance)
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

    install: RunStepState
    boot: RunStepState
    boot_outcome: str | None
    console_evidence_artifact_id: str | None = None
    available_capture: list[str] | None = None
    inert_capture: list[str] | None = None
    matched_line: str | None = None
    installed_cmdline: str | None = None
    installed_crashkernel: str | None = None

    def steps_map(self) -> dict[str, str]:
        """The fixed-key `runs.get` `data.steps` map; `build` is `succeeded` by construction."""
        return {"build": RUN_STEP_SUCCEEDED, "install": self.install, "boot": self.boot}


# The persisted `boot` step `boot_outcome` value for a clean boot (recorded by the boot handler).
READY_BOOT_OUTCOME = "ready"
_READY_BOOT_SIGNAL = "console_marker"
_READY_BOOT_RULE = "marker line reached with no pre-marker crash signature"


def ready_boot_outcome() -> dict[str, str]:
    """The structured ``ready`` boot-outcome descriptor for the ``runs.get`` success path (#837).

    Mirrors the failure side's disclosure: names *what* defined a clean boot's success so an agent
    need not scrape the console to trust the verdict. The ``marker``/``unit`` derive from the
    image's single-source-of-truth ``READINESS_MARKER`` and the ``rule`` describes the
    console-verdict logic (``classify_console``, ADR-0055), so the surfaced wording cannot drift —
    the same single-sourcing the failure-side ``inert_capture_reason`` uses (ADR-0239). It
    interpolates no guest output (only build-time constants), so surfacing it is redaction-safe.
    Returns a fresh dict each call, so a caller nesting it into a response cannot mutate the source.
    """
    return {
        "outcome": READY_BOOT_OUTCOME,
        "signal": _READY_BOOT_SIGNAL,
        "marker": READINESS_MARKER,
        "unit": f"{READINESS_MARKER}.service",
        "rule": _READY_BOOT_RULE,
    }


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
    neither. ``matched_line`` is the console line that matched an ``expected_boot_failure``
    (ADR-0260); ``None`` when boot recorded no match. ``installed_cmdline`` is the applied client
    cmdline extra the install handler recorded (ADR-0299), surfaced for sweep read-back on
    ``runs.get``; ``None`` when install is unrecorded or applied no extra. ``installed_crashkernel``
    is the applied kdump reservation the install handler recorded (ADR-0300, #989); ``None`` when
    install is unrecorded or the default 256M was in force.
    """
    states: dict[str, RunStepState] = {step: RUN_STEP_PENDING for step in _PROGRESS_STEPS}
    boot_outcome: str | None = None
    console_evidence_artifact_id: str | None = None
    available_capture: list[str] | None = None
    inert_capture: list[str] | None = None
    matched_line: str | None = None
    installed_cmdline: str | None = None
    installed_crashkernel: str | None = None
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT step, state, result FROM run_steps WHERE run_id = %s AND step = ANY(%s)",
            (run_id, list(_PROGRESS_STEPS)),
        )
        rows = await cur.fetchall()
    for row in rows:
        step = cast("str", row["step"])
        states[step] = parse_persisted_run_step_state(row["state"], run_id=run_id, step=step)
        if step == "install" and isinstance(row["result"], Mapping):
            install_result = cast("Mapping[str, object]", row["result"])
            installed_cmdline = _optional_str(install_result.get("cmdline"))
            installed_crashkernel = _optional_str(install_result.get("crashkernel"))
        if step == "boot" and isinstance(row["result"], Mapping):
            boot_result = cast("Mapping[str, object]", row["result"])
            outcome = boot_result.get("boot_outcome")
            boot_outcome = outcome if isinstance(outcome, str) else None
            console_evidence_artifact_id = _optional_str(boot_result.get("evidence_artifact_id"))
            available_capture = _optional_str_list(boot_result.get("available_capture"))
            inert_capture = _optional_str_list(boot_result.get("inert_capture"))
            matched_line = _optional_str(boot_result.get("matched_line"))
    return StepProgress(
        install=states["install"],
        boot=states["boot"],
        boot_outcome=boot_outcome,
        console_evidence_artifact_id=console_evidence_artifact_id,
        available_capture=available_capture,
        inert_capture=inert_capture,
        matched_line=matched_line,
        installed_cmdline=installed_cmdline,
        installed_crashkernel=installed_crashkernel,
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


_LATEST_BOOTED_RUN_SQL: LiteralString = (
    "SELECT r.id FROM runs r "
    "JOIN run_steps st ON st.run_id = r.id AND st.step = 'boot' "
    "WHERE r.system_id = %s "
    "ORDER BY r.created_at DESC LIMIT 1"
)


async def latest_booted_run_id(conn: AsyncConnection, system_id: UUID) -> UUID | None:
    """Return the System's most-recently-booted Run id, or ``None`` (ADR-0279, #935).

    The console-rotation worker calls this once per job (under the per-System advisory lock)
    to attribute the parts it seals to the Run that produced the current boot. "Most-recently-
    booted" is the most-recently *created* Run bound to ``system_id`` that has a ``boot``
    ``run_steps`` row: a System hosts Runs sequentially and a Run is created before it boots, so
    among Runs that reached boot the newest-created one owns the live boot. Ordering is on the
    immutable ``runs.created_at`` rather than the trigger-bumped ``run_steps.updated_at``. A Run
    still in the build phase has no ``boot`` step and is excluded by the join.
    """
    async with conn.cursor() as cur:
        await cur.execute(_LATEST_BOOTED_RUN_SQL, (system_id,))
        row = await cur.fetchone()
    return None if row is None else row[0]


async def installed_initrd_ref(conn: AsyncConnection, run_id: UUID) -> str | None:
    result = await existing_build_result(conn, run_id)
    if result is None:
        return None
    return result.initrd_ref


async def build_baked_cmdline_extra(conn: AsyncConnection, run_id: UUID) -> str | None:
    """The extra cmdline args recorded on the Run's ``build`` step, or ``None`` (ADR-0299).

    This is the value ``runs.install`` compares a requested override against, and what the install
    handler records when no override is supplied. Matches the extra ``cmdline_for`` appends when
    ``override is None``.
    """
    result = await existing_build_result(conn, run_id)
    return result.cmdline if result is not None else None


async def installed_debuginfo_ref(conn: AsyncConnection, run_id: UUID) -> str | None:
    """The Run's published DWARF vmlinux ref (ADR-0221), or ``None`` if it built none.

    Threaded into install so the local provider can stage the vmlinux in-guest for live drgn;
    other providers ignore it.
    """
    result = await existing_build_result(conn, run_id)
    if result is None:
        return None
    return result.debuginfo_ref


def system_required_cmdline(
    method: CaptureMethod,
    root_cmdline: str | None,
    *,
    arch: str,
    crashkernel: str | None = None,
) -> str:
    """Compose the platform-owned kernel cmdline (ADR-0183, ADR-0300).

    ``console=<device>`` (serial console capture parity) leads; the device is arch-resolved
    (``ttyS0`` on x86, ``hvc0`` on pseries — see ``kdive.domain.platform``), so a ppc64le guest
    emits the readiness marker on the console it actually has. ``root_cmdline`` follows when the
    provider owns the root device (``"root=/dev/vda"`` for local-libvirt's direct-kernel boot,
    ``None`` for remote-libvirt where the in-guest bootloader supplies ``root=UUID=…``); the kdump
    crashkernel reservation, or the gdbstub ``nokaslr`` pin (#711), is last. The trailing token is
    keyed off the resolved ``method``, so a System that sets both ``crashkernel`` and
    ``debug.gdbstub`` resolves to ``KDUMP`` (crashkernel wins in ``capture_method``) and gets
    crashkernel, not ``nokaslr`` — a live gdb symbol breakpoint over such a System would still miss
    the running KASLR base. Tokens are emitted in this fixed order, dropping ``None``.

    ``crashkernel`` is the per-install reservation size (ADR-0300, #989): when set it replaces the
    default ``256M`` in the ``crashkernel=<size>`` token. It is honored **only** on the ``KDUMP``
    path — a non-kdump method never emits the token, so a supplied value there is inert (the tool
    boundary rejects that request; this stays a pure composition function).
    """
    tokens = [f"console={arch_traits(arch).console_device}"]
    if root_cmdline:
        tokens.append(root_cmdline)
    if method is CaptureMethod.KDUMP:
        tokens.append(f"crashkernel={crashkernel or DEFAULT_CRASHKERNEL}")
    elif method is CaptureMethod.GDBSTUB:
        tokens.append(_GDBSTUB_NOKASLR)
    return " ".join(tokens)


def platform_owned_cmdline_token(cmdline: str | None) -> str | None:
    if not cmdline:
        return None
    return next((tok for tok in _PLATFORM_OWNED_CMDLINE_TOKENS if tok in cmdline), None)


async def cmdline_for(
    conn: AsyncConnection,
    run: Run,
    method: CaptureMethod,
    *,
    root_cmdline: str | None,
    arch: str,
    override: str | None = None,
    crashkernel: str | None = None,
) -> str:
    """Compose the boot cmdline (ADR-0183, ADR-0299, ADR-0300).

    ``arch`` is the System's provisioning-profile architecture; it selects the leading
    ``console=`` device (``ttyS0``/``hvc0``). ``override`` is the ``runs.install`` cmdline (#988):
    when set it **replaces** the build-baked extra args for this install so an agent can iterate
    boot-parameter variants without a rebuild; when ``None`` the build step's recorded extra is
    appended (unchanged). ``crashkernel`` is the per-install kdump reservation size (#989): it tunes
    the platform ``crashkernel=<size>`` token and is orthogonal to ``override`` (both may be set).
    The platform-required tokens (``system_required_cmdline``) always lead and are never modifiable
    either way.
    """
    required = system_required_cmdline(method, root_cmdline, arch=arch, crashkernel=crashkernel)
    if override is not None:
        return f"{required} {override.strip()}"
    result = await existing_build_result(conn, run.id)
    if result is not None and result.cmdline is not None and result.cmdline.strip():
        return f"{required} {result.cmdline.strip()}"
    return required


def install_method_for(system: System, profile_policy: ProfilePolicy) -> CaptureMethod:
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    return capture_method(profile_policy, profile)


def system_arch(system: System) -> str:
    """The System's provisioning-profile architecture, for arch-resolved cmdline composition."""
    return ProvisioningProfile.parse(system.provisioning_profile).arch
