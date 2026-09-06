"""Fail-closed external-boot authority host readiness runtime (ADR-0584)."""

from __future__ import annotations

import asyncio
import os
import pwd
import re
import socket
import stat
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

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
    AUTHORITY_CLIENT_GID,
    AUTHORITY_DENIED_IDENTITIES,
    AUTHORITY_GID,
    AUTHORITY_INSTANCE,
    AUTHORITY_JOURNAL_DIR,
    AUTHORITY_NETWORK_ADDRESS,
    AUTHORITY_NETWORK_PORT,
    AUTHORITY_PROVIDER_SOCKET,
    AUTHORITY_REQUEST_SOCKET,
    AUTHORITY_UID,
    DEFAULT_DENIED_IDENTITIES,
)
from kdive.providers.external_boot_authority.transport import (
    AuthorityListener,
    AuthorityNetworkListener,
    SocketLockBusyError,
    authority_server_name,
    health_tls_context,
    serve_authority_network_transport,
    serve_authority_transport,
    validate_protected_parents,
    validate_socket_parent,
)

if TYPE_CHECKING:
    from kdive.providers.external_boot_authority.service import AuthenticatedPeer

READINESS_INTERVAL_SECONDS = 30.0
READINESS_CHECK_TIMEOUT_SECONDS = 20.0
JOURNAL_VALIDATION_TIMEOUT_SECONDS = 10.0
_HEALTH_ACCEPT_TIMEOUT_SECONDS = 0.1
PRIVATE_FILE_MODE = 0o400
SYSTEMD_CREDENTIAL_MODE = 0o440
JOURNAL_DIRECTORY_MODE = 0o700
PROVIDER_DIRECTORY_MODE = 0o700
PROVIDER_SOCKET_MODE = 0o700
MAX_JOURNAL_LANES = 4_096
_DATABASE_DSN_MAX_BYTES = 4_096
_DIAGNOSTIC_MAX_BYTES = 192
_SAFE_DIAGNOSTIC = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_DATABASE_CONNECTION_OPTIONS = (
    "-c statement_timeout=5000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=5000"
)
_AUTHORITY_INSTALL_DIR = Path("/opt/kdive-provider-authority")
_AUTHORITY_CREDENTIALS_SOURCE_DIR = Path("/etc/kdive/credentials/provider-authority")
_AUTHORITY_STATE_DIR = Path("/var/lib/kdive/provider-authority")
_POSIX_ACL_XATTRS = frozenset({"system.posix_acl_access", "system.posix_acl_default"})


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
    authority_gid: int
    authority_client_gid: int
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
    install_dir: Path = _AUTHORITY_INSTALL_DIR
    credentials_source_dir: Path = _AUTHORITY_CREDENTIALS_SOURCE_DIR
    state_dir: Path = _AUTHORITY_STATE_DIR
    denied_identities: tuple[str, ...] = DEFAULT_DENIED_IDENTITIES
    network_address: str | None = None
    network_port: int | None = None

    def __post_init__(self) -> None:
        if (self.network_address is None) != (self.network_port is None):
            raise HostReadinessError("configuration", "invalid")
        if self.network_address is not None and self.network_port is not None:
            try:
                AUTHORITY_NETWORK_ADDRESS.parse(self.network_address)
                if type(self.network_port) is not int:
                    raise ValueError
                AUTHORITY_NETWORK_PORT.parse(str(self.network_port))
            except ValueError, TypeError:
                raise HostReadinessError("configuration", "invalid") from None

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
            authority_gid = config_registry.require(AUTHORITY_GID)
            authority_client_gid = config_registry.require(AUTHORITY_CLIENT_GID)
            journal_dir = config_registry.require(AUTHORITY_JOURNAL_DIR)
            request_socket = config_registry.require(AUTHORITY_REQUEST_SOCKET)
            provider_socket = config_registry.require(AUTHORITY_PROVIDER_SOCKET)
            denied_identities = config_registry.require(AUTHORITY_DENIED_IDENTITIES)
            network_address = config_registry.get(AUTHORITY_NETWORK_ADDRESS)
            network_port = config_registry.get(AUTHORITY_NETWORK_PORT)
        except CategorizedError:
            raise HostReadinessError("configuration", "invalid") from None
        return cls(
            authority_instance=authority_instance,
            authority_uid=authority_uid,
            authority_gid=authority_gid,
            authority_client_gid=authority_client_gid,
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
            network_address=network_address,
            network_port=network_port,
            denied_identities=denied_identities,
        )


