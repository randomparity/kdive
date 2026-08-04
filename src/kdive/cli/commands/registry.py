"""Build and dispatch the schema-generated ``kdivectl`` command surface.

The committed generated descriptors own every MCP command path and parser shape.  This module
only keeps the small tool-keyed handler map for commands with specialised rendering or payload
assembly; it never re-declares a command path or its argument grammar.
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Awaitable, Callable

import kdive.cli.commands.doctor as doctor
import kdive.cli.commands.images as images
import kdive.cli.commands.mutations as mutations
import kdive.cli.commands.reads as reads
from kdive.cli.commands._generated_verbs import GENERATED_VERBS
from kdive.cli.commands.generated_args import GENERATED_ARG_PREFIX
from kdive.cli.commands.verb_spec import GeneratedFlag, GeneratedLocalFlag, GeneratedVerb
from kdive.cli.reserved_flags import derive_cli_flag

__all__ = [
    "HANDLER_OVERRIDES",
    "add_subparsers",
    "doctor",
    "images",
    "run_verb",
]


Handler = Callable[[argparse.Namespace], Awaitable[int]]


# These handlers specialise rendering or reshape descriptor-owned parser values into the MCP
# payload. They are keyed by tool because paths belong exclusively to ``GENERATED_VERBS``.
HANDLER_OVERRIDES: dict[str, Handler] = {
    "resources.list": reads.resources_list,
    "resources.describe": reads.resources_get,
    "allocations.list": reads.allocations_list,
    "systems.list": reads.systems_list,
    "systems.get": reads.systems_get,
    "runs.get": reads.runs_get,
    "jobs.list": reads.jobs_list,
    "jobs.wait": reads.jobs_wait,
    "allocations.wait": reads.allocations_wait,
    "accounting.usage": reads.ledger_get,
    "accounting.report": reads.ledger_report,
    "inventory.list": reads.inventory_show,
    "secrets.list": reads.secrets_list,
    "ops.force_teardown": mutations.teardown,
    "ops.force_release": mutations.allocations_force_release,
    "resources.set_scheduling": mutations.resources_set_scheduling,
    "resources.drain": mutations.resources_drain,
    "images.list": images.images_list,
    "images.describe": reads.images_get,
    "images.upload": images.images_upload,
    "images.delete": images.images_delete,
    "images.prune_expired": images.images_prune,
    "images.extend": images.images_extend,
}


_GENERATED_BY_PATH: dict[tuple[str, str], GeneratedVerb] = {
    (v.group, v.sub): v for v in GENERATED_VERBS
}


def _finite_float(raw: str) -> float:
    """Parse ``raw`` as a float, refusing ``inf``/``nan`` (ADR-0474 decision 2).

    ``float()`` accepts both, but JSON encodes neither: pydantic serializes them to ``null``
    without raising, so the value does not reach the tool as a number its own validation could
    reject — it reaches it as a missing key, and the tool quietly applies its default. Every
    float parameter in the CLI is a timeout or a deadline in seconds, where an infinite or
    undefined value has no meaning the wire format could carry.

    Raises:
        argparse.ArgumentTypeError: When ``raw`` is not a finite number, so argparse renders it
            as a usage error on exit 2 rather than letting a ``ValueError`` escape.
    """
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid float value: {raw!r}") from None
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"must be a finite number, not {raw!r}")
    return value


#: How argparse consumes each ``GeneratedFlag.arg_type``.  Both option and positional forms use
#: this one map, so schema-derived numeric validation cannot drift between presentations.
#: ``int`` stays the builtin: it already refuses ``inf``/``nan``/``1.5`` with the ``ValueError``
#: argparse renders as a usage error, so a wrapper there would change no outcome.
_ARG_TYPES: dict[str, Callable[[str], object]] = {
    "str": str,
    "int": int,
    "float": _finite_float,
}


def _json_parent() -> argparse.ArgumentParser:
    """A parent parser letting ``--json`` follow the verb (e.g. ``resources list --json``).

    The default is ``SUPPRESS`` so an absent post-verb ``--json`` does not clobber the
    top-level ``--json`` already parsed onto the namespace (argparse subparser-default trap).
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    return parent


def _add_generated_flag(
    parser: argparse.ArgumentParser, flag: GeneratedFlag, *, positional: bool
) -> None:
    """Declare one schema-derived ``--flag`` on ``parser`` per its :class:`GeneratedFlag`.

    Honors ``action`` (``store_true`` / ``bool_optional`` / ``append``), ``arg_type``
    (``str`` / ``int`` / ``float``), and ``choices`` (enum). ``bool_optional`` declares the
    ``--flag`` / ``--no-flag`` pair a required boolean needs to express false.
    The ``--<param>-json`` escape for non-scalar params is
    a sibling (:func:`_add_generated_json_flag`); the flag-value-to-payload assembly (#1450) is
    downstream. This only shapes the parser so every generated verb is reachable at its path.
    """
    dest = f"{GENERATED_ARG_PREFIX}{flag.dest}"
    help_ = flag.help or None
    choices = flag.choices or None
    metavar = None if choices is not None else flag.dest.upper()
    if positional:
        if flag.action is not None:
            raise ValueError(f"positional {flag.dest!r} cannot use {flag.action!r}")
        parser.add_argument(
            dest,
            metavar=flag.dest,
            choices=choices,
            type=_ARG_TYPES[flag.arg_type] if flag.arg_type is not None else str,
            help=help_,
        )
        return
    if flag.action == "store_true":
        parser.add_argument(flag.name, dest=dest, action="store_true", help=help_)
    elif flag.action == "bool_optional":
        parser.add_argument(
            flag.name,
            dest=dest,
            action=argparse.BooleanOptionalAction,
            required=flag.required,
            help=help_,
        )
    elif flag.action == "append":
        parser.add_argument(
            flag.name,
            dest=dest,
            action="append",
            required=flag.required,
            choices=choices,
            metavar=metavar,
            help=help_,
        )
    else:
        parser.add_argument(
            flag.name,
            dest=dest,
            default=None,
            required=flag.required,
            choices=choices,
            metavar=metavar,
            type=_ARG_TYPES[flag.arg_type] if flag.arg_type is not None else str,
            help=help_,
        )


