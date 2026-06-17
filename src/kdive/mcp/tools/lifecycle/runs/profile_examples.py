"""``runs.profile_examples`` — discoverable example build profiles (#536, ADR-0160).

A read-only, auth-only discovery tool (modeled on ``systems.profile_examples``, ADR-0124;
auth posture per ADR-0117: a valid token gates the transport as defence-in-depth, but there
is no platform/project gate and no audit). Unlike ``systems.profile_examples`` — which
projects the file-based ``systems.toml`` provider inventory — the build-host inventory lives
in Postgres, so this tool is pool-backed: it reads the registered ``build_hosts`` rows and
emits one ready-to-edit server-build profile per host.

Each example's ``kernel_source_ref`` matches the host's accepted source kind, derived from
the shared :func:`~kdive.services.runs.build_host_selection.accepted_source_kinds` matrix
(ADR-0099 §5): a warm-tree string for ``local`` hosts, a ``{"git": {...}}`` object for
``ssh``/``ephemeral_libvirt`` hosts. Because the source form is derived from the same matrix
the validator enforces, the example always advertises the lane it itself uses. The example is
schema-valid as emitted (it parses via ``BuildProfile.parse`` and survives
``check_source_kind_compatibility`` for its host), but not buildable as-is: every
``REPLACE_ME`` placeholder must be replaced before building.
"""

from __future__ import annotations

from kdive.db.build_hosts import BuildHost
from kdive.mcp.responses import ToolResponse
from kdive.serialization import JsonValue
from kdive.services.runs.build_host_selection import SourceKind, accepted_source_kinds

_OBJECT_ID = "profile-examples"

# The build lane a cold agent should follow: edit an example, create the Run, enqueue the
# build. Each is a registered tool identifier.
_NEXT_ACTIONS = ["runs.create", "runs.build"]

# Placeholders the caller must replace before building.
_PLACEHOLDER_WARM_TREE = "REPLACE_ME-warm-tree-source"
_PLACEHOLDER_GIT_REMOTE = "REPLACE_ME-git-remote"
_PLACEHOLDER_GIT_REF = "REPLACE_ME-git-ref"

_NOTE = (
    "Example shape only; replace every REPLACE_ME placeholder in kernel_source_ref with a "
    "real value before building. A local build host takes a warm-tree string; an "
    "ssh/ephemeral_libvirt host takes a git {remote, ref} object."
)


def build_host_profile_examples(hosts: list[BuildHost]) -> ToolResponse:
    """Build the example-build-profiles collection from a list of build hosts.

    Args:
        hosts: The registered build-host rows (e.g. from
            :func:`~kdive.db.build_hosts.list_all_hosts`). A migrated database always has
            at least the seeded ``worker-local`` row, so the collection is normally
            non-empty; an empty list yields a valid empty collection.

    Returns:
        A :class:`ToolResponse` collection with one item per host; each item's ``data``
        carries ``build_host``, ``host_kind``, ``supported_source_kinds``, the ready-to-edit
        ``profile`` dict, and a ``note``.
    """
    items = [_example_item(host) for host in hosts]
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=list(_NEXT_ACTIONS),
    )


def _example_item(host: BuildHost) -> ToolResponse:
    """Build one example item for ``host``."""
    accepted = accepted_source_kinds(host.kind)
    data: dict[str, JsonValue] = {
        "build_host": host.name,
        "host_kind": host.kind.value,
        "supported_source_kinds": [kind.value for kind in accepted],
        "profile": _example_profile(host),
        "note": _NOTE,
    }
    return ToolResponse.success(host.name, "ok", data=data)


def _example_profile(host: BuildHost) -> dict[str, JsonValue]:
    """A ``ServerBuildProfile``-parseable document whose source form matches ``host``'s kind.

    The ``kernel_source_ref`` shape is derived from
    :func:`accepted_source_kinds` (not a second ``if host.kind`` branch), so the example
    can never advertise a lane the validator rejects.
    """
    kernel_source_ref: JsonValue
    if SourceKind.GIT in accepted_source_kinds(host.kind):
        kernel_source_ref = {
            "git": {"remote": _PLACEHOLDER_GIT_REMOTE, "ref": _PLACEHOLDER_GIT_REF}
        }
    else:
        kernel_source_ref = _PLACEHOLDER_WARM_TREE
    return {
        "schema_version": 1,
        "source": "server",
        "kernel_source_ref": kernel_source_ref,
        "build_host": host.name,
    }