type CredentialProfile = Literal["authority-source", "systemd-projection"]


def _validate_credential_file(path: Path, *, owner_uid: int) -> CredentialProfile:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        raise HostReadinessError("credentials", "unsafe-file") from None
    try:
        status = os.fstat(descriptor)
        identity = (status.st_uid, stat.S_IMODE(status.st_mode))
        if not stat.S_ISREG(status.st_mode):
            raise HostReadinessError("credentials", "unsafe-file")
        if identity == (owner_uid, PRIVATE_FILE_MODE):
            profile: CredentialProfile = "authority-source"
        elif identity == (0, SYSTEMD_CREDENTIAL_MODE):
            profile = "systemd-projection"
        else:
            raise HostReadinessError("credentials", "unsafe-file")
    finally:
        os.close(descriptor)
    try:
        validate_protected_parents(path, owner_uid, require_root=profile == "systemd-projection")
    except OSError:
        raise HostReadinessError("credentials", "unsafe-path") from None
    return profile


def _validate_parent_components(path: Path, component: str, owner_uid: int) -> None:
    try:
        validate_protected_parents(path, owner_uid)
    except OSError:
        raise HostReadinessError(component, "unsafe-path") from None


def validate_credential_paths(config: AuthorityHostConfig) -> None:
    profiles = {
        _validate_credential_file(path, owner_uid=config.authority_uid)
        for path in (
            config.database_dsn,
            config.server_private_key,
            config.server_certificate,
            config.server_ca,
            config.worker_client_ca,
            config.health_client_certificate,
            config.health_client_key,
        )
    }
    if len(profiles) != 1:
        raise HostReadinessError("credentials", "mixed-profiles")


def _validate_access_boundary(config: AuthorityHostConfig) -> None:
    runtime_dir = config.request_socket.parent.parent
    protected_directories = (
        (config.install_dir, config.authority_uid, config.authority_gid, 0o700),
        (config.credentials_source_dir, config.authority_uid, config.authority_gid, 0o700),
        (config.state_dir, config.authority_uid, config.authority_gid, 0o700),
        (config.journal_dir, config.authority_uid, config.authority_gid, 0o700),
        (runtime_dir, config.authority_uid, config.authority_client_gid, 0o710),
        (config.request_socket.parent, config.authority_uid, config.authority_client_gid, 0o2750),
        (config.provider_socket.parent, config.authority_uid, config.authority_gid, 0o700),
    )
    for path, owner_uid, group_gid, mode in protected_directories:
        try:
            status = os.stat(path, follow_symlinks=False)
            attributes = frozenset(os.listxattr(path, follow_symlinks=False))
        except OSError:
            raise HostReadinessError("access-boundary", "unsafe-path") from None
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != owner_uid
            or status.st_gid != group_gid
            or stat.S_IMODE(status.st_mode) != mode
        ):
            raise HostReadinessError("access-boundary", "unsafe-path")
        if attributes & _POSIX_ACL_XATTRS:
            raise HostReadinessError("access-boundary", "unsafe-acl")

    authority_groups = {config.authority_gid, config.authority_client_gid}
    for identity in config.denied_identities:
        try:
            account = pwd.getpwnam(identity)
            identity_groups = set(os.getgrouplist(identity, account.pw_gid))
        except KeyError, OSError:
            raise HostReadinessError("access-boundary", "identity-missing") from None
        if account.pw_uid in {0, config.authority_uid} or identity_groups & authority_groups:
            raise HostReadinessError("access-boundary", "denied-identity")


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


type _JournalIdentity = tuple[int, int, int, int, int]


def _journal_identity(status: os.stat_result) -> _JournalIdentity:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _local_lanes(
    config: AuthorityHostConfig, root_fd: int
) -> dict[str, tuple[str, _JournalIdentity]]:
    lanes: dict[str, tuple[str, _JournalIdentity]] = {}
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
        lanes[system_id] = (name, _journal_identity(status))
    return lanes