def _add_generated_local_flag(parser: argparse.ArgumentParser, flag: GeneratedLocalFlag) -> None:
    """Add a descriptor-owned acknowledgement that never becomes MCP payload data."""
    parser.add_argument(flag.name, dest=flag.dest, action="store_true", help=flag.help or None)


def _json_container_arg(value: str) -> str:
    """argparse ``type=`` validating a ``--<param>-json`` value is a JSON object or array.

    Mirrors :func:`kdive.cli.dispatch._parse_payload`'s "valid JSON, not a bare scalar" gate,
    but raises :class:`argparse.ArgumentTypeError` so a malformed or scalar value fails as a
    clean usage error (exit 2) at parse time — before the verb dispatches — instead of the
    server-side error a bad payload would otherwise raise. Both a JSON object and a JSON array
    are accepted because the non-scalar params span both shapes (e.g. ``profile`` is an object,
    ``artifacts`` is a ``Sequence[...]`` array); the descriptor does not record which, and the
    per-param typed payload assembly is #1450. The raw string is returned unchanged.
    """
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict | list):
        raise argparse.ArgumentTypeError("must be a JSON object or array")
    return value


def _add_generated_json_flag(parser: argparse.ArgumentParser, param: str) -> None:
    """Declare the ``--<param>-json`` escape for a non-scalar generated-verb parameter (#1449).

    Params the generator cannot express as a typed scalar flag — nested objects, object arrays —
    are recorded in :attr:`GeneratedVerb.json_params` (#1447) and surfaced here as a single
    JSON-valued flag, validated to a JSON container (:func:`_json_container_arg`) at parse time.
    The raw string lands on the namespace under the ``genarg_<param>_json`` dest for the
    flag-value-to-payload assembly seam (#1450).
    """
    parser.add_argument(
        f"{derive_cli_flag(param)}-json",
        dest=f"{GENERATED_ARG_PREFIX}{param}_json",
        default=None,
        metavar=f"{param.upper()}_JSON",
        type=_json_container_arg,
        help=f"JSON-encoded value (object or array) for the {param!r} parameter",
    )


def _generated_verb_parser(
    group_parser: argparse._SubParsersAction,
    verb: GeneratedVerb,
    parent: argparse.ArgumentParser,
) -> None:
    """Add one descriptor-defined verb parser.

    A descriptor with ``confirm_destructive`` gets ``--yes`` so its typed-``yes`` confirmation
    (ADR-0421 decision 4, driven by :func:`kdive.cli.dispatch.invoke_generated_verb`) is
    dischargeable non-interactively. ``--yes`` is reserved (``RESERVED_CLI_FLAGS``), so it can
    never shadow a generated parameter flag. The live-annotation tier still governs the actual
    ceremony at call time; descriptor metadata only decides whether the flag exists.
    """
    parser = group_parser.add_parser(verb.sub, parents=[parent], help=verb.help or None)
    for flag in verb.flags:
        _add_generated_flag(parser, flag, positional=flag.dest in verb.positionals)
    for param in verb.json_params:
        _add_generated_json_flag(parser, param)
    for flag in verb.local_flags:
        _add_generated_local_flag(parser, flag)
    if verb.confirm_destructive:
        parser.add_argument(
            "--yes",
            dest="yes",
            action="store_true",
            help="skip the destructive-call confirmation prompt (for non-interactive use)",
        )


def add_subparsers(sub: argparse._SubParsersAction) -> None:
    """Add one descriptor-owned subparser for every generated MCP verb."""
    parent = _json_parent()
    groups: dict[str, argparse._SubParsersAction] = {}
    for generated in GENERATED_VERBS:
        group_parser = groups.get(generated.group)
        if group_parser is None:
            parser = sub.add_parser(generated.group)
            group_parser = parser.add_subparsers(dest="subcommand", required=True)
            groups[generated.group] = group_parser
        _generated_verb_parser(group_parser, generated, parent)
    _doctor_parser(sub, parent)


def _doctor_parser(sub: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    """Add the ``doctor`` verb: a deployment-diagnostics gate, not a generic read verb.

    It has no MCP tool descriptor because it runs local diagnostics. Its bespoke
    ``--with-egress`` flag, fixed verdict table, and gate-safe exit codes therefore remain a
    standalone CLI path (ADR-0091 §5).
    """
    parser = sub.add_parser("doctor", parents=[parent], help="run deployment diagnostics")
    parser.add_argument("--provider", dest="provider", default=None)
    parser.add_argument("--with-egress", dest="with_egress", action="store_true")


async def run_verb(args: argparse.Namespace) -> int:
    """Resolve the generated command path, then select custom execution by its MCP tool.

    A handler override changes rendering or payload shaping only.  It never participates in path
    resolution or parser construction.

    Raises:
        SystemExit: When no registered tool matches the parsed command/subcommand.
    """
    subcommand = getattr(args, "subcommand", None)
    key = (args.command, subcommand)
    generated = _GENERATED_BY_PATH.get(key)
    if generated is not None:
        handler = HANDLER_OVERRIDES.get(generated.tool)
        if handler is not None:
            return await handler(args)
        from kdive.cli import dispatch

        return await dispatch.invoke_generated_verb(generated, args)
    raise SystemExit(f"unknown command: {args.command} {subcommand or ''}".rstrip())
