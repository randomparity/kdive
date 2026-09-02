"""Fail-closed external-boot authority host readiness runtime (ADR-0584)."""

from __future__ import annotations

import asyncio
import os
import re
import socket
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from psycopg import AsyncConnection
from pydantic import SecretStr

import kdive.config as config_registry
from kdive.db.external_boot_authority_journal import (
    JournalHead,
    authenticate_authority_peer,
    list_journal_heads,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import record_digest
from kdive.providers.external_boot_authority.settings import (
    AUTHORITY_INSTANCE,
    AUTHORITY_JOURNAL_DIR,
    AUTHORITY_PROVIDER_SOCKET,
    AUTHORITY_REQUEST_SOCKET,
    AUTHORITY_UID,
)
from kdive.providers.external_boot_authority.transport import (
    AuthorityListener,
    authority_server_name,
    health_tls_context,
    serve_authority_transport,
    validate_protected_parents,
    validate_socket_parent,
)

if TYPE_CHECKING:
    from kdive.providers.external_boot_authority.service import AuthenticatedPeer

READINESS_INTERVAL_SECONDS = 30.0
_HEALTH_ACCEPT_TIMEOUT_SECONDS = 0.1
PRIVATE_FILE_MODE = 0o400
JOURNAL_DIRECTORY_MODE = 0o700
MAX_JOURNAL_LANES = 4_096
_DATABASE_DSN_MAX_BYTES = 4_096
_DIAGNOSTIC_MAX_BYTES = 192
_SAFE_DIAGNOSTIC = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class HostReadinessError(CategorizedError):
    """A bounded, secret-free authority readiness failure."""

    def __init__(self, component: str, reason: str) -> None:
        safe_component = component if _SAFE_DIAGNOSTIC.fullmatch(component) else "host"
        safe_reason = reason if _SAFE_DIAGNOSTIC.fullmatch(reason) else "check-failed"
        message = f"{safe_component}: {safe_reason}"
        if len(message.encode("utf-8")) > _DIAGNOSTIC_MAX_BYTES:
            message = "host: check-failed"
        self.component = safe_component
        self.reason = safe_reason
        super().__init__(
            message,
            category=ErrorCategory.READINESS_FAILURE,
            details={"component": safe_component, "reason": safe_reason},
        )


@dataclass(frozen=True, slots=True)
class AuthorityHostConfig:
    authority_instance: str
    authority_uid: int
    journal_dir: Path
    request_socket: Path
    provider_socket: Path
    database_dsn: Path
    server_private_key: Path
    server_certificate: Path
    server_ca: Path
    worker_client_ca: Path
    health_client_certificate: Path
    health_client_key: Path

    @classmethod
    def from_environment(cls) -> AuthorityHostConfig:
        """Load fixed registry settings and fixed systemd credential descriptors."""
        credentials_raw = os.environ.get("CREDENTIALS_DIRECTORY")
        if not credentials_raw:
            raise HostReadinessError("credentials", "directory-missing")
        credentials = Path(credentials_raw)
        if not credentials.is_absolute():
            raise HostReadinessError("credentials", "directory-invalid")
        try:
            authority_instance = config_registry.require(AUTHORITY_INSTANCE)
            authority_uid = config_registry.require(AUTHORITY_UID)
            journal_dir = config_registry.require(AUTHORITY_JOURNAL_DIR)
            request_socket = config_registry.require(AUTHORITY_REQUEST_SOCKET)
            provider_socket = config_registry.require(AUTHORITY_PROVIDER_SOCKET)
        except CategorizedError:
            raise HostReadinessError("configuration", "invalid") from None
        return cls(
            authority_instance=authority_instance,
            authority_uid=authority_uid,
            journal_dir=journal_dir,
            request_socket=request_socket,
            provider_socket=provider_socket,
            database_dsn=credentials / "database-dsn",
            server_private_key=credentials / "service-credential",
            server_certificate=credentials / "server-certificate",
            server_ca=credentials / "server-ca",
            worker_client_ca=credentials / "worker-client-ca",
            health_client_certificate=credentials / "health-client-certificate",
            health_client_key=credentials / "health-client-key",
        )


def _validate_file(path: Path, *, owner_uid: int, mode: int, component: str) -> None:
    _validate_parent_components(path, component, owner_uid)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        raise HostReadinessError(component, "unsafe-file") from None
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != owner_uid
            or stat.S_IMODE(status.st_mode) != mode
        ):
            raise HostReadinessError(component, "unsafe-file")
    finally:
        os.close(descriptor)


