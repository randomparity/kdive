"""Register operator docs as MCP resources (ADR-0151).

`build_app()` registers tools only, so `ListMcpResourcesTool` returns nothing even though
the tool surface cites operator docs (``docs/operating/build-source-staging.md``) in
schema/error strings. This module registers those cited docs as
``TextResource``s over a **fixed, code-defined allowlist** — no request-supplied path, no
parameterized template — so a doc named in an error string is reachable over MCP. Internal
ADRs are deliberately not served (ADR-0270).

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
        uri="resource://kdive/docs/operating/external-build-upload.md",
        source="docs/operating/external-build-upload.md",
        content_file="external-build-upload.md",
        name="external-build-upload",
        title="Preparing artifacts for the external-build lane",
        description=(
            "The default build lane: build the kernel locally and upload it, no "
            "operator-staged source tree or build host needed. How to shape the upload "
            "artifacts: the combined kernel+modules gzip tar (boot/vmlinuz bzImage + "
            "lib/modules/<release>/), the exact tar recipe, and the optional "
            "vmlinux/effective_config/initrd. Cited by the runs.create build_profile schema "
            "and artifacts.expected_uploads."
        ),
    ),
    DocResource(
        uri="resource://kdive/docs/operating/build-source-staging.md",
        source="docs/operating/build-source-staging.md",
        content_file="build-source-staging.md",
        name="build-source-staging",
        title="Staging kernel source for runs.build",
        description=(
            "Advanced single-host alternative to the default external-upload lane: how to "
            "stage a warm kernel source tree (KDIVE_KERNEL_SRC) or register a remote build "
            "host for the server-build lane. Cited by the runs.create build_profile schema."
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
            "advertised tool outputSchema."
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
