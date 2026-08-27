"""Detect stored Systems whose profile provider section does not match their Resource kind.

Read-only residue sweep for issue #1907: ADR-0549 rejects a kind-mismatched provisioning
profile on the write path (``systems.provision`` / ``systems.reprovision``), but repairs
nothing already stored. ADR-0579 records this module's shape — an impure reader, a pool-opening
wrapper, and a pure formatter — mirroring ``kdive.admin.projects``'s split.

This module holds the :class:`ProfileKindMismatch` dataclass, the section-label renderer,
:func:`_project_token` — the control that keeps a stored project name from forging a field in
the report — :func:`scan_profile_kinds`, :func:`format_profile_kind_result`, and the
pool-opening wrapper :func:`verify_profile_kinds`. The CLI wiring lives in ``kdive.__main__``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.catalog.resources import ResourceKind

# Derived from the enum, never a literal: `resources.kind` has been widened twice already
# (0018, 0020), and both migrations say they mirror ResourceKind. A literal would not move
# with a fourth kind, so the report would render that kind `<unrecognized>` — hiding the one
# field criterion 2 asks for, in exactly the window a residue sweep exists for.
_KNOWN_KINDS = {kind.value for kind in ResourceKind}

# The four lanes ADR-0549 names as raising a bare `AttributeError` from `ProviderSection` at
# first use on a kind-mismatched, `ready` System.
#
# Each parenthetical names the function **defined in the file cited**, which is the enclosing
# call site — not the policy method it calls. `destructive_opt_in` and `capture_method` are
# `ProfilePolicy` members declared in `profiles/provider_policy.py`, so naming them here would
# send an operator to grep a file that does not define them; `capture_method` is also a
# module-level function in that same policy module, so the bare name is ambiguous besides. No
# line numbers: nothing keeps one accurate. `test_raising_lanes_resolve` checks each path exists
# and each symbol is defined in it, so a rename or move reddens instead of rotting silently.
_RAISING_LANES = (
    "src/kdive/mcp/tools/lifecycle/control/registrar.py (_op_opt_in)",
    "src/kdive/services/runs/steps.py (install_method_for)",
    "src/kdive/jobs/handlers/runs/boot_evidence.py (inert_capture)",
    "src/kdive/mcp/tools/lifecycle/vmcore/handlers.py (_resolve_capture_method)",
)


@dataclass(frozen=True, slots=True)
class ProfileKindMismatch:
    """A stored System whose provisioning-profile provider section does not match its bound
    Resource's kind (ADR-0579).

    ``state``, ``profile_section``, and ``resource_kind`` are ``str``, not ``SystemState`` /
    ``ResourceKind``: this is a residue sweep, and parsing a stored value into an enum would be
    a way for the sweep to raise on exactly the data it exists to find. The column ``CHECK``
    constraints already bound the vocabulary. ``profile_section`` is the rendered label from
    :func:`_section_label`, not the raw stored ``provider`` value.
    """

    system_id: UUID
    project: str
    state: str
    profile_section: str
    resource_kind: str


def _section_label(provider: object) -> str:
    """Render an observed ``provider`` section as a closed-vocabulary label (ADR-0579).

    Total over what psycopg can hand back for a ``jsonb`` column. JSON ``null`` and an absent
    ``provider`` key are indistinguishable at this layer — both arrive as ``None`` — so both map
    to ``"<none>"``. Any other non-``dict`` value (scalar, array) maps to ``"<not-an-object>"``.
    A ``dict`` renders one entry per key via :func:`_entry`, deduplicated and sorted so the
    label is bounded to at most ``len(ResourceKind) + 2`` entries however large the stored
    object is — one per known kind, plus ``<unrecognized>`` in each of its two forms. Stated as
    the expression rather than today's 5, because ``resources.kind`` has been widened twice.
    """
    if provider is None:
        return "<none>"
    if not isinstance(provider, dict):
        return "<not-an-object>"
    if not provider:
        return "<none>"
    return ",".join(sorted({_entry(key, value) for key, value in provider.items()}))


def _entry(key: object, value: object) -> str:
    """Render a closed-vocabulary label without exposing an unrecognized key."""
    name = key if isinstance(key, str) and key in _KNOWN_KINDS else "<unrecognized>"
    return name if isinstance(value, dict) else f"{name}=<not-an-object>"


# A System is clean only when its stored `provider` is exactly `{<the bound kind>: {…}}`. The
# first disjunct says the section under the bound kind is not an object; the second says the
# `provider` object holds anything besides that one section. The second is not belt-and-braces:
# without it, `{"fault-inject": {}, "local-libvirt": {}}` on a fault-inject Resource passes as
# clean, and that row fails `ProvisioningProfile.parse` outright rather than only on the four
# lanes ADR-0549 names. The section key is deliberately not extracted in SQL — the row carries
# the whole `provider` value back and `_section_label` renders it (ADR-0579).
_SCAN_QUERY: LiteralString = """
SELECT s.id            AS system_id,
       s.project       AS project,
       s.state         AS state,
       r.kind          AS resource_kind,
       s.provisioning_profile -> 'provider' AS provider_section
