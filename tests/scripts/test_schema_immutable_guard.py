"""The schema-immutability guard (ADR-0015, issue #1218).

Applied migrations are byte-immutable: the runner hashes whole-file bytes, so a
cosmetic edit to an already-committed migration breaks upgrades of any DB migrated by
an earlier build. The guard parses ``git diff --name-status`` and must allow a new
migration file while rejecting any modify/delete/rename of an existing one.
"""

from __future__ import annotations

from scripts.schema_immutable_guard import find_violations

_EXISTING = "src/kdive/db/schema/0003_reprovision_job_kind.sql"
_NEW = "src/kdive/db/schema/0069_new_migration.sql"


def _paths(hits) -> list[str]:
    return [v.path for v in hits]


def test_modifying_existing_schema_file_is_a_violation() -> None:
    # The exact #1218 regression: a comment-only edit to an applied migration.
    hits = find_violations([f"M\t{_EXISTING}"])
    assert _paths(hits) == [_EXISTING]
    assert hits[0].status == "M"


def test_adding_a_new_schema_file_is_allowed() -> None:
    assert find_violations([f"A\t{_NEW}"]) == []


def test_deleting_an_existing_schema_file_is_a_violation() -> None:
    assert _paths(find_violations([f"D\t{_EXISTING}"])) == [_EXISTING]


def test_renaming_an_existing_schema_file_flags_the_source() -> None:
    # A rename changes the numbered identity a released DB recorded; the old path is
    # the offending one.
    hits = find_violations([f"R100\t{_EXISTING}\t{_NEW}"])
    assert _paths(hits) == [_EXISTING]


def test_copying_to_a_new_schema_file_is_allowed() -> None:
    # A copy leaves the source migration untouched; the destination is effectively new.
    assert find_violations([f"C100\t{_EXISTING}\t{_NEW}"]) == []


def test_type_change_on_existing_schema_file_is_a_violation() -> None:
    assert _paths(find_violations([f"T\t{_EXISTING}"])) == [_EXISTING]


def test_non_schema_files_are_ignored() -> None:
    assert find_violations(["M\tsrc/kdive/db/migrate.py", "A\tdocs/adr/0015.md"]) == []


def test_sql_outside_schema_dir_is_ignored() -> None:
    assert find_violations(["M\ttests/db/fixtures/sample.sql"]) == []


def test_blank_lines_are_skipped() -> None:
    assert find_violations(["", f"A\t{_NEW}", ""]) == []


def test_mixed_batch_reports_only_disallowed_changes() -> None:
    hits = find_violations(
        [
            f"A\t{_NEW}",
            f"M\t{_EXISTING}",
            "M\tsrc/kdive/db/migrate.py",
            "D\tsrc/kdive/db/schema/0018_resources_kind_fault_inject.sql",
        ]
    )
    assert _paths(hits) == [
        _EXISTING,
        "src/kdive/db/schema/0018_resources_kind_fault_inject.sql",
    ]
