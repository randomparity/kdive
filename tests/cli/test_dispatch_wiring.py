"""Descriptor-owned CLI verbs are wired into the parser and reachable through dispatch."""

from __future__ import annotations

import argparse
import ast
import asyncio
import inspect

import pytest

from kdive.cli import dispatch
from kdive.cli.__main__ import build_parser
from kdive.cli.commands._generated_verbs import GENERATED_VERBS, GeneratedVerb
from kdive.cli.commands.registry import HANDLER_OVERRIDES
from tests.ansi import strip_ansi
from tests.cli.verb_argv import required_argv_for_generated


def test_descriptor_verb_is_a_known_subcommand() -> None:
    args = build_parser().parse_args(["resources", "list"])
    assert args.command == "resources" and args.subcommand == "list"


def test_record_verb_takes_its_positional() -> None:
    args = build_parser().parse_args(["systems", "get", "sys-1"])
    assert args.genarg_system_id == "sys-1"


def _assert_operator_facing_help(help_text: str, verb: GeneratedVerb) -> None:
    """Rendered help for ``verb`` never leaks its internal dest prefix, and for
    ``images.extend`` uses the schema's field name and the flag's real metavar.

    ``help_text`` is stripped of ANSI colour before either assertion: argparse colours a
    flag and its metavar in different SGR spans, which splits ``"--reason REASON"`` apart
    without stripping first (#1891). The `not in` check is stripped too, on the same terms
    — colour can only make it spuriously pass, never spuriously fail, so leaving it
    unstripped would go quietly vacuous rather than red.
    """
    help_text = strip_ansi(help_text)
    assert "GENARG_" not in help_text, verb.tool
    if verb.tool == "images.extend":
        assert "image_id" in help_text
        assert "--reason REASON" in help_text


