"""Dependency-neutral types shared by worker lifecycle implementations."""

from typing import Literal, Protocol

type TerminationOutcome = Literal["succeeded", "failed", "killed"]


class WorkerDeathVerifier(Protocol):
    """Prove an immutable worker incarnation dead through deployment authority."""

    def verify_dead(self, worker_incarnation: str) -> str | None: ...
