"""The build-profile schema and its parse boundary.

A build profile is a versioned, declarative document for the external-upload lane: the agent
builds the kernel locally and uploads the artifacts, so the profile names no source tree and no
kernel ``.config``. It is the opaque ``build_profile`` jsonb a Run carries; the create boundary
parses it via :meth:`BuildProfile.parse`.

The model is ``frozen`` (an immutable request input) and rejects unknown fields.
:meth:`BuildProfile.parse` is the sanctioned entry point: it maps Pydantic's structural
``ValidationError`` onto the wire taxonomy's ``configuration_error`` and scrubs submitted values
out of the error details, so a profile that references secret or guest-derived material cannot
leak it. Constructing the model directly bypasses this mapping and is a caller error.

kdive never *rejects* a build over the uploaded kernel ``.config``: no config-correctness
requirement is enforced here or downstream. ``runs.complete_build`` does read an uploaded
``effective_config`` to emit a non-blocking ``missing_boot_config`` advisory (ADR-0330), but the
completion always succeeds.
"""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES
from kdive.domain.profile_documents import SerializedBuildProfile
from kdive.profiles._schema import schema_version_validator
from kdive.profiles.types import BuildProfileInput


class BuildProfile(BaseModel):
    """External-build profile: a thin, versioned document with no source-tree fields.

    It carries its schema version and the target ``arch`` (default ``x86_64``, validated against
    ``arch_traits.SUPPORTED_ARCHES``); the artifact set is delivered through the upload lane
    (``artifacts.expected_uploads`` -> ``artifacts.create_run_upload`` -> ``runs.complete_build``),
    not named here. It remains the persisted ``build_profile`` jsonb envelope, and :meth:`parse`
    is the boundary that maps a structural ``ValidationError`` onto ``configuration_error`` and
    scrubs submitted values from the error detail (ADR-0029) — a bare ``model_validate`` would not.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    arch: str = Field(
        default="x86_64",
        description=(
            "Target CPU architecture the uploaded kernel is built for. One of "
            f"{', '.join(sorted(SUPPORTED_ARCHES))}; defaults to x86_64. Selects the "
            "boot/vmlinuz payload format the upload must carry (bzImage for x86_64, ELF "
            "vmlinux for ppc64le) - see resource://kdive/docs/operating/external-build-upload.md."
        ),
    )

    _reject_coerced_version = schema_version_validator

    @field_validator("arch")
    @classmethod
    def _known_arch(cls, value: str) -> str:
        if value not in SUPPORTED_ARCHES:
            supported = ", ".join(sorted(SUPPORTED_ARCHES))
            raise ValueError(f"unsupported arch; expected one of {supported}")
        return value

    @classmethod
    def parse(cls, data: BuildProfileInput) -> BuildProfile:
        """Validate a build-profile document, mapping any failure to ``configuration_error``.

        Args:
            data: The deserialized profile document (a mapping; YAML/JSON parsing is the
                caller's responsibility). Non-mapping inputs are rejected as
                ``CONFIGURATION_ERROR``.

        Returns:
            The validated, frozen :class:`BuildProfile`.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for any structural failure — a
                missing/unknown field, a wrong type, or an unreadable schema version. The
                error details carry field locations, types, and messages, but never the
                submitted values (redaction guarantee).
        """
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise CategorizedError(
                "invalid build profile",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "errors": exc.errors(
                        include_url=False, include_input=False, include_context=False
                    )
                },
            ) from exc


def dump_build_profile(profile: BuildProfile) -> SerializedBuildProfile:
    """Serialize a parsed build profile for JSON persistence."""
    return cast(SerializedBuildProfile, profile.model_dump(mode="json"))
