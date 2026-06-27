"""``runs.profile_examples`` — discoverable example build profiles (#536, ADR-0160).

A read-only, auth-only discovery tool (modeled on ``systems.profile_examples``, ADR-0124;
auth posture per ADR-0117: a valid token gates the transport as defence-in-depth, but there
is no platform/project gate and no audit). Unlike ``systems.profile_examples`` — which
projects the file-based ``systems.toml`` provider inventory — the build-host inventory lives
in Postgres, so this tool is pool-backed: it reads the registered ``build_hosts`` rows and
emits one ready-to-edit server-build profile per host. The collection leads with a single,
host-independent ``source='external'`` example — the recommended default upload lane
(ADR-0234) — followed by the per-host server-build examples (a single-host convenience).

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

from collections.abc import Collection

from kdive.db.build_hosts import BuildHost
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog.artifacts.expected_uploads import EXPECTED_UPLOADS_TOOL
from kdive.mcp.tools.catalog.artifacts.uploads import CREATE_RUN_UPLOAD_TOOL
from kdive.serialization import JsonValue
from kdive.services.runs.build_host_selection import (
    SourceKind,
    accepted_source_kinds,
    build_host_resolves,
)

_OBJECT_ID = "profile-examples"
_EXTERNAL_OBJECT_ID = "external-upload"

# The recommended default lane (ADR-0234): create the Run as source='external', learn the
# required bytes, upload the prebuilt artifact. Each is a registered tool identifier. The
# server-build verb (runs.build) trails as the secondary single-host convenience.
_NEXT_ACTIONS = ["runs.create", EXPECTED_UPLOADS_TOOL, CREATE_RUN_UPLOAD_TOOL, "runs.build"]

# Item-level next actions for the external example: the upload sequence (ADR-0234 §1/§5).
_EXTERNAL_NEXT_ACTIONS = ["runs.create", EXPECTED_UPLOADS_TOOL, CREATE_RUN_UPLOAD_TOOL]

_EXTERNAL_NOTE = (
    "Recommended default build lane (ADR-0234): upload a prebuilt kernel artifact instead of "
    "building on a host. This example is host-independent — it names no build_host and no "
    "kernel_source_ref. After runs.create with this profile, call artifacts.expected_uploads "
    "to learn the exact bytes to produce, then artifacts.create_run_upload to upload, then "
    "runs.complete_build. The per-host server-build examples below are a single-host "
    "convenience that needs a staged source tree or git-clone access."
)

# Placeholders the caller must replace before building.
_PLACEHOLDER_WARM_TREE = "REPLACE_ME-warm-tree-source"
_PLACEHOLDER_GIT_REMOTE = "REPLACE_ME-git-remote"
_PLACEHOLDER_GIT_REF = "REPLACE_ME-git-ref"

_NOTE = (
    "Example shape only; replace every REPLACE_ME placeholder in kernel_source_ref with a "
    "real value before building. A local build host takes a warm-tree string; an "
    "ssh/ephemeral_libvirt host takes a git {remote, ref} object. The warm-tree string is a "
    "provenance label only — it does not select the tree; the operator stages the actual "
    "source via KDIVE_KERNEL_SRC on the worker. After a build, runs.get reports the label and "
    "resolved commit in data.build_provenance."
)


def build_host_profile_examples(
    hosts: list[BuildHost], declared_instances: Collection[str]
) -> ToolResponse:
    """Build the example-build-profiles collection from a list of build hosts.

    Omits any host that does not resolve to a declared ``[[remote_libvirt]]`` instance — an
    ``ephemeral_libvirt`` host whose name names no instance (ADR-0195, #626) — so every emitted
    example is buildable for its host. ``local`` and ``ssh`` hosts always resolve.

    Args:
        hosts: The registered build-host rows (e.g. from
            :func:`~kdive.db.build_hosts.list_all_hosts`). A migrated database always has
            at least the seeded ``worker-local`` row, so the collection is normally
            non-empty; an empty list yields a valid empty collection.
        declared_instances: The declared ``[[remote_libvirt]]`` instance names (from
            :func:`~kdive.providers.assembly.build_hosts.declared_remote_instance_names`),
            used to drop ``ephemeral_libvirt`` hosts with no backing instance.

    Returns:
        A :class:`ToolResponse` collection whose FIRST item is the host-independent,
        recommended external-upload example (ADR-0234) and whose remaining items are one
        per *resolving* server-build host; each host item's ``data`` carries ``build_host``,
        ``host_kind``, ``supported_source_kinds``, the ready-to-edit ``profile`` dict, and a
        ``note``.
    """
    items = [_external_example_item()]
    items += [
        _example_item(host)
        for host in hosts
        if build_host_resolves(host.kind, host.name, declared_instances)
    ]
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        items,
        suggested_next_actions=list(_NEXT_ACTIONS),
    )


def _external_example_item() -> ToolResponse:
    """The recommended, host-independent ``source='external'`` example (ADR-0234 §1/§5).

    Unlike the per-host server examples, this one names no build host and no source tree: the
    agent uploads a prebuilt kernel artifact. Its ``suggested_next_actions`` chain into the
    upload sequence so the lane is self-describing from discovery alone.
    """
    data: dict[str, JsonValue] = {
        "recommended": True,
        "lane": "external",
        "profile": {"schema_version": 1, "source": "external"},
        "note": _EXTERNAL_NOTE,
    }
    return ToolResponse.success(
        _EXTERNAL_OBJECT_ID,
        "ok",
        data=data,
        suggested_next_actions=list(_EXTERNAL_NEXT_ACTIONS),
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
    can never advertise a lane the validator rejects. A host that accepts more than one
    source kind (e.g. a local host after ADR-0161) gets an example for its **primary**
    (first-listed) kind — warm-tree for local, which needs no allowlist — while its other
    accepted kinds are still advertised in ``supported_source_kinds``.
    """
    kernel_source_ref: JsonValue
    if accepted_source_kinds(host.kind)[0] is SourceKind.GIT:
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
