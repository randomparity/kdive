"""The curated read verbs are wired into the parser and reachable through ``dispatch.run``."""

from __future__ import annotations

import argparse
import asyncio

import pytest

from kdive.cli import dispatch
from kdive.cli.__main__ import build_parser
from kdive.cli.commands.registry import REGISTRY, _curated_flags


def test_curated_verb_is_a_known_subcommand() -> None:
    args = build_parser().parse_args(["resources", "list"])
    assert args.command == "resources" and args.subcommand == "list"


def test_record_verb_takes_its_positional() -> None:
    args = build_parser().parse_args(["systems", "get", "sys-1"])
    assert args.system_id == "sys-1"


def test_list_verb_takes_its_optional_filter() -> None:
    args = build_parser().parse_args(["resources", "list", "--kind", "remote-libvirt"])
    assert args.kind == "remote-libvirt"


def test_optional_filter_defaults_to_none() -> None:
    args = build_parser().parse_args(["systems", "list"])
    assert args.state is None


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


def test_every_registry_verb_parses_through_the_built_parser() -> None:
    parser = build_parser()
    for verb in REGISTRY:

        def placeholder(name: str, verb=verb) -> str:
            # Curated parameters take their type and enum from the generated verb at the same
            # path (ADR-0469, ADR-0474), so a bare "<name>-val" no longer parses for a numeric
            # or enumerated one.
            flag = _curated_flags(verb).get(name)
            if flag is None:
                return f"{name}-val"
            if flag.choices:
                return flag.choices[0]
            return "1" if flag.arg_type in {"int", "float"} else f"{name}-val"

        argv = [verb.group, verb.sub, *(placeholder(p) for p in verb.positionals)]
        for option in verb.required_options:
            argv += [f"--{option.replace('_', '-')}", placeholder(option)]
        args = parser.parse_args(argv)
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
    assert args.project == "proj-a"


def test_usage_verb_offers_both_targets() -> None:
    # accounting.usage discriminates on target.kind, so the curated verb must keep both
    # doors open — dropping --investigation-id would delete the investigation read path.
    args = build_parser().parse_args(["accounting", "usage", "--investigation-id", "inv-1"])
    assert args.investigation_id == "inv-1" and args.project is None


def test_inventory_project_filter_stays_optional() -> None:
    # ``inventory.list`` is a cross-project auditor read; ``--project`` is a narrowing
    # filter, not a requirement.
    args = build_parser().parse_args(["inventory", "list"])
    assert args.project is None


def test_secrets_list_has_no_project_flag() -> None:
    # ``secrets.list`` takes no project argument; the flag must not exist.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["secrets", "list", "--project", "proj-a"])


def test_images_list_carries_the_scope_flag() -> None:
    # ``images list`` is CURATED, so it overrides the generated shape at its path — the scope
    # flag exists only because the curated verb declares it. It is the operator replacement for
    # the removed ``fixtures list`` verb (ADR-0465), so parse it from real argv.
    args = build_parser().parse_args(["images", "list", "--scope", "public_baseline"])
    assert args.scope == "public_baseline"
    assert build_parser().parse_args(["images", "list"]).scope is None


def test_dispatch_routes_curated_verb_to_run_verb(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert args.scope == "all-projects"
    assert args.group_by == "principal"
    assert args.since == "2026-01-01T00:00:00+00:00" and args.until is None


def test_report_parses_projects_flag() -> None:
    args = build_parser().parse_args(
        ["accounting", "report", "--scope", "granted-set", "--projects", "a,b"]
    )
    assert args.subcommand == "report" and args.projects == "a,b"


def test_report_requires_an_explicit_scope() -> None:
    # --scope is required: the CLI never picks a reporting scope on the caller's behalf.
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["accounting", "report"])
    assert excinfo.value.code == 2


def test_run_verb_routes_generated_only_verb_to_the_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-curated generated path resolves to its tool and routes through the passthrough seam.
    from kdive.cli.commands import registry
    from kdive.cli.commands.verb_spec import GeneratedVerb

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
    `wait` with a zero timeout; ADR-0470 then gave it back its positional id with a curated
    verb. The runbook line is hand-written markdown no generator checks, so this pins the exact
    documented invocation — the bare id, and `--timeout-s 0` still spelled out rather than
    defaulted — and keeping the runbook's wording in step with it is a review obligation.
    """
    args = build_parser().parse_args(argv)
    assert getattr(args, dest) == expected_id
    # The parser coerces it (ADR-0474); the runbook still spells the zero out rather than
    # relying on a default, which is the part this test pins.
    assert args.timeout_s == 0.0
    assert isinstance(args.timeout_s, float)
    assert not [k for k in vars(args) if k.startswith("genarg")]


@pytest.mark.parametrize(
    "argv",
    [
        ["jobs", "wait", "--job-id", "j-1", "--timeout-s", "0"],
        ["allocations", "wait", "--allocation-id", "a-1", "--timeout-s", "0"],
    ],
)
def test_generated_wait_flag_form_is_gone(argv: list[str]) -> None:
    """`--job-id` / `--allocation-id` no longer parse (ADR-0470, breaking change).

    A curated `Verb` *replaces* the generated parser at its path rather than adding to it, so
    the flag form ADR-0468 §5 documented ceased to exist when the positional form landed.
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
    assert getattr(args, dest) == argv[-1]
    assert args.timeout_s is None


@pytest.mark.parametrize("argv", [["jobs", "get", "j-1"], ["allocations", "get", "a-1"]])
def test_removed_getter_verbs_are_gone(argv: list[str]) -> None:
    """`kdivectl jobs get` / `allocations get` no longer exist (ADR-0468, breaking change)."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)
