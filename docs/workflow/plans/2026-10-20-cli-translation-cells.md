# CLI translation cells (#3099) — plan

Goal: implement [the spec](../specs/2026-10-20-cli-translation-cells-design.md) and
[ADR-0795](../../adr/0795-cli-translation-exposure.md). Python 3.14, uv, just; pytest.

Global Constraints: no production code, schema, migration or dependency change; `direct` and
`gateway` cells and behaviour unchanged; contract version stays 1; ADR-0795 flips to
`Accepted (<merge date>)` in Task 1; plain wording (no "critical", "robust", "comprehensive").
Guardrails: `just lint`, `just type`, focused pytest; the pre-push hook runs `just ci`.

Expected implementation size: 230–300 changed lines (M) — ~45 contract lines and 70 contract
test lines (Task 1), ~110 harness lines and ~90 harness test lines (Task 2), 10 carrier and runbook
lines (Task 3).

## Task 1 — `cli` exposure in the contract

Files: `scripts/coverage_campaign/contract.py`, `scripts/coverage_campaign/obligations.toml`,
`tests/scripts/test_coverage_contract.py`, `docs/adr/0795-cli-translation-exposure.md` (status),
`docs/adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md` (append one amendment
paragraph pointing at ADR-0795 for the third exposure).
Interfaces: produces `Cell.exposure == "cli"` cells, owner 3099, sharing the `direct` twin's
`scenario_id`/`node_id`; `OperationGroup.cli: bool = False`.

Verification:
- focused-test — twin contract: new `test_cli_cells_twin_the_core_direct_cells`: for every cell
  with exposure `cli`, the cell with the same id but `/direct/` exists and equals it except `id`,
  `exposure` and `owner` (3099); the `cli` operations equal the five groups' tools; the count is
  454; no other group has `cli` cells. Red: no `cli` cells (count 0). Green:
  `uv run pytest tests/scripts/test_coverage_contract.py -k cli -q` → passed.
- focused-test — validators: `test_cli_exposure_validators_fail_closed`: a mapping whose provider
  group (owner 3112) sets `cli=True` raises `ValueError` matching `service tools only`;
  monkeypatching `contract.GENERATED_VERBS` to drop `session.whoami` raises matching
  `no generated kdivectl verb`. Red: no error. Green: same command.
- task-test-not-applicable — the two ADR status/amendment edits: prose with no executable
  consumer beyond `just records`, which runs in `just ci`.

Steps: write both tests and see them fail; then in `contract.py` add
`from kdive.cli.commands._generated_verbs import GENERATED_VERBS`, the field
`cli: bool = False` on `OperationGroup`, and below `_ADMISSION_REFUSALS`:

```python
# #3099 owns the kdivectl translation cells of the core service tools (ADR-0795).
_CLI_OWNER = 3099
```

In `_validate_mapping`, inside the `for group in mapping.groups:` loop:

```python
        if group.cli and group.execution != "service":
            raise ValueError("cli exposure covers service tools only; remove cli from the group")
        if group.cli and set(group.tools) - {verb.tool for verb in GENERATED_VERBS}:
            raise ValueError("cli tool has no generated kdivectl verb; run just cli-verbs")
```

In `_tool_cells`, replace `for exposure in ("direct", "gateway"):` with
`for exposure in ("direct", "gateway", "cli") if group.cli else ("direct", "gateway"):` and, after
the owner `elif` chain, add `if exposure == "cli": owner = _CLI_OWNER` (two lines). In
`obligations.toml` add `cli = true` after `execution = "service"` in the groups with
`owner = 2811`, `3095`, `3096`, `3097` and `3098`. Fix any existing test that counts exposures
(run the whole file: `uv run pytest tests/scripts/test_coverage_contract.py -q` → all passed).
Commit `test(coverage): account CLI translation cells for the core tools`.

## Task 2 — `CliCaller` in the shared harness

