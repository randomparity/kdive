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


def test_version_flag_prints_and_exits(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("kdive ")


def test_startup_logs_version(monkeypatch, caplog):
    # Don't actually run the async loop; just confirm main logs before dispatching.
    # A runnable command now validates config at startup, so supply the one var the
    # reconciler requires (KDIVE_DATABASE_URL) so validation passes and dispatch is reached.
    monkeypatch.setenv("KDIVE_DATABASE_URL", "postgresql://kdive@localhost/kdive")
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


def test_seed_demo_defaults() -> None:
    args = build_parser().parse_args(["seed-demo"])
    assert args.project == "demo"
    assert args.limit_kcu == "1000000"
    assert args.max_concurrent_allocations == 4
    assert args.max_concurrent_systems == 4


def test_seed_demo_overrides_and_int_coercion() -> None:
    args = build_parser().parse_args(
        [
            "seed-demo",
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
