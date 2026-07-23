"""Registry mapping ``(group, subcommand)`` to a CLI verb handler and its argparse shape.

The registry is the single source of truth: :func:`add_subparsers` builds the parser tree
from it and :func:`run_verb` dispatches against it, so adding a verb is one ``Verb`` entry.
Mutating verbs (a later M2.2 task) append their own entries to this same tuple (ADR-0089).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import kdive.cli.commands.doctor as doctor
import kdive.cli.commands.images as images
import kdive.cli.commands.mutations as mutations
import kdive.cli.commands.reads as reads
from kdive.cli.commands._generated_verbs import GENERATED_VERBS
from kdive.cli.commands.verb_spec import GeneratedFlag, GeneratedVerb
from kdive.cli.reserved_flags import derive_cli_flag

__all__ = [
    "GENERATED_ARG_PREFIX",
    "REGISTRY",
    "Verb",
    "add_subparsers",
    "doctor",
    "images",
    "run_verb",
]


@dataclass(frozen=True)
class Verb:
    """One CLI verb: its ``group subcommand`` path, handler, MCP tool, and argparse shape.

    ``tool`` is the MCP tool the handler calls. It is declared here so the read-only gate
    test (``tests/mcp/test_read_tools_annotated.py``) can prove, from the same registry that
    drives dispatch, that no curated read verb reaches a non-read-only tool (ADR-0089).

    ``read_only`` distinguishes the curated read verbs (default ``True``) from the
    break-glass mutating verbs (``False``), whose ``tool`` is intentionally a
    ``destructive()``-annotated server tool. The gate test only holds read-only verbs to
    the read-only hint; the mutating verbs are never reachable through the *read-only* default
    of the ``tool call`` passthrough (a destructive tool needs an explicit ``--allow-destructive``
    opt-in there — ADR-0107).

    ``required_options`` are ``--`` options the underlying tool declares as required
    arguments (no server-side default); the CLI marks them ``required=True`` so an omission
    fails up front with a clean usage error (exit 2) rather than an opaque server-side
    missing-argument error. ``options`` stay optional (default ``None``).
    """

    group: str
    sub: str
    handler: Callable[[argparse.Namespace], Awaitable[int]]
    tool: str
    positionals: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    required_options: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    read_only: bool = True
    help: str = ""


REGISTRY: tuple[Verb, ...] = (
    Verb("resources", "list", reads.resources_list, "resources.list", options=("kind",)),
    Verb("resources", "describe", reads.resources_get, "resources.describe", ("resource_id",)),
    Verb(
        "allocations",
        "list",
        reads.allocations_list,
        "allocations.list",
        required_options=("project",),
    ),
    Verb("allocations", "get", reads.allocations_get, "allocations.get", ("allocation_id",)),
    Verb("systems", "list", reads.systems_list, "systems.list", options=("state",)),
    Verb("systems", "get", reads.systems_get, "systems.get", ("system_id",)),
    Verb("runs", "get", reads.runs_get, "runs.get", ("run_id",)),
    Verb("jobs", "list", reads.jobs_list, "jobs.list"),
    Verb("jobs", "get", reads.jobs_get, "jobs.get", ("job_id",)),
    Verb(
        "accounting",
        "usage-project",
        reads.ledger_get,
        "accounting.usage_project",
        required_options=("project",),
    ),
    Verb(
        "accounting",
        "report-all-projects",
        reads.ledger_report_all,
        "accounting.report_all_projects",
        options=("group_by", "since", "until"),
        help="platform-wide accounting rollup (requires a platform_auditor token)",
    ),
    Verb(
        "accounting",
        "report-granted-set",
        reads.ledger_report_granted,
        "accounting.report_granted_set",
        options=("projects", "group_by", "since", "until"),
        help="accounting rollup across your granted projects",
    ),
    Verb("inventory", "list", reads.inventory_show, "inventory.list", options=("project",)),
    Verb("secrets", "list", reads.secrets_list, "secrets.list"),
    Verb("fixtures", "list", reads.fixtures_list, "fixtures.list"),
    Verb(
        "ops",
        "force-teardown",
        mutations.teardown,
        "ops.force_teardown",
        ("system_id",),
        options=("reason",),
        flags=("force",),
        read_only=False,
    ),
    Verb(
        "ops",
        "force-release",
        mutations.allocations_force_release,
        "ops.force_release",
        ("allocation_id",),
        options=("reason",),
        read_only=False,
    ),
    Verb(
        "resources",
        "cordon",
        mutations.resources_cordon,
        "resources.cordon",
        ("resource_id",),
        read_only=False,
    ),
    Verb(
        "resources",
        "drain",
        mutations.resources_drain,
        "resources.drain",
        ("resource_id",),
        options=("mode", "reason"),
        read_only=False,
    ),
    Verb("images", "list", images.images_list, "images.list"),
    Verb(
        "images",
        "describe",
        reads.images_get,
        "images.describe",
        ("image_id",),
        options=("target_kernel",),
    ),
    Verb(
        "images",
        "upload",
        images.images_upload,
        "images.upload",
        options=("project", "name", "arch", "quarantine_key", "lifetime_seconds"),
        read_only=False,
    ),
    Verb(
        "images",
        "delete",
        images.images_delete,
        "images.delete",
        ("image_id",),
        read_only=False,
    ),
    Verb(
        "images",
        "build",
        images.images_build,
        "images.build",
        options=("provider", "name", "packages"),
        read_only=False,
    ),
    Verb(
        "images",
        "publish",
        images.images_publish,
        "images.publish",
        options=("provider", "name", "packages"),
        read_only=False,
    ),
    Verb(
        "images",
        "prune-expired",
        images.images_prune,
        "images.prune_expired",
        options=("reason",),
        flags=("expired",),
        read_only=False,
    ),
    Verb(
        "images",
        "extend",
        images.images_extend,
        "images.extend",
        ("image_id",),
        options=("seconds", "reason"),
        read_only=False,
    ),
)


_CURATED_BY_PATH: dict[tuple[str, str], Verb] = {(v.group, v.sub): v for v in REGISTRY}
_GENERATED_BY_PATH: dict[tuple[str, str], GeneratedVerb] = {
    (v.group, v.sub): v for v in GENERATED_VERBS
}
_ARG_TYPES: dict[str, type] = {"str": str, "int": int, "float": float}

#: Generated-verb flag values land on the namespace under this prefix (``genarg_<param>``),
#: so a tool parameter named ``command``/``subcommand``/``json`` can never clobber argparse's
#: routing keys. The generic dispatch handler (#1450) strips the prefix to rebuild the payload.
GENERATED_ARG_PREFIX = "genarg_"


def _json_parent() -> argparse.ArgumentParser:
    """A parent parser letting ``--json`` follow the verb (e.g. ``resources list --json``).

    The default is ``SUPPRESS`` so an absent post-verb ``--json`` does not clobber the
    top-level ``--json`` already parsed onto the namespace (argparse subparser-default trap).
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    return parent