def _validate_parent_components(path: Path, component: str, owner_uid: int) -> None:
    try:
        validate_protected_parents(path, owner_uid)
    except OSError:
        raise HostReadinessError(component, "unsafe-path") from None


def validate_credential_paths(config: AuthorityHostConfig) -> None:
    for path in (
        config.database_dsn,
        config.server_private_key,
        config.server_certificate,
        config.server_ca,
        config.worker_client_ca,
        config.health_client_certificate,
        config.health_client_key,
    ):
        _validate_file(
            path, owner_uid=config.authority_uid, mode=PRIVATE_FILE_MODE, component="credentials"
        )


def _validate_journal_root(config: AuthorityHostConfig) -> int:
    _validate_parent_components(config.journal_dir, "journal", config.authority_uid)
    try:
        descriptor = os.open(
            config.journal_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        )
    except OSError:
        raise HostReadinessError("journal", "unsafe-tree") from None
    status = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != config.authority_uid
        or stat.S_IMODE(status.st_mode) != JOURNAL_DIRECTORY_MODE
    ):
        os.close(descriptor)
        raise HostReadinessError("journal", "unsafe-tree")
    return descriptor


def _local_lanes(config: AuthorityHostConfig, root_fd: int) -> dict[str, str]:
    lanes: dict[str, str] = {}
    try:
        names = os.listdir(root_fd)
    except OSError:
        raise HostReadinessError("journal", "unsafe-tree") from None
    if len(names) > MAX_JOURNAL_LANES:
        raise HostReadinessError("journal", "lane-limit")
    for name in names:
        if not name.endswith(".jsonl") or name.count("/"):
            raise HostReadinessError("journal", "unsafe-tree")
        system_id = name.removesuffix(".jsonl")
        try:
            status = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except OSError:
            raise HostReadinessError("journal", "unsafe-tree") from None
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != config.authority_uid
            or stat.S_IMODE(status.st_mode) != 0o600
        ):
            raise HostReadinessError("journal", "unsafe-tree")
        try:
            from uuid import UUID

            UUID(system_id)
        except ValueError:
            raise HostReadinessError("journal", "unsafe-tree") from None
        lanes[system_id] = name
    return lanes


def restore_journal_inventory(config: AuthorityHostConfig, heads: tuple[JournalHead, ...]) -> None:
    """Require an exact local/database lane bijection and terminal head equality."""
    if len(heads) > MAX_JOURNAL_LANES:
        raise HostReadinessError("journal", "lane-limit")
    root_fd = _validate_journal_root(config)
    try:
        local = _local_lanes(config, root_fd)
    finally:
        os.close(root_fd)
    trusted = {str(head.system_id): head for head in heads}
    if len(trusted) != len(heads) or set(local) != set(trusted):
        raise HostReadinessError("journal", "inventory-mismatch")
    for system_id, head in trusted.items():
        journal: FileAuthorityJournal | None = None
        try:
            journal = FileAuthorityJournal(
                config.journal_dir, local[system_id], owner_uid=config.authority_uid
            )
            records = journal.load()
            if not records:
                raise HostReadinessError("journal", "head-mismatch")
            terminal = records[-1]
            if (
                head.authority_instance != config.authority_instance
                or terminal.authority_instance != head.authority_instance
                or terminal.system_id != head.system_id
                or terminal.sequence != head.sequence
                or record_digest(terminal) != head.digest
                or terminal.phase is not head.phase
                or terminal.authority_id != head.authority_id
                or terminal.generation != head.generation
                or terminal.operation_identity != head.operation_identity
            ):
                raise HostReadinessError("journal", "head-mismatch")
        except HostReadinessError:
            raise
        except OSError, ValueError:
            raise HostReadinessError("journal", "invalid-lane") from None
        finally:
            if journal is not None:
                journal.close()


