"""``kdivectl images`` verbs: list (read) + the operator/admin mutating verbs (ADR-0089).

The verbs are thin MCP clients over the shared ``images.*`` server tools — there is no second
source of truth. ``images list`` is a read passthrough (RBAC-filtered server-side to public +
the caller's projects' private rows). The mutating verbs run the fail-closed token-``exp``
preflight before their one MCP call, exactly like the break-glass mutations:
``upload``/``delete`` route the project-scoped tools, ``build``/``publish`` the
``platform_operator`` tools, and ``prune --expired``/``extend`` the ``platform_admin``
break-glass tools. A server-side denial returns a typed failure envelope the verb maps to a
non-zero exit, so an unprivileged or cross-project invocation is observable as exit ``3``.
"""

from __future__ import annotations

import argparse

from kdive.cli.commands.mutations import run_mutating_tool
from kdive.cli.commands.reads import collection_rows, fetch_collection_envelope
from kdive.cli.render import emit, render


def _packages(args: argparse.Namespace) -> list[str]:
    """Return repeatable ``--packages`` values, omitting blanks."""
    raw = getattr(args, "packages", None)
    if not raw:
        return []
    return [str(package).strip() for package in raw if str(package).strip()]


def _image_build_request(args: argparse.Namespace) -> dict[str, object]:
    return {
        "provider": args.provider,
        "name": args.name,
        "packages": _packages(args),
    }


async def images_list(args: argparse.Namespace) -> int:
    envelope = await fetch_collection_envelope("images.list", {})
    columns = ["id", "name", "arch", "visibility", "owner", "state"]
    emit(envelope, lambda: render(collection_rows(envelope), columns=columns), as_json=args.json)
    return 0


async def images_upload(args: argparse.Namespace) -> int:
    """Register a quarantined upload as a project-private image (operator on the project)."""
    arguments: dict[str, object] = {
        "project": args.project,
        "name": args.name,
        "arch": args.arch,
        "quarantine_key": args.quarantine_key,
    }
    lifetime = getattr(args, "lifetime_seconds", None)
    if lifetime is not None:
        arguments["lifetime_seconds"] = int(lifetime)
    return await run_mutating_tool("images.upload", arguments, as_json=args.json)


async def images_delete(args: argparse.Namespace) -> int:
    return await run_mutating_tool("images.delete", {"image_id": args.image_id}, as_json=args.json)


async def images_build(args: argparse.Namespace) -> int:
    return await run_mutating_tool(
        "images.build",
        {"request": _image_build_request(args)},
        as_json=args.json,
    )


async def images_publish(args: argparse.Namespace) -> int:
    return await run_mutating_tool(
        "images.publish",
        {"request": _image_build_request(args)},
        as_json=args.json,
    )


async def images_prune(args: argparse.Namespace) -> int:
    """Force the expired-private-image sweep now (platform_admin break-glass).

    Raises:
        SystemExit: When ``--expired`` is not supplied; the flag is the explicit
            acknowledgement that this triggers the destructive expiry sweep.
    """
    if not getattr(args, "expired", False):
        raise SystemExit("images prune is destructive: pass --expired to confirm the sweep")
    return await run_mutating_tool(
        "images.prune_expired", {"reason": args.reason}, as_json=args.json
    )


async def images_extend(args: argparse.Namespace) -> int:
    return await run_mutating_tool(
        "images.extend",
        {"image_id": args.image_id, "seconds": int(args.seconds), "reason": args.reason},
        as_json=args.json,
    )
