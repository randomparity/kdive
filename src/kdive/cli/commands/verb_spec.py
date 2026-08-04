"""Descriptor types for the schema-generated ``kdivectl`` verbs (epic #1442 R5/R6).

The generator (:mod:`scripts.gen_cli_verbs`) emits :data:`GENERATED_VERBS` in the
committed module :mod:`kdive.cli.commands._generated_verbs` as a tuple of
:class:`GeneratedVerb`, one per registered MCP tool. A descriptor is the sole source for
its command path and parser shape; this module defines that stable type so the generated
file does not redefine it.

Most fields come directly from the tool schema. Generator-owned presentation policy adds
positionals, local acknowledgement flags, and destructive-confirmation behavior where the
operator-facing CLI intentionally differs from a flat schema projection.
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
    * ``action`` — ``"store_true"`` for an optional boolean flag, ``"bool_optional"`` for a
      required one (argparse ``BooleanOptionalAction``: ``--flag`` / ``--no-flag``, because a
      required boolean must be able to express false), or ``"append"`` for an
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
class GeneratedLocalFlag:
    """A parser-only acknowledgement flag that is never sent to an MCP tool."""

    name: str
    dest: str
    help: str


@dataclass(frozen=True)
class GeneratedVerb:
    """One CLI verb derived from a registered MCP tool.

    ``group``/``sub`` are the ``group subcommand`` path, split from the ``namespace.op``
    tool name (``op`` underscores become dashes, mirroring the verb rule). ``flags`` are
    the scalar parameters that derive to ``--flags``.

    ``unwrap_request`` is set for the tools whose sole parameter is a ``request`` wrapper
    object: their flags are the *wrapper body's* scalar fields, flattened, and are
    re-wrapped under a single ``{"request": ...}`` key at call time (with no ``request``
    key when no flag is given), including for tools dispatched by specialised handlers.

    ``json_params`` names the parameters that are *not* scalar-derivable (nested objects,
    object arrays, typeless/tuple arrays, or scalar unions). This generator emits no flag
    for them; the ``--<param>-json`` escape that surfaces them is a separate entry (#1449).

    ``confirm_destructive`` controls whether this path exposes the generic ``--yes`` ceremony.
    ``None`` retains the legacy default of following :attr:`destructive`; generated descriptors
    set it explicitly so a bespoke historical handler can retain its narrower CLI surface.
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
    positionals: tuple[str, ...] = ()
    local_flags: tuple[GeneratedLocalFlag, ...] = ()
    confirm_destructive: bool | None = None
