import asyncio
from decimal import Decimal
from pathlib import Path
from typing import cast

import psycopg
import pytest

import kdive.config as config
from kdive.admin.fixtures import default_fixture_files, install_fixtures
from kdive.admin.migrations import migrate
from kdive.admin.projects import (
    seed_project,
    seed_project_statements,
)


def test_seed_project_sql_contains_budget_and_quota_upserts() -> None:
    statements = seed_project_statements(
        project="demo",
        limit_kcu=Decimal("1000000"),
        max_concurrent_allocations=4,
        max_concurrent_systems=4,
    )

    joined = "\n".join(statement for statement, _params in statements)
    assert "INSERT INTO budgets" in joined
    assert "INSERT INTO quotas" in joined
    assert "ON CONFLICT" in joined


def test_seed_project_sql_params_are_parameterized() -> None:
    statements = seed_project_statements(
        project="demo'; drop table budgets; --",
        limit_kcu=Decimal("1000000"),
        max_concurrent_allocations=4,
        max_concurrent_systems=4,
    )

    joined = "\n".join(statement for statement, _params in statements)

    assert "drop table" not in joined.lower()
    assert any("demo'; drop table budgets; --" in params for _statement, params in statements)


def test_seed_project_registers_discovered_resources(
    monkeypatch: pytest.MonkeyPatch, migrated_url: str
) -> None:
    calls: list[str] = []

    async def fake_register(pool: object) -> None:
        del pool
        calls.append("registered")

    monkeypatch.setenv("KDIVE_DATABASE_URL", migrated_url)
    monkeypatch.setattr(
        "kdive.admin.projects.register_discovered_resources",
        fake_register,
    )

    asyncio.run(
        seed_project(
            project="demo",
            limit_kcu=Decimal("1000000"),
            max_concurrent_allocations=4,
            max_concurrent_systems=4,
        )
    )

    assert calls == ["registered"]


def test_register_discovered_resources_skips_local_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ADR-0131: the migrate-time resource discovery step is a
    # build_provider_resolver().register_all_discovery() call. With local-libvirt disabled and
    # no other provider configured the resolver is empty, so it registers nothing local and
    # never constructs the local discovery target (which would open the libvirt socket).
    from psycopg_pool import AsyncConnectionPool

    from kdive.admin.projects import register_discovered_resources

    monkeypatch.setenv("KDIVE_LOCAL_LIBVIRT_ENABLED", "false")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "absent.toml"))
    config.load()
    monkeypatch.setattr(
        "kdive.providers.local_libvirt.composition._discovery_target",
        _fail_local_discovery_target,
    )

    asyncio.run(register_discovered_resources(cast(AsyncConnectionPool, object())))


def _fail_local_discovery_target() -> object:
    raise AssertionError("local discovery must not run when local-libvirt is disabled")


def test_default_fixture_files_include_catalog() -> None:
    fixture_files = default_fixture_files()

    assert "manifest.yaml" in fixture_files
    assert "profiles/console-ready_x86_64.yaml" in fixture_files


# fedora-kdive-ready-44 is the kdump-capable default (ADR-0251); 43 is retained as the #817
# regression reference (its older makedumpfile cannot filter the newest kernels).
_BASELINE_SYSTEMS_TOML = """schema_version = 2

[[image]]
provider = "local-libvirt"
name = "fedora-kdive-ready-44"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
capabilities = ["agent", "drgn"]
[image.source]
kind = "s3"
object_key = "rootfs/local/fedora-kdive-ready-44.qcow2"

[[image]]
provider = "local-libvirt"
name = "fedora-kdive-ready-43"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
capabilities = ["agent", "drgn"]
[image.source]
kind = "s3"
object_key = "rootfs/local/fedora-kdive-ready-43.qcow2"
"""


def _write_baseline_systems_toml(tmp_path: Path) -> Path:
    path = tmp_path / "systems.toml"
    path.write_text(_BASELINE_SYSTEMS_TOML, encoding="utf-8")
    return path


