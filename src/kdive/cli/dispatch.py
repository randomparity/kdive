"""Async dispatch for ``kdivectl`` subcommands (ADR-0089, ADR-0107).

The generic ``tool call`` passthrough lives here. It lists the server's tools, classifies the
requested one into a mutation tier, admits it only when the caller opted in to that tier
(``--allow-mutating`` / ``--allow-destructive``), runs the token-``exp`` preflight for mutating
tiers, confirms a destructive call (typed ``yes`` on a TTY, or ``--yes``), calls the tool, prints
the structured result, and derives the exit code from the response envelope. ``login`` mints and
caches a bearer token; curated verbs route through ``commands.run_verb``.

Schema-generated verbs route through :func:`invoke_generated_verb` (ADR-0423), which unites the
passthrough's live-annotation tier ceremony with typed argparse arguments: it assembles the tool
payload from the parsed namespace, resolves the tier from the live annotations, drives the
mutating/destructive ceremony from that tier, and renders through :func:`render_envelope`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable

from fastmcp.exceptions import ToolError

from kdive.cli.commands import registry as commands
from kdive.cli.commands.mutations import TokenExpiringError, ensure_token_valid
from kdive.cli.commands.verb_spec import GeneratedVerb
from kdive.cli.errors import exit_code_for_envelope
from kdive.cli.passthrough import (
    ToolNotAllowedError,
    ToolTier,
    assert_tool_allowed,
    classify_tool,
)
from kdive.cli.render import render_envelope
from kdive.cli.transport import Session, tool_envelope

_TIER_NOT_ALLOWED_EXIT = 3
_TOOL_ERROR_EXIT = 1
_PREFLIGHT_TIERS = frozenset({ToolTier.MUTATING, ToolTier.DESTRUCTIVE})


async def run(args: argparse.Namespace) -> int:
    """Dispatch a parsed ``kdivectl`` invocation to its handler.

    A tool that signals failure by *raising* ``ToolError`` (rather than returning a failure
    envelope) — e.g. a project-not-granted call to ``allocations.list`` — is surfaced as a
    one-line stderr message and a generic nonzero exit, never an uncaught traceback. Failures
    returned as a ``ToolResponse`` envelope keep their mapped exit code (:mod:`kdive.cli.errors`).

    Returns:
        The process exit code (0 on success; see :mod:`kdive.cli.errors` for failures).
    """
    try:
        return await _dispatch(args)
    except ToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _TOOL_ERROR_EXIT


async def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "tool" and args.tool_command == "call":
        return await _tool_call(args)
    if args.command == "login":
        return _login(args)
    if args.command == "completion":
        return _completion(args)
    if args.command == "doctor":
        return await commands.doctor.doctor(args)
    return await commands.run_verb(args)


def _completion(args: argparse.Namespace) -> int:
    """Print the offline shell completion script for the requested shell (ADR-0424).

    Dispatched before any handler builds a ``Session``, so it never reads a token or reaches the
    server — completing a command line is not an authenticated operation.
    """
    from kdive.cli.completion import render_completion

    print(render_completion(args.shell), end="")
    return 0


def _login(args: argparse.Namespace) -> int:
    """Acquire a bearer token on the platform-role axis and cache it 0600.

    The token is never printed or logged; only a confirmation line is emitted.
    """
    from kdive.cli.login import login

    login(args.platform_role)
    role = args.platform_role or "none"
    print(f"login ok (platform_role={role}); token cached")
    return 0


def _session_factory() -> Session:
    return Session.from_env()


def _max_tier(args: argparse.Namespace) -> ToolTier:
    if args.allow_destructive:
        return ToolTier.DESTRUCTIVE
    if args.allow_mutating:
        return ToolTier.MUTATING
    return ToolTier.READ_ONLY


def _confirm_destructive(
    name: str, *, assume_yes: bool, is_tty: bool, read_line: Callable[[], str]
) -> bool:
    """Return whether a destructive call is confirmed.

    ``--yes`` (``assume_yes``) discharges the prompt without reading. Otherwise a non-interactive
    stdin (``is_tty`` false) is an immediate refusal — the prompt would be unanswerable — and is
    never read. On a TTY the caller must type exactly ``yes``; EOF or anything else refuses.

    Args:
        name: The destructive tool name (accepted so callers pass it positionally; the prompt text
            is built by the injected ``read_line``).
        assume_yes: Whether ``--yes`` was passed.
        is_tty: Whether stdin is interactive.
        read_line: A zero-arg callable returning the typed line (injected for tests).

    Returns:
        ``True`` to proceed, ``False`` to refuse.
    """
    del name
    if assume_yes:
        return True
    if not is_tty:
        return False
    try:
        answer = read_line()
    except EOFError:
        return False
    return answer.strip() == "yes"


def _prompt_line(name: str) -> str:
    return input(f"type 'yes' to call destructive tool {name!r}: ")


async def _tool_call(args: argparse.Namespace) -> int:
    arguments = _parse_payload(args.payload)
    max_tier = _max_tier(args)
    session = _session_factory()
    async with session.client() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        try:
            tier = assert_tool_allowed(args.name, tools.get(args.name), max_tier=max_tier)
        except ToolNotAllowedError as exc:
            print(str(exc))
            return _TIER_NOT_ALLOWED_EXIT
        if tier in _PREFLIGHT_TIERS:
            try:
                ensure_token_valid(session.token, now=int(time.time()))
            except TokenExpiringError as exc:
                print(str(exc))
                return _TIER_NOT_ALLOWED_EXIT
        if tier is ToolTier.DESTRUCTIVE and not _confirm_destructive(
            args.name,
            assume_yes=args.yes,
            is_tty=sys.stdin.isatty(),
            read_line=lambda: _prompt_line(args.name),
        ):
            print("destructive call needs confirmation: re-run with --yes for non-interactive use")
            return _TIER_NOT_ALLOWED_EXIT
        result = await client.call_tool(args.name, arguments)
    envelope = tool_envelope(result)
    print(json.dumps(envelope, indent=2, default=str))
    return exit_code_for_envelope(envelope)


async def invoke_generated_verb(verb: GeneratedVerb, args: argparse.Namespace) -> int:
    """Dispatch one schema-generated verb: payload, tier, ceremony, call, render, exit (ADR-0423).

    Assembles the tool payload from the parsed namespace (:func:`_assemble_generated_payload`),
    resolves the verb's tier from the *live* server annotations — never the committed artifact, so
    a stale artifact cannot downgrade a tool's tier (ADR-0107 decision 4) — and drives the mutating
    ceremony from that tier: a mutating verb needs no opt-in flag (naming the verb is the
    acknowledgement, ADR-0421 decision 4), a mutating or destructive verb runs the fail-closed
    token-``exp`` preflight, and a destructive verb additionally needs the typed-``yes`` confirm
    (``--yes`` for non-interactive use). An unclassifiable tool (``UNKNOWN``) is refused. On the
    call, the response envelope is rendered (:func:`render_envelope` — a table by default, the whole
    envelope on ``--json``) and its exit code derived (:func:`exit_code_for_envelope`).
    """
    arguments = _assemble_generated_payload(verb, args)
    session = _session_factory()
    async with session.client() as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        tier = classify_tool(tools.get(verb.tool))
        refusal = _generated_ceremony_refusal(verb, tier, args, token=session.token)
        if refusal is not None:
            print(refusal)
            return _TIER_NOT_ALLOWED_EXIT
        result = await client.call_tool(verb.tool, arguments)
    envelope = tool_envelope(result)
    render_envelope(envelope, as_json=getattr(args, "json", False))
    return exit_code_for_envelope(envelope)


def _generated_ceremony_refusal(
    verb: GeneratedVerb, tier: ToolTier, args: argparse.Namespace, *, token: str
) -> str | None:
    """Return a refusal message when the generated-verb ceremony blocks the call, else ``None``.

    An ``UNKNOWN`` tier is fail-closed (unclassifiable tools are unreachable). Both mutating tiers
    run the token-``exp`` preflight; a destructive tier further requires the typed-``yes`` confirm.
    Unlike the ``tool call`` passthrough there is no tier opt-in flag: naming the verb authorizes
    its own tier (ADR-0421 decision 4).
    """
    if tier is ToolTier.UNKNOWN:
        return (
            f"{verb.tool!r} is not positively classified (read-only/mutating/destructive); "
            "it is unreachable"
        )
    if tier in _PREFLIGHT_TIERS:
        try:
            ensure_token_valid(token, now=int(time.time()))
        except TokenExpiringError as exc:
            return str(exc)
    if tier is ToolTier.DESTRUCTIVE and not _confirm_destructive(
        verb.tool,
        assume_yes=getattr(args, "yes", False),
        is_tty=sys.stdin.isatty(),
        read_line=lambda: _prompt_line(verb.tool),
    ):
        return "destructive call needs confirmation: re-run with --yes for non-interactive use"
    return None


def _assemble_generated_payload(verb: GeneratedVerb, args: argparse.Namespace) -> dict[str, object]:
    """Build a generated verb's MCP argument payload from its parsed argparse namespace.

    Each scalar/append flag value and each ``--<param>-json`` value lands on the namespace under
    the ``registry.GENERATED_ARG_PREFIX`` dest; this strips the prefix to rebuild the tool payload.
    A ``store_true`` flag contributes only when set (an unset boolean is omitted, letting the
    server default hold — argparse cannot distinguish "unset" from an explicit ``False``); every
    other absent flag (``None``) is likewise omitted. A ``--<param>-json`` value was validated to a
    JSON container at parse time (:func:`registry._json_container_arg`), so it re-parses cleanly.
    For an ``unwrap_request`` verb the whole body is re-wrapped under a single ``request`` key (and
    no key at all when nothing was given), exactly as the curated read verbs do by hand.
    """
    prefix = commands.GENERATED_ARG_PREFIX
    body: dict[str, object] = {}
    for flag in verb.flags:
        value = getattr(args, f"{prefix}{flag.dest}", None)
        if flag.action == "store_true":
            if value:
                body[flag.dest] = True
        elif value is not None:
            body[flag.dest] = value
    for param in verb.json_params:
        raw = getattr(args, f"{prefix}{param}_json", None)
        if raw is not None:
            body[param] = json.loads(raw)
    if verb.unwrap_request:
        return {"request": body} if body else {}
    return body


def _parse_payload(payload: str) -> dict[str, object]:
    """Parse the ``--json`` payload into an arguments dict, failing on malformed input."""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--json payload is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("--json payload must be a JSON object")
    return parsed