def _verb_parser(
    group_parser: argparse._SubParsersAction, verb: Verb, parent: argparse.ArgumentParser
) -> None:
    """Add ``verb``'s sub-subparser, declaring its positionals and ``--`` options."""
    parser = group_parser.add_parser(verb.sub, parents=[parent], help=verb.help or None)
    for positional in verb.positionals:
        parser.add_argument(positional)
    for option in verb.options:
        if option == "packages":
            parser.add_argument(f"--{option.replace('_', '-')}", dest=option, action="append")
            continue
        parser.add_argument(f"--{option.replace('_', '-')}", dest=option, default=None)
    for option in verb.required_options:
        parser.add_argument(f"--{option.replace('_', '-')}", dest=option, required=True)
    for flag in verb.flags:
        parser.add_argument(f"--{flag.replace('_', '-')}", dest=flag, action="store_true")


def _add_generated_flag(parser: argparse.ArgumentParser, flag: GeneratedFlag) -> None:
    """Declare one schema-derived ``--flag`` on ``parser`` per its :class:`GeneratedFlag`.

    Honors ``action`` (``store_true`` / ``append``), ``arg_type`` (``str`` / ``int`` /
    ``float``), and ``choices`` (enum). The ``--<param>-json`` escape for non-scalar params is
    a sibling (:func:`_add_generated_json_flag`); the flag-value-to-payload assembly (#1450) is
    downstream. This only shapes the parser so every generated verb is reachable at its path.
    """
    dest = f"{GENERATED_ARG_PREFIX}{flag.dest}"
    help_ = flag.help or None
    choices = flag.choices or None
    if flag.action == "store_true":
        parser.add_argument(flag.name, dest=dest, action="store_true", help=help_)
    elif flag.action == "append":
        parser.add_argument(
            flag.name,
            dest=dest,
            action="append",
            required=flag.required,
            choices=choices,
            help=help_,
        )
    else:
        parser.add_argument(
            flag.name,
            dest=dest,
            default=None,
            required=flag.required,
            choices=choices,
            type=_ARG_TYPES[flag.arg_type] if flag.arg_type is not None else str,
            help=help_,
        )


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
        type=_json_container_arg,
        help=f"JSON-encoded value (object or array) for the {param!r} parameter",
    )


