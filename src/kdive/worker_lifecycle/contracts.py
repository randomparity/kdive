"""Dependency-neutral types shared by worker lifecycle implementations."""

from typing import Literal

type TerminationOutcome = Literal["succeeded", "failed", "killed"]


class WorkerDeathVerifier:
    """Registration-only marker retained under the existing configuration name.

    Recovery requires the witness's exact-holder termination row (ADR-0722),
    independently of this marker. It exposes no direct death-verification API.
    """
