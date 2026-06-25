"""Register operator docs and cited ADRs as MCP resources (ADR-0151).

`build_app()` registers tools only, so `ListMcpResourcesTool` returns nothing even though
the tool surface cites operator docs (``docs/operating/build-source-staging.md``) and ADRs
(ADR-0080) in schema/error strings. This module registers those cited docs as
``TextResource``s over a **fixed, code-defined allowlist** — no request-supplied path, no
parameterized template — so a doc named in an error string is reachable over MCP.

The served bytes are packaged snapshots under ``_content/`` (generated from the canonical
``docs/`` tree by ``scripts/gen_doc_resources.py`` and drift-guarded). They live inside the
package because the runtime image ships only ``src/``; reading the repo-root ``docs/`` tree
at request time would return nothing in a container deploy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.resources import TextResource
from pydantic import AnyUrl

_CONTENT_DIR = Path(__file__).parent / "_content"
_MARKDOWN = "text/markdown"


@dataclass(frozen=True, slots=True)
class DocResource:
    """One allowlisted documentation resource.

    Attributes:
        uri: The stable resource URI advertised in ``list_resources``.
        source: The canonical doc path relative to the repo root. Used only by the
            generator and the drift test — never read at request time.
        content_file: The packaged snapshot filename under ``_content/``.
        name: Short machine name for the resource.
        title: Human title shown in the listing.
        description: Human description shown in the listing.
        mime_type: The content mime type.
    """

    uri: str
    source: str
    content_file: str
    name: str
    title: str
    description: str
    mime_type: str = _MARKDOWN


DOC_RESOURCES: tuple[DocResource, ...] = (
    DocResource(
        uri="resource://kdive/docs/operating/build-source-staging.md",
        source="docs/operating/build-source-staging.md",
        content_file="build-source-staging.md",
        name="build-source-staging",
        title="Staging kernel source for runs.build",
        description=(
            "Operator prerequisite for the server-build lane: how to stage a warm kernel "
            "source tree (KDIVE_KERNEL_SRC) or register a remote build host. Cited by the "
            "runs.create build_profile schema."
        ),
    ),
    DocResource(
        uri="resource://kdive/docs/operating/external-build-upload.md",
        source="docs/operating/external-build-upload.md",
        content_file="external-build-upload.md",
        name="external-build-upload",
        title="Preparing artifacts for the external-build lane",
        description=(
            "How to shape the upload artifacts for the external-build lane: the combined "
            "kernel+modules gzip tar (boot/vmlinuz bzImage + lib/modules/<release>/), the exact "
            "tar recipe, and the optional vmlinux/effective_config/initrd. Cited by the "
            "runs.create build_profile schema and artifacts.expected_uploads."
        ),
    ),
    DocResource(
        uri="resource://kdive/adr/0080",
        source="docs/adr/0080-remote-provisioning-disk-image-profile.md",
        content_file="0080-remote-provisioning-disk-image-profile.md",
        name="adr-0080",
        title="ADR-0080 — Remote provisioning disk-image profile",
        description=(
            "The remote-libvirt disk-image provisioning profile (base_image_volume staging). "
            "Cited by systems.profile_examples and remote provisioning errors."
        ),
    ),
    DocResource(
        uri="resource://kdive/docs/guide/response-envelope.md",
        source="docs/guide/response-envelope.md",
        content_file="response-envelope.md",
        name="response-envelope",
        title="The kdive ToolResponse envelope",
        description=(
            "How to read any kdive tool result: the uniform ToolResponse envelope fields and how "
            "to interpret the intentionally-open data, items, and refs. Referenced by the "
            "advertised tool outputSchema (ADR-0170)."
        ),
    ),
)


def register(app: FastMCP) -> int:
    """Register every allowlisted doc as a ``TextResource`` on ``app``.

    Reads each entry's packaged snapshot from ``_content/`` (importable package data, present
    in the runtime image). A missing snapshot is a packaging regression and raises rather
    than registering an empty resource.

    Args:
        app: The FastMCP app to register resources on.

    Returns:
        The number of resources registered.

    Raises:
        RuntimeError: If an entry's packaged snapshot file is absent.
    """
    for entry in DOC_RESOURCES:
        content_path = _CONTENT_DIR / entry.content_file
        if not content_path.is_file():
            raise RuntimeError(
                f"packaged doc-resource snapshot missing: {content_path} "
                f"(for {entry.uri}); run 'just resources-docs' (ADR-0151)"
            )
        text = content_path.read_text(encoding="utf-8")
        app.add_resource(
            TextResource(
                uri=AnyUrl(entry.uri),
                name=entry.name,
                title=entry.title,
                description=entry.description,
                mime_type=entry.mime_type,
                text=text,
            )
        )
    return len(DOC_RESOURCES)