Files: `tests/integration/live_stack/tool_cells.py`, `tests/integration/live_stack/test_tool_cells.py`.
Interfaces (consumed by Task 3): `caller_for(exposure: str, base_url: str, issuer: OidcIssuer) ->
HttpCaller`; `CliCaller(HttpCaller)`; `cli_argv(tool: str, args: Mapping[str, object]) -> list[str]`;
`async run_kdivectl(argv: list[str], *, token: str, base_url: str) -> CliRun`;
`CliUsageError(LiveStackToolError)`. Borrowed and confirmed: `kdive.cli.__main__.build_parser()`,
`kdive.cli.commands.generated_args.assemble_generated_payload(verb, args)`,
`kdive.cli.commands.registry.HANDLER_OVERRIDES`, `kdive.cli.commands._generated_verbs.GENERATED_VERBS`,
`kdive.cli.reserved_flags.derive_cli_flag(param)`,
`kdive.cli.commands.verb_spec.GeneratedFlag`/`GeneratedVerb`, `LiveStackToolError(tool, message)`.

Verification (all: `uv run pytest tests/integration/live_stack/test_tool_cells.py -k cli -q`):
- focused-test — translation: `test_cli_argv_follows_the_descriptor` asserts exact argv for
  `investigations.list` `{"request": {"project": "p", "limit": 5}}` →
  `["investigations","list","--project","p","--limit","5","--json"]`; `jobs.wait`
  `{"job_id": "j","timeout_s": 0}` → positional `j` first; `investigations.link`
  `{"investigation_id":"i","ref":{"k":1}}` → `--ref-json '{"k": 1}'`; `tools.search`
  `{"names":["a","b"]}` → `--names a --names b`; `tools.invoke` ends `--yes --json`;
  `accounting.usage` `{"target":{"kind":"project","project":"p"}}` → `--project p`;
  `accounting.report` `{"request":{"scope":"granted-set","projects":["a","b"],"window":["s",None]}}`
  → `--scope granted-set --projects a,b --since s`; unknown `{"bogus": 1}` → `--bogus 1`.
  Red: `AttributeError` (no `cli_argv`).
- focused-test — exactness: `test_cli_exactness_names_the_dropped_field`: `investigations.close`
  with `force="yes"` raises `AssertionError` naming `force`; an override verb is not checked.
- focused-test — results: with `run_kdivectl` monkeypatched, `CliCaller.call` returns a
  `ToolResponse` for exit 0 + envelope; a list for `{"result": [...]}`; raises `AssertionError`
  for exit 1 + `authorization_denied` envelope; `CliUsageError` for exit 2 and no stdout; a
  `LiveStackToolError` for exit 1 + `error: boom`; `rejected_by_validation("cli", CliUsageError(..))`
  is `True`, and `rejected_by_validation("gateway", ...)` stays `False`.
- focused-test — authentication: `prove_rejection` with a `CliCaller` and boundary
  `authentication` records `{"exit": 1, "diagnostic": "authentication-failed", ...}` when the fake
  runner answers the forged token with the diagnostic, and fails when the issued token also does.
- focused-test — selection: `test_caller_for_selects_the_exposure`: `caller_for("cli", …)` is a
  `CliCaller` whose `token` carries the `kdivectl` client id; `"direct"`/`"gateway"` give a plain
  `HttpCaller`.
- focused-test — real entry point: `test_run_kdivectl_drives_the_entry_point` runs
  `["session","whoami","--json"]` against `http://127.0.0.1:9` and gets exit 1 with
  `error: connection failed` on stderr.

Steps: write the tests, see them fail; then add to `tool_cells.py` (imports `os`, `sys`, `io`,
`contextlib.redirect_stderr` and the borrowed names above); widen
`Exposure = Literal["direct", "gateway", "cli"]`; in `HttpCaller.token` use
`if self.exposure != "gateway"` for the client id; in `rejected_by_validation` accept
`CliUsageError` only for `cli`, and the tool-error and envelope branches for `exposure in
("direct", "cli")`; in `_observe`'s authentication branch, first
`if isinstance(caller, CliCaller): return await caller.authenticate(tool, rejection.args, genuine)`.
Add after `HttpCaller`:

