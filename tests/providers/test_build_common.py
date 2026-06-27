"""Tests for the shared kernel-build fragment helpers (ADR-0096)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from uuid import UUID

import pytest

from kdive.artifacts.storage import FetchedArtifact
from kdive.build_configs import defaults as build_defaults
from kdive.build_configs.catalog import BuildConfigEntry
from kdive.build_configs.defaults import DEFAULT_CONFIG_REF, build_config_fetch_from_env
from kdive.components.references import CatalogComponentRef
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import BuildProfile, ServerBuildProfile
from kdive.providers.shared.build_host.common import (
    _dropped_fragment_symbols,
    _fragment_symbols,
)
from kdive.providers.shared.build_host.execution import CapturedStep
from kdive.providers.shared.build_host.orchestration import BuildHostOrchestrator

_RUN = UUID("44444444-4444-4444-4444-444444444444")


def test_fragment_symbols_keeps_y_and_m_drops_comments_and_unset() -> None:
    fragment = (
        "CONFIG_CRASH_DUMP=y\n"
        "CONFIG_FOO=m\n"
        "# CONFIG_BAR is not set\n"
        "\n"
        "CONFIG_BAZ=n\n"
        "CONFIG_QUX=128\n"
    )
    assert _fragment_symbols(fragment) == ["CONFIG_CRASH_DUMP", "CONFIG_FOO"]


def test_fragment_symbols_skips_commented_assignments_and_uses_first_equals() -> None:
    fragment = (
        "# CONFIG_HEADER comment\nCONFIG_CRASH_DUMP=y\n# CONFIG_HID=y\nFOO=BAR=y\nCONFIG_FOO=m\n"
    )
    # A leading comment must not stop the scan (continue, not break), a commented
    # assignment is dropped even though it contains '=', and the symbol name is taken
    # from the first '=' so a value containing '=' is not treated as y/m.
    assert _fragment_symbols(fragment) == ["CONFIG_CRASH_DUMP", "CONFIG_FOO"]


def test_dropped_fragment_symbols_reports_a_dropped_option() -> None:
    fragment = "CONFIG_CRASH_DUMP=y\nCONFIG_PROC_VMCORE=y\n# a comment\n"
    final = "CONFIG_CRASH_DUMP=y\n# CONFIG_PROC_VMCORE is not set\n"
    assert _dropped_fragment_symbols(fragment, final) == ["CONFIG_PROC_VMCORE"]


def test_dropped_fragment_symbols_empty_when_all_survive() -> None:
    fragment = "CONFIG_CRASH_DUMP=y\n"
    final = "CONFIG_CRASH_DUMP=y\nCONFIG_OTHER=y\n"
    assert _dropped_fragment_symbols(fragment, final) == []


def test_dropped_fragment_symbols_accepts_module_survivor() -> None:
    fragment = "CONFIG_FOO=m\n"
    final = "CONFIG_FOO=m\n"
    assert _dropped_fragment_symbols(fragment, final) == []


def test_dropped_fragment_symbols_reports_builtin_downgraded_to_module() -> None:
    # A fragment requesting =y must end up =y. olddefconfig honoring a base-defconfig
    # value of =m instead silently disarms a built-in-only requirement (e.g. the
    # qemu_fw_cfg VMCOREINFO write path probing at boot under =y; #708). Treat the
    # downgrade as a dropped symbol so the build fails loud rather than producing an
    # unparseable host_dump core.
    fragment = "CONFIG_FW_CFG_SYSFS=y\n"
    final = "CONFIG_FW_CFG_SYSFS=m\n"
    assert _dropped_fragment_symbols(fragment, final) == ["CONFIG_FW_CFG_SYSFS"]


def test_dropped_fragment_symbols_accepts_module_request_built_in() -> None:
    # A fragment requesting =m is satisfied by either =m or a stronger =y in the final
    # config: asking for a module and getting it built in still arms the symbol.
    fragment = "CONFIG_FOO=m\n"
    final = "CONFIG_FOO=y\n"
    assert _dropped_fragment_symbols(fragment, final) == []


def test_dropped_fragment_symbols_only_counts_enabled_non_comment_lines() -> None:
    # A symbol requested =y but written =n in the final config is dropped: the enabled
    # set is built only from lines that genuinely end in =y/=m and are not comments,
    # so a disabled (=n) final line must NOT be treated as enabling the symbol.
    fragment = "CONFIG_C=y\n"
    final = "CONFIG_C=n\n"
    assert _dropped_fragment_symbols(fragment, final) == ["CONFIG_C"]


def test_dropped_fragment_symbols_uses_first_equals_for_enabled_names() -> None:
    # The enabled name comes from the text before the FIRST '=', so a final line whose
    # value contains '=' still yields the bare symbol name.
    fragment = "FOO=y\n"
    final = "FOO=BAR=y\n"
    assert _dropped_fragment_symbols(fragment, final) == []


def test_dropped_fragment_symbols_ignores_trailing_whitespace_on_final_lines() -> None:
    # The =y/=m suffix check strips trailing whitespace (rstrip), so a final line with
    # trailing spaces still counts as enabling the symbol.
    fragment = "CONFIG_E=y\n"
    final = "CONFIG_E=y   \n"
    assert _dropped_fragment_symbols(fragment, final) == []


def test_default_config_ref_is_the_kdump_catalog_entry() -> None:
    assert (
        CatalogComponentRef(kind="catalog", provider="system", name="kdump") == DEFAULT_CONFIG_REF
    )


def test_build_host_orchestrator_runs_neutral_build_sequence(tmp_path: Path) -> None:
    profile = BuildProfile.parse(
        {
            "schema_version": 1,
            "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
            "config": {"kind": "catalog", "provider": "system", "name": "kdump"},
            "patch_ref": None,
        }
    )
    assert isinstance(profile, ServerBuildProfile)
    calls: list[str] = []
    fragment = b"CONFIG_CRASH_DUMP=y\nCONFIG_DEBUG_INFO_DWARF5=y\n"

    def checkout(
        run_id: UUID, checkout_profile: ServerBuildProfile, workspace: Path, data: bytes
    ) -> None:
        assert run_id == _RUN
        assert checkout_profile is profile
        assert workspace == tmp_path / str(_RUN)
        assert data == fragment
        calls.append("checkout")

    def step(name: str) -> Callable[[Path], CapturedStep]:
        def _run(workspace: Path) -> CapturedStep:
            assert workspace == tmp_path / str(_RUN)
            calls.append(name)
            return CapturedStep(0, "")

        return _run

    def read_config(workspace: Path) -> str:
        assert workspace == tmp_path / str(_RUN)
        calls.append("read_config")
        return fragment.decode()

    orchestrator = BuildHostOrchestrator.create(
        workspace_root=tmp_path,
        catalog_fetch=lambda name: fragment if name == "kdump" else b"",
        checkout=checkout,
        run_olddefconfig=step("olddefconfig"),
        read_config=read_config,
        run_make=step("make"),
    )

    result = orchestrator.build_workspace(_RUN, profile)
    assert result.workspace == tmp_path / str(_RUN)
    assert result.clone_provenance is None
    assert calls == ["checkout", "olddefconfig", "read_config", "make"]


# --- build_config_fetch_from_env wrapper ---------------------------------------------


class _FakeConn:
    """A sync-connection double that records whether it was closed (the per-build leak guard)."""

    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.closed = True


class _FetchCalls:
    """Records the arguments the fetch wrapper passed to its injected seams."""

    def __init__(self) -> None:
        self.connect_url: object = "<unset>"
        self.lookup_conn: object = "<unset>"
        self.lookup_name: object = "<unset>"


def _patch_fetch_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    conn: _FakeConn,
    entry: BuildConfigEntry | None,
    store: object,
) -> _FetchCalls:
    calls = _FetchCalls()
    monkeypatch.setenv("KDIVE_DATABASE_URL", "postgresql://stub/stub")

    def _connect(url: object) -> _FakeConn:
        calls.connect_url = url
        return conn

    def _lookup(lookup_conn: object, name: object) -> BuildConfigEntry | None:
        calls.lookup_conn = lookup_conn
        calls.lookup_name = name
        return entry

    monkeypatch.setattr(build_defaults.psycopg, "connect", _connect)
    monkeypatch.setattr(build_defaults, "get_build_config_sync", _lookup)
    monkeypatch.setattr(build_defaults, "object_store_from_env", lambda: store)
    return calls


def test_build_config_seed_remediation_command_is_the_migrate_command() -> None:
    """Pin the affordance to the literal operator command (ADR-0105).

    The error's remediation must name the one command an operator actually runs; a
    rename of the seed command without updating this constant is a CI failure here, so
    the affordance can never drift into pointing at a command that does not exist.
    """
    assert build_defaults.SEED_REMEDIATION_COMMAND == "python -m kdive migrate"


def test_build_config_fetch_unknown_name_is_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _FakeConn()
    calls = _patch_fetch_env(monkeypatch, conn=conn, entry=None, store=object())

    with pytest.raises(CategorizedError) as caught:
        build_config_fetch_from_env()("nope")

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert conn.closed  # the connection is released even on the not-found branch
    # The fetch resolves the connection string from config (not a hardcoded/None URL)
    # and looks the catalog up by the open connection and the requested name.
    assert calls.connect_url == "postgresql://stub/stub"
    assert calls.lookup_conn is conn
    assert calls.lookup_name == "nope"
    # The error carries an actionable affordance (ADR-0105): the missing name plus a
    # literal seed command in `details["remediation"]` (the worker copies details into
    # the job response's `failure_detail_*` fields), and the message names the command.
    assert caught.value.details["name"] == "nope"
    assert caught.value.details["remediation"] == build_defaults.SEED_REMEDIATION_COMMAND
    assert build_defaults.SEED_REMEDIATION_COMMAND in str(caught.value)


def test_build_config_fetch_returns_verified_bytes_and_closes_conn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"CONFIG_CRASH_DUMP=y\n"
    entry = BuildConfigEntry(
        name="kdump",
        object_key="system/build-configs/kdump/kdump.config",
        sha256=hashlib.sha256(data).hexdigest(),
        description="",
        source="seed",
    )

    class _FakeStore:
        def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
            assert key == entry.object_key
            assert etag is None
            return FetchedArtifact(data, Sensitivity.REDACTED, "build-config")

    conn = _FakeConn()
    calls = _patch_fetch_env(monkeypatch, conn=conn, entry=entry, store=_FakeStore())

    assert build_config_fetch_from_env()("kdump") == data
    assert conn.closed  # the sync connection is released after the fetch (leak guard)
    # The catalog lookup receives the open connection and the requested name verbatim.
    assert calls.lookup_conn is conn
    assert calls.lookup_name == "kdump"
