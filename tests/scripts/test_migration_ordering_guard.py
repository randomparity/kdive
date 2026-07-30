"""The migration-ordering guard (issue #1720).

Migration numbers are pre-assigned to parallel branches, which prevents *filename*
collisions but not *ordering* ones: 0085 merged after 0086 was already on `origin/main`
(#1553 / #1718). A database already at 0086 then applies 0085 afterwards — safe only
because those two happened to be independent. The guard rejects a newly added migration
numbered at or below the highest version already on the base branch.

The comparison logic is exercised directly here; `just migration-order-check` wires it
to git.
"""

from __future__ import annotations

from scripts.migration_ordering_guard import find_violations, parse_version


def _names(hits) -> list[str]:
    return [v.filename for v in hits]


def test_new_migration_above_the_base_maximum_is_allowed() -> None:
    base = ["0085_a.sql", "0086_b.sql"]
    assert find_violations(base, [*base, "0087_c.sql"]) == []


def test_new_migration_below_the_base_maximum_is_a_violation() -> None:
    # The exact #1553 / #1718 shape: 0085 authored while main had already reached 0086.
    base = ["0084_a.sql", "0086_b.sql"]
    hits = find_violations(base, [*base, "0085_c.sql"])
    assert _names(hits) == ["0085_c.sql"]
    assert hits[0].version == "0085"
    assert hits[0].base_max == "0086"


def test_new_migration_equal_to_the_base_maximum_is_a_violation() -> None:
    # Two branches pre-assigned the same number: whichever merges second collides.
    base = ["0086_b.sql"]
    hits = find_violations(base, [*base, "0086_other.sql"])
    assert _names(hits) == ["0086_other.sql"]


def test_a_gap_above_the_maximum_is_allowed() -> None:
    # The rule is strictly-greater-than-max, not exactly-max-plus-one: numbers may be
    # abandoned (a reverted branch) and the survivors still apply in order.
    base = ["0086_b.sql"]
    assert find_violations(base, [*base, "0090_c.sql"]) == []


def test_unchanged_tree_passes() -> None:
    base = ["0085_a.sql", "0086_b.sql"]
    assert find_violations(base, list(base)) == []


def test_no_migrations_at_all_passes() -> None:
    assert find_violations([], []) == []


def test_first_ever_migration_passes() -> None:
    # An empty base branch has no maximum to be above.
    assert find_violations([], ["0001_initial.sql"]) == []


def test_every_offending_file_is_reported() -> None:
    base = ["0086_b.sql"]
    hits = find_violations(base, [*base, "0084_c.sql", "0085_d.sql", "0087_e.sql"])
    assert _names(hits) == ["0084_c.sql", "0085_d.sql"]
    assert [v.version for v in hits] == ["0084", "0085"]


def test_a_migration_removed_from_head_is_not_our_concern() -> None:
    # Deleting an applied migration is a violation of ADR-0015, caught by the sibling
    # schema-immutability guard. This guard only looks at what the branch adds.
    assert find_violations(["0085_a.sql", "0086_b.sql"], ["0085_a.sql"]) == []


def test_unparseable_new_filename_is_a_violation() -> None:
    # Never skip a file we cannot classify: a guard that silently ignores its own
    # blind spot is worse than no guard.
    hits = find_violations(["0086_b.sql"], ["0086_b.sql", "0087a_c.sql"])
    assert _names(hits) == ["0087a_c.sql"]
    assert hits[0].version is None


def test_unparseable_base_filename_does_not_become_the_maximum() -> None:
    hits = find_violations(["0086_b.sql", "junk.sql"], ["0086_b.sql", "junk.sql", "0087_c.sql"])
    assert hits == []


def test_violation_message_names_the_file_its_number_and_the_maximum() -> None:
    hits = find_violations(["0086_b.sql"], ["0086_b.sql", "0085_c.sql"])
    message = hits[0].message()
    assert "0085_c.sql" in message
    assert "0086" in message


def test_unparseable_violation_message_names_the_expected_shape() -> None:
    hits = find_violations([], ["nope.sql"])
    assert "NNNN_" in hits[0].message()


def test_parse_version_reads_the_four_digit_prefix() -> None:
    assert parse_version("0086_restore_incomplete_category.sql") == "0086"
    assert parse_version("0086.sql") is None
    assert parse_version("86_short.sql") is None
    assert parse_version("00086_long.sql") is None
    assert parse_version("0086_x.txt") is None
