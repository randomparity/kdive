"""Provider-neutral module requests and validated evidence used by service orchestration."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityPreparationMutationRequestV1,
)
from kdive.serialization import JsonValue


@dataclass(frozen=True, slots=True)
class ModuleDocument:
    document: dict[str, JsonValue]
    identity: str


@dataclass(frozen=True, slots=True)
class ModuleOperation:
    system_id: str
    run_id: str
    plan_identity: str
    operation_nonce: str
    source_manifest: str
    release: str
    evidence: ModuleDocument


@dataclass(frozen=True, slots=True)
class ModuleRecovery:
    system_id: str
    run_id: str
    plan_identity: str
    operation_nonce: str
    operation_identity: str
    result_identity: str
    installed_entry_count: int | None
    installed_content_bytes: int | None
    document: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ModuleBeginRequest:
    authority: AuthorityPreparationMutationRequestV1
    budget_seconds: int


@dataclass(frozen=True, slots=True)
class ModuleBeginResponse:
    preparation: ModuleAttemptPreparationRequestV1
    operation: ModuleOperation


@dataclass(frozen=True, slots=True)
class ModulePreparationRequest:
    authority: AuthorityPreparationMutationRequestV1
    operation: ModuleOperation


@dataclass(frozen=True, slots=True)
class ModuleLifecycleRequest:
    authority: AuthorityMutationRequestV1
    action: Literal["restore", "reap"]
    budget_seconds: int


@dataclass(frozen=True, slots=True)
class ModuleCompletion:
    operation: ModuleOperation
    result: ModuleDocument
    recovery: ModuleRecovery
    action: Literal["restore", "reap"] | None = None
    volumes_absent: bool = False


class ModuleAuthority(Protocol):
    async def open_remote_module_attempt(
        self, request: ModuleBeginRequest, *, deadline: float
    ) -> ModuleBeginResponse: ...

    async def execute_remote_module_preparation(
        self, request: ModulePreparationRequest, *, deadline: float
    ) -> ModuleCompletion: ...

    async def execute_remote_module_lifecycle(
        self, request: ModuleLifecycleRequest, *, deadline: float
    ) -> ModuleCompletion: ...


class PreparationExecutor(Protocol):
    async def run[ResultT](self, operation: Callable[[], ResultT]) -> ResultT: ...


@dataclass(frozen=True, slots=True)
class RemoteDeviceIdentity:
    """Opaque physical identity returned by the provider host."""

    kind: Literal["inode", "block"]
    primary: int
    secondary: int


class RemoteDeviceIdentityPort(Protocol):
    """Resolve one host path within the enclosing preparation deadline.

    Return no path; operational timeouts raise redacted infrastructure failures.
    """

    def identity(self, path: str) -> RemoteDeviceIdentity | None: ...
