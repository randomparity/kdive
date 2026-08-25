"""Detect stored Systems whose profile provider section does not match their Resource kind.

Read-only residue sweep for issue #1907: ADR-0549 rejects a kind-mismatched provisioning
profile on the write path (``systems.provision`` / ``systems.reprovision``), but repairs
nothing already stored. ADR-0579 records this module's shape — an impure reader, a pool-opening
wrapper, and a pure formatter — mirroring ``kdive.admin.projects``'s split.

This module holds only the pure surface: the :class:`ProfileKindMismatch` dataclass, the
section-label renderer, and :func:`format_profile_kind_result`. The database reader
(``scan_profile_kinds`` / ``verify_profile_kinds``) and the CLI wiring are built separately.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from kdive.domain.catalog.resources import ResourceKind

# Derived from the enum, never a literal: `resources.kind` has been widened twice already
# (0018, 0020), and both migrations say they mirror ResourceKind. A literal would not move
# with a fourth kind, so the report would render that kind `<unrecognized>` — hiding the one
# field criterion 2 asks for, in exactly the window a residue sweep exists for.
_KNOWN_KINDS = {kind.value for kind in ResourceKind}

# The four lanes ADR-0549 names as raising a bare `AttributeError` from `ProviderSection` at
# first use on a kind-mismatched, `ready` System.
_RAISING_LANES = (
    "src/kdive/mcp/tools/lifecycle/control/registrar.py:207 (destructive_opt_in)",
    "src/kdive/services/runs/steps.py:445 (install_method_for)",
    "src/kdive/jobs/handlers/runs/boot_evidence.py:243 (capture_method)",
    "src/kdive/mcp/tools/lifecycle/vmcore/handlers.py:181 (capture_method)",
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
    label is bounded to at most five entries however large the stored object is.
    """
    if provider is None:
        return "<none>"
    if not isinstance(provider, dict):
        return "<not-an-object>"
    if not provider:
        return "<none>"
    return ",".join(sorted({_entry(key, value) for key, value in provider.items()}))


def _entry(key: object, value: object) -> str:
    """Render one ``provider`` entry as either its kind name or a `<kind>=<not-an-object>` row.

    Takes ``key: object`` and narrows with ``isinstance(key, str)`` rather than ``key: str``,
    because the obvious form does not type-check: ``isinstance(provider, dict)`` in
    :func:`_section_label` narrows to ``dict[Unknown, Unknown]``, so the keys are ``object`` and
    both a ``key: str`` signature and indexing ``provider[key]`` are rejected by ``ty``. A key
    outside ``_KNOWN_KINDS`` — including one that is not even a ``str`` — renders
    ``"<unrecognized>"`` so its bytes never reach the terminal.
    """
    name = key if isinstance(key, str) and key in _KNOWN_KINDS else "<unrecognized>"
    return name if isinstance(value, dict) else f"{name}=<not-an-object>"


def format_profile_kind_result(
    mismatches: Sequence[ProfileKindMismatch], *, redacted_url: str
) -> str:
    """Render the ``verify-profile-kinds`` report text (ADR-0579).

    The command always exits ``0``; this text is the answer. A clean result is one fixed line
    naming the target database. A non-empty result is a fixed header carrying the count and the
    target, one line per mismatch, then a closing block naming ADR-0549, the four raising lanes,
    and that remediation is not automated.

    ``project`` is the one field nothing else bounds (no ``CHECK``, no charset validation on its
    live path), so it is rendered with ``repr`` — ``f"project={mismatch.project!r}"`` — which
    escapes exactly what ``str.isprintable()`` rejects, delimits the value, and escapes any
    quote inside it. The other four fields are printed as they stand.

    Args:
        mismatches: The rows :func:`scan_profile_kinds` returned, in report order.
        redacted_url: The credential-redacted target DB
            (:func:`kdive.admin.projects.redact_database_url`).

    Returns:
        The full report text, with no trailing newline.
    """
    if not mismatches:
        return (
            "verified no System's provisioning-profile provider section mismatches its "
            f"Resource kind in {redacted_url}"
        )
    lines = [
        f"found {len(mismatches)} System(s) whose provisioning-profile provider section "
        f"does not match their Resource kind in {redacted_url}"
    ]
    for mismatch in mismatches:
        lines.append(
            f"system={mismatch.system_id} project={mismatch.project!r} "
            f"state={mismatch.state} profile_section={mismatch.profile_section} "
            f"resource_kind={mismatch.resource_kind}"
        )
    lines.append(
        "each listed System's stored provisioning-profile provider section does not match "
        "its bound Resource's kind: a plain kind mismatch reaches ready and raises at first "
        "use on the four lanes below (ADR-0549), while a section that fails "
        "ProvisioningProfile.parse outright (a provider holding two sections, or none) breaks "
        "every parse site instead. Remediation is not automated."
    )
    lines.extend(_RAISING_LANES)
    return "\n".join(lines)
