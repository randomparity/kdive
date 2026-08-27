"""Dependency-neutral types shared by worker lifecycle implementations."""

from typing import Literal

type TerminationOutcome = Literal["succeeded", "failed", "killed"]