def _restore_journal_inventory(
    config: AuthorityHostConfig,
    heads: tuple[JournalHead, ...],
    cache: dict[str, tuple[_JournalIdentity, JournalHead]],
    *,
    deadline: float | None,
) -> None:
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
    validated: dict[str, tuple[_JournalIdentity, JournalHead]] = {}
    for system_id, head in trusted.items():
        lane_name, identity = local[system_id]
        evidence = (identity, head)
        if cache.get(system_id) == evidence:
            validated[system_id] = evidence
            continue
        journal: FileAuthorityJournal | None = None
        try:
            journal = FileAuthorityJournal(
                config.journal_dir, lane_name, owner_uid=config.authority_uid
            )
            records = journal.load(deadline=deadline)
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
            validated[system_id] = evidence
        except HostReadinessError:
            raise
        except TimeoutError:
            raise
        except OSError, ValueError:
            raise HostReadinessError("journal", "invalid-lane") from None
        finally:
            if journal is not None:
                journal.close()
    cache.clear()
    cache.update(validated)


def restore_journal_inventory(config: AuthorityHostConfig, heads: tuple[JournalHead, ...]) -> None:
    """Require an exact local/database lane bijection and terminal head equality."""
    _restore_journal_inventory(config, heads, {}, deadline=None)


