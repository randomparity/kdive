"""`--version` prints and exits; every command logs the version at startup (ADR-0041)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kdive.__main__ import (
    _handle_reconciler,
    _handle_server,
    _handle_worker,
    build_parser,
    main,
)
from kdive.cli.errors import exit_code_for_category
from kdive.domain.errors import CategorizedError, ErrorCategory


def test_version_flag_prints_and_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("kdive ")


def test_startup_logs_version(monkeypatch, caplog):
    # Don't actually run the async loop; just confirm main logs before dispatching.
    # A runnable command now validates config at startup, so supply the vars the
    # reconciler requires (KDIVE_DATABASE_URL and the S3 backend, ADR-0337) so
    # validation passes and dispatch is reached.
    monkeypatch.setenv("KDIVE_DATABASE_URL", "postgresql://kdive@localhost/kdive")
    monkeypatch.setenv("KDIVE_S3_ENDPOINT_URL", "http://localhost:9000")
    monkeypatch.setenv("KDIVE_S3_BUCKET", "kdive")
    monkeypatch.setattr("kdive.__main__.asyncio.run", lambda coro: coro.close())
    # Capture on the emitting logger directly — if configure_logging() is later
    # changed to attach handlers to the kdive hierarchy directly (bypassing root),
    # caplog at root would miss it.
    with caplog.at_level("INFO", logger="kdive.__main__"):
        main(["reconciler"])
    startup = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "starting kdive" in r.getMessage()
    ]
    assert startup, "expected a 'starting kdive' INFO record"
    message = startup[0].getMessage()
    # The record interpolates the resolved version and the dispatched command name; a
    # mutant that drops either (or rewrites the template) must not still read as startup.
    assert message.startswith("starting kdive ")
    assert message.endswith("(reconciler)")
    from kdive.version import full_version

    assert full_version() in message


def test_parser_uses_kdive_program_name() -> None:
    assert build_parser().prog == "kdive"


def test_parser_requires_a_subcommand() -> None:
    # required=True on the subparsers: bare `kdive` with no command is a usage error.
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_reconcile_systems_parses_path_and_check() -> None:
    args = build_parser().parse_args(["reconcile-systems", "--path", "x.toml", "--check"])
    assert args.command == "reconcile-systems"
    assert args.path == Path("x.toml")
    assert args.check is True


def test_reconcile_systems_defaults() -> None:
    # --check is a store_true flag (defaults False); --path defaults to None.
    args = build_parser().parse_args(["reconcile-systems"])
    assert args.check is False
    assert args.path is None


def test_install_fixtures_defaults() -> None:
    args = build_parser().parse_args(["install-fixtures"])
    assert args.dest == "/etc/kdive/fixtures/local-libvirt"
    assert args.force is False


def test_install_fixtures_overrides() -> None:
    args = build_parser().parse_args(["install-fixtures", "--dest", "/tmp/fx", "--force"])
    assert args.dest == "/tmp/fx"
    assert args.force is True


def test_seed_project_defaults() -> None:
    args = build_parser().parse_args(["seed-project"])
    assert args.project == "demo"
    assert args.limit_kcu == "1000000"
    assert args.max_concurrent_allocations == 4
    assert args.max_concurrent_systems == 4


def test_seed_project_overrides_and_int_coercion() -> None:
    args = build_parser().parse_args(
        [
            "seed-project",
            "--project",
            "p",
            "--limit-kcu",
            "5",
            "--max-concurrent-allocations",
            "7",
            "--max-concurrent-systems",
            "9",
        ]
    )
    assert args.project == "p"
    assert args.limit_kcu == "5"
    # --max-concurrent-* are typed int; a string default/type drop would leave them as str.
    assert args.max_concurrent_allocations == 7
    assert isinstance(args.max_concurrent_allocations, int)
    assert args.max_concurrent_systems == 9
    assert isinstance(args.max_concurrent_systems, int)


def test_categorized_error_surfaces_message_details_and_exit_code(monkeypatch, capsys) -> None:
    """A CategorizedError raised by an operator command surfaces its message *and* the
    already-computed details (path, error class, remediation) to stderr and exits with the
    category's stable code — not a bare "failed to read console log" behind a traceback (#1220)."""

    def _boom(_args: object) -> None:
        raise CategorizedError(
            "failed to read console log",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "operation": "read_console_log",
                "path": "/var/lib/kdive/console/abc.log",
                "error": "PermissionError",
                "remediation": "run the worker as root, or grant it group read access",
            },
        )

    monkeypatch.setattr("kdive.__main__.run_build_fs", _boom)
    with pytest.raises(SystemExit) as exc:
        main(["build-fs", "--image", "fedora-kdive-ready-44"])

    assert exc.value.code == exit_code_for_category("configuration_error")
    err = capsys.readouterr().err
    assert "failed to read console log" in err
    assert "/var/lib/kdive/console/abc.log" in err
    assert "PermissionError" in err
    assert "run the worker as root, or grant it group read access" in err


