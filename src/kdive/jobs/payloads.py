"""Typed payload contracts for durable jobs.

The database stores payloads as JSONB, but the jobs boundary validates each
``JobKind`` before enqueue and handlers decode through these models instead of
sharing raw dict key conventions across modules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.images import ImageVisibility
from kdive.domain.operations.jobs import (
    RETIRED_JOB_KINDS,
    Job,
    JobAuthorizing,
    JobKind,
    PowerAction,
)
from kdive.domain.operations.sysrq import SysRqCommand


class PayloadValidationError(ValueError):
    """A job payload or authorizing tuple does not match its contract."""


class _PayloadBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Authorizing(_PayloadBase):
    """Principal and project that authorized a durable job."""

    principal: str
    agent_session: str | None = None
    project: str


class SystemPayload(_PayloadBase):
    system_id: str

    @field_validator("system_id")
    @classmethod
    def _valid_system_id(cls, value: str) -> str:
        UUID(value)
        return value


class ReprovisionPayload(SystemPayload):
    profile_digest: str


class AuthorizeSshKeyPayload(SystemPayload):
    """A request to authorize an agent SSH public key in a System's guest (ADR-0271)."""

    public_key: str


class CheckSshReachablePayload(SystemPayload):
    """A request to probe a ready System's guest sshd reachability (ADR-0298, #972)."""


class ConsoleRotatePayload(SystemPayload):
    """A request to rotate a live System's growing console into redacted parts (#892).

    ``boot_id`` is a per-boot identity (the console log's ``os.stat`` ``dev:ino:mtime``) the
    worker handler uses to detect a power-cycle even when the new boot has already grown past the
    prior cursor offset (the local serial ``<log>`` truncates per power-cycle, ADR-0258). An empty
    string is a reset-forcing identity: the reconciler that enqueues this may not be co-located
    with the worker that owns the console file, so a stat it cannot take degrades to ``""``.
    """

    boot_id: str = ""


class RunPayload(_PayloadBase):
    run_id: str

    @field_validator("run_id")
    @classmethod
    def _valid_run_id(cls, value: str) -> str:
        UUID(value)
        return value


