"""CLI entrypoints for KDIVE processes and operator commands.

The long-running processes are `python -m kdive {server|worker|reconciler}`:
`server` runs the FastMCP streamable-HTTP app, `worker` runs the job-queue worker
loop, and `reconciler` runs the drift-repair loop (ADR-0021). One-shot operator
commands share the same parser: `migrate`, `install-fixtures`, `seed-project`, and
`build-fs`. Every command configures the structured logger first (ADR-0014).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import kdive.config as config
from kdive.cli.errors import exit_code_for_category
from kdive.config.core_settings import (
    HTTP_HOST,
    HTTP_PORT,
    LOG_LEVEL,
)
from kdive.db.pool import create_pool
from kdive.domain.errors import CategorizedError
from kdive.images.rootfs.command import add_build_fs_parser, run_build_fs
from kdive.images.rootfs.stage_volume import add_stage_volume_parser, run_stage_volume
from kdive.processes.reconciler import run_reconciler as _run_reconciler
from kdive.processes.server import run_server as _run_server
from kdive.processes.worker import run_worker as _run_worker
from kdive.security.secrets.redaction import Redactor, redact_url_credentials
from kdive.version import full_version

if TYPE_CHECKING:
    from kdive.observability.facade import Telemetry
    from kdive.security.secrets.secret_registry import SecretRegistry


_log = logging.getLogger(__name__)


class _VersionAction(argparse.Action):
    def __init__(self, option_strings: list[str], dest: str = argparse.SUPPRESS) -> None:
        super().__init__(option_strings=option_strings, dest=dest, nargs=0)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: object,
        option_string: str | None = None,
    ) -> None:
        del namespace, values, option_string
        print(f"kdive {full_version()}")
        parser.exit()


type _CommandHandler = Callable[[argparse.Namespace, SecretRegistry, Telemetry | None], None]
type _ArgumentAdder = Callable[[argparse.ArgumentParser], None]
type _CommandRegistrar = Callable[[Any], None]


@dataclass(frozen=True, slots=True)
class _Command:
    name: str
    help: str
    handler: _CommandHandler
    runnable: bool = False
    add_arguments: _ArgumentAdder | None = None
    custom_register: _CommandRegistrar | None = None

    def register(self, subparsers: Any) -> None:
        if self.custom_register is not None:
            self.custom_register(subparsers)
            return
        parser = subparsers.add_parser(self.name, help=self.help)
        if self.add_arguments is not None:
            self.add_arguments(parser)


def _require_telemetry(command: str, telemetry: Telemetry | None) -> Telemetry:
    if telemetry is None:
        raise RuntimeError(f"{command} command requires telemetry bootstrap")
    return telemetry


def _add_install_fixtures_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dest", default="/etc/kdive/fixtures/local-libvirt")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")


def _add_seed_project_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default="demo")
    parser.add_argument("--limit-kcu", default="1000000")
    parser.add_argument("--max-concurrent-allocations", type=int, default=4)
    parser.add_argument("--max-concurrent-systems", type=int, default=4)


def _handle_server(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del args
    initialized = _require_telemetry("server", telemetry)
    host = config.require(HTTP_HOST)
    port = config.require(HTTP_PORT)
    asyncio.run(_run_server(host, port, secret_registry, initialized))


def _handle_worker(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del args
    asyncio.run(_run_worker(secret_registry, _require_telemetry("worker", telemetry)))


def _handle_reconciler(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del args
    asyncio.run(_run_reconciler(secret_registry, _require_telemetry("reconciler", telemetry)))


def _handle_migrate(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del args, secret_registry, telemetry
    from kdive.admin.migrations import migrate

    migrate()


def _handle_install_fixtures(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del secret_registry, telemetry
    from pathlib import Path

    from kdive.admin.fixtures import install_fixtures

    install_fixtures(Path(args.dest), force=args.force)


def _handle_seed_project(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del secret_registry, telemetry
    from decimal import Decimal

    from kdive.admin.projects import seed_project

    asyncio.run(
        seed_project(
            project=args.project,
            limit_kcu=Decimal(args.limit_kcu),
            max_concurrent_allocations=args.max_concurrent_allocations,
            max_concurrent_systems=args.max_concurrent_systems,
        )
    )


def _add_verify_project_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default="demo")


def _handle_verify_project(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del secret_registry, telemetry
    from kdive.admin.projects import format_verify_result, redact_database_url, verify_project
    from kdive.db.pool import database_url

    status = asyncio.run(verify_project(project=args.project))
    message, code = format_verify_result(
        status, project=args.project, redacted_url=redact_database_url(database_url())
    )
    print(message)
    raise SystemExit(code)


def _handle_build_fs(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del secret_registry, telemetry
    run_build_fs(args)


def _handle_stage_volume(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del secret_registry, telemetry
    run_stage_volume(args)


def _add_reconcile_systems_arguments(parser: argparse.ArgumentParser) -> None:
    from pathlib import Path

    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help=(
            "path to systems.toml (default: KDIVE_SYSTEMS_TOML, then ~/.config/kdive/systems.toml)"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate systems.toml only (no DB/S3 writes); exit non-zero on a schema error",
    )


def _handle_reconcile_systems(
    args: argparse.Namespace, secret_registry: SecretRegistry, telemetry: Telemetry | None
) -> None:
    del secret_registry, telemetry
    from kdive.inventory.cli import reconcile_systems, validate_systems

    if args.check:
        raise SystemExit(validate_systems(args.path))

    from kdive.store.objectstore import object_store_from_env

    # A misconfigured object store raises a CategorizedError; the central handler in main()
    # surfaces its details and maps it to the category's exit code (ADR-0089).
    store = object_store_from_env()
    pool = create_pool(min_size=1)

    async def _run() -> int:
        await pool.open()
        try:
            return await reconcile_systems(args.path, pool=pool, store=store)
        finally:
            await pool.close()

    raise SystemExit(asyncio.run(_run()))


_COMMANDS: tuple[_Command, ...] = (
    _Command("server", "run the MCP streamable-HTTP server", _handle_server, runnable=True),
    _Command("worker", "run the job-queue worker loop", _handle_worker, runnable=True),
    _Command(
        "reconciler", "run the drift-repair reconciler loop", _handle_reconciler, runnable=True
    ),
    _Command("migrate", "apply database migrations", _handle_migrate, runnable=True),
    _Command(
        "install-fixtures",
        "install default fixture catalog",
        _handle_install_fixtures,
        add_arguments=_add_install_fixtures_arguments,
    ),
    _Command(
        "seed-project",
        "seed a project's budget/quota and register discovered resources",
        _handle_seed_project,
        add_arguments=_add_seed_project_arguments,
    ),
    _Command(
        "verify-project",
        "read back a project's budget/quota rows; exit non-zero if either is absent",
        _handle_verify_project,
        add_arguments=_add_verify_project_arguments,
    ),
    _Command(
        "build-fs",
        "build a local-libvirt filesystem image (debug guest or build host)",
        _handle_build_fs,
        custom_register=add_build_fs_parser,
    ),
    _Command(
        "stage-volume",
        "upload a built qcow2 to a remote-libvirt pool and capture its kernel config",
        _handle_stage_volume,
        custom_register=add_stage_volume_parser,
    ),
    _Command(
        "reconcile-systems",
        "reconcile systems.toml into the catalog once",
        _handle_reconcile_systems,
        add_arguments=_add_reconcile_systems_arguments,
    ),
)
_COMMAND_BY_NAME = {command.name: command for command in _COMMANDS}
_RUNNABLE = frozenset(command.name for command in _COMMANDS if command.runnable)


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser for process and operator subcommands."""
    parser = argparse.ArgumentParser(prog="kdive")
    parser.add_argument(
        "--log-level",
        default=None,
        help="structured-logging level (default: KDIVE_LOG_LEVEL, else INFO)",
    )
    parser.add_argument(
        "--version",
        action=_VersionAction,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for command in _COMMANDS:
        command.register(sub)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse arguments, configure logging, and dispatch to the chosen subcommand."""
    args = build_parser().parse_args(argv)
    from kdive.observability.facade import bootstrap_stdout_floor, init_telemetry
    from kdive.security.secrets.secret_registry import SecretRegistry

    # Snapshot the environment before any setting is read, including the logging
    # bootstrap (ADR-0087 decision 4): config.load() must precede the first config.get().
    config.load()
    level = args.log_level or config.require(LOG_LEVEL)
    secret_registry = SecretRegistry()
    # Bootstrap-ordering invariant (ADR-0090 §1): the stdlib stdout JSON floor is the
    # first startup step — before the OTel providers, config validation, or any backend
    # client — so early-startup records (config-validation failures, the most common
    # first-run fault) are never lost to an unconfigured root logger.
    bootstrap_stdout_floor(level, secret_registry=secret_registry)
    telemetry = None
    if args.command in _RUNNABLE:
        config.validate(args.command)
        # The config is validated on the stdout floor first; only then is the OTel
        # pipeline (which may construct an OTLP client) built and the floor handed over.
        telemetry = init_telemetry(args.command, secret_registry=secret_registry, level=level)
    _log.info("starting kdive %s (%s)", full_version(), args.command)
    try:
        _COMMAND_BY_NAME[args.command].handler(args, secret_registry, telemetry)
    except CategorizedError as error:
        _report_categorized_error(args.command, error, Redactor(registry=secret_registry))
        raise SystemExit(exit_code_for_category(error.category)) from error


def _report_categorized_error(command: str, error: CategorizedError, redactor: Redactor) -> None:
    """Log a categorized failure and print its actionable details, both surfaces redacted.

    Turns a bare one-line message behind a traceback into an operator-readable cause: an ERROR
    record on the structured-log floor (ADR-0090) that a deployment scrapes — carrying the
    command, category, message, details, and stack (``exc_info``) so an unexpected category is
    still diagnosable — plus the message and details on the operator's stderr. The caller maps
    ``error.category`` to a stable exit code.

    Two orthogonal redaction concerns:

    - ADR-0123 existence-leak seam (``suppressed_detail()``): intentionally *not* applied. That
      seam guards remote tool-response clients from resource-existence leaks on
      ``authorization_denied``/``not_found``; an operator invoking ``python -m kdive`` already has
      host access and needs the full, unredacted reason. Do not reuse this printer on a
      remote-client path.
    - Value-redaction floor: applied as defense-in-depth, matching the logging path. ``details`` is
      contractually secret-free, but stderr and the scraped log commonly land in the systemd
      journal or a CI log with broader read scope, so registered secrets, ``secret=value`` pairs
      and secret-KEYED values (``Redactor``), and URL-userinfo credentials
      (``redact_url_credentials``) are stripped here.
    """
    message = _redact_text(str(error), redactor)
    details = _redact_details(error.details, redactor)
    details_suffix = f" {details}" if details else ""
    _log.error(
        "%s command failed (%s): %s%s",
        command,
        error.category,
        message,
        details_suffix,
        exc_info=error,
    )
    print(f"error: {message}", file=sys.stderr)
    for key, value in details.items():
        print(f"  {key}: {value}", file=sys.stderr)


def _redact_text(text: str, redactor: Redactor) -> str:
    """Strip registered secrets, ``secret=value`` pairs, and URL-userinfo credentials from text.

    URL basic-auth userinfo — a ``user``/``password`` pair before an ``@`` host — is the common way
    a credential lands in a DSN or endpoint detail, and the ``Redactor`` key/value patterns alone do
    not catch it, so the text is run through :func:`redact_url_credentials` first.
    """
    return redactor.redact_text(redact_url_credentials(text))


def _redact_details(details: dict[str, object], redactor: Redactor) -> dict[str, object]:
    """Redact a details mapping to match ``Redactor.redact_value``'s mapping semantics.

    Strips URL-userinfo from string leaves first (which ``Redactor`` alone misses), then runs the
    whole mapping through :meth:`Redactor.redact_value` so a value under a *secret-named* key
    (``password``/``token``/``api_key``/...) is masked even when the value itself carries no
    recognizable secret pattern — the key-name signal the codebase's mapping redaction relies on.
    """
    url_stripped = {
        key: (redact_url_credentials(value) if isinstance(value, str) else value)
        for key, value in details.items()
    }
    return redactor.redact_value(url_stripped)


if __name__ == "__main__":
    main()