def test_migrate_is_sql_only(
    monkeypatch: pytest.MonkeyPatch, postgres_url: str, tmp_path: Path
) -> None:
    # migrate() applies the schema and nothing else: even with a systems.toml present it creates
    # no image_catalog config rows. Inventory reconcile is the reconciler's job (ADR-0112); a
    # failed "migrate" therefore always means SQL failed.
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(_write_baseline_systems_toml(tmp_path)))

    applied = migrate(postgres_url)

    assert applied > 0  # the schema was migrated
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        images = conn.execute(
            "SELECT count(*) FROM image_catalog WHERE managed_by = 'config'"
        ).fetchone()
    assert images is not None and images[0] == 0


def test_install_fixtures_refuses_overwrite_without_force(tmp_path: Path) -> None:
    fixture_dest = tmp_path / "fixtures"
    (fixture_dest / "manifest.yaml").parent.mkdir(parents=True)
    (fixture_dest / "manifest.yaml").write_text("custom", encoding="utf-8")

    with pytest.raises(FileExistsError):
        install_fixtures(fixture_dest)

    install_fixtures(fixture_dest, force=True)


def _seed_rows(url: str, *, budget: bool, quota: bool, project: str = "demo") -> None:
    """Apply the budget and/or quota seed upserts to ``url`` for ``project``."""
    statements = seed_project_statements(
        project=project,
        limit_kcu=Decimal("1000000"),
        max_concurrent_allocations=4,
        max_concurrent_systems=4,
    )
    budget_sql, quota_sql = statements[0], statements[1]
    chosen = ([budget_sql] if budget else []) + ([quota_sql] if quota else [])
    with psycopg.connect(url, autocommit=True) as conn:
        for sql, params in chosen:
            conn.execute(sql.encode(), params)


def test_redact_database_url_masks_url_password() -> None:
    from kdive.admin.projects import redact_database_url

    secret = "p4ss-w0rd"  # noqa: S105 # pragma: allowlist secret - test literal
    redacted = redact_database_url(f"postgresql://kdive:{secret}@db.example:5432/kdive")

    assert secret not in redacted
    assert "***" in redacted
    assert "db.example" in redacted
    assert "5432" in redacted
    assert "/kdive" in redacted


def test_redact_database_url_leaves_passwordless_url_unchanged() -> None:
    from kdive.admin.projects import redact_database_url

    url = "postgresql://kdive@db.example:5432/kdive"
    assert redact_database_url(url) == url
    assert "***" not in redact_database_url(url)


def test_redact_database_url_blanket_redacts_conninfo_with_password() -> None:
    from kdive.admin.projects import redact_database_url

    secret = "s3cr3t"  # noqa: S105 # pragma: allowlist secret - test literal
    # A keyword/value conninfo is redacted wholesale: a token regex would only partially mask a
    # spaced or quoted value, leaking the tail. Cover the plain, spaced, and quoted forms.
    for conninfo in (
        f"host=db.example dbname=kdive password={secret}",
        f"host=db.example password = {secret} dbname=kdive",
        f"host=db.example password='{secret} more' dbname=kdive",
    ):
        redacted = redact_database_url(conninfo)
        assert secret not in redacted
        assert "redacted" in redacted.lower()


def test_redact_database_url_passwordless_conninfo_unchanged() -> None:
    from kdive.admin.projects import redact_database_url

    conninfo = "host=db.example dbname=kdive user=kdive"
    assert redact_database_url(conninfo) == conninfo


def test_verify_project_both_rows_present(
    monkeypatch: pytest.MonkeyPatch, migrated_url: str
) -> None:
    from kdive.admin.projects import verify_project

    _seed_rows(migrated_url, budget=True, quota=True)
    monkeypatch.setenv("KDIVE_DATABASE_URL", migrated_url)

    status = asyncio.run(verify_project(project="demo"))

    assert status.budget_present is True
    assert status.quota_present is True
    assert status.funded is True
    assert status.limit_kcu == Decimal("1000000")
    assert status.spent_kcu == Decimal("0")
    assert status.max_concurrent_allocations == 4
    assert status.occupancy == 0


def test_verify_project_missing_budget(monkeypatch: pytest.MonkeyPatch, migrated_url: str) -> None:
    from kdive.admin.projects import verify_project

    _seed_rows(migrated_url, budget=False, quota=True)
    monkeypatch.setenv("KDIVE_DATABASE_URL", migrated_url)

    status = asyncio.run(verify_project(project="demo"))

    assert status.budget_present is False
    assert status.quota_present is True
    assert status.funded is False
    assert status.limit_kcu is None