def test_descriptor_help_uses_operator_facing_metavars(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()
    for verb in GENERATED_VERBS:
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args([verb.group, verb.sub, "--help"])

        assert excinfo.value.code == 0
        _assert_operator_facing_help(capsys.readouterr().out, verb)


def test_operator_facing_help_survives_colour() -> None:
    """ANSI spans between a flag and metavar do not invalidate the help contract."""
    verb = next(verb for verb in GENERATED_VERBS if verb.tool == "images.extend")
    coloured_help = "image_id \x1b[1;36m--reason\x1b[0m \x1b[1;33mREASON\x1b[0m"

    _assert_operator_facing_help(coloured_help, verb)


def test_generated_positional_uses_descriptor_and_routes_to_tool_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A special renderer changes dispatch only; the generated descriptor owns its argv shape."""
    from kdive.cli.commands import registry
    from kdive.cli.commands._generated_verbs import GENERATED_VERBS

    verb = next(verb for verb in GENERATED_VERBS if verb.tool == "jobs.wait")
    assert verb.positionals == ("job_id",)
    assert "jobs.wait" in HANDLER_OVERRIDES

    seen: list[argparse.Namespace] = []

    async def _handler(args: argparse.Namespace) -> int:
        seen.append(args)
        return 0

    monkeypatch.setitem(registry.HANDLER_OVERRIDES, "jobs.wait", _handler)
    args = build_parser().parse_args(["jobs", "wait", "job-1"])
    assert args.genarg_job_id == "job-1"
    assert asyncio.run(registry.run_verb(args)) == 0
    assert seen == [args]


def test_specialized_handlers_use_only_the_generated_argument_boundary() -> None:
    """Descriptor fields cannot re-enter specialised handlers as namespace attributes."""
    from kdive.cli.commands import images, mutations, reads, registry

    descriptor_fields = {
        flag.dest
        for verb in GENERATED_VERBS
        if verb.tool in HANDLER_OVERRIDES
        for flag in verb.flags
    }
    for module in (reads, images, mutations):
        tree = ast.parse(inspect.getsource(module))
        direct_reads = [
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "args"
            and node.attr in descriptor_fields
        ]
        direct_reads += [
            node.args[1].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "args"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and node.args[1].value in descriptor_fields
        ]
        assert not direct_reads, f"{module.__name__}: {direct_reads}"
    assert "_adapt" + "_handler_args" not in vars(registry)


def test_list_verb_takes_its_optional_filter() -> None:
    args = build_parser().parse_args(["resources", "list", "--kind", "remote-libvirt"])
    assert args.genarg_kind == "remote-libvirt"


def test_optional_filter_defaults_to_none() -> None:
    args = build_parser().parse_args(["systems", "list"])
    assert args.genarg_state is None


def test_json_flag_accepted_after_the_verb() -> None:
    args = build_parser().parse_args(["resources", "list", "--json"])
    assert args.json is True


def test_json_flag_accepted_before_the_verb() -> None:
    args = build_parser().parse_args(["--json", "resources", "list"])
    assert args.json is True


def test_json_absent_after_verb_does_not_clobber_top_level() -> None:
    # The post-verb --json default is SUPPRESS, so omitting it leaves the top-level value.
    args = build_parser().parse_args(["--json", "resources", "list"])
    assert args.json is True
    args = build_parser().parse_args(["resources", "list"])
    assert args.json is False


def test_every_descriptor_verb_parses_through_the_built_parser() -> None:
    parser = build_parser()
    for verb in GENERATED_VERBS:
        args = parser.parse_args([verb.group, verb.sub, *required_argv_for_generated(verb)])
        assert args.command == verb.group and args.subcommand == verb.sub


def test_project_required_verb_rejects_a_missing_project() -> None:
    # The underlying tool's ``project`` is a required argument, so the CLI enforces it up
    # front (clean argparse usage error / exit 2) instead of a server-side missing-arg error.
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["allocations", "list"])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(("group", "sub"), [("allocations", "list"), ("accounting", "usage")])
def test_project_required_verb_accepts_an_explicit_project(group: str, sub: str) -> None:
    args = build_parser().parse_args([group, sub, "--project", "proj-a"])
    assert args.genarg_project == "proj-a"


def test_usage_verb_offers_both_targets() -> None:
    # accounting.usage discriminates on target.kind, so its descriptor must keep both doors
    # open — dropping --investigation-id would delete the investigation read path.
    args = build_parser().parse_args(["accounting", "usage", "--investigation-id", "inv-1"])
    assert args.genarg_investigation_id == "inv-1" and args.genarg_project is None


def test_inventory_project_filter_stays_optional() -> None:
    # ``inventory.list`` is a cross-project auditor read; ``--project`` is a narrowing
    # filter, not a requirement.
    args = build_parser().parse_args(["inventory", "list"])
    assert args.genarg_project is None


def test_secrets_list_has_no_project_flag() -> None:
    # ``secrets.list`` takes no project argument; the flag must not exist.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["secrets", "list", "--project", "proj-a"])


def test_images_list_carries_the_scope_flag() -> None:
    # The generator's presentation overlay selects ``scope`` for this path. It is the operator
    # replacement for the removed ``fixtures list`` verb (ADR-0465), so parse it from real argv.
    args = build_parser().parse_args(["images", "list", "--scope", "public_baseline"])
    assert args.genarg_scope == "public_baseline"
    assert build_parser().parse_args(["images", "list"]).genarg_scope is None


def test_dispatch_routes_descriptor_verb_to_run_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[argparse.Namespace] = []

    async def _fake_run_verb(args: argparse.Namespace) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(dispatch.commands, "run_verb", _fake_run_verb)
    args = build_parser().parse_args(["resources", "list"])
    assert asyncio.run(dispatch.run(args)) == 0
    assert seen and seen[0].command == "resources"


def test_dispatch_unknown_command_exits() -> None:
    args = argparse.Namespace(command="nope", subcommand=None)
    with pytest.raises(SystemExit):
        asyncio.run(dispatch.run(args))


def _route_spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    # Replace every leaf handler with a spy so a routing test observes which branch _dispatch
    # selected without running real session/transport machinery.
    called: dict[str, bool] = {}

    async def _tool_call(args: argparse.Namespace) -> int:
        called["tool_call"] = True
        return 0

    def _login(args: argparse.Namespace) -> int:
        called["login"] = True
        return 0

    async def _doctor(args: argparse.Namespace) -> int:
        called["doctor"] = True
        return 0

    async def _run_verb(args: argparse.Namespace) -> int:
        called["run_verb"] = True
        return 0

    monkeypatch.setattr(dispatch, "_tool_call", _tool_call)
    monkeypatch.setattr(dispatch, "_login", _login)
    monkeypatch.setattr(dispatch.commands.doctor, "doctor", _doctor)
    monkeypatch.setattr(dispatch.commands, "run_verb", _run_verb)
    return called


def test_dispatch_routes_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    called = _route_spies(monkeypatch)
    args = argparse.Namespace(command="tool", tool_command="call")
    assert asyncio.run(dispatch.run(args)) == 0
    assert called == {"tool_call": True}


def test_dispatch_does_not_route_tool_without_call_subcommand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `tool` with a non-`call` subcommand must fall through to run_verb, not the passthrough.
    called = _route_spies(monkeypatch)
    args = argparse.Namespace(command="tool", tool_command="list", subcommand=None)
    assert asyncio.run(dispatch.run(args)) == 0
    assert called == {"run_verb": True}


def test_dispatch_routes_login(monkeypatch: pytest.MonkeyPatch) -> None:
    called = _route_spies(monkeypatch)
    args = argparse.Namespace(command="login")
    assert asyncio.run(dispatch.run(args)) == 0
    assert called == {"login": True}


def test_dispatch_routes_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    called = _route_spies(monkeypatch)
    args = argparse.Namespace(command="doctor")
    assert asyncio.run(dispatch.run(args)) == 0
    assert called == {"doctor": True}


def test_report_parses_scope_window_and_group_by() -> None:
    args = build_parser().parse_args(
        [
            "accounting",
            "report",
            "--scope",
            "all-projects",
            "--group-by",
            "principal",
            "--since",
            "2026-01-01T00:00:00+00:00",
        ]
    )
    assert args.command == "accounting" and args.subcommand == "report"
    assert args.genarg_scope == "all-projects"
    assert args.genarg_group_by == "principal"
    assert args.genarg_since == "2026-01-01T00:00:00+00:00" and args.genarg_until is None


def test_report_parses_projects_flag() -> None:
    args = build_parser().parse_args(
        ["accounting", "report", "--scope", "granted-set", "--projects", "a,b"]
    )
    assert args.subcommand == "report" and args.genarg_projects == "a,b"


def test_report_requires_an_explicit_scope() -> None:
    # --scope is required: the CLI never picks a reporting scope on the caller's behalf.
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["accounting", "report"])
    assert excinfo.value.code == 2


def test_run_verb_routes_generic_handler_verb_to_the_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A path with no handler override resolves to its tool and uses generic dispatch.
    from kdive.cli.commands import registry

    seen: dict[str, str] = {}

    async def _fake_seam(verb: GeneratedVerb, args: argparse.Namespace) -> int:
        seen["tool"] = verb.tool
        return 0

    monkeypatch.setattr(dispatch, "invoke_generated_verb", _fake_seam)
    args = argparse.Namespace(command="accounting", subcommand="estimate")
    assert asyncio.run(registry.run_verb(args)) == 0
    assert seen["tool"] == "accounting.estimate"


def test_run_verb_unknown_generated_path_exits() -> None:
    from kdive.cli.commands import registry

    args = argparse.Namespace(command="accounting", subcommand="does-not-exist")
    with pytest.raises(SystemExit):
        asyncio.run(registry.run_verb(args))


@pytest.mark.parametrize(
    ("argv", "dest", "expected_id"),
    [
        (["jobs", "wait", "j-1", "--timeout-s", "0"], "job_id", "j-1"),
        (["allocations", "wait", "a-1", "--timeout-s", "0"], "allocation_id", "a-1"),
    ],
)
def test_documented_point_read_invocation_parses(
    argv: list[str], dest: str, expected_id: str
) -> None:
    """The point-read invocation shape `docs/operating/runbooks/kdivectl.md` documents parses.

    `jobs.get` / `allocations.get` were removed (ADR-0468) and the point read became
    `wait` with a zero timeout; ADR-0470 then made the generated descriptor present its id as a
    positional. The runbook line is hand-written markdown no generator checks, so this pins the
    exact documented invocation — the bare id, and `--timeout-s 0` still spelled out rather than
    defaulted — and keeping the runbook's wording in step with it is a review obligation.
    """
    args = build_parser().parse_args(argv)
    assert getattr(args, f"genarg_{dest}") == expected_id
    # The parser coerces it (ADR-0474); the runbook still spells the zero out rather than
    # relying on a default, which is the part this test pins.
    assert args.genarg_timeout_s == 0.0
    assert isinstance(args.genarg_timeout_s, float)


@pytest.mark.parametrize(
    "argv",
    [
        ["jobs", "wait", "--job-id", "j-1", "--timeout-s", "0"],
        ["allocations", "wait", "--allocation-id", "a-1", "--timeout-s", "0"],
    ],
)
def test_generated_wait_flag_form_is_gone(argv: list[str]) -> None:
    """`--job-id` / `--allocation-id` no longer parse (ADR-0470, breaking change).

    The generated descriptor owns one parser shape, so the flag form ADR-0468 §5 documented
    ceased to exist when its presentation metadata changed to a positional.
    """
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


@pytest.mark.parametrize(
    ("argv", "dest"),
    [(["jobs", "wait", "j-1"], "job_id"), (["allocations", "wait", "a-1"], "allocation_id")],
)
def test_wait_verb_omitted_timeout_stays_none(argv: list[str], dest: str) -> None:
    """An omitted `--timeout-s` parses to `None` so the handler can send no key at all.

    ADR-0470 decision 2: the CLI mirrors the tools' 30-second default by staying silent rather
    than restating it, so the namespace must not carry a CLI-chosen value here.
    """
    args = build_parser().parse_args(argv)
    assert getattr(args, f"genarg_{dest}") == argv[-1]
    assert args.genarg_timeout_s is None


@pytest.mark.parametrize("argv", [["jobs", "get", "j-1"], ["allocations", "get", "a-1"]])
def test_removed_getter_verbs_are_gone(argv: list[str]) -> None:
    """`kdivectl jobs get` / `allocations get` no longer exist (ADR-0468, breaking change)."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)