FROM systems AS s
JOIN allocations AS a ON a.id = s.allocation_id
JOIN resources AS r ON r.id = a.resource_id
WHERE jsonb_typeof(s.provisioning_profile -> 'provider' -> r.kind) IS DISTINCT FROM 'object'
   OR s.provisioning_profile -> 'provider'
        IS DISTINCT FROM jsonb_build_object(r.kind, s.provisioning_profile -> 'provider' -> r.kind)
ORDER BY s.created_at, s.id
"""


async def scan_profile_kinds(conn: AsyncConnection) -> list[ProfileKindMismatch]:
    """Return stored provider-kind mismatches in deterministic report order (ADR-0579)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SCAN_QUERY)
        rows = await cur.fetchall()
    return [
        ProfileKindMismatch(
            system_id=row["system_id"],
            project=row["project"],
            state=row["state"],
            profile_section=_section_label(row["provider_section"]),
            resource_kind=row["resource_kind"],
        )
        for row in rows
    ]


async def verify_profile_kinds() -> list[ProfileKindMismatch]:
    """Open a pool and delegate to :func:`scan_profile_kinds` (ADR-0579).

    Connects with :func:`kdive.db.pool.create_pool`, which raises
    ``CategorizedError(CONFIGURATION_ERROR)`` if ``KDIVE_DATABASE_URL`` is unset, before any
    query — mirroring :func:`kdive.admin.projects.verify_project`, the function this module is
    modelled on. The pool is closed in a ``finally`` regardless of how the scan finishes.

    Returns:
        The mismatches :func:`scan_profile_kinds` found, in report order.
    """
    from kdive.db.pool import create_pool

    pool = create_pool()
    await pool.open()
    try:
        async with pool.connection() as conn:
            return await scan_profile_kinds(conn)
    finally:
        await pool.close()


def _project_token(project: str) -> str:
    """Prevent an untrusted project name from forging whitespace-delimited report fields."""
    return repr(project).replace(" ", "\\x20").replace("=", "\\x3d")


def format_profile_kind_result(
    mismatches: Sequence[ProfileKindMismatch], *, redacted_url: str
) -> str:
    """Render the ``verify-profile-kinds`` report text (ADR-0579).

    The command always exits ``0``; this text is the answer. A clean result is one fixed line
    naming the target database. A non-empty result is a fixed header carrying the count and the
    target, one line per mismatch, then a closing block naming ADR-0549, the four raising lanes,
    and that remediation is not automated.

    ``project`` is the one field nothing else bounds (no ``CHECK``, no charset validation on its
    live path), so it is rendered through :func:`_project_token` — which is the control, not a
    bare ``!r``; that function's docstring carries why ``repr`` alone is insufficient. The other
    four fields are printed as they stand.

    Args:
        mismatches: The rows :func:`scan_profile_kinds` returned, in report order.
        redacted_url: The credential-redacted target DB
            (:func:`kdive.admin.projects.redact_database_url`).

    Returns:
        The full report text, with no trailing newline.
    """
    if not mismatches:
        return (
            "verified every System bound to a Resource has a stored provisioning-profile "
            "provider of exactly one object-valued section keyed by its Resource kind "
            f"in {redacted_url}"
        )
    lines = [
        f"found {len(mismatches)} System(s) whose stored provisioning-profile provider is not "
        f"exactly one object-valued section keyed by their bound Resource's kind "
        f"in {redacted_url}"
    ]
    for mismatch in mismatches:
        lines.append(
            f"system={mismatch.system_id} project={_project_token(mismatch.project)} "
            f"state={mismatch.state} profile_section={mismatch.profile_section} "
            f"resource_kind={mismatch.resource_kind}"
        )
    lines.append(
        "each listed System's stored provisioning-profile provider is not exactly one "
        "object-valued section keyed by its bound Resource's kind. Read profile_section to tell "
        "the two cases apart. Where it is a bare kind name (one of "
        f"{', '.join(sorted(_KNOWN_KINDS))}) other than the row's resource_kind, the profile "
        "still parses: that System raises only at first use, on the four lanes below "
        "(ADR-0549), and only if it is live enough to reach them. Every other profile_section "
        "this report can print — <none>, <not-an-object>, <unrecognized>, anything carrying "
        "=<not-an-object>, and any comma-joined pair — fails ProvisioningProfile.parse "
        "outright, so no parse site can read that System at all. state is reported per row so "
        "you can judge which rows are worth acting on. Remediation is not automated."
    )
    lines.extend(_RAISING_LANES)
    return "\n".join(lines)
