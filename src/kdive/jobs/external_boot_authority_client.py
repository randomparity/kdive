"""Bind one worker invocation to its configured authority route (ADR-0612)."""

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import SecretStr

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.authority_sender import (
    AuthorityRequestSender,
    authority_sender_factory,
    local_authority_sender_factory,
)
from kdive.jobs.models import ExternalBootAuthorityMarkerV1
from kdive.providers.core.resolver import ProviderBinding
from kdive.providers.external_boot_authority.local_client import local_authority_binding
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
from kdive.providers.ports.external_boot import RunningKernelObservation
from kdive.providers.remote_libvirt.config import remote_config_for_resource
from kdive.providers.remote_libvirt.external_boot_authority import (
    RemoteModuleLifecycleRequestV1,
    RemoteModuleLifecycleResponseV1,
    RemoteModulePreparationBeginRequestV1,
    RemoteModulePreparationBeginResponseV1,
    RemoteModuleTerminalPreparationResponseV1,
    RemoteModuleVolumePreparationRequestV1,
)
from kdive.providers.system_authority.protocol import AuthoritySystemMarkerV1
from kdive.security.secrets.secrets import SecretBackend


def _refuse(reason: str) -> CategorizedError:
    return CategorizedError(
        f"authority: {reason}", category=ErrorCategory.CONFIGURATION_ERROR, terminal=True
    )


@dataclass(frozen=True, slots=True)
class ExternalBootAuthorityClient:
    sender: AuthorityRequestSender
    marker: ExternalBootAuthorityMarkerV1
    deadline: float

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
        self, request: RemoteModulePreparationBeginRequestV1, *, deadline: float
    ) -> RemoteModulePreparationBeginResponseV1:
        del deadline
        self._validate(request.authority)
        return await self.sender.open_remote_module_attempt(request, deadline=self.deadline)

    async def execute_remote_module_preparation(
        self, request: RemoteModuleVolumePreparationRequestV1, *, deadline: float
    ) -> RemoteModuleTerminalPreparationResponseV1:
        del deadline
        self._validate(request.authority)
        return await self.sender.execute_remote_module_preparation(request, deadline=self.deadline)

    async def execute_remote_module_lifecycle(
        self, request: RemoteModuleLifecycleRequestV1, *, deadline: float
    ) -> RemoteModuleLifecycleResponseV1:
        del deadline
        self._validate(request.authority)
        return await self.sender.execute_remote_module_lifecycle(request, deadline=self.deadline)

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


def authority_system_sender_factory(
    secrets: SecretBackend, borrow: Callable[[], SecretStr]
) -> AuthoritySystemSenderFactory:
    """Resolve one fixed authority route for a server-derived System marker."""
    remote_sender = authority_sender_factory(secrets, borrow)

    def build(binding: ProviderBinding, marker: AuthoritySystemMarkerV1) -> AuthorityRequestSender:
        if binding.kind.value != marker.provider_kind:
            raise _refuse("binding-mismatch")
        if marker.provider_kind == "local-libvirt":
            configured = local_authority_binding()
            if configured is None or configured.authority_instance != marker.authority_instance:
                raise _refuse("binding-mismatch")
            sender = local_authority_sender_factory(secrets, borrow, binding=configured)
            if sender is None:
                raise _refuse("binding-unavailable")
            return sender
        if binding.resource_name != marker.resource_name:
            raise _refuse("binding-mismatch")
        try:
            configured = remote_config_for_resource(marker.resource_name).authority
        except CategorizedError:
            raise _refuse("binding-unavailable") from None
        if configured is None or configured.authority_instance != marker.authority_instance:
            raise _refuse("binding-mismatch")
        return remote_sender(configured)

    return build


def external_boot_client_factory(
    secrets: SecretBackend, borrow: Callable[[], SecretStr]
) -> ExternalBootClientFactory:
    """Resolve optional authority configuration only for a marked worker operation."""
    remote_sender = authority_sender_factory(secrets, borrow)

    def build(
        binding: ProviderBinding, marker: ExternalBootAuthorityMarkerV1, deadline: float
    ) -> ExternalBootAuthorityClient:
        if binding.kind.value != marker.provider_kind:
            raise _refuse("binding-mismatch")
        if marker.provider_kind == "local-libvirt":
            configured = local_authority_binding()
            if configured is None or configured.authority_instance != marker.authority_instance:
                raise _refuse("binding-mismatch")
            sender = local_authority_sender_factory(secrets, borrow, binding=configured)
            if sender is None:
                raise _refuse("binding-unavailable")
        else:
            if binding.resource_name is None:
                raise _refuse("binding-unavailable")
            try:
                remote_binding = remote_config_for_resource(binding.resource_name).authority
            except CategorizedError:
                raise _refuse("binding-unavailable") from None
            if (
                remote_binding is None
                or remote_binding.authority_instance != marker.authority_instance
            ):
                raise _refuse("binding-mismatch")
            sender = remote_sender(remote_binding)
        return ExternalBootAuthorityClient(sender, marker, deadline)

    return build


def recovery_orphan_authority_sender_factory(
    secrets: SecretBackend, borrow: Callable[[], SecretStr]
) -> RecoveryOrphanAuthoritySenderFactory | None:
    """Build the one fixed local authority route for closed orphan requests."""
    binding = local_authority_binding()
    if binding is None:
        return None

    def build() -> AuthorityRequestSender:
        sender = local_authority_sender_factory(secrets, borrow, binding=binding)
        if sender is None:
            raise _refuse("binding-unavailable")
        return sender

    return build