class BuildPayload(RunPayload):
    # Inert: the server-build lane was removed, but the JobKind.BUILD enum value cannot be
    # dropped from Postgres, so this payload shape is retained for the enum->payload registry.
    cmdline: str | None = None
    build_host_id: str

    @field_validator("cmdline")
    @classmethod
    def _nonblank_cmdline(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("cmdline must not be blank")
        return stripped

    @field_validator("build_host_id")
    @classmethod
    def _valid_build_host_id(cls, value: str) -> str:
        UUID(value)
        return value


class BuildInstallBootPayload(BuildPayload):
    """Inert composite build->install->boot payload shape (retained for the JobKind enum).

    The server-build lane was removed; this payload no longer has a handler. It is kept only
    because ``JobKind.BUILD_INSTALL_BOOT`` cannot be dropped from the Postgres enum.
    """


class InstallPayload(RunPayload):
    """Payload for a `runs.install` step: the Run plus optional overrides (ADR-0299, ADR-0300).

    ``cmdline`` **replaces** the build-baked extra args for this install so an agent can iterate
    boot-parameter variants against an already-built kernel without a rebuild; ``None`` reuses the
    build-baked extra. A blank value is rejected (a caller mistake, distinct from omitting it). A
    pre-#988 install job serialized as bare ``{run_id}`` decodes here with ``cmdline=None``.

    ``crashkernel`` (#989) is the per-install kdump reservation size that replaces the default
    ``256M`` in the platform ``crashkernel=<size>`` token; ``None`` uses the default. The token is
    opaque (a size, or a multi-range like ``1G-2G:128M,2G-:256M``), but injection-safe: a blank
    value, internal whitespace (which would inject an extra kernel token into the space-joined
    cmdline), a non-printable character (which would fail XML rendering of the domain
    ``<cmdline>``), or a leading ``crashkernel=`` prefix is rejected. This validator is the
    worker-side backstop; the tool boundary rejects the same set with per-reason
    ``configuration_error`` codes.
    """

    cmdline: str | None = None
    crashkernel: str | None = None

    @field_validator("cmdline")
    @classmethod
    def _nonblank_cmdline(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("cmdline must not be blank")
        return stripped

    @field_validator("crashkernel")
    @classmethod
    def _safe_crashkernel(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("crashkernel must not be blank")
        if stripped.split() != [stripped]:
            raise ValueError("crashkernel must be a single token with no internal whitespace")
        if not stripped.isprintable():
            raise ValueError("crashkernel must be a single printable token")
        if stripped.lower().startswith("crashkernel="):
            raise ValueError("crashkernel must not include the 'crashkernel=' prefix")
        return stripped


class PowerPayload(SystemPayload):
    action: PowerAction


class SysRqPayload(SystemPayload):
    """A `diagnostic_sysrq` job: the System plus the allowlisted command to inject (ADR-0285).

    ``command`` is a :class:`~kdive.domain.operations.sysrq.SysRqCommand` value; the tool
    validates it against the allowlist before enqueue, and the worker resolves its magic-SysRq
    trigger character.
    """

    command: SysRqCommand


class CaptureVmcorePayload(RunPayload):
    """A `capture_vmcore` job: the crashing Run + core method (ADR-0244).

    Run-addressed (not System-addressed): the core is owned by the Run that crashed, and the
    worker resolves the bound System from ``run_id`` to locate the live resource.
    """

    method: CaptureMethod


class ImageBuildPayload(_PayloadBase):
    """The inputs an ``IMAGE_BUILD`` job carries: provider catalog identity + row scope.

    Cataloged providers derive arch, release, source, capabilities, format, and root device from
    their catalog row at job execution. ``visibility`` is ``public`` for an operator base image;
    a private image carries ``owner`` and ``expires_at`` (the handler validates the pairing
    through the publish service and the DB CHECK constraints).
    """

    provider: str
    name: str
    packages: tuple[str, ...] = ()
    visibility: ImageVisibility = ImageVisibility.PUBLIC
    owner: str | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _scope_fields_match_visibility(self) -> ImageBuildPayload:
        private = self.visibility is ImageVisibility.PRIVATE
        if private != (self.owner is not None):
            raise ValueError("owner must be set iff visibility is private")
        if private != (self.expires_at is not None):
            raise ValueError("expires_at must be set iff visibility is private")
        return self


class DiagnosticsWorkerCheckPayload(_PayloadBase):
    """The inputs a ``DIAGNOSTICS_WORKER_CHECK`` job carries (ADR-0164).

    Only the concrete provider id (``remote-libvirt``); the handler re-resolves the host config
    from the inventory at probe time, so no host identity or secret rides on the queue.
    """

    provider: str


_PayloadModel = (
    type[SystemPayload]
    | type[ReprovisionPayload]
    | type[AuthorizeSshKeyPayload]
    | type[CheckSshReachablePayload]
    | type[ConsoleRotatePayload]
    | type[RunPayload]
    | type[BuildPayload]
    | type[BuildInstallBootPayload]
    | type[InstallPayload]
    | type[PowerPayload]
    | type[SysRqPayload]
    | type[CaptureVmcorePayload]
    | type[ImageBuildPayload]
    | type[DiagnosticsWorkerCheckPayload]
)
PayloadModel = (
    SystemPayload
    | ReprovisionPayload
    | AuthorizeSshKeyPayload
    | CheckSshReachablePayload
    | ConsoleRotatePayload
    | RunPayload
    | BuildPayload
    | BuildInstallBootPayload
    | InstallPayload
    | PowerPayload
    | SysRqPayload
    | CaptureVmcorePayload
    | ImageBuildPayload
    | DiagnosticsWorkerCheckPayload
)

_ACTIVE_PAYLOAD_MODELS: dict[JobKind, _PayloadModel] = {
    JobKind.PROVISION: SystemPayload,
    JobKind.REPROVISION: ReprovisionPayload,
    JobKind.TEARDOWN: SystemPayload,
    JobKind.INSTALL: InstallPayload,
    JobKind.BOOT: RunPayload,
    JobKind.FORCE_CRASH: SystemPayload,
    JobKind.POWER: PowerPayload,
    JobKind.DIAGNOSTIC_SYSRQ: SysRqPayload,
    JobKind.CAPTURE_VMCORE: CaptureVmcorePayload,
    JobKind.IMAGE_BUILD: ImageBuildPayload,
    JobKind.DIAGNOSTICS_WORKER_CHECK: DiagnosticsWorkerCheckPayload,
    JobKind.AUTHORIZE_SSH_KEY: AuthorizeSshKeyPayload,
    JobKind.CHECK_SSH_REACHABLE: CheckSshReachablePayload,
    JobKind.CONSOLE_ROTATE: ConsoleRotatePayload,
}
_HISTORICAL_RUN_PAYLOAD_MODELS: dict[JobKind, type[RunPayload]] = {
    JobKind.BUILD: BuildPayload,
    JobKind.BUILD_INSTALL_BOOT: BuildInstallBootPayload,
}
_PAYLOAD_MODELS = _ACTIVE_PAYLOAD_MODELS
_RUN_PAYLOAD_MODELS: dict[JobKind, type[RunPayload]] = {
    JobKind.INSTALL: InstallPayload,
    JobKind.BOOT: RunPayload,
    JobKind.CAPTURE_VMCORE: CaptureVmcorePayload,
    **_HISTORICAL_RUN_PAYLOAD_MODELS,
}


def _validation_error(label: str, exc: ValidationError) -> PayloadValidationError:
    error = exc.errors()[0]
    loc = ".".join(str(part) for part in error.get("loc", ()))
    detail = f"{loc}: {error['msg']}" if loc else str(error["msg"])
    return PayloadValidationError(f"invalid {label}: {detail}")


def dump_authorizing(authorizing: Authorizing | JobAuthorizing) -> JobAuthorizing:
    """Validate and serialize the authorizing tuple for JSONB persistence."""
    try:
        model = (
            authorizing
            if isinstance(authorizing, Authorizing)
            else Authorizing.model_validate(authorizing)
        )
    except ValidationError as exc:
        raise _validation_error("job authorizing", exc) from exc
    return cast(JobAuthorizing, model.model_dump(mode="json"))


def load_authorizing(job: Job) -> Authorizing:
    """Decode a persisted job's authorizing tuple."""
    try:
        return Authorizing.model_validate(job.authorizing)
    except ValidationError as exc:
        raise _validation_error("job authorizing", exc) from exc


def dump_payload(kind: JobKind, payload: PayloadModel | dict[str, Any]) -> dict[str, Any]:
    """Validate and serialize a payload for ``kind``."""
    if kind in RETIRED_JOB_KINDS:
        raise PayloadValidationError(f"{kind.value} payload contract is retired")
    model_class = _ACTIVE_PAYLOAD_MODELS[kind]
    try:
        model = payload if isinstance(payload, model_class) else model_class.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error(f"{kind.value} payload", exc) from exc
    return model.model_dump(mode="json", exclude_none=True)


def load_payload[T: PayloadModel](job: Job, model_class: type[T]) -> T:
    """Decode ``job.payload`` as ``model_class`` after checking the job kind contract."""
    expected = _ACTIVE_PAYLOAD_MODELS.get(job.kind)
    if expected is None:
        raise PayloadValidationError(f"{job.kind.value} payload contract is retired")
    if model_class is not expected:
        raise PayloadValidationError(
            f"{model_class.__name__} does not match {job.kind.value} payload contract"
        )
    try:
        return model_class.model_validate(job.payload)
    except ValidationError as exc:
        raise _validation_error(f"{job.kind.value} payload", exc) from exc


def run_id_from_payload(kind: JobKind, payload: dict[str, Any]) -> UUID | None:
    """Return the payload's Run id for run-bearing job kinds, otherwise ``None``."""
    model_class = _RUN_PAYLOAD_MODELS.get(kind)
    if model_class is None:
        return None
    try:
        return UUID(model_class.model_validate(payload).run_id)
    except ValidationError as exc:
        raise _validation_error(f"{kind.value} payload", exc) from exc
