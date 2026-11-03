# CLI translation cells (#3099) — plan

Goal: implement [the spec](../specs/2026-10-20-cli-translation-cells-design.md) and
[ADR-0795](../../adr/0795-cli-translation-exposure.md). Python 3.14, uv, just; pytest.

Global Constraints: no production code, schema, migration or dependency change; `direct` and
`gateway` cells and behaviour unchanged; contract version stays 1; ADR-0795 flips to
`Accepted (<merge date>)` in Task 3; plain wording (no "critical", "robust", "comprehensive").
Guardrails: `just lint`, `just type`, focused pytest; the pre-push hook runs `just ci`.
Order: the caller (Task 1) and carriers (Task 2) land before the contract creates `cli` cells
(Task 3), so no commit has `cli` cells a carrier would run over HTTP.

Expected implementation size: 340–400 changed lines (M) — ~200 harness lines and ~100 harness
test lines (Task 1), ~10 carrier and runbook lines (Task 2), ~20 contract lines and ~60 contract
test lines (Task 3).

## Task 1 — `CliCaller` in the shared harness

Files: `tests/integration/live_stack/tool_cells.py`,
`tests/integration/live_stack/test_tool_cells.py`.
Interfaces (consumed by Task 2): `caller_for(exposure: str, base_url: str, issuer: OidcIssuer) ->
HttpCaller`; `http_caller(caller: HttpCaller) -> HttpCaller`; `CliCaller(HttpCaller)`;
`cli_argv(tool: str, args: Mapping[str, object]) -> list[str]`;
`async run_kdivectl(argv: list[str], *, token: str, base_url: str) -> CliRun`; `CliUsageError`,
`CliTierRefusal` (both `LiveStackToolError`), `CliTranslationGap(AssertionError)`.
Borrowed and confirmed: `kdive.cli.__main__.build_parser()`,
`kdive.cli.commands.generated_args.assemble_generated_payload(verb, args)`,
`kdive.cli.commands._generated_verbs.GENERATED_VERBS`, `kdive.cli.reserved_flags.derive_cli_flag`,
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
  → `--scope granted-set --projects a,b --since s`; `resources.availability`
  `{"request": {"include_devices": "maybe"}}` → `--include-devices maybe`. Red: no `cli_argv`.
- focused-test — gaps: `allocations.list` `{"request": {"limit": "many"}}` raises
  `CliTranslationGap` naming `limit` (the verb has no such flag).
- focused-test — results: with `run_kdivectl` monkeypatched, `CliCaller.call` returns a
  `ToolResponse` for exit 0 + envelope and a list for `{"result": [...]}`; raises `AssertionError`
  for exit 1 + `authorization_denied` envelope; `CliUsageError` whose message names `--kind` for
  exit 2 + stderr `kdivectl: error: unrecognized arguments: --kind x`; `CliTierRefusal` for exit 3
  + stdout `'x' is not positively classified …`; `LiveStackToolError` for exit 1 + `error: boom`.
  `rejected_by_validation("cli", CliUsageError(...))` is `True`; for `"gateway"` it stays `False`.
- focused-test — denial: `prove_rejection` on an `authorization` cell with a `CliCaller` whose
  runner answers `CliTierRefusal` (and `_catalog` monkeypatched to omit the tool) records
  `rejected` `{"tool_error": True, ...}` with category `authorization_denied`; with `_catalog`
  listing the tool the call raises `AssertionError` naming it.
- focused-test — authentication: `prove_rejection` with a `CliCaller` and boundary
  `authentication` records `{"exit": 1, "diagnostic": "authentication-failed", ...}` when the fake
  runner answers the forged token with the diagnostic; it fails when the issued token also does,
  and raises `CliTranslationGap` when the forged run exits 2.
- focused-test — selection: `caller_for("cli", …)` is a `CliCaller` whose `token` carries the
  `kdivectl` client id; `http_caller` of it is a `direct` `HttpCaller`; `HttpCaller("cli", …)`
  raises `AssertionError`.
- focused-test — real entry point: `run_kdivectl(["session","whoami","--json"], …)` against
  `http://127.0.0.1:9` exits 1 with `error: connection failed` on stderr.