```python
_EXIT_CODES = {  # ADR-0089: the CLI's exit code per error category; any other category is 1.
    "configuration_error": 2,
    "authorization_denied": 3,
    "not_found": 4,
    "conflict": 5,
    "capacity_exhausted": 6,
}
_AUTH_FAILED = "error: authentication failed"
_CLI_TIMEOUT_S = 900.0
_VERBS = {verb.tool: verb for verb in GENERATED_VERBS}


class CliUsageError(LiveStackToolError):
    """kdivectl refused the command line itself (exit 2, no envelope): no call was made."""


@dataclass(frozen=True)
class CliRun:
    code: int
    stdout: str
    stderr: str


async def run_kdivectl(argv: list[str], *, token: str, base_url: str) -> CliRun:
    """Run the real ``kdivectl`` entry point as a subprocess, stdin closed."""
    env = {**os.environ, "KDIVE_TOKEN": token, "KDIVE_SERVER_URL": base_url}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kdive.cli",
        *argv,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), _CLI_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise AssertionError(f"kdivectl {argv[0]} {argv[1]} ran past {_CLI_TIMEOUT_S}s") from None
    assert proc.returncode is not None
    return CliRun(proc.returncode, out.decode(), err.decode(errors="replace"))


def _auth_failed(done: CliRun) -> bool:
    return any(line.startswith(_AUTH_FAILED) for line in done.stderr.splitlines())


def _text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _usage_body(args: Mapping[str, object]) -> dict[str, object]:
    target = args.get("target")
    if set(args) == {"target"} and isinstance(target, Mapping):
        return {str(k): v for k, v in target.items() if k != "kind"}
    return dict(args)


def _report_body(args: Mapping[str, object]) -> dict[str, object]:
    request = args.get("request")
    if set(args) != {"request"} or not isinstance(request, Mapping):
        return dict(args)
    body = {str(k): v for k, v in request.items()}
    if isinstance(projects := body.get("projects"), list):
        body["projects"] = ",".join(str(p) for p in projects)
    if isinstance(window := body.get("window"), list) and len(window) == 2:
        del body["window"]
        body["since"], body["until"] = window
    return body


# The handlers of these verbs reshape the payload, so their command line is not the payload's.
_INVERSES = {"accounting.usage": _usage_body, "accounting.report": _report_body}


def _body(verb: GeneratedVerb, args: Mapping[str, object]) -> dict[str, object]:
    request = args.get("request")
    if verb.unwrap_request and set(args) == {"request"} and isinstance(request, Mapping):
        return {str(k): v for k, v in request.items()}
    return dict(args)


def _flag(flag: GeneratedFlag | None, key: str, value: object, *, as_json: bool) -> list[str]:
    if value is None:
        return []
    if as_json:
        return [f"{derive_cli_flag(key)}-json", json.dumps(value)]
    name, action = (flag.name, flag.action) if flag else (derive_cli_flag(key), None)
    if action == "store_true":
        return [name] if value is True else []
    if action == "bool_optional":
        return [name if value else "--no-" + name.removeprefix("--")]
    if action == "append" and isinstance(value, list):
        return [token for item in value for token in (name, _text(item))]
    return [name, _text(value)]


def cli_argv(tool: str, args: Mapping[str, object]) -> list[str]:
    """The ``kdivectl`` command line that makes ``tool``'s call with ``args`` (ADR-0795)."""
    verb = _VERBS[tool]
    inverse = _INVERSES.get(tool)
    body = inverse(args) if inverse else _body(verb, args)
    flags = {flag.dest: flag for flag in verb.flags}
    argv = [verb.group, verb.sub]
    argv += [_text(body.pop(name)) for name in verb.positionals if body.get(name) is not None]
    for key, value in body.items():
        argv += _flag(flags.get(key), key, value, as_json=key in verb.json_params)
    return [*argv, *(["--yes"] if verb.confirm_destructive else []), "--json"]


def _check_exact(tool: str, argv: list[str], args: Mapping[str, object]) -> None:
    """A parsed command line of a non-override verb reassembles exactly ``args``."""
    verb = _VERBS[tool]
    if tool in HANDLER_OVERRIDES:
        return
    try:
        with redirect_stderr(io.StringIO()):
            namespace = build_parser().parse_args(argv)
    except SystemExit:
        return  # The subprocess shows the same refusal as a usage error.
    dropped = {f.dest for f in verb.flags if f.action == "store_true"}
    body = {
        k: v
        for k, v in _body(verb, args).items()
        if v is not None and not (k in dropped and v is False)
    }
    expected = ({"request": body} if body else {}) if verb.unwrap_request else body
    got = assemble_generated_payload(verb, namespace)
    differs = sorted(k for k in expected.keys() | got.keys() if expected.get(k) != got.get(k))
    assert not differs, f"kdivectl {verb.group} {verb.sub} cannot carry fields {differs}"


def _result(tool: str, argv: list[str], done: CliRun) -> ToolResponse | list[ToolResponse]:
    if done.stdout.strip().startswith("{"):
        payload = json.loads(done.stdout)
        category = payload.get("error_category")
        expected = 0 if category is None else _EXIT_CODES.get(category, 1)
        assert done.code == expected, (
            f"kdivectl {argv[0]} {argv[1]} exited {done.code} for {category!r}, not {expected}"
        )
        inner = payload.get("result")
        if list(payload) == ["result"] and isinstance(inner, list):
            return [ToolResponse.model_validate(item) for item in inner]
        return ToolResponse.model_validate(payload)
    if done.code == 2:
        raise CliUsageError(tool, "kdivectl refused the command line")
    errors = [line for line in done.stderr.splitlines() if line.startswith("error: ")]
    if done.code == 1 and errors:
        raise LiveStackToolError(tool, errors[0].removeprefix("error: "))
    raise AssertionError(f"kdivectl {argv[0]} {argv[1]} exited {done.code} with no envelope")


@dataclass(frozen=True)
class CliCaller(HttpCaller):
    """Reach a tool through its generated ``kdivectl`` verb (ADR-0795)."""

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse | list[ToolResponse]:
        argv = cli_argv(tool, args)
        _check_exact(tool, argv, args)
        return _result(tool, argv, await run_kdivectl(argv, token=token, base_url=self.base_url))

    async def authenticate(
        self, tool: str, args: Mapping[str, object], genuine: str
    ) -> dict[str, object]:
        argv = cli_argv(tool, args)
        forged = await run_kdivectl(argv, token=forge(genuine), base_url=self.base_url)
        assert forged.code == 1 and _auth_failed(forged), (
            f"forged-token kdivectl {argv[0]} {argv[1]} exited {forged.code} without {_AUTH_FAILED!r}"
        )
        control = await run_kdivectl(argv, token=genuine, base_url=self.base_url)
        assert not _auth_failed(control), f"the issued token's kdivectl {argv[1]} also failed"
        return {
            "exit": 1,
            "diagnostic": "authentication-failed",
            "token": "foreign-signature",
            "issued_token_exit": control.code,
        }


def caller_for(exposure: str, base_url: str, issuer: OidcIssuer) -> HttpCaller:
    """The caller of a cell's exposure: ``cli`` drives kdivectl, the others call over HTTP."""
    if exposure == "cli":
        return CliCaller("cli", base_url, issuer)
    return HttpCaller(cast(Exposure, exposure), base_url, issuer)
```

