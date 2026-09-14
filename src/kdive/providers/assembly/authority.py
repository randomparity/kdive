"""Provider-owned authority route construction; worker encoding is injected by assembly."""

from collections.abc import Callable

from pydantic import SecretStr

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.providers.external_boot_authority.local_client import (
    LocalAuthorityBinding,
    _AuthorityUnixTransport,
    local_authority_binding,
)
from kdive.providers.external_boot_authority.network_client import (
    _AuthorityNetworkTransport,
    _resolve_tls_material,
)
from kdive.providers.remote_libvirt.config import RemoteAuthorityBinding
from kdive.security.secrets.secrets import SecretBackend


def authority_sender_factory(
    secret_backend: SecretBackend, borrow: Callable[[], SecretStr]
) -> Callable[[RemoteAuthorityBinding], AuthorityRequestSender]:
    """Capture the selected binding and existing owners; resolve no authority material."""

    def build(binding: RemoteAuthorityBinding) -> AuthorityRequestSender:
        return AuthorityRequestSender(
            lambda: _AuthorityNetworkTransport(
                binding, _resolve_tls_material(binding, secret_backend)
            ),
            borrow,
        )

    return build


def local_authority_sender_factory(
    secret_backend: SecretBackend,
    borrow: Callable[[], SecretStr],
    *,
    binding: LocalAuthorityBinding | None = None,
) -> AuthorityRequestSender | None:
    """Build the configured worker-local sender without accepting a caller route."""
    binding = binding if binding is not None else local_authority_binding()
    if binding is None:
        return None
    return AuthorityRequestSender(
        lambda: _AuthorityUnixTransport(binding, _resolve_tls_material(binding, secret_backend)),
        borrow,
    )


def recovery_orphan_authority_sender_factory(
    secrets: SecretBackend, borrow: Callable[[], SecretStr]
) -> Callable[[], AuthorityRequestSender] | None:
    """Build the one fixed local authority route for closed orphan requests."""
    binding = local_authority_binding()
    if binding is None:
        return None

    def build() -> AuthorityRequestSender:
        sender = local_authority_sender_factory(secrets, borrow, binding=binding)
        if sender is None:
            raise CategorizedError(
                "authority: binding-unavailable",
                category=ErrorCategory.CONFIGURATION_ERROR,
                terminal=True,
            )
        return sender

    return build