Steps: write the tests, see them fail; then edit `tool_cells.py` (imports `os`, `re`, `sys`, `io`,
`contextlib.redirect_stderr` and the borrowed names): widen
`Exposure = Literal["direct", "gateway", "cli"]`; give `HttpCaller` a
`__post_init__` asserting `(self.exposure == "cli") == isinstance(self, CliCaller)`; in
`HttpCaller.token` use `if self.exposure != "gateway"` for the client id; in
`rejected_by_validation` return `exposure == "cli"` first for a `CliUsageError`, and widen the
tool-error and envelope branches to `exposure in ("direct", "cli")`; in `_observe`'s
authentication branch, first `if isinstance(caller, CliCaller): return await
caller.authenticate(tool, rejection.args, genuine)`; in `_observe`'s envelope branch read
`category = "authorization_denied" if isinstance(result, CliTierRefusal) else (result.error_category
if isinstance(result, ToolResponse) else None)`. Add after `HttpCaller`:

```python
_EXIT_CODES = {  # ADR-0089: the CLI's exit code per error category; any other category is 1.
    "configuration_error": 2,
    "authorization_denied": 3,
    "not_found": 4,
    "conflict": 5,
    "capacity_exhausted": 6,
}
_AUTH_FAILED = "error: authentication failed"
_TIER_REFUSED = "is not positively classified"
_CLI_TIMEOUT_S = 900.0
_VERBS = {verb.tool: verb for verb in GENERATED_VERBS}


class CliUsageError(LiveStackToolError):
    """kdivectl refused the command line itself (exit 2, no envelope): no call was made."""


class CliTierRefusal(LiveStackToolError):
    """kdivectl refused a verb whose tool the caller's catalog hides (exit 3, no call)."""


class CliTranslationGap(AssertionError):
    """The call cannot be expressed as this tool's kdivectl command line."""


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


def _refused_flags(done: CliRun) -> str:
    """The option names argparse's last diagnostic line cites; never their values."""
    last = done.stderr.strip().splitlines()[-1:] or [""]
    return " ".join(re.findall(r"--[a-z0-9-]+", last[0])) or "no option named"


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


# These handlers reshape the payload, so their command line is not the payload's.
_INVERSES = {"accounting.usage": _usage_body, "accounting.report": _report_body}


def _body(verb: GeneratedVerb, args: Mapping[str, object]) -> dict[str, object]:
    request = args.get("request")
    if verb.unwrap_request and set(args) == {"request"} and isinstance(request, Mapping):
        return {str(k): v for k, v in request.items()}
    return dict(args)


def _flag(verb: GeneratedVerb, key: str, value: object) -> list[str]:
    if value is None:
        return []
    if key in verb.json_params:
        return [f"{derive_cli_flag(key)}-json", json.dumps(value)]
    flag = next((f for f in verb.flags if f.dest == key), None)
    if flag is None:
        raise CliTranslationGap(f"kdivectl {verb.group} {verb.sub} has no option for {key!r}")
    if flag.action == "store_true" and isinstance(value, bool):
        return [flag.name] if value else []
    if flag.action == "bool_optional" and isinstance(value, bool):
        return [flag.name if value else "--no-" + flag.name.removeprefix("--")]
    if flag.action == "append" and isinstance(value, list):
        return [token for item in value for token in (flag.name, _text(item))]
    return [flag.name, _text(value)]


def cli_argv(tool: str, args: Mapping[str, object]) -> list[str]:
    """The ``kdivectl`` command line that makes ``tool``'s call with ``args`` (ADR-0795)."""
    verb = _VERBS[tool]
    inverse = _INVERSES.get(tool)
    body = inverse(args) if inverse else _body(verb, args)
    argv = [verb.group, verb.sub]
    argv += [_text(body.pop(name)) for name in verb.positionals if body.get(name) is not None]
    for key, value in body.items():
        argv += _flag(verb, key, value)
    return [*argv, *(["--yes"] if verb.confirm_destructive else []), "--json"]


def _check_exact(tool: str, argv: list[str], args: Mapping[str, object]) -> None:
    """A parsed command line reassembles exactly ``args`` (the two reshaping verbs excepted)."""
    verb = _VERBS[tool]
    if tool in _INVERSES:
        return
    try:
        with redirect_stderr(io.StringIO()):
            namespace = build_parser().parse_args(argv)
    except SystemExit:
        return  # The subprocess reports the same refusal as a usage error.
    bools = {f.dest for f in verb.flags if f.action == "store_true"}
    body = {
        k: v
        for k, v in _body(verb, args).items()
        if v is not None and not (k in bools and v is False)
    }
    expected = ({"request": body} if body else {}) if verb.unwrap_request else body
    got = assemble_generated_payload(verb, namespace)
    differs = sorted(k for k in expected.keys() | got.keys() if expected.get(k) != got.get(k))
    if differs:
        raise CliTranslationGap(f"kdivectl {verb.group} {verb.sub} cannot carry {differs}")


def _result(tool: str, argv: list[str], done: CliRun) -> ToolResponse | list[ToolResponse]:
    verb = f"kdivectl {argv[0]} {argv[1]}"
    if done.stdout.strip().startswith("{"):
        payload = json.loads(done.stdout)
        category = payload.get("error_category")
        expected = 0 if category is None else _EXIT_CODES.get(category, 1)
        assert done.code == expected, f"{verb} exited {done.code} for {category!r}, not {expected}"
        inner = payload.get("result")
        if list(payload) == ["result"] and isinstance(inner, list):
            return [ToolResponse.model_validate(item) for item in inner]
        return ToolResponse.model_validate(payload)
    if done.code == 2:
        raise CliUsageError(tool, f"{verb} refused the command line: {_refused_flags(done)}")
    if done.code == 3 and _TIER_REFUSED in done.stdout:
        raise CliTierRefusal(tool, f"{verb}: the caller's catalog hides {tool}")
    errors = [line for line in done.stderr.splitlines() if line.startswith("error: ")]
    if done.code == 1 and errors:
        raise LiveStackToolError(tool, errors[0].removeprefix("error: "))
    first = done.stdout.strip().splitlines()[:1]
    raise AssertionError(f"{verb} exited {done.code} with no envelope: {first}")


@dataclass(frozen=True)
class CliCaller(HttpCaller):
    """Reach a tool through its generated ``kdivectl`` verb (ADR-0795)."""

    async def call(
        self, tool: str, args: Mapping[str, object], token: str, *, discover: bool = False
    ) -> ToolResponse | list[ToolResponse]:
        argv = cli_argv(tool, args)
        _check_exact(tool, argv, args)
        done = await run_kdivectl(argv, token=token, base_url=self.base_url)
        try:
            return _result(tool, argv, done)
        except CliTierRefusal:
            # UNKNOWN also answers a listed tool with bad annotations; only a hidden one is denied.
            assert tool not in await _catalog(self.base_url, token), (
                f"kdivectl refused listed {tool}"
            )
            raise

    async def authenticate(
        self, tool: str, args: Mapping[str, object], genuine: str
    ) -> dict[str, object]:
        argv = cli_argv(tool, args)
        forged = await run_kdivectl(argv, token=forge(genuine), base_url=self.base_url)
        if forged.code == 2:
            raise CliTranslationGap(f"kdivectl {argv[0]} {argv[1]}: {_refused_flags(forged)}")
        assert forged.code == 1 and _auth_failed(forged), (
            f"forged-token kdivectl {argv[0]} {argv[1]} exited {forged.code} without the diagnostic"
        )
        control = await run_kdivectl(argv, token=genuine, base_url=self.base_url)
        assert not _auth_failed(control), f"the issued token's kdivectl {argv[1]} also failed"
        return {
            "exit": 1,
            "diagnostic": "authentication-failed",
            "token": "foreign-signature",
            "issued_token_exit": control.code,
        }


async def _catalog(base_url: str, token: str) -> set[str]:
    transport = StreamableHttpTransport(url=base_url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport) as client:
        return {tool.name for tool in await client.list_tools()}


def caller_for(exposure: str, base_url: str, issuer: OidcIssuer) -> HttpCaller:
    """The caller of a cell's exposure: ``cli`` drives kdivectl, the others call over HTTP."""
    if exposure == "cli":
        return CliCaller("cli", base_url, issuer)
    return HttpCaller(cast(Exposure, exposure), base_url, issuer)


def http_caller(caller: HttpCaller) -> HttpCaller:
    """``caller``, or a ``direct`` one for a CLI cell's owned-state cleanup."""
    if isinstance(caller, CliCaller):
        return HttpCaller("direct", caller.base_url, caller.issuer)
    return caller
```