Run `just format`, the focused tests (→ passed), the whole file
(`uv run pytest tests/integration/live_stack/test_tool_cells.py -q` → passed) and `just type`.
Commit `test(live-stack): drive core tool cells through kdivectl`.

## Task 3 — select the caller in the core carriers

Files: `tests/integration/test_core_tool_cells_live.py:203`,
`tests/integration/test_catalog_tool_cells_live.py:564`,
`tests/integration/test_investigation_tool_cells_live.py:748`,
`tests/integration/live_stack/run_system_job_cells.py:763`,
`tests/integration/live_stack/allocation_accounting_cells.py:1001`,
`docs/operating/runbooks/live-testing.md:1135`.
Interfaces: consumes `caller_for` from Task 2.

Verification: task-test-not-applicable — each edit swaps
`HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)` for
`caller_for(cell.exposure, base_url, issuer)` in a live-only entry point, whose selection Task 2's
`caller_for` test already covers; `just type` and `just lint` confirm the call sites and imports
(drop `Exposure`/`cast` where unused). The runbook sentence gains "or `cli` for the core service
tools (ADR-0795)". Run `uv run pytest tests/integration/live_stack -q` → passed (no stack:
live cells skip). Commit `test(live-stack): run the core carriers' cli cells through kdivectl`.

## Live proof (after an operator grant)

Lane: one exclusive x86_64 demo-up lab host with the provider authority installed, snapshot taken
first. Run the five carriers with `-k "/cli/"` in the `default` and then the `recovery`
configuration; assemble one evidence root; qualify only the `cli` cells. Cleanup: each cell's
cleanup proof, then roll the host back to the snapshot. Report pass/fail per cell; failing
translation cells are reported to the campaign root, not fixed here.