def _generated_verb_parser(
    group_parser: argparse._SubParsersAction,
    verb: GeneratedVerb,
    parent: argparse.ArgumentParser,
) -> None:
    """Add a schema-generated verb's sub-subparser, declaring its scalar and JSON ``--flags``.

    A verb the committed artifact marks ``destructive`` also gets ``--yes`` so its typed-``yes``
    confirmation (ADR-0421 decision 4, driven by :func:`kdive.cli.dispatch.invoke_generated_verb`)
    is dischargeable non-interactively. ``--yes`` is reserved (``RESERVED_CLI_FLAGS``), so it can
    never shadow a generated parameter flag. The live-annotation tier still governs the actual
    ceremony at call time — the committed ``destructive`` bit only decides whether the flag exists.
    """
    parser = group_parser.add_parser(verb.sub, parents=[parent], help=verb.help or None)
    for flag in verb.flags:
        _add_generated_flag(parser, flag)
    for param in verb.json_params:
        _add_generated_json_flag(parser, param)
    if verb.destructive:
        parser.add_argument(
            "--yes",
            dest="yes",
            action="store_true",
            help="skip the destructive-call confirmation prompt (for non-interactive use)",
        )


def add_subparsers(sub: argparse._SubParsersAction) -> None:
    """Add one subparser per verb across the merged generated + curated surface.

    Every registered MCP tool contributes a verb at its canonical ``group subcommand`` path
    (derived from the tool name). A curated :class:`Verb` overrides the argparse shape at its
    derived path — never a second path — so its hand-tuned positionals/options win; every other
    path takes the schema-derived generated shape.
    """
    parent = _json_parent()
    groups: dict[str, argparse._SubParsersAction] = {}
    for generated in GENERATED_VERBS:
        group_parser = groups.get(generated.group)
        if group_parser is None:
            parser = sub.add_parser(generated.group)
            group_parser = parser.add_subparsers(dest="subcommand", required=True)
            groups[generated.group] = group_parser
        curated = _CURATED_BY_PATH.get((generated.group, generated.sub))
        if curated is not None:
            _verb_parser(group_parser, curated, parent)
        else:
            _generated_verb_parser(group_parser, generated, parent)
    _doctor_parser(sub, parent)


def _doctor_parser(sub: argparse._SubParsersAction, parent: argparse.ArgumentParser) -> None:
    """Add the ``doctor`` verb: a deployment-diagnostics gate, not a generic read verb.

    It is wired here (not as a ``Verb``) because it has a bespoke flag (``--with-egress``),
    renders a fixed verdict table, and maps its own gate-safe exit codes (ADR-0091 §5).
    """
    parser = sub.add_parser("doctor", parents=[parent], help="run deployment diagnostics")
    parser.add_argument("--provider", dest="provider", default=None)
    parser.add_argument("--with-egress", dest="with_egress", action="store_true")


async def run_verb(args: argparse.Namespace) -> int:
    """Resolve ``(command, subcommand)`` against the merged verb surface and dispatch it.

    A curated :class:`Verb` at the path runs its hand-written handler. Any other registered
    tool routes through the generic generated-verb seam, which invokes the tool via the same
    ``tool call`` passthrough (:func:`kdive.cli.dispatch.invoke_generated_verb`).

    Raises:
        SystemExit: When no registered tool matches the parsed command/subcommand.
    """
    subcommand = getattr(args, "subcommand", None)
    key = (args.command, subcommand)
    curated = _CURATED_BY_PATH.get(key)
    if curated is not None:
        return await curated.handler(args)
    generated = _GENERATED_BY_PATH.get(key)
    if generated is not None:
        from kdive.cli import dispatch

        return await dispatch.invoke_generated_verb(generated, args)
    raise SystemExit(f"unknown command: {args.command} {subcommand or ''}".rstrip())
