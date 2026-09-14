"""Bind one worker invocation to its configured authority route (ADR-0612)."""

from collections.abc import Callable
from dataclasses import dataclass

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.models import ExternalBootAuthorityMarkerV1
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.external_boot_authority.protocol import (
    AuthorityAcknowledgementV1,
    AuthorityConflictResolutionRequestV1,
    AuthorityMutationRequestV1,
    AuthorityObservationV1,
    AuthorityPreparationMutationRequestV1,
    AuthorityPreparationResponseV1,
    AuthorityTakeoverRequestV1,
    AuthorityTeardownMutationRequestV1,
    AuthorityTeardownResponseV1,
)
from kdive.providers.ports.authority import AuthorityRequestSender
from kdive.providers.ports.external_boot import RunningKernelObservation
from kdive.providers.ports.module_operation import (
    ModuleAuthority,
    ModuleBeginRequest,
    ModuleBeginResponse,
    ModuleCompletion,
    ModuleLifecycleRequest,
    ModulePreparationRequest,
)
from kdive.providers.system_authority.protocol import AuthoritySystemMarkerV1


def _refuse(reason: str) -> CategorizedError:
    return CategorizedError(
        f"authority: {reason}", category=ErrorCategory.CONFIGURATION_ERROR, terminal=True
    )


@dataclass(frozen=True, slots=True)
class ExternalBootAuthorityClient:
    sender: AuthorityRequestSender
    marker: ExternalBootAuthorityMarkerV1
    deadline: float
    modules: ModuleAuthority | None = None

    def _validate(
        self,
        request: (
            AuthorityTakeoverRequestV1
            | AuthorityMutationRequestV1
            | AuthorityPreparationMutationRequestV1
            | AuthorityTeardownMutationRequestV1
        ),
    ) -> None:
        for name in (
            "system_id",
            "activation_id",
            "run_id",
            "plan_identity",
            "purpose",
            "provider_kind",
            "authority_instance",
        ):
            if getattr(request, name) != getattr(self.marker, name):
                raise _refuse("binding-mismatch")

    async def acknowledge(self, request: AuthorityTakeoverRequestV1) -> AuthorityAcknowledgementV1:
        self._validate(request)
        return await self.sender.acknowledge_takeover(request, deadline=self.deadline)

    async def execute(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        self._validate(request)
        return await self.sender.execute_mutation(request, deadline=self.deadline)

    async def execute_preparation(
        self, request: AuthorityPreparationMutationRequestV1
    ) -> AuthorityPreparationResponseV1:
        self._validate(request)
        return await self.sender.execute_preparation(request, deadline=self.deadline)

    async def execute_teardown(
        self, request: AuthorityTeardownMutationRequestV1
    ) -> AuthorityTeardownResponseV1:
        self._validate(request)
        return await self.sender.execute_teardown(request, deadline=self.deadline)

    async def open_remote_module_attempt(
        self, request: ModuleBeginRequest, *, deadline: float
    ) -> ModuleBeginResponse:
        del deadline
        self._validate(request.authority)
        if self.modules is None:
            raise _refuse("binding-unavailable")
        return await self.modules.open_remote_module_attempt(request, deadline=self.deadline)

    async def execute_remote_module_preparation(
        self, request: ModulePreparationRequest, *, deadline: float
    ) -> ModuleCompletion:
        del deadline
        self._validate(request.authority)
        if self.modules is None:
            raise _refuse("binding-unavailable")
        return await self.modules.execute_remote_module_preparation(request, deadline=self.deadline)

    async def execute_remote_module_lifecycle(
        self, request: ModuleLifecycleRequest, *, deadline: float
    ) -> ModuleCompletion:
        del deadline
        self._validate(request.authority)
        if self.modules is None:
            raise _refuse("binding-unavailable")
        return await self.modules.execute_remote_module_lifecycle(request, deadline=self.deadline)

    async def execute_conflict_resolution(
        self, request: AuthorityConflictResolutionRequestV1
    ) -> AuthorityObservationV1:
        self._validate(request)
        return await self.sender.execute_conflict_resolution(request, deadline=self.deadline)

    async def observe(self, request: AuthorityMutationRequestV1) -> AuthorityObservationV1:
        self._validate(request)
        return await self.sender.observe_authority(request, deadline=self.deadline)

    async def observe_running(
        self, request: AuthorityMutationRequestV1
    ) -> RunningKernelObservation:
        self._validate(request)
        return (await self.sender.observe_running(request, deadline=self.deadline)).to_observation()


type ExternalBootClientFactory = Callable[
    [ProviderBinding, ExternalBootAuthorityMarkerV1, float], ExternalBootAuthorityClient
]
type RecoveryOrphanAuthoritySenderFactory = Callable[[], AuthorityRequestSender]
type AuthoritySystemSenderFactory = Callable[
    [ProviderBinding, AuthoritySystemMarkerV1], AuthorityRequestSender
]


def _bound_sender(
    binding: ProviderBinding, provider_kind: str, authority_instance: str
) -> AuthorityRequestSender:
    if binding.kind.value != provider_kind:
        raise _refuse("binding-mismatch")
    if provider_kind != "local-libvirt" and binding.resource_name is None:
        raise _refuse("binding-unavailable")
    capability = binding.runtime.authority
    if capability is None or capability.authority_instance != authority_instance:
        raise _refuse("binding-mismatch")
    if capability.sender is None:
        raise _refuse("binding-unavailable")
    return capability.sender


def authority_system_sender_factory() -> AuthoritySystemSenderFactory:
    """Use the resolved runtime route for a server-derived System marker."""

    def build(binding: ProviderBinding, marker: AuthoritySystemMarkerV1) -> AuthorityRequestSender:
        if (
            marker.provider_kind != "local-libvirt"
            and binding.resource_name != marker.resource_name
        ):
            raise _refuse("binding-mismatch")
        return _bound_sender(binding, marker.provider_kind, marker.authority_instance)

    return build


def external_boot_client_factory() -> ExternalBootClientFactory:
    """Bind an invocation to its provider-owned route without consulting configuration."""

    def build(
        binding: ProviderBinding, marker: ExternalBootAuthorityMarkerV1, deadline: float
    ) -> ExternalBootAuthorityClient:
        sender = _bound_sender(binding, marker.provider_kind, marker.authority_instance)
        capability = binding.runtime.authority
        assert capability is not None
        return ExternalBootAuthorityClient(sender, marker, deadline, capability.modules)

    return build