async def check_database_role(connection: Any) -> None:
    """Require the authority LOGIN's exact non-privileged role and function shape."""
    query = """
        WITH RECURSIVE session_role AS (
            SELECT role.oid, role.rolname, role.rolcanlogin, role.rolinherit, role.rolsuper,
                   role.rolcreatedb, role.rolcreaterole, role.rolreplication, role.rolbypassrls
              FROM pg_roles AS role
             WHERE role.rolname = session_user
        ), role_walk(role_oid, path, depth, cycle) AS (
            SELECT membership.roleid, ARRAY[role.oid, membership.roleid], 1,
                   membership.roleid = role.oid
              FROM session_role AS role
              JOIN pg_auth_members AS membership ON membership.member = role.oid
            UNION ALL
            SELECT membership.roleid, walk.path || membership.roleid, walk.depth + 1,
                   membership.roleid = ANY(walk.path)
              FROM role_walk AS walk
              JOIN pg_auth_members AS membership ON membership.member = walk.role_oid
             WHERE NOT walk.cycle AND walk.depth < 64
        ), membership_shape AS (
            SELECT ARRAY(
                       SELECT DISTINCT granted.rolname
                         FROM role_walk AS walk
                         JOIN pg_roles AS granted ON granted.oid = walk.role_oid
                        WHERE NOT walk.cycle
                        ORDER BY granted.rolname
                        LIMIT 2
                   ) AS memberships,
                   COALESCE(
                       (SELECT bool_or(walk.cycle OR walk.depth = 64) FROM role_walk AS walk),
                       FALSE
                   ) OR (
                       SELECT count(DISTINCT walk.role_oid) > 1
                         FROM role_walk AS walk
                        WHERE NOT walk.cycle
                   ) AS membership_overflow
        )
        SELECT role.rolname, role.rolcanlogin, role.rolinherit, role.rolsuper,
               role.rolcreatedb, role.rolcreaterole, role.rolreplication, role.rolbypassrls,
               membership_shape.memberships, membership_shape.membership_overflow,
               has_function_privilege(session_user,
                   'public.list_external_boot_authority_journal_heads(text)', 'EXECUTE'),
               has_function_privilege(session_user,
                   'public.authenticate_external_boot_authority_peer(bytea)', 'EXECUTE'),
               has_table_privilege(session_user,
                   'public.external_boot_authority_journal_heads', 'SELECT,INSERT,UPDATE,DELETE')
          FROM session_role AS role
          CROSS JOIN membership_shape
    """
    try:
        async with connection.cursor() as cursor:
            await cursor.execute(query)
            row = await cursor.fetchone()
    except Exception:
        raise HostReadinessError("database-role", "query-failed") from None
    if row is None or len(row) != 13:
        raise HostReadinessError("database-role", "shape-invalid")
    (
        _session_user,
        can_login,
        inherit,
        superuser,
        create_db,
        create_role,
        replication,
        bypass_rls,
        memberships,
        membership_overflow,
        can_list,
        can_authenticate,
        has_table_access,
    ) = row
    membership_names = tuple(memberships)
    if any(
        (
            superuser,
            create_db,
            create_role,
            replication,
            bypass_rls,
            membership_overflow,
            any(name != "kdive_provider_authority" for name in membership_names),
            has_table_access,
        )
    ):
        raise HostReadinessError("database-role", "excessive-privilege")
    if (
        can_login is not True
        or inherit is not True
        or membership_names != ("kdive_provider_authority",)
        or can_list is not True
        or can_authenticate is not True
    ):
        raise HostReadinessError("database-role", "shape-invalid")


def _read_dsn(config: AuthorityHostConfig) -> str:
    _validate_file(
        config.database_dsn,
        owner_uid=config.authority_uid,
        mode=PRIVATE_FILE_MODE,
        component="credentials",
    )
    descriptor = os.open(config.database_dsn, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        raw = os.read(descriptor, _DATABASE_DSN_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    if not raw or len(raw) > _DATABASE_DSN_MAX_BYTES:
        raise HostReadinessError("database", "credential-invalid")
    try:
        dsn = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise HostReadinessError("database", "credential-invalid") from None
    if not dsn:
        raise HostReadinessError("database", "credential-invalid")
    return dsn


@asynccontextmanager
async def _database_connection(config: AuthorityHostConfig) -> AsyncIterator[AsyncConnection]:
    try:
        connection = await AsyncConnection.connect(_read_dsn(config), connect_timeout=5)
    except Exception:
        raise HostReadinessError("database", "connection-failed") from None
    try:
        yield connection
    finally:
        await connection.close()


async def _database_heads(config: AuthorityHostConfig) -> tuple[JournalHead, ...]:
    async with _database_connection(config) as connection:
        await check_database_role(connection)
        try:
            heads = await list_journal_heads(connection, config.authority_instance)
        except Exception:
            raise HostReadinessError("database", "inventory-failed") from None
    if len(heads) > MAX_JOURNAL_LANES:
        raise HostReadinessError("journal", "lane-limit")
    return heads


async def _check_provider_socket(config: AuthorityHostConfig) -> None:
    try:
        validate_protected_parents(config.provider_socket, config.authority_uid)
    except OSError:
        raise HostReadinessError("provider-socket", "unsafe-path") from None
    try:
        status = os.stat(config.provider_socket, follow_symlinks=False)
        if not stat.S_ISSOCK(status.st_mode) or status.st_uid != config.authority_uid:
            raise OSError
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(config.provider_socket)), timeout=5
        )
        del reader
        writer.close()
        await writer.wait_closed()
    except Exception:
        raise HostReadinessError("provider-socket", "unreachable") from None