Run `just format`, the focused tests (→ passed), the whole file
(`uv run pytest tests/integration/live_stack/test_tool_cells.py -q` → passed) and `just type`.
Commit `test(live-stack): add a kdivectl caller for tool cells`.

## Task 2 — select the caller in the core carriers

Files: `tests/integration/test_core_tool_cells_live.py:203`,
`tests/integration/test_catalog_tool_cells_live.py:564`,
`tests/integration/test_investigation_tool_cells_live.py:748`,
`tests/integration/live_stack/run_system_job_cells.py:763` and `:922`,
`tests/integration/live_stack/allocation_accounting_cells.py:1001`,
`docs/operating/runbooks/live-testing.md:1135`.
Interfaces: consumes `caller_for` and `http_caller` from Task 1.

Verification: task-test-not-applicable — each entry-point edit swaps
`HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)` for
`caller_for(cell.exposure, base_url, issuer)`, and `run_system_job_cells.py:922` becomes
`await _end_runs(http_caller(c), project, db, inv.id)`, so owned-Run cleanup never depends on a
verb translation. These are live-only call sites whose selection and fallback Task 1's tests
cover; `just type` and `just lint` confirm them (drop `Exposure`/`cast` imports where unused). The
runbook sentence at `:1135` gains "or `cli` for the core service tools (ADR-0795)", and its
current per-carrier cell counts at `:1134`, `:1190` and `:1233` (56, 152, 260) become 84, 228
and 390; its recorded past-run results stay as written. Run
`uv run pytest tests/integration/live_stack -q` → passed (live cells skip with no stack). Commit
`test(live-stack): select the cell's caller in the core carriers`.

