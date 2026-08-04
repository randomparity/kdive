"""``kdivectl images`` verbs: list (read) + the operator/admin mutating verbs (ADR-0089).

The verbs are thin MCP clients over the shared ``images.*`` server tools — there is no second
source of truth. ``images list`` is a read passthrough (RBAC-filtered server-side to public +
the caller's projects' private rows). The mutating verbs run the fail-closed token-``exp``
preflight before their one MCP call, exactly like the break-glass mutations:
``upload``/``delete`` route the project-scoped tools, and ``prune --expired``/``extend`` the
``platform_admin`` break-glass tools. A server-side denial returns a typed failure envelope the
verb maps to a non-zero exit, so an unprivileged or cross-project invocation is observable as
exit ``3``.

``images publish`` has no specialised handler override, so the descriptor-driven generic path
derives its flags and payload from the live ``images.publish`` schema (ADR-0423, ADR-0461).

``images list`` carries ``--scope``, the operator path to the public baseline catalog that the
removed ``fixtures list`` verb used to print (ADR-0465). Its descriptor owns that flag; this
module only owns specialised rendering.
"""

from __future__ import annotations

import argparse

from kdive.cli.commands.generated_args import (
    local_acknowledgement,
    optional_generated_arg,
    required_generated_arg,
)
from kdive.cli.commands.mutations import run_mutating_tool
from kdive.cli.commands.reads import collection_rows, fetch_collection_envelope
from kdive.cli.errors import exit_code_for_envelope
from kdive.cli.render import emit, render


async def images_list(args: argparse.Namespace) -> int:
    """List catalog images; ``--scope public_baseline`` narrows to the baseline set (ADR-0465).

    An omitted ``--scope`` sends no ``request`` at all, leaving the server's ``visible`` default
    authoritative rather than restating it here. An unrecognized value is refused by argument
    parsing: the flag's ``choices`` are the tool schema's own enum (ADR-0469).
    """
    scope = optional_generated_arg(args, "scope", str)
    arguments: dict[str, object] = {} if scope is None else {"request": {"scope": scope}}
    envelope = await fetch_collection_envelope("images.list", arguments)
    columns = ["id", "name", "arch", "visibility", "owner", "state"]
    emit(envelope, lambda: render(collection_rows(envelope), columns=columns), as_json=args.json)
    return exit_code_for_envelope(envelope)


async def images_upload(args: argparse.Namespace) -> int:
    """Register a quarantined upload as a project-private image (operator on the project)."""
    arguments: dict[str, object] = {
        "project": required_generated_arg(args, "project", str),
        "name": required_generated_arg(args, "name", str),
        "arch": required_generated_arg(args, "arch", str),
        "quarantine_key": required_generated_arg(args, "quarantine_key", str),
    }
    lifetime = optional_generated_arg(args, "lifetime_seconds", int)
    if lifetime is not None:
        arguments["lifetime_seconds"] = lifetime
    return await run_mutating_tool("images.upload", arguments, as_json=args.json)


async def images_delete(args: argparse.Namespace) -> int:
    return await run_mutating_tool(
        "images.delete",
        {"image_id": required_generated_arg(args, "image_id", str)},
        as_json=args.json,
    )


async def images_prune(args: argparse.Namespace) -> int:
    """Force the expired-private-image sweep now (platform_admin break-glass).

    Raises:
        SystemExit: When ``--expired`` is not supplied; the flag is the explicit
            acknowledgement that this triggers the destructive expiry sweep.
    """
    if not local_acknowledgement(args, "expired"):
        raise SystemExit("images prune is destructive: pass --expired to confirm the sweep")
    return await run_mutating_tool(
        "images.prune_expired",
        {"reason": required_generated_arg(args, "reason", str)},
        as_json=args.json,
    )


async def images_extend(args: argparse.Namespace) -> int:
    """Extend a private image's expiry; ``--seconds`` arrives already coerced (ADR-0474)."""
    return await run_mutating_tool(
        "images.extend",
        {
            "image_id": required_generated_arg(args, "image_id", str),
            "seconds": required_generated_arg(args, "seconds", int),
            "reason": required_generated_arg(args, "reason", str),
        },
        as_json=args.json,
    )
