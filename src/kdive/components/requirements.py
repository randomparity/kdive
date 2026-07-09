"""Inert provider/profile requirement data shapes.

These classes are declared as fields on :class:`kdive.components.catalog.FixtureManifest` and
populated by fixture profile YAML. No code reads them for gating: kdive does not validate the
kernel ``.config`` or the boot cmdline against them. They survive only as data shapes so the
fixture catalog continues to parse.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ConfigRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required: dict[str, str] = Field(default_factory=dict)


class CmdlineRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_tokens: list[str] = Field(default_factory=list)
    protected_prefixes: list[str] = Field(default_factory=list)
