"""Descriptor types for the schema-generated ``kdivectl`` verbs (epic #1442 R5/R6).

The generator (:mod:`scripts.gen_cli_verbs`) emits :data:`GENERATED_VERBS` in the
committed module :mod:`kdive.cli.commands._generated_verbs` as a tuple of
:class:`GeneratedVerb`, one per registered MCP tool. These are the *data* the later
parser-merge (#1448) and generic dispatch (#1450) consume; this module only defines
their shape, so the generated file imports a stable type rather than redefining it.

A :class:`GeneratedVerb` mirrors the fields of :class:`kdive.cli.commands.registry.Verb`
that are derivable from a tool schema — ``group``/``sub``/``tool``/``read_only`` — and
adds the schema-derived flag detail (:class:`GeneratedFlag`) and the ``request``-wrapper
unwrap marker that the hand-curated verbs encode by hand today.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GeneratedFlag:
    """One ``--flag`` derived from a scalar tool parameter (ADR-0421 decision 2).

    ``dest`` is the tool parameter name the flag's value is sent back under; ``name`` is
    the derived long flag (``derive_cli_flag(dest)``). Exactly one of ``arg_type`` /
    ``action`` describes how argparse consumes the value:

    * ``arg_type`` — ``"str"`` | ``"int"`` | ``"float"`` for a typed single value.
    * ``action`` — ``"store_true"`` for a boolean flag, or ``"append"`` for an
      array-of-string parameter (repeat the flag once per element).

    ``choices`` is the enum's allowed values (argparse ``choices=``) when the parameter
    carries an ``enum``, else empty.
    """

    name: str
    dest: str
    required: bool
    help: str
    arg_type: str | None = None
    action: str | None = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratedVerb:
    """One CLI verb derived from a registered MCP tool.

    ``group``/``sub`` are the ``group subcommand`` path, split from the ``namespace.op``
    tool name (``op`` underscores become dashes, mirroring the verb rule). ``flags`` are
    the scalar parameters that derive to ``--flags``.

    ``unwrap_request`` is set for the tools whose sole parameter is a ``request`` wrapper
    object: their flags are the *wrapper body's* scalar fields, flattened, and are
    re-wrapped under a single ``{"request": ...}`` key at call time (with no ``request``
    key when no flag is given), exactly as the curated read verbs do by hand.

    ``json_params`` names the parameters that are *not* scalar-derivable (nested objects,
    object arrays, typeless/tuple arrays, or scalar unions). This generator emits no flag
    for them; the ``--<param>-json`` escape that surfaces them is a separate entry (#1449).
    """

    group: str
    sub: str
    tool: str
    read_only: bool
    destructive: bool
    help: str = ""
    unwrap_request: bool = False
    flags: tuple[GeneratedFlag, ...] = ()
    json_params: tuple[str, ...] = field(default_factory=tuple)