async def _check_static_authority_host(config: AuthorityHostConfig) -> None:
    """Reconstruct the identity, filesystem, database, journal, and provider facts."""
    if os.geteuid() != config.authority_uid:
        raise HostReadinessError("identity", "uid-mismatch")
    validate_credential_paths(config)
    heads = await _database_heads(config)
    restore_journal_inventory(config, heads)
    await _check_provider_socket(config)


async def check_authority_host(config: AuthorityHostConfig, listener: AuthorityListener) -> None:
    """Reconstruct every static, journal, database, provider, socket, and TLS fact."""
    await _check_static_authority_host(config)
    try:
        listener.validate()
    except OSError, ValueError:
        raise HostReadinessError("listener", "invalid-evidence") from None


async def check_tls_health(listener: AuthorityListener, config: AuthorityHostConfig) -> None:
    """Perform a real authority-owned mutual-TLS handshake without an application frame."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(
                str(listener.socket_path),
                ssl=health_tls_context(config),
                server_hostname=authority_server_name(config.authority_instance),
            ),
            timeout=5,
        )
        try:
            result = await asyncio.wait_for(reader.read(1), timeout=_HEALTH_ACCEPT_TIMEOUT_SECONDS)
        except TimeoutError:
            result = None
        if result is not None:
            raise HostReadinessError("tls-health", "handshake-failed")
        writer.close()
        await writer.wait_closed()
    except Exception:
        raise HostReadinessError("tls-health", "handshake-failed") from None


def _notify_systemd(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.connect(address)
            notifier.sendall(message.encode("ascii"))
    except OSError:
        raise HostReadinessError("systemd-notify", "failed") from None


async def _close_listener(listener: AuthorityListener) -> None:
    try:
        await listener.close()
    except Exception:
        raise HostReadinessError("listener", "cleanup-failed") from None


async def _authenticate(config: AuthorityHostConfig, credential: SecretStr) -> AuthenticatedPeer:
    async with _database_connection(config) as connection:
        try:
            return await authenticate_authority_peer(connection, credential)
        except Exception:
            raise ValueError("unauthenticated") from None


async def run_authority_host(config: AuthorityHostConfig) -> None:
    """Validate, publish readiness, and retract it before exiting on any later drift."""
    listener: AuthorityListener | None = None

    async def authenticate(credential: SecretStr) -> AuthenticatedPeer:
        return await _authenticate(config, credential)

    try:
        await _check_static_authority_host(config)
        try:
            listener = await serve_authority_transport(config, authenticate, service=None)
        except Exception:
            raise HostReadinessError("listener", "bind-failed") from None
        try:
            listener.validate()
        except OSError, ValueError:
            raise HostReadinessError("listener", "invalid-evidence") from None
        await listener.start_serving()
        await check_tls_health(listener, config)
        _notify_systemd("READY=1")
        while True:
            await asyncio.sleep(READINESS_INTERVAL_SECONDS)
            await check_authority_host(config, listener)
            await check_tls_health(listener, config)
    finally:
        try:
            _notify_systemd("STOPPING=1")
        finally:
            if listener is not None:
                await _close_listener(listener)


async def check_authority_host_once(config: AuthorityHostConfig) -> None:
    """Run the complete check on fixed sibling probe and lock paths."""
    probe = config.request_socket.with_name("authority-probe.sock")
    try:
        validate_socket_parent(probe, config.authority_uid)
    except OSError:
        raise HostReadinessError("probe", "unsafe-directory") from None
    probe_config = replace(config, request_socket=probe)
    await _check_static_authority_host(probe_config)

    async def never_authenticate(_credential: SecretStr) -> AuthenticatedPeer:
        raise ValueError("health client has no incarnation credential")

    try:
        listener = await serve_authority_transport(probe_config, never_authenticate, service=None)
    except Exception:
        raise HostReadinessError("listener", "bind-failed") from None
    try:
        try:
            listener.validate()
        except OSError, ValueError:
            raise HostReadinessError("listener", "invalid-evidence") from None
        await listener.start_serving()
        await check_tls_health(listener, probe_config)
    finally:
        await _close_listener(listener)