@dataclass(slots=True)
class JournalInventoryValidator:
    """Reuse unchanged lane evidence while keeping journal parsing off the event loop."""

    _cache: dict[str, tuple[_JournalIdentity, JournalHead]] = field(default_factory=dict)

    async def validate(self, config: AuthorityHostConfig, heads: tuple[JournalHead, ...]) -> None:
        deadline = time.monotonic() + JOURNAL_VALIDATION_TIMEOUT_SECONDS
        try:
            await asyncio.to_thread(
                _restore_journal_inventory,
                config,
                heads,
                self._cache,
                deadline=deadline,
            )
        except TimeoutError:
            raise HostReadinessError("journal", "validation-timeout") from None


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
        ), accepted_role AS (
            SELECT capability_role.oid,
                   capability_role.rolcanlogin
                   OR capability_role.rolinherit
                   OR capability_role.rolsuper
                   OR capability_role.rolcreatedb
                   OR capability_role.rolcreaterole
                   OR capability_role.rolreplication
                   OR capability_role.rolbypassrls
                   OR EXISTS (
                       SELECT 1
                         FROM pg_auth_members AS parent_membership
                        WHERE parent_membership.member = capability_role.oid
                   ) AS shape_invalid
              FROM pg_roles AS capability_role
             WHERE capability_role.rolname = 'kdive_provider_authority'
        ), accepted_membership AS (
            SELECT count(membership.roleid) <> 1
                   OR COALESCE(bool_or(
                       membership.admin_option
                       OR NOT membership.inherit_option
                       OR NOT membership.set_option
                   ), TRUE) AS shape_invalid
              FROM session_role AS role
              CROSS JOIN accepted_role
              LEFT JOIN pg_auth_members AS membership
                ON membership.member = role.oid
               AND membership.roleid = accepted_role.oid
        ), accepted_relation AS (
            SELECT unnest(ARRAY[
                'public.external_boot_authorities'::regclass,
                'public.external_boot_authority_acknowledgements'::regclass
            ])::oid AS oid
        ), accepted_function AS (
            SELECT unnest(ARRAY[
                'public.acknowledge_external_boot_authority(uuid,bigint,uuid,uuid,uuid,uuid,'
                    'text,uuid,integer,text,text,text,text,text,text,text,bigint,text,text)'
                    ::regprocedure,
                'public.resolve_allocating_external_boot_authority(text,uuid,bigint)'
                    ::regprocedure,
                'public.resolve_current_external_boot_authority_candidate(text,uuid,bigint)'
                    ::regprocedure,
                'public.resolve_current_external_boot_authority(text,uuid,bigint,bigint,text)'
                    ::regprocedure,
                'public.read_external_boot_authority_journal_head(text,uuid,bigint,text)'
                    ::regprocedure,
                'public.advance_external_boot_authority_journal_head(text,uuid,bigint,bigint,'
                    'text,jsonb)'::regprocedure,
                'public.list_external_boot_authority_journal_heads(text)'::regprocedure,
                'public.authenticate_external_boot_authority_peer(bytea)'::regprocedure
            ])::oid AS oid
        ), accepted_public_function AS (
            SELECT unnest(ARRAY[
                'public.image_catalog_phase_two_recovery_disarm()'::regprocedure,
                'public.reject_external_boot_release_mutation()'::regprocedure,
                'public.reject_external_boot_reservation_identity_change()'::regprocedure,
                'public.reject_system_root_provenance_update()'::regprocedure,
                'public.set_updated_at()'::regprocedure
            ])::oid AS oid
        ), unexpected_role_dependency AS (
            SELECT EXISTS (
                SELECT 1
                  FROM pg_shdepend AS dependency
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                 WHERE dependency.refclassid = 'pg_authid'::regclass
                   AND dependency.refobjid IN (role.oid, accepted_role.oid)
                   AND dependency.dbid IN (
                       0,
                       (SELECT database_row.oid
                          FROM pg_database AS database_row
                         WHERE database_row.datname = current_database())
                   )
                   AND dependency.deptype IN ('a', 'o', 'i', 'r')
                   AND NOT (
                       dependency.refobjid = accepted_role.oid
                       AND dependency.deptype = 'a'
                       AND dependency.dbid <> 0
                       AND (
                           (
                               dependency.classid = 'pg_namespace'::regclass
                               AND dependency.objid = 'public'::regnamespace
                           )
                           OR (
                               dependency.classid = 'pg_class'::regclass
                               AND dependency.objid IN (SELECT oid FROM accepted_relation)
                           )
                           OR (
                               dependency.classid = 'pg_proc'::regclass
                               AND dependency.objid IN (SELECT oid FROM accepted_function)
                           )
                       )
                   )
            ) AS present
        ), excess_application_acl AS (
            SELECT EXISTS (
                SELECT 1
                  FROM pg_class AS object
                  JOIN pg_namespace AS schema ON schema.oid = object.relnamespace
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(COALESCE(
                      object.relacl,
                      acldefault(
                          CASE WHEN object.relkind = 'S' THEN 's' ELSE 'r' END::"char",
                          object.relowner
                      )
                  )) AS acl
                 WHERE schema.nspname !~ '^pg_'
                   AND schema.nspname <> 'information_schema'
                   AND acl.grantee IN (0, role.oid, accepted_role.oid)
                   AND NOT (
                       acl.grantee = accepted_role.oid
                       AND NOT acl.is_grantable
                       AND acl.privilege_type = 'SELECT'
                       AND schema.nspname = 'public'
                       AND object.relkind IN ('r', 'p')
                       AND object.relname IN (
                           'external_boot_authorities',
                           'external_boot_authority_acknowledgements'
                       )
                   )
                UNION ALL
                SELECT 1
                  FROM pg_attribute AS column_acl
                  JOIN pg_class AS object ON object.oid = column_acl.attrelid
                  JOIN pg_namespace AS schema ON schema.oid = object.relnamespace
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(column_acl.attacl) AS acl
                 WHERE schema.nspname !~ '^pg_'
                   AND schema.nspname <> 'information_schema'
                   AND acl.grantee IN (0, role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_proc AS function_row
                  JOIN pg_namespace AS schema ON schema.oid = function_row.pronamespace
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(COALESCE(
                      function_row.proacl, acldefault('f', function_row.proowner)
                  )) AS acl
                 WHERE schema.nspname !~ '^pg_'
                   AND schema.nspname <> 'information_schema'
                   AND acl.grantee IN (0, role.oid, accepted_role.oid)
                   AND NOT (
                       NOT acl.is_grantable
                       AND acl.privilege_type = 'EXECUTE'
                       AND (
                           (
                               acl.grantee = accepted_role.oid
                               AND function_row.oid IN (SELECT oid FROM accepted_function)
                           )
                           OR (
                               acl.grantee = 0
                               AND function_row.oid IN (SELECT oid FROM accepted_public_function)
                           )
                       )
                   )
                UNION ALL
                SELECT 1
                  FROM pg_type AS type_row
                  JOIN pg_namespace AS schema ON schema.oid = type_row.typnamespace
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(COALESCE(
                      type_row.typacl, acldefault('T', type_row.typowner)
                  )) AS acl
                 WHERE schema.nspname !~ '^pg_'
                   AND schema.nspname <> 'information_schema'
                   AND acl.grantee IN (0, role.oid, accepted_role.oid)
                   AND NOT (
                       acl.grantee = 0
                       AND NOT acl.is_grantable
                       AND acl.privilege_type = 'USAGE'
                   )
                UNION ALL
                SELECT 1
                  FROM pg_namespace AS schema
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(COALESCE(
                      schema.nspacl, acldefault('n', schema.nspowner)
                  )) AS acl
                 WHERE schema.nspname !~ '^pg_'
                   AND schema.nspname <> 'information_schema'
                   AND acl.grantee IN (0, role.oid, accepted_role.oid)
                   AND NOT (
                       acl.grantee IN (0, accepted_role.oid)
                       AND NOT acl.is_grantable
                       AND acl.privilege_type = 'USAGE'
                       AND schema.nspname = 'public'
                   )
                UNION ALL
                SELECT 1
                  FROM pg_database AS database_row
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(COALESCE(
                      database_row.datacl, acldefault('d', database_row.datdba)
                  )) AS acl
                 WHERE acl.grantee IN (0, role.oid, accepted_role.oid)
                   AND NOT (
                       acl.grantee = 0
                       AND NOT acl.is_grantable
                       AND acl.privilege_type IN ('CONNECT', 'TEMPORARY')
                   )
                UNION ALL
                SELECT 1
                  FROM pg_foreign_data_wrapper AS wrapper
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(COALESCE(
                      wrapper.fdwacl, acldefault('F', wrapper.fdwowner)
                  )) AS acl
                 WHERE acl.grantee IN (0, role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_foreign_server AS server
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(COALESCE(
                      server.srvacl, acldefault('S', server.srvowner)
                  )) AS acl
                 WHERE acl.grantee IN (0, role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_language AS language_row
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(COALESCE(
                      language_row.lanacl, acldefault('l', language_row.lanowner)
                  )) AS acl
                 WHERE acl.grantee IN (0, role.oid, accepted_role.oid)
                   AND NOT (
                       acl.grantee = 0
                       AND NOT acl.is_grantable
                       AND acl.privilege_type = 'USAGE'
                   )
                UNION ALL
                SELECT 1
                  FROM pg_largeobject_metadata AS large_object
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(COALESCE(
                      large_object.lomacl, acldefault('L', large_object.lomowner)
                  )) AS acl
                 WHERE acl.grantee IN (0, role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_parameter_acl AS parameter_acl
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(parameter_acl.paracl) AS acl
                 WHERE acl.grantee IN (0, role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_tablespace AS tablespace
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(COALESCE(
                      tablespace.spcacl, acldefault('t', tablespace.spcowner)
                  )) AS acl
                 WHERE acl.grantee IN (0, role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_default_acl AS default_acl
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                  CROSS JOIN LATERAL aclexplode(default_acl.defaclacl) AS acl
                 WHERE acl.grantee IN (0, role.oid, accepted_role.oid)
            ) AS present
        ), owned_application_object AS (
            SELECT EXISTS (
                SELECT 1
                  FROM pg_class AS object
                  JOIN pg_namespace AS schema ON schema.oid = object.relnamespace
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                 WHERE schema.nspname !~ '^pg_'
                   AND schema.nspname <> 'information_schema'
                   AND object.relowner IN (role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_proc AS function_row
                  JOIN pg_namespace AS schema ON schema.oid = function_row.pronamespace
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                 WHERE schema.nspname !~ '^pg_'
                   AND schema.nspname <> 'information_schema'
                   AND function_row.proowner IN (role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_type AS type_row
                  JOIN pg_namespace AS schema ON schema.oid = type_row.typnamespace
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                 WHERE schema.nspname !~ '^pg_'
                   AND schema.nspname <> 'information_schema'
                   AND type_row.typowner IN (role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_namespace AS schema
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                 WHERE schema.nspname !~ '^pg_'
                   AND schema.nspname <> 'information_schema'
                   AND schema.nspowner IN (role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_database AS database_row
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                 WHERE database_row.datdba IN (role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_foreign_data_wrapper AS wrapper
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                 WHERE wrapper.fdwowner IN (role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_foreign_server AS server
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                 WHERE server.srvowner IN (role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_language AS language_row
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                 WHERE language_row.lanowner IN (role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_largeobject_metadata AS large_object
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                 WHERE large_object.lomowner IN (role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_tablespace AS tablespace
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                 WHERE tablespace.spcowner IN (role.oid, accepted_role.oid)
                UNION ALL
                SELECT 1
                  FROM pg_default_acl AS default_acl
                  CROSS JOIN session_role AS role
                  CROSS JOIN accepted_role
                 WHERE default_acl.defaclrole IN (role.oid, accepted_role.oid)
            ) AS present
        )
        SELECT role.rolname, role.rolcanlogin, role.rolinherit, role.rolsuper,
               role.rolcreatedb, role.rolcreaterole, role.rolreplication, role.rolbypassrls,
               membership_shape.memberships, membership_shape.membership_overflow,
               has_function_privilege(session_user,
                   'public.list_external_boot_authority_journal_heads(text)', 'EXECUTE'),
               has_function_privilege(session_user,
                   'public.authenticate_external_boot_authority_peer(bytea)', 'EXECUTE'),
               accepted_role.shape_invalid
                   OR accepted_membership.shape_invalid
                   OR unexpected_role_dependency.present
                   OR excess_application_acl.present
                   OR owned_application_object.present
          FROM session_role AS role
          CROSS JOIN membership_shape
          CROSS JOIN accepted_role
          CROSS JOIN accepted_membership
          CROSS JOIN unexpected_role_dependency
          CROSS JOIN excess_application_acl
          CROSS JOIN owned_application_object
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
        has_excess_application_privilege,
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
            has_excess_application_privilege,
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
    validate_credential_paths(config)
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
        connection = await AsyncConnection.connect(
            _read_dsn(config),
            connect_timeout=5,
            options=_DATABASE_CONNECTION_OPTIONS,
        )
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
        parent = os.stat(config.provider_socket.parent, follow_symlinks=False)
        status = os.stat(config.provider_socket, follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != config.authority_uid
            or parent.st_gid != config.authority_gid
            or stat.S_IMODE(parent.st_mode) != PROVIDER_DIRECTORY_MODE
            or not stat.S_ISSOCK(status.st_mode)
            or status.st_uid != config.authority_uid
            or status.st_gid != config.authority_gid
            or stat.S_IMODE(status.st_mode) != PROVIDER_SOCKET_MODE
        ):
            raise OSError
    except OSError:
        raise HostReadinessError("provider-socket", "unsafe-acl") from None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(config.provider_socket)), timeout=5
        )
        del reader
        writer.close()
        await writer.wait_closed()
    except Exception:
        raise HostReadinessError("provider-socket", "unreachable") from None


async def _check_static_authority_host(
    config: AuthorityHostConfig,
    journal_validator: JournalInventoryValidator | None = None,
) -> None:
    """Reconstruct the identity, filesystem, database, journal, and provider facts."""
    if os.geteuid() != config.authority_uid:
        raise HostReadinessError("identity", "uid-mismatch")
    await asyncio.to_thread(_validate_access_boundary, config)
    validate_credential_paths(config)
    heads = await _database_heads(config)
    await (journal_validator or JournalInventoryValidator()).validate(config, heads)
    await _check_provider_socket(config)


async def check_authority_host(
    config: AuthorityHostConfig,
    listener: AuthorityListener,
    journal_validator: JournalInventoryValidator | None = None,
    network_listener: AuthorityNetworkListener | None = None,
) -> None:
    """Reconstruct every static, journal, database, provider, socket, and TLS fact."""
    await _check_static_authority_host(config, journal_validator)
    try:
        listener.validate()
        if network_listener is not None:
            network_listener.validate()
    except OSError, ValueError:
        raise HostReadinessError("listener", "invalid-evidence") from None


async def check_tls_health(
    listener: AuthorityListener | AuthorityNetworkListener,
    config: AuthorityHostConfig,
) -> None:
    """Perform a real authority-owned mutual-TLS handshake without an application frame."""
    await _check_tls_connection(
        config,
        listener.address
        if isinstance(listener, AuthorityNetworkListener)
        else str(listener.socket_path),
    )


async def _check_tls_connection(
    config: AuthorityHostConfig,
    address: str | tuple[str, int],
) -> None:
    writer: asyncio.StreamWriter | None = None
    try:
        context = health_tls_context(config)
        server_name = authority_server_name(config.authority_instance)
        if isinstance(address, tuple):
            host, port = address
            connection = asyncio.open_connection(
                "127.0.0.1" if host == "0.0.0.0" else host,
                port,
                family=socket.AF_INET,
                ssl=context,
                server_hostname=server_name,
                ssl_handshake_timeout=5,
                ssl_shutdown_timeout=5,
            )
        else:
            connection = asyncio.open_unix_connection(
                address,
                ssl=context,
                server_hostname=server_name,
                ssl_handshake_timeout=5,
                ssl_shutdown_timeout=5,
            )
        reader, writer = await asyncio.wait_for(connection, timeout=5)
        try:
            result = await asyncio.wait_for(reader.read(1), timeout=_HEALTH_ACCEPT_TIMEOUT_SECONDS)
        except TimeoutError:
            result = None
        if result is not None:
            raise HostReadinessError("tls-health", "handshake-failed")
    except Exception:
        raise HostReadinessError("tls-health", "handshake-failed") from None
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=5)
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


async def _close_listener(listener: AuthorityListener | AuthorityNetworkListener) -> None:
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


async def _bounded_readiness_check(check: Awaitable[None]) -> None:
    try:
        async with asyncio.timeout(READINESS_CHECK_TIMEOUT_SECONDS):
            await check
    except TimeoutError:
        raise HostReadinessError("readiness", "timeout") from None


async def run_authority_host(config: AuthorityHostConfig) -> None:
    """Validate, publish readiness, and retract it before exiting on any later drift."""
    listener: AuthorityListener | None = None
    network_listener: AuthorityNetworkListener | None = None
    journal_validator = JournalInventoryValidator()

    async def authenticate(credential: SecretStr) -> AuthenticatedPeer:
        return await _authenticate(config, credential)

    try:
        await _bounded_readiness_check(_check_static_authority_host(config, journal_validator))
        try:
            listener = await serve_authority_transport(config, authenticate, service=None)
            if config.network_address is not None:
                network_listener = await serve_authority_network_transport(
                    config,
                    authenticate,
                    service=None,
                )
        except Exception:
            raise HostReadinessError("listener", "bind-failed") from None
        try:
            listener.validate()
            if network_listener is not None:
                network_listener.validate()
        except OSError, ValueError:
            raise HostReadinessError("listener", "invalid-evidence") from None
        await listener.start_serving()
        if network_listener is not None:
            await network_listener.start_serving()
        await _bounded_readiness_check(check_tls_health(listener, config))
        if network_listener is not None:
            await _bounded_readiness_check(check_tls_health(network_listener, config))
        _notify_systemd("READY=1")
        _notify_systemd("WATCHDOG=1")
        while True:
            await asyncio.sleep(READINESS_INTERVAL_SECONDS)

            async def periodic_check() -> None:
                await check_authority_host(config, listener, journal_validator, network_listener)
                await check_tls_health(listener, config)
                if network_listener is not None:
                    await check_tls_health(network_listener, config)

            await _bounded_readiness_check(periodic_check())
            _notify_systemd("WATCHDOG=1")
    finally:
        try:
            _notify_systemd("STOPPING=1")
        finally:
            try:
                if network_listener is not None:
                    await _close_listener(network_listener)
            finally:
                if listener is not None:
                    await _close_listener(listener)


async def check_authority_host_once(config: AuthorityHostConfig) -> None:
    """Run the complete check on fixed sibling probe and lock paths."""
    probe = config.request_socket.with_name("authority-probe.sock")
    try:
        validate_socket_parent(probe, config.authority_uid, config.authority_client_gid)
    except OSError:
        raise HostReadinessError("probe", "unsafe-directory") from None
    probe_config = replace(config, request_socket=probe)
    journal_validator = JournalInventoryValidator()
    await _bounded_readiness_check(_check_static_authority_host(probe_config, journal_validator))

    async def never_authenticate(_credential: SecretStr) -> AuthenticatedPeer:
        raise ValueError("health client has no incarnation credential")

    try:
        listener = await serve_authority_transport(probe_config, never_authenticate, service=None)
    except SocketLockBusyError:
        raise HostReadinessError("probe", "probe-busy") from None
    except Exception:
        raise HostReadinessError("listener", "bind-failed") from None
    try:
        try:
            listener.validate()
        except OSError, ValueError:
            raise HostReadinessError("listener", "invalid-evidence") from None
        await listener.start_serving()
        await _bounded_readiness_check(check_tls_health(listener, probe_config))
        if config.network_address is not None and config.network_port is not None:
            await _bounded_readiness_check(
                _check_tls_connection(config, (config.network_address, config.network_port))
            )
    finally:
        await _close_listener(listener)