def test_verify_project_missing_quota(monkeypatch: pytest.MonkeyPatch, migrated_url: str) -> None:
    from kdive.admin.projects import verify_project

    _seed_rows(migrated_url, budget=True, quota=False)
    monkeypatch.setenv("KDIVE_DATABASE_URL", migrated_url)

    status = asyncio.run(verify_project(project="demo"))

    assert status.budget_present is True
    assert status.quota_present is False
    assert status.funded is False
    assert status.max_concurrent_allocations is None


def test_verify_project_neither_row(monkeypatch: pytest.MonkeyPatch, migrated_url: str) -> None:
    from kdive.admin.projects import verify_project

    monkeypatch.setenv("KDIVE_DATABASE_URL", migrated_url)

    status = asyncio.run(verify_project(project="demo"))

    assert status.budget_present is False
    assert status.quota_present is False
    assert status.funded is False


def test_format_verify_result_funded_returns_zero() -> None:
    from kdive.admin.projects import ProjectFundingStatus, format_verify_result

    status = ProjectFundingStatus(
        budget_present=True,
        quota_present=True,
        limit_kcu=Decimal("1000000"),
        spent_kcu=Decimal("0"),
        max_concurrent_allocations=4,
        occupancy=0,
    )
    message, code = format_verify_result(
        status, project="demo", redacted_url="postgresql://kdive:***@db/kdive"
    )

    assert code == 0
    assert "demo" in message
    assert "postgresql://kdive:***@db/kdive" in message
    assert "1000000" in message


def test_verify_project_command_registered_with_project_arg() -> None:
    from kdive import __main__ as main_mod

    default_args = main_mod.build_parser().parse_args(["verify-project"])
    assert default_args.command == "verify-project"
    assert default_args.project == "demo"

    explicit_args = main_mod.build_parser().parse_args(["verify-project", "--project", "acme"])
    assert explicit_args.project == "acme"


def test_verify_project_command_exits_nonzero_when_unseeded(
    monkeypatch: pytest.MonkeyPatch, migrated_url: str
) -> None:
    # The funding gate rests on this wire: the handler must propagate format_verify_result's
    # non-zero code via SystemExit so onboard's `set -e` aborts on an unseeded DB.
    from kdive import __main__ as main_mod
    from kdive.security.secrets.secret_registry import SecretRegistry

    monkeypatch.setenv("KDIVE_DATABASE_URL", migrated_url)
    args = main_mod.build_parser().parse_args(["verify-project", "--project", "demo"])

    with pytest.raises(SystemExit) as exc:
        main_mod._COMMAND_BY_NAME["verify-project"].handler(args, SecretRegistry(), None)

    assert exc.value.code != 0


def test_verify_project_command_exits_zero_when_seeded(
    monkeypatch: pytest.MonkeyPatch, migrated_url: str
) -> None:
    from kdive import __main__ as main_mod
    from kdive.security.secrets.secret_registry import SecretRegistry

    _seed_rows(migrated_url, budget=True, quota=True)
    monkeypatch.setenv("KDIVE_DATABASE_URL", migrated_url)
    args = main_mod.build_parser().parse_args(["verify-project", "--project", "demo"])

    with pytest.raises(SystemExit) as exc:
        main_mod._COMMAND_BY_NAME["verify-project"].handler(args, SecretRegistry(), None)

    assert exc.value.code == 0


def test_format_verify_result_missing_row_returns_nonzero() -> None:
    from kdive.admin.projects import ProjectFundingStatus, format_verify_result

    status = ProjectFundingStatus(
        budget_present=False,
        quota_present=True,
        limit_kcu=None,
        spent_kcu=None,
        max_concurrent_allocations=4,
        occupancy=0,
    )
    message, code = format_verify_result(
        status, project="demo", redacted_url="postgresql://kdive:***@db/kdive"
    )

    assert code != 0
    assert "demo" in message
    assert "budget" in message.lower()
    assert "postgresql://kdive:***@db/kdive" in message