def test_categorized_error_emits_error_level_structured_record(monkeypatch, caplog) -> None:
    """The categorized failure also lands on the structured-log floor as an ERROR record naming
    the command, category, message, AND its actionable details, so a log-scraping deployment sees
    the whole cause on-channel and not only as plain stderr text (ADR-0090 observability)."""

    def _boom(_args: object) -> None:
        raise CategorizedError(
            "failed to read console log",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"path": "/var/lib/kdive/console/abc.log", "remediation": "run as root"},
        )

    monkeypatch.setattr("kdive.__main__.run_build_fs", _boom)
    with caplog.at_level(logging.ERROR, logger="kdive.__main__"), pytest.raises(SystemExit):
        main(["build-fs", "--image", "fedora-kdive-ready-44"])

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR-level record for the categorized failure"
    message = errors[0].getMessage()
    assert "build-fs" in message
    assert "configuration_error" in message
    assert "failed to read console log" in message
    # The actionable details must ride the structured record, not only stderr.
    assert "/var/lib/kdive/console/abc.log" in message
    assert "run as root" in message


def test_categorized_error_redacts_secret_pattern_on_stderr(monkeypatch, capsys) -> None:
    """A credential accidentally embedded in a detail value is scrubbed on stderr by the same
    Redactor floor the log path uses (defense-in-depth over the secret-free details contract)."""

    def _boom(_args: object) -> None:
        raise CategorizedError(
            "connect failed",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"dsn": "token=hunter2"},
        )

    monkeypatch.setattr("kdive.__main__.run_build_fs", _boom)
    with pytest.raises(SystemExit):
        main(["build-fs", "--image", "fedora-kdive-ready-44"])

    err = capsys.readouterr().err
    assert "hunter2" not in err
    assert "dsn:" in err  # the key is preserved; only the secret value is masked


def test_categorized_error_masks_secret_keyed_detail_value(monkeypatch, capsys) -> None:
    """A plain value under a secret-named key (password/token/api_key) — which carries no
    recognizable secret pattern of its own — is masked on stderr by the mapping-level key-name
    signal, matching Redactor.redact_value."""

    def _boom(_args: object) -> None:
        raise CategorizedError(
            "auth failed",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"password": "hunter2plain"},  # pragma: allowlist secret
        )

    monkeypatch.setattr("kdive.__main__.run_build_fs", _boom)
    with pytest.raises(SystemExit):
        main(["build-fs", "--image", "fedora-kdive-ready-44"])

    err = capsys.readouterr().err
    assert "hunter2plain" not in err
    assert "password:" in err  # the key is still shown; only its value is masked


def test_categorized_error_redacts_url_userinfo_on_both_surfaces(monkeypatch, capsys, caplog):
    """A credential in URL basic-auth userinfo — the common DSN/endpoint shape the Redactor
    key/value patterns miss — is stripped from both stderr and the structured record."""
    endpoint = "https://AKIAKEY:s3cr3tPazz@s3.example.com/bucket"  # pragma: allowlist secret

    def _boom(_args: object) -> None:
        raise CategorizedError(
            "object store unreachable",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"endpoint": endpoint},
        )

    monkeypatch.setattr("kdive.__main__.run_build_fs", _boom)
    with caplog.at_level(logging.ERROR, logger="kdive.__main__"), pytest.raises(SystemExit):
        main(["build-fs", "--image", "fedora-kdive-ready-44"])

    err = capsys.readouterr().err
    assert "s3cr3tPazz" not in err
    assert "AKIAKEY" not in err
    assert "s3.example.com" in err  # the host is preserved; only userinfo is stripped
    log_message = next(r for r in caplog.records if r.levelno == logging.ERROR).getMessage()
    assert "s3cr3tPazz" not in log_message


def test_reconcile_systems_object_store_misconfig_routes_through_central_handler(
    monkeypatch, capsys
) -> None:
    """A misconfigured object store on `reconcile-systems` flows through the central handler:
    actionable details to stderr and the category's exit code (2) — not the prior bare
    one-line message and exit 1 (#1220, ADR-0089). Pins the removed local try/except."""

    def _raise() -> object:
        raise CategorizedError(
            "S3 endpoint not configured",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"setting": "KDIVE_S3_ENDPOINT_URL"},
        )

    monkeypatch.setattr("kdive.store.objectstore.object_store_from_env", _raise)
    with pytest.raises(SystemExit) as exc:
        main(["reconcile-systems"])

    assert exc.value.code == exit_code_for_category("configuration_error")
    err = capsys.readouterr().err
    assert "S3 endpoint not configured" in err
    assert "KDIVE_S3_ENDPOINT_URL" in err


def test_categorized_error_unmapped_category_exits_generic(monkeypatch, capsys) -> None:
    """A category with no dedicated exit code maps to the generic failure code (1), and the
    message still reaches stderr rather than a traceback."""

    def _boom(_args: object) -> None:
        raise CategorizedError("infra broke", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    monkeypatch.setattr("kdive.__main__.run_build_fs", _boom)
    with pytest.raises(SystemExit) as exc:
        main(["build-fs", "--image", "fedora-kdive-ready-44"])

    assert exc.value.code == 1
    assert "infra broke" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("handler", "command"),
    [
        (_handle_server, "server"),
        (_handle_worker, "worker"),
        (_handle_reconciler, "reconciler"),
    ],
)
def test_runnable_handler_requires_telemetry(handler, command) -> None:
    # A runnable process handler must refuse to start without the telemetry bootstrap,
    # naming the command in the error so the missing-bootstrap fault is actionable.
    with pytest.raises(RuntimeError) as exc:
        handler(object(), object(), None)
    assert str(exc.value) == f"{command} command requires telemetry bootstrap"
