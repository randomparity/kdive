"""Thin MCP import surface for `runs.create`."""

from __future__ import annotations

from kdive.services.runs.admission import RunCreateRequest as RunCreateRequest
from kdive.services.runs.admission import RunReuseRequirementInput as RunReuseRequirementInput
from kdive.services.runs.admission import create_run as create_run

__all__ = ["RunCreateRequest", "RunReuseRequirementInput", "create_run"]
