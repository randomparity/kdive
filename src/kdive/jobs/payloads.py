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
from kdive.domain.operations.jobs import Job, JobAuthorizing, JobKind, PowerAction


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


class RunPayload(_PayloadBase):
    run_id: str

    @field_validator("run_id")
    @classmethod
    def _valid_run_id(cls, value: str) -> str:
        UUID(value)
        return value


class BuildPayload(RunPayload):
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
    """Payload for the composite build->install->boot job (ADR-0268, #866).

    Carries the build admission result (`build_host_id`, selected + leased at the
    `runs.build_install_boot` boundary) so the handler can synthesize a `BuildPayload` for the
    build phase; install/boot need only `run_id`.
    """


class PowerPayload(SystemPayload):
    action: PowerAction


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
    | type[RunPayload]
    | type[PowerPayload]
    | type[CaptureVmcorePayload]
    | type[ImageBuildPayload]
    | type[DiagnosticsWorkerCheckPayload]
    | type[BuildInstallBootPayload]
)
PayloadModel = (
    SystemPayload
    | ReprovisionPayload
    | RunPayload
    | PowerPayload
    | CaptureVmcorePayload
    | ImageBuildPayload
    | DiagnosticsWorkerCheckPayload
    | BuildInstallBootPayload
)

_PAYLOAD_MODELS: dict[JobKind, _PayloadModel] = {
    JobKind.PROVISION: SystemPayload,
    JobKind.REPROVISION: ReprovisionPayload,
    JobKind.TEARDOWN: SystemPayload,
    JobKind.BUILD: BuildPayload,
    JobKind.INSTALL: RunPayload,
    JobKind.BOOT: RunPayload,
    JobKind.FORCE_CRASH: SystemPayload,
    JobKind.POWER: PowerPayload,
    JobKind.CAPTURE_VMCORE: CaptureVmcorePayload,
    JobKind.IMAGE_BUILD: ImageBuildPayload,
    JobKind.DIAGNOSTICS_WORKER_CHECK: DiagnosticsWorkerCheckPayload,
    JobKind.BUILD_INSTALL_BOOT: BuildInstallBootPayload,
    JobKind.AUTHORIZE_SSH_KEY: AuthorizeSshKeyPayload,
}
_RUN_PAYLOAD_MODELS: dict[JobKind, type[RunPayload]] = {
    JobKind.BUILD: BuildPayload,
    JobKind.INSTALL: RunPayload,
    JobKind.BOOT: RunPayload,
    JobKind.CAPTURE_VMCORE: CaptureVmcorePayload,
    JobKind.BUILD_INSTALL_BOOT: BuildInstallBootPayload,
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
    model_class = _PAYLOAD_MODELS[kind]
    try:
        model = payload if isinstance(payload, model_class) else model_class.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error(f"{kind.value} payload", exc) from exc
    return model.model_dump(mode="json", exclude_none=True)


def load_payload[T: PayloadModel](job: Job, model_class: type[T]) -> T:
    """Decode ``job.payload`` as ``model_class`` after checking the job kind contract."""
    expected = _PAYLOAD_MODELS[job.kind]
    if not issubclass(model_class, expected) and not issubclass(expected, model_class):
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
