"""Every ``kdivectl`` verb's argparse shape still fits the tool schema it dispatches to.

The three guards that came before this one are all artifact-vs-artifact or annotation-only:
``test_every_live_tool_is_covered`` proves each tool has a descriptor, ``cli-verbs-check``
proves the committed descriptors match a fresh generation, and
``tests/mcp/test_read_tools_annotated.py`` proves a curated ``read_only`` verb targets a
read-only tool. None of them compares a *parameter shape* to a *schema*, so a curated verb
kept dispatching its old payload after the tool it calls was reshaped — three times in epic
#1576 (#1589, #1590, and a third silent regression of the generated-dispatch fixture).

Two complementary guards close that (ADR-0469):

* **Curated verbs — payload capture.** A curated ``Verb``'s payload is built by hand-written
  Python, so nothing static can predict it. Each curated verb is therefore driven over the
  *real* parser with a capturing fake client, and every payload it emits is validated against
  the tool's live JSON schema. Because every tool schema is ``additionalProperties: false``,
  that single assertion covers unknown keys, dropped properties, and the ``{"request": ...}``
  wrapper convention in both directions.
* **Generated verbs — structural.** Their payload assembly is mechanical
  (``dispatch._assemble_generated_payload``), so the descriptor itself is checked against the
  schema: required properties reachable, no dest the schema lacks, enums carrying ``choices``,
  and no argument-less verb for a tool that demands arguments.

The invocation matrix is derived from each verb's own descriptor, not hand-written per verb, so
a new curated verb is covered the day it is added and there is nothing to keep in sync. The
rule it encodes: **every argv the parser accepts must produce a schema-valid payload.** A verb
that refuses an argv up front (``accounting usage`` demanding exactly one of
``--project`` / ``--investigation-id``) simply emits no payload for that row and is judged on
the rows it does emit; a verb that emits none at all is itself a failure.

No exclusion lists: a mismatch here is a broken verb, and the fix is the verb.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import itertools
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

import pytest
from jsonschema import Draft202012Validator

import kdive.cli.commands.mutations as mutations
import kdive.cli.commands.reads as reads
from kdive.cli.__main__ import build_parser
from kdive.cli.commands._generated_verbs import GENERATED_VERBS
from kdive.cli.commands.registry import REGISTRY, Verb
from kdive.cli.commands.verb_spec import GeneratedVerb
from scripts import gen_cli_verbs as gen

_OK_ENVELOPE = {"object_id": "o", "status": "ok", "data": {}, "items": []}


class _FakeResult:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


class _CapturingClient:
    """Records every ``call_tool`` instead of reaching a server."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _CapturingClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeResult:
        self.calls.append((name, dict(arguments)))
        return _FakeResult(dict(_OK_ENVELOPE))


class _FakeSession:
    def __init__(self, client: _CapturingClient) -> None:
        self._client = client
        self.token = "x.y.z"

    def client(self) -> _CapturingClient:
        return self._client


@pytest.fixture(scope="module")
def tool_schemas() -> dict[str, dict[str, Any]]:
    """The live registry's tool name -> JSON schema, built once for the module."""
    return {tool.name: (tool.parameters or {}) for tool in gen._registry_tools()}


@pytest.fixture(scope="module")
def parser() -> argparse.ArgumentParser:
    return build_parser()


# --- Schema helpers -------------------------------------------------------------------------


def _defs(schema: Mapping[str, Any]) -> Mapping[str, Any]:
    table = schema.get("$defs")
    return table if isinstance(table, Mapping) else {}


