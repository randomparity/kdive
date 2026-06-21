"""The hand-rolled app bootstrap is retired (ADR-0088 decision 9).

The `stack` supervisor and the `install-compose`/`print-local-env` dev crutches are
removed; only `migrate`/`install-fixtures`/`seed-demo` remain. The image (or the compose
app tier) is the bring-up path that replaces them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.__main__ import build_parser
from kdive.admin.bootstrap import install_fixtures
from kdive.admin.default_fixtures import LOCAL_LIBVIRT_FIXTURES


def test_removed_subcommands_exit_on_parse() -> None:
    parser = build_parser()
    for removed in ("stack", "install-compose", "print-local-env"):
        with pytest.raises(SystemExit):
            parser.parse_args([removed])


def test_retained_subcommands_still_parse() -> None:
    parser = build_parser()
    for retained in ("server", "worker", "reconciler", "migrate", "seed-demo"):
        args = parser.parse_args([retained])
        assert args.command == retained
    assert parser.parse_args(["install-fixtures"]).command == "install-fixtures"


def test_run_stack_not_importable() -> None:
    import kdive.admin.bootstrap as bootstrap

    for removed in ("run_stack", "install_compose", "print_local_env", "supervisor_commands"):
        assert not hasattr(bootstrap, removed)


def test_install_fixtures_writes_every_packaged_fixture_including_nested(tmp_path: Path) -> None:
    dest = tmp_path / "fixtures"

    install_fixtures(dest)

    # Every packaged fixture lands at its relative path with its exact content, and a nested
    # path's parent directory is created on the way (parents=True).
    assert {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()} == set(
        LOCAL_LIBVIRT_FIXTURES
    )
    for relative, content in LOCAL_LIBVIRT_FIXTURES.items():
        assert (dest / relative).read_text(encoding="utf-8") == content
    assert any("/" in relative for relative in LOCAL_LIBVIRT_FIXTURES)


def test_install_fixtures_refuses_an_existing_destination_by_default(tmp_path: Path) -> None:
    dest = tmp_path / "fixtures"
    dest.mkdir()

    with pytest.raises(FileExistsError, match="--force"):
        install_fixtures(dest)


def test_install_fixtures_force_overwrites_an_existing_destination(tmp_path: Path) -> None:
    dest = tmp_path / "fixtures"
    dest.mkdir()
    # Pre-create one fixture so the rewrite must succeed over an existing file (exist_ok=True).
    relative, content = next(iter(LOCAL_LIBVIRT_FIXTURES.items()))
    (dest / relative).parent.mkdir(parents=True, exist_ok=True)
    (dest / relative).write_text("stale", encoding="utf-8")

    install_fixtures(dest, force=True)

    assert (dest / relative).read_text(encoding="utf-8") == content