## Task 3 — `cli` exposure in the contract

Files: `scripts/coverage_campaign/contract.py`, `scripts/coverage_campaign/obligations.toml`,
`tests/scripts/test_coverage_contract.py`, `docs/adr/0795-cli-translation-exposure.md` (status),
`docs/adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md` (append a dated
amendment paragraph pointing at ADR-0795 for the third exposure).
Interfaces: produces `Cell.exposure == "cli"` cells, owner 3099, sharing the `direct` twin's
`scenario_id`/`node_id`; `OperationGroup.cli: bool = False`.

Verification:
- focused-test — twin contract: `test_cli_cells_twin_the_core_direct_cells`: every `cli` cell's
  `/direct/` twin exists and equals it except `id`, `exposure` and `owner` (3099); the `cli`
  operations equal the five groups' tools; 454 cells; no other group has `cli` cells. Red: count 0.
  Green: `uv run pytest tests/scripts/test_coverage_contract.py -k cli -q` → passed.
- focused-test — validators: `test_cli_exposure_validators_fail_closed`: owner 3112's provider
  group with `cli=True` raises `ValueError` matching `service tools only`; monkeypatching
  `contract.GENERATED_VERBS` without `session.whoami` raises matching `no generated kdivectl verb`.
- focused-test — filtered pairs: in `test_filtered_list_boundaries_are_the_adr_named_pairs`
  (`test_coverage_contract.py:606`) restrict the existing assertions to `direct`/`gateway` and
  assert the six `cli` twins of the `investigations.list`/`artifacts.list` pairs, owner 3099.
- focused-test — existing counts: in `test_coverage_contract.py` the per-carrier counts at
  lines 268, 270, 272, 311 and 313 (56, 152, 260, 220, 220) become 84, 228, 390, 330 and 330,
  since each carrier's node now carries its `cli` twins. Red before the TOML edit is reversed:
  the new numbers fail until `cli = true` lands.
- task-test-not-applicable — the ADR status and amendment edits: prose with no executable
  consumer beyond `just records`, which `just ci` runs.

Steps: write the tests and see them fail; in `contract.py` add
`from kdive.cli.commands._generated_verbs import GENERATED_VERBS`, `cli: bool = False` on
`OperationGroup`, and below `_ADMISSION_REFUSALS`:

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
`for exposure in ("direct", "gateway", "cli") if group.cli else ("direct", "gateway"):` and after
the owner `elif` chain add `if exposure == "cli": owner = _CLI_OWNER`. In `obligations.toml` add
`cli = true` after `execution = "service"` in the groups with `owner = 2811`, `3095`, `3096`,
`3097` and `3098`. Run `uv run pytest tests/scripts/test_coverage_contract.py
tests/integration/live_stack -q` → passed. Commit
`test(coverage): account CLI translation cells for the core tools`.

## Live proof (after an operator grant)

Lane: one exclusive x86_64 demo-up lab host with the provider authority installed, snapshot taken
first. Run the five carriers with `-k "/cli/"` in the `default` and then the `recovery`
configuration; assemble one evidence root; qualify the `cli` cells. Compare failures with the
spec's predicted translation gaps; report each cell's outcome. Cleanup: each cell's cleanup
proof, then roll the host back to the snapshot.