def _alternatives(spec: Mapping[str, Any], defs: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The non-null subschemas ``spec`` could be: its union members, or itself.

    ``$ref``s are resolved (:func:`gen._resolve_ref`) so an enum behind a ``#/$defs`` reference
    is seen as an enum rather than an opaque parameter (#1584).
    """
    resolved = gen._resolve_ref(dict(spec), defs)
    members = [
        gen._resolve_ref(member, defs)
        for key in ("oneOf", "anyOf", "allOf")
        for member in resolved.get(key, []) or []
        if isinstance(member, dict)
    ]
    non_null = [m for m in members if m.get("type") != "null"]
    return non_null or [resolved]


def _enum_of(spec: Mapping[str, Any], defs: Mapping[str, Any]) -> tuple[str, ...] | None:
    """The ``enum`` a parameter is drawn from, or ``None`` when it is not enumerated."""
    for candidate in _alternatives(spec, defs):
        values = candidate.get("enum")
        if isinstance(values, list) and values:
            return tuple(str(value) for value in values)
    return None


def _find_property(schema: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    """The subschema of the property ``name``, wherever it sits under ``schema``.

    A curated flag can feed a property nested inside a ``request`` wrapper
    (``systems list --state``) or inside a discriminated union branch
    (``accounting usage --project``), so the search descends through properties and union
    members rather than reading only the top level. Depth is bounded because the point is to
    find the parameter a flag names, not to walk arbitrarily deep response models.
    """
    defs = _defs(schema)

    def walk(node: Mapping[str, Any], depth: int) -> Iterator[dict[str, Any]]:
        if depth > 4:
            return
        for candidate in _alternatives(node, defs):
            props = candidate.get("properties")
            if not isinstance(props, Mapping):
                continue
            if name in props:
                yield dict(props[name])
            for child in props.values():
                if isinstance(child, Mapping):
                    yield from walk(child, depth + 1)

    return next(walk(schema, 0), None)


def _sample_value(spec: Mapping[str, Any] | None, defs: Mapping[str, Any]) -> str:
    """A command-line string the schema would accept for ``spec``.

    Enumerated and ``const`` parameters must get a member value or the payload is invalid for a
    reason that says nothing about the verb's shape; numeric parameters must parse. Everything
    else is an opaque identifier, which every remaining parameter type accepts.
    """
    if spec is None:
        return "x"
    for candidate in _alternatives(spec, defs):
        if "const" in candidate:
            return str(candidate["const"])
        values = candidate.get("enum")
        if isinstance(values, list) and values:
            return str(values[0])
        if candidate.get("type") == "integer":
            return "1"
        if candidate.get("type") == "number":
            return "1.5"
    return "x"


# --- Curated verbs: drive the real parser, validate every payload ---------------------------


def _flag(name: str) -> str:
    return f"--{name.replace('_', '-')}"


def _invocations(verb: Verb, schema: Mapping[str, Any]) -> list[list[str]]:
    """Every argv row to drive ``verb`` with, derived from its own descriptor.

    The base row is the minimal command line the parser accepts: the positionals, the required
    options, and every ``store_true`` flag (those gate the call — ``--force``, ``--expired`` —
    so an unset one produces no payload at all). On top of that, one row adding every optional
    option at once and one row per optional option alone, which is what separates "this option
    is misspelled" from "these two options are mutually exclusive".
    """
    defs = _defs(schema)

    def value(name: str) -> str:
        return _sample_value(_find_property(schema, name), defs)

    base = [verb.group, verb.sub]
    base += [value(name) for name in verb.positionals]
    base += [part for name in verb.required_options for part in (_flag(name), value(name))]
    base += [_flag(name) for name in verb.flags]

    def with_options(names: Sequence[str]) -> list[str]:
        extra = itertools.chain.from_iterable((_flag(n), value(n)) for n in names)
        return [*base, *extra]

    return [base, with_options(verb.options), *(with_options([n]) for n in verb.options)]


def _payloads_for(
    verb: Verb,
    schema: Mapping[str, Any],
    parser: argparse.ArgumentParser,
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[list[str], str, dict[str, Any]]]:
    """Run ``verb`` over each derived argv and return the ``(argv, tool, payload)`` it emitted.

    A row the verb refuses (a usage error, or a ``SystemExit`` from a missing break-glass
    acknowledgement) contributes nothing — that is the verb declining to call, not a mismatch.
    Output is swallowed because the handlers render to stdout on the way through.
    """
    captured: list[tuple[list[str], str, dict[str, Any]]] = []
    for argv in _invocations(verb, schema):
        client = _CapturingClient()
        monkeypatch.setattr(reads, "_session_factory", lambda c=client: _FakeSession(c))
        monkeypatch.setattr(mutations, "_session_factory", lambda c=client: _FakeSession(c))
        monkeypatch.setattr(mutations, "ensure_token_valid", lambda *a, **k: None)
        try:
            args = parser.parse_args(argv)
        except SystemExit:
            continue
        with contextlib.suppress(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(verb.handler(args))
        captured += [(argv, name, payload) for name, payload in client.calls]
    return captured


@pytest.mark.parametrize("verb", REGISTRY, ids=lambda v: f"{v.group}-{v.sub}")
def test_curated_verb_payload_matches_its_tool_schema(
    verb: Verb,
    tool_schemas: dict[str, dict[str, Any]],
    parser: argparse.ArgumentParser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every payload a curated verb can emit validates against its tool's live schema.

    This is the guard the epic was missing. Because the schemas forbid additional properties,
    one validation covers the whole class: a payload key the tool dropped, a flat payload sent
    to a ``request``-wrapped tool (#1589), a wrapped payload sent to a flattened one, and a
    ``null`` sent for a property the tool declares required.
    """
    schema = tool_schemas[verb.tool]
    validator = Draft202012Validator(schema)
    captured = _payloads_for(verb, schema, parser, monkeypatch)
    assert captured, f"{verb.group} {verb.sub}: no accepted command line reached the tool"
    for argv, tool, payload in captured:
        assert tool == verb.tool, f"{' '.join(argv)} called {tool!r}, not {verb.tool!r}"
        errors = [
            f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
            for error in validator.iter_errors(payload)
        ]
        assert not errors, (
            f"kdivectl {' '.join(argv)} -> {payload!r} violates {verb.tool}: {errors}"
        )


@pytest.mark.parametrize("verb", REGISTRY, ids=lambda v: f"{v.group}-{v.sub}")
def test_curated_verb_reaches_every_required_property(
    verb: Verb,
    tool_schemas: dict[str, dict[str, Any]],
    parser: argparse.ArgumentParser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some accepted command line supplies each property the tool declares required.

    A verb can be individually valid on every row and still be unusable if it has no way to
    send a required property at all — the ``fixtures list`` removal (#1590) left exactly that
    kind of hole behind.
    """
    schema = tool_schemas[verb.tool]
    reachable: set[str] = set()
    for _argv, _tool, payload in _payloads_for(verb, schema, parser, monkeypatch):
        reachable |= set(payload)
    missing = sorted(set(schema.get("required", [])) - reachable)
    assert not missing, f"{verb.group} {verb.sub}: no command line sends required {missing}"


@pytest.mark.parametrize("verb", REGISTRY, ids=lambda v: f"{v.group}-{v.sub}")
def test_curated_enum_parameter_declares_its_choices(
    verb: Verb, tool_schemas: dict[str, dict[str, Any]], parser: argparse.ArgumentParser
) -> None:
    """A curated parameter over an enumerated property offers exactly that enum as ``choices``.

    Without this a curated override silently drops the ``choices`` its generated counterpart
    derives, turning a typo into a server round-trip instead of a usage error — and leaving the
    completion tree and ``--help`` with no idea what the legal values are.
    """
    schema = tool_schemas[verb.tool]
    defs = _defs(schema)
    actions = {
        action.dest: action
        for action in _subparser_for(parser, verb.group, verb.sub)._actions  # noqa: SLF001
    }
    for name in (*verb.positionals, *verb.options, *verb.required_options):
        spec = _find_property(schema, name)
        if spec is None:
            continue
        enum = _enum_of(spec, defs)
        if enum is None:
            continue
        declared = actions[name].choices
        assert declared is not None and tuple(declared) == enum, (
            f"{verb.group} {verb.sub} {_flag(name)}: schema enum {enum} but argparse "
            f"choices {declared}"
        )


def _child_parser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    """The subparser ``parser`` registers under ``name``."""
    for action in parser._actions:  # noqa: SLF001
        if not isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            continue
        child = action.choices.get(name)
        if isinstance(child, argparse.ArgumentParser):
            return child
    raise AssertionError(f"no subparser named {name!r}")


def _subparser_for(
    parser: argparse.ArgumentParser, group: str, sub: str
) -> argparse.ArgumentParser:
    """The built sub-subparser at ``group sub``, read back out of the real parser tree."""
    return _child_parser(_child_parser(parser, group), sub)


# --- Generated verbs: the descriptor still fits the schema ----------------------------------


def _effective_shape(
    verb: GeneratedVerb, schema: Mapping[str, Any]
) -> tuple[Mapping[str, Any], set[str]]:
    """The properties and required names a generated verb's flags are derived from.

    For an ``unwrap_request`` verb that is the wrapper body, because the flags are the body's
    fields and dispatch re-wraps them; otherwise it is the schema's own top level.
    """
    props = schema.get("properties", {})
    if not verb.unwrap_request:
        return props, set(schema.get("required", []))
    body = gen._object_body(props["request"], _defs(schema)) or {}
    return body.get("properties", {}), set(body.get("required", []))


@pytest.mark.parametrize("verb", GENERATED_VERBS, ids=lambda v: v.tool)
def test_generated_verb_shape_matches_its_tool_schema(
    verb: GeneratedVerb, tool_schemas: dict[str, dict[str, Any]]
) -> None:
    """Every generated flag names a real property, and every required property has a flag.

    ``cli-verbs-check`` only proves the committed artifact equals a fresh generation, so a
    generator that derives the wrong shape stays green there; this asks the schema directly.
    """
    schema = tool_schemas[verb.tool]
    props, required = _effective_shape(verb, schema)
    dests = {flag.dest for flag in verb.flags} | set(verb.json_params)
    assert not dests - set(props), (
        f"{verb.tool}: flags/json params {sorted(dests - set(props))} are not tool properties"
    )
    assert not required - dests, (
        f"{verb.tool}: required {sorted(required - dests)} has no flag and no JSON escape"
    )
    optional_but_required = {flag.dest for flag in verb.flags if not flag.required} & required
    assert not optional_but_required, (
        f"{verb.tool}: {sorted(optional_but_required)} is required by the tool but optional "
        "on the command line, so an omission sends nothing"
    )


@pytest.mark.parametrize("verb", GENERATED_VERBS, ids=lambda v: v.tool)
def test_generated_verb_is_invokable(
    verb: GeneratedVerb, tool_schemas: dict[str, dict[str, Any]]
) -> None:
    """No generated verb is argument-less for a tool that requires arguments (#1588).

    Includes the ``request``-wrapper case: dispatch omits the ``request`` key entirely when no
    body flag was given, so a required ``request`` whose body has no required field would send
    an empty payload to a tool that demands one.
    """
    schema = tool_schemas[verb.tool]
    _props, required = _effective_shape(verb, schema)
    top_required = set(schema.get("required", []))
    if verb.flags or verb.json_params:
        if verb.unwrap_request and "request" in top_required:
            assert required, (
                f"{verb.tool}: 'request' is required but its body has no required field, so an "
                "invocation with no flags would send an empty payload"
            )
        return
    assert not (required or top_required), (
        f"{verb.tool}: requires {sorted(required or top_required)} but the verb has no flags "
        "and no JSON escape, so it cannot be invoked"
    )


@pytest.mark.parametrize("verb", GENERATED_VERBS, ids=lambda v: v.tool)
def test_generated_enum_parameter_declares_its_choices(
    verb: GeneratedVerb, tool_schemas: dict[str, dict[str, Any]]
) -> None:
    """An enumerated parameter derives ``choices``, and never degrades to the JSON escape.

    The ``$ref``-into-``$defs`` rendering of a ``StrEnum`` is the way this breaks: unresolved,
    the parameter looks typeless, loses its ``choices``, and falls through to
    ``--<param>-json``, which rejects the bare string the enum wants (#1584).
    """
    schema = tool_schemas[verb.tool]
    props, _required = _effective_shape(verb, schema)
    defs = _defs(schema)
    for flag in verb.flags:
        enum = _enum_of(props[flag.dest], defs)
        if enum is not None:
            assert flag.choices == enum, (
                f"{verb.tool} {flag.name}: schema enum {enum} but choices {flag.choices}"
            )
    for param in verb.json_params:
        assert _enum_of(props[param], defs) is None, (
            f"{verb.tool}: {param!r} is an enum but became a --{param}-json escape, which "
            "rejects the bare string the enum accepts"
        )
