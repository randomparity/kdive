# Provider-lane tool cells and x86_64 local System tool cells (#3062) — implementation plan

Goal: split the lifecycle contract group as the operator decided, extend the tool-cell frame to
bind and run provider-lane (local-libvirt) cells, and bind and prove the 120 x86_64 local cells of
the six `systems.*` lifecycle tools in both configurations and both exposures.

Architecture: an `obligations.toml` regrouping plus a two-owner route in `contract.py`; frame
additions in `tests/integration/live_stack/tool_cells.py` (provider bindings, foreign-arch skip,
`on_lane_system`, `lane_target`) and one optional `provision` argument on
`scenario.on_catalog_system`; one live carrier `tests/integration/test_system_tool_cells_live.py`;
30 `[implementations]` rows; a runbook section. Design:
[spec](../specs/2026-10-06-system-tool-cells-design.md); decision record ADR-0722, evidence
identity ADR-0715.

Tech stack: Python 3.14, pytest, fastmcp client (`kdive.mcp.dev_harness`), psycopg 3,
libvirt-python — all existing dependencies; nothing is added.

Expected implementation size: 700–900 changed lines (L) — carrier (~400), frame (~170), unit tests
(~90), contract split and tests (~70), 30 bindings, runbook (~40).

## Global Constraints

- No product source change, no new dependency, no ADR, no migration.
- ADR-0722 governs exposure, configuration proof, rejection rules and the protected-state
  snapshot; ADR-0715 governs evidence identity. Existing carriers keep their behaviour and their
  tests stay green; `on_catalog_system`'s existing callers pass no new argument.
- Keep `obligations.toml` edits additive around other groups: campaign 6460ad12693e may land
  #3097/#3098 rows in `[implementations]` concurrently. Refresh the base before publishing.
- Line length 100; `just lint`, `just type` and focused pytest (or `just test-changed`) green
  before each commit; `git fetch origin main && just records` before pushing.
- Live proof only on the disposable lab host, both lanes, at a committed deployed head; wipe the
  stack after. Public text names no host, address, user or lab identifier.

## File map

| File | Today | After |
|---|---|---|
| `scripts/coverage_campaign/obligations.toml` | one 11-tool group, owner 3062, kernel+authority | six `systems.*` tools (owner 3062, no flags); five run/image tools (owner 3119, kernel+authority); + 30 bindings |
| `scripts/coverage_campaign/contract.py` | ppc64le route for 3062/3112, remote route for 3062 | the same routes also for 3119 |
| `tests/scripts/test_coverage_contract.py` | lifecycle split asserts 3062 owns all eleven local tools | asserts the 3062/3119 split, flags, 3080 still 216, and the new node |
| `tests/integration/live_stack/scenario.py` | `on_catalog_system` provisions with `provision_to_ready` | optional `provision` callable, default unchanged behaviour |
| `tests/integration/live_stack/tool_cells.py` | service bindings only; no provider frame | provider bindings + kernel fields; foreign-arch skip; `on_lane_system`; `lane_target` |
| `tests/integration/live_stack/test_tool_cells.py` | asserts provider cells unbound | provider bindings, kernel fields, authority role, foreign-arch skip |
| `tests/integration/test_system_tool_cells_live.py` | — | the six-tool carrier |
| `docs/operating/runbooks/live-testing.md` | sections through #2812 | + System lifecycle tool cells section and run record |

No path becomes obsolete.

## Task 1 — contract split and routing

Files: modify `scripts/coverage_campaign/obligations.toml`, `scripts/coverage_campaign/contract.py`,
`tests/scripts/test_coverage_contract.py`.

Interfaces: produces owners 3062 (six tools, `roles=("server","worker","reconciler")`,
`inputs=()` on functional cells) and 3119. Consumes `build_contract`, `load_mapping`.

Verification:

- Contract: split, flags and routing. `Mode: focused-test` — rewrite
  `test_lifecycle_owners_follow_the_approved_split`; red: local run/image cells owned by 3062 and
  the six tools' functional cells carry `authority`; green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.

Steps:

1. In the test file replace `_LIFECYCLE_TOOLS` with two sets and rewrite the test:

```python
_SYSTEM_TOOLS = {
    "systems.authorize_ssh_key",
    "systems.check_ssh_reachable",
    "systems.provision",
    "systems.reprovision",
    "systems.ssh_info",
    "systems.teardown",
}
_RUN_TOOLS = {
    "images.publish",
    "runs.boot",
    "runs.cancel",
    "runs.install",
    "runs.release_external_boot",
}
_LIFECYCLE_TOOLS = _SYSTEM_TOOLS | _RUN_TOOLS


def test_lifecycle_owners_follow_the_approved_split(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells

    def owners(operations: set[str], provider: str, arch: str) -> set[int]:
        return {
            c.owner
            for c in cells
            if c.operation in operations and c.provider == provider and c.guest_arch == arch
        }

    deep = {"deep-lifecycle"}
    for operations, provider, owner in (
        (_SYSTEM_TOOLS, "local-libvirt", 3062),
        (_RUN_TOOLS, "local-libvirt", 3119),
        (_LIFECYCLE_TOOLS, "remote-libvirt", 3080),
        (deep, "local-libvirt", 2809),
        (deep, "remote-libvirt", 2810),
    ):
        assert owners(operations, provider, "x86_64") == {owner}
    for provider in ("local-libvirt", "remote-libvirt"):
        assert owners(_LIFECYCLE_TOOLS | deep, provider, "ppc64le") == {2818}
    assert len([c for c in cells if c.owner == 3062]) == 120
    assert len([c for c in cells if c.owner == 3119]) == 96
    assert len([c for c in cells if c.owner == 2809]) == 8
    assert len([c for c in cells if c.owner == 2810]) == 8
    assert len([c for c in cells if c.owner == 3080]) == 216
    functional = [c for c in cells if c.kind == "functional" and c.operation in _LIFECYCLE_TOOLS]
    systems = {(c.roles, c.inputs) for c in functional if c.operation in _SYSTEM_TOOLS}
    assert systems == {(("server", "worker", "reconciler"), ())}
    runs = {(c.roles, len(c.inputs)) for c in functional if c.operation in _RUN_TOOLS}
    assert runs == {(("server", "worker", "reconciler", "authority"), 6)}
    local = {c.scenario_id for c in cells if c.operation in deep and c.provider == "local-libvirt"}
    assert local == {"deep-lifecycle/local-libvirt/longterm", "deep-lifecycle/local-libvirt/stable"}
```

2. Run it; expect failures on the 3062/3119 owner sets.
3. In `obligations.toml` replace the `owner = 3062` group with two groups (same observation
   strings): `owner = 3062`, `execution = "provider"` holding the six `systems.*` tools, and
   `owner = 3119`, `execution = "provider"`, `kernel = true`, `authority = true` holding the five
   run/image tools.
4. In `contract.py` `_tool_cells` change the routing to:

```python
                    # Lifecycle and break-glass ppc64le cells wait for the POWER lane (#2818).
                    if group.owner in (3062, 3112, 3119) and arch == "ppc64le":
                        owner = 2818
                    elif group.owner in (3062, 3119) and provider == "remote-libvirt":
                        owner = 3080
```

5. Run the focused command; expect pass. `just lint`, `just type`; commit
   `test(coverage): split the lifecycle group between #3062 and #3119`.

## Task 2 — provider-lane frame

Files: modify `tests/integration/live_stack/scenario.py`, `tests/integration/live_stack/tool_cells.py`,
`tests/integration/live_stack/test_tool_cells.py`.

Interfaces produced (used by Task 3):

```python
# scenario.py
Provision = Callable[[LiveStackClient, str, dict[str, object]], Awaitable[str]]
async def provision_catalog(op: LiveStackClient, allocation_id: str, profile: dict[str, object]) -> str
async def on_catalog_system(run, base_url, issuer, db_url, *, project, image, body,
                            provision: Provision = provision_catalog) -> None
# tool_cells.py
LANE_IMAGES: dict[str, str]                      # {"x86_64": "fedora-kdive-ready-44"}
IDENTITY_PROBE: str                              # PROBE + product_uuid line
def lane_image() -> tuple[str, RootfsCatalogEntry]
@dataclass(frozen=True) class Guest: op, project, system_id, image, entry, endpoint, key, probe, owned, scratch
LaneBody = Callable[[Guest], Awaitable[dict[str, object]]]
async def on_lane_system(run, base_url, issuer, db_url, *, project: str, body: LaneBody,
                         provision: Provision = provision_catalog) -> None
@dataclass(frozen=True) class LaneTarget: project, allocation_id, system_id, observed, artifacts
async def lane_target(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> LaneTarget
def bindings(candidate, *, host_os, host_arch, matrix, cells, staged=staged_image,
             kernel: Mapping[str, str] | None = None) -> InputBindings
```

Consumed from the codebase (verified present): `scenario.on_catalog_system`, `authorize_ssh`,
`ssh_probe`, `catalog_profile`, `CellRun`, `ScenarioStop`; `image_smoke.PROBE`, `Endpoint`,
`os_matches`, `staged_image`; `deep_lifecycle.FIXTURE_ROOT_ENV`, `bound_kernel`, `file_sha256`,
`load_fixture`; `spine.provision_to_ready`; `tool_cells.project_state`.

Verification:

- Contract: provider bindings. `Mode: focused-test` — `test_bindings_cover_bound_tool_cells`
  (rewritten) in `test_tool_cells.py`: a bound native local cell gets `guest_os` `fedora:44`,
  `guest_arch` x86_64, `kvm` and the staged digest; a ppc64le local cell and a remote cell stay
  unbound; service cells keep `accelerator=none`. Red: provider cell absent from `inputs.cells`.
- Contract: kernel fields. `Mode: focused-test` — `test_kernel_inputs_bind_only_declaring_cells`:
  with `kernel={...}` a local `runs.install` functional cell (declares inputs) carries them, a
  `systems.ssh_info` cell does not. Red: `kernel` keyword unknown.
- Contract: authority role. `Mode: focused-test` — `test_declared_authority_fails_without_it`:
  `scenario.run_cell` on a `runs.install` local functional cell with deployed roles
  server/worker/reconciler raises `AssertionError` naming `missing:authority`. Green against the
  existing `run_cell` (pins the generic behaviour; no red step because no code changes).
- Contract: foreign-arch skip. `Mode: focused-test` — `test_foreign_arch_cell_skips_first`:
  a ppc64le local cell skips before `require_stack` is called (monkeypatched to raise). Red:
  `require_stack` raises.
- Contract: `on_lane_system`, `lane_target`, `provision` override. `Mode:
  task-test-not-applicable` — they drive a live server, worker libvirt and a guest over SSH;
  nothing below that surface observes them meaningfully; proven by the live run (Task 4).

Green command for the focused tests:
`uv run python -m pytest tests/integration/live_stack/test_tool_cells.py -q`.

Steps:

1. Write the four tests:

```python
def _cell(operation: str, provider: str, arch: str, kind: str = "functional") -> Cell:
    return next(
        c
        for c in build_contract().cells
        if c.operation == operation
        and c.provider == provider
        and c.guest_arch == arch
        and c.kind == kind
    )


def test_bindings_cover_bound_tool_cells(tmp_path: Path) -> None:
    contract = build_contract()
    whoami = [c for c in contract.cells if c.operation == "session.whoami"]
    bound = [replace(c, node_id="tests/x.py::test_x") for c in whoami]
    unbound = replace(whoami[0], node_id=None, id="unbound")
    native = replace(_cell("systems.ssh_info", "local-libvirt", "x86_64"), node_id="tests/x.py::t")
    foreign = replace(
        _cell("systems.ssh_info", "local-libvirt", "ppc64le"), node_id="tests/x.py::t"
    )
    remote = replace(_cell("systems.ssh_info", "remote-libvirt", "x86_64"), node_id="tests/x.py::t")
    image = tmp_path / "image.qcow2"
    image.write_bytes(b"lane")
    inputs = bindings(
        "a" * 40,
        host_os="fedora:44",
        host_arch="x86_64",
        matrix=contract.matrix_sha256,
        cells=[*bound, unbound, native, foreign, remote],
        staged=lambda _name: image,
    )
    assert set(inputs.cells) == {c.id for c in bound} | {native.id}
    assert {inputs.cells[c.id].accelerator for c in bound} == {"none"}
    lane = inputs.cells[native.id]
    assert (lane.guest_os, lane.guest_arch, lane.accelerator) == ("fedora:44", "x86_64", "kvm")
    assert lane.image_sha256 == hashlib.sha256(b"lane").hexdigest()


def test_kernel_inputs_bind_only_declaring_cells() -> None:
    kernel = {"kernel_sha256": "c" * 64, "kernel_build_id": "d" * 40}
    install = replace(_cell("runs.install", "local-libvirt", "x86_64"), node_id="tests/x.py::t")
    ssh_info = replace(
        _cell("systems.ssh_info", "local-libvirt", "x86_64"), node_id="tests/x.py::t"
    )
    inputs = bindings(
        "a" * 40,
        host_os="fedora:44",
        host_arch="x86_64",
        matrix="b" * 64,
        cells=[install, ssh_info],
        staged=lambda _name: None,
        kernel=kernel,
    )
    assert inputs.cells[install.id].kernel_sha256 == "c" * 64
    assert inputs.cells[install.id].kernel_build_id == "d" * 40
    assert inputs.cells[ssh_info.id].kernel_sha256 is None


def test_declared_authority_fails_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "a" * 40
    roles = {"server": sha, "worker": sha, "reconciler": sha}
    identity = RunIdentity(sha, "b" * 64, "fedora:44", "x86_64", True, roles)
    monkeypatch.setattr(scenario, "require_stack", lambda: "http://stack.test/mcp")
    monkeypatch.setattr(scenario, "run_identity", lambda _url: identity)
    monkeypatch.setattr(scenario, "prerequisites", lambda: (object(), "postgresql://x"))
    monkeypatch.setattr(scenario, "evidence_root", lambda: tmp_path)
    cell = replace(_cell("runs.install", "local-libvirt", "x86_64"), node_id="tests/x.py::t")
    assert "authority" in cell.roles

    async def body(run: CellRun, *_: object) -> None:
        for name in cell.assertions:
            run.prove(name, {})

    with pytest.raises(AssertionError, match="missing:authority"):
        scenario.run_cell(cell, body)


def test_foreign_arch_cell_skips_first(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_stack() -> str:
        raise AssertionError("the stack must not be read")

    monkeypatch.setattr(tool_cells, "require_stack", no_stack)
    monkeypatch.setattr(tool_cells.platform, "machine", lambda: "x86_64")
    cell = _cell("systems.ssh_info", "local-libvirt", "ppc64le")

    async def never(*_: object) -> None:
        raise AssertionError("the scenario must not run")

    with pytest.raises(pytest.skip.Exception, match="ppc64le"):
        tool_cells.run_tool_cell(cell, never)
```

(Imports to add: `hashlib`, `Cell` from `scripts.coverage_campaign.contract`.)

2. Run; expect the binding, kernel and skip tests red, the authority test green.
3. `scenario.py`: add after `catalog_profile`:

```python
Provision = Callable[[LiveStackClient, str, dict[str, object]], Awaitable[str]]


async def provision_catalog(
    op: LiveStackClient, allocation_id: str, profile: dict[str, object]
) -> str:
    """Provision ``profile`` on ``allocation_id`` with the frame's client and wait for ``ready``."""
    return await provision_to_ready(
        op, allocation_id=allocation_id, profile=profile, phase_name="provision"
    )
```

   Give `on_catalog_system` a keyword `provision: Provision = provision_catalog`, document it
   ("``provision`` creates the System; a cell whose tool under test is the provision passes its
   own"), and replace the `provision_to_ready(...)` call with
   `system_id = await provision(op, allocation, catalog_profile(entry, image, f"{project}-unread"))`.
4. `tool_cells.py`: update the module docstring for the provider frame; add constants
   `LANE_IMAGES = {"x86_64": "fedora-kdive-ready-44"}`, `IDENTITY_PROBE = PROBE + '; printf
   "product_uuid=%s\\n" "$(cat /sys/class/dmi/id/product_uuid)"'`, `_SETTLE_S = 15.0`,
   `_SETTLE_ATTEMPTS = 8`, `_TARGETS: dict[str, LaneTarget | Exception] = {}`; at the top of
   `run_tool_cell`:

```python
    if cell.host_arch not in (None, platform.machine()):
        pytest.skip(f"{cell.id} runs on a {cell.host_arch} host")
```

   replace `bindings` with:

```python
def bindings(
    candidate: str,
    *,
    host_os: str,
    host_arch: str,
    matrix: str,
    cells: Iterable[Cell],
    staged: Callable[[str], Path | None] = staged_image,
    kernel: Mapping[str, str] | None = None,
) -> InputBindings:
    """The expected ``Context`` of every bound service and native local-libvirt tool cell.

    A native cell boots ``LANE_IMAGES[host_arch]``: its ``image_sha256`` is the staged bytes'
    digest, null when unstaged. ``kernel`` fills only the kernel fields a cell declares.
    """
    bound = [c for c in cells if c.scenario_id.startswith("tool/") and c.node_id]
    contexts = {
        c.id: Context.model_validate(
            {"host_os": host_os, "host_arch": host_arch, "accelerator": "none"}
        )
        for c in bound
        if c.provider == "service"
    }
    native = [c for c in bound if c.provider == "local-libvirt" and c.guest_arch == host_arch]
    name = LANE_IMAGES.get(host_arch)
    if native and name is not None:
        entry = load_rootfs_catalog()[name]
        image = staged(name)
        digest = file_sha256(image) if image is not None else None
        for cell in native:
            declared = {k: v for k, v in (kernel or {}).items() if k in cell.inputs}
            contexts[cell.id] = Context.model_validate(
                {
                    "host_os": host_os,
                    "host_arch": host_arch,
                    "guest_os": f"{entry.distro}:{entry.version}",
                    "guest_arch": entry.arch,
                    "accelerator": cell.accelerator,
                    "image_sha256": digest,
                    **declared,
                }
            )
    return InputBindings(version=1, candidate_sha=candidate, matrix_sha256=matrix, cells=contexts)
```

   In `main`, add `write.add_argument("--kernel-baseline", help=...)`; when given, require
   `$KDIVE_FIXTURE_ROOT` (`parser.error` otherwise), set
   `kernel = bound_kernel(Path(root), args.kernel_baseline, host_arch, load_fixture)`, print
   `"no verified kernel fixture; kernel fields stay null"` when empty, and pass `kernel=kernel`.
5. Add the frame:

```python
def lane_image() -> tuple[str, RootfsCatalogEntry]:
    """The catalog image this host's provider cells boot; blocked on an unlisted architecture."""
    name = LANE_IMAGES.get(platform.machine())
    if name is None:
        raise ScenarioStop(Outcome.BLOCKED, f"no lane image for {platform.machine()}")
    return name, load_rootfs_catalog()[name]


async def observe_guest(
    run: CellRun, op: LiveStackClient, system_id: str, scratch: Path, entry: RootfsCatalogEntry
) -> tuple[Endpoint, Path, dict[str, str]]:
    """Authorize a frame key, probe the guest as root and record its identity on ``run``."""
    scratch.mkdir(parents=True, exist_ok=True)
    endpoint, key = await asyncio.wait_for(
        authorize_ssh(op, system_id, scratch, "tool-cell"), timeout=900
    )
    probe = await asyncio.to_thread(ssh_probe, endpoint, key, IDENTITY_PROBE)
    assert probe.get("uid") == "0", f"ssh as root reported uid {probe.get('uid')!r}"
    assert os_matches(entry, probe), f"guest {probe.get('ID')} is not catalog {entry.distro}"
    run.observed |= {"guest_os": f"{entry.distro}:{entry.version}", "guest_arch": entry.arch}
    return endpoint, key, probe


@dataclass(frozen=True)
class Guest:
    """A ``ready`` lane System as a provider cell's body sees it.

    ``owned`` is the frame's list of host paths the cleanup proves absent; a body that makes the
    System own more appends to it.
    """

    op: LiveStackClient
    project: str
    system_id: str
    image: str
    entry: RootfsCatalogEntry
    endpoint: Endpoint
    key: Path
    probe: dict[str, str]
    owned: list[str]
    scratch: Path


LaneBody = Callable[[Guest], Awaitable[dict[str, object]]]


async def on_lane_system(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    *,
    project: str,
    body: LaneBody,
    provision: Provision = provision_catalog,
) -> None:
    """Provision the lane image in ``project``, observe its guest, prove ``effect``, then cleanup.

    ``body`` returns the ``effect`` observation; the frame adds the exposure.
    """
    name, entry = lane_image()

    async def framed(op: LiveStackClient, system_id: str, owned: list[str]) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            endpoint, key, probe = await observe_guest(run, op, system_id, root / "frame", entry)
            guest = Guest(op, project, system_id, name, entry, endpoint, key, probe, owned, root)
            observed = await body(guest)
        run.prove("effect", {"exposure": run.cell.exposure, **observed})

    await on_catalog_system(
        run,
        base_url,
        issuer,
        db_url,
        project=project,
        image=name,
        body=framed,
        provision=provision,
    )


@dataclass(frozen=True)
class LaneTarget:
    """A torn-down lane System and its released Allocation: what rejection cells aim at.

    ``observed`` is the target's guest, accelerator and image identity, which each rejection
    record carries; ``artifacts`` hold its cleanup proof.
    """

    project: str
    allocation_id: str
    system_id: str
    observed: dict[str, object]
    artifacts: tuple[str, ...]


async def _allocation_of(db_url: str, system_id: str) -> str:
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        row = await (
            await conn.execute("SELECT allocation_id FROM systems WHERE id = %s", (system_id,))
        ).fetchone()
    assert row is not None, f"System {system_id} has no row"
    return str(row[0])


async def _settled(db_url: str, project: str) -> None:
    """Wait until ``project``'s snapshot stops changing (release bookkeeping runs after it)."""
    for _ in range(_SETTLE_ATTEMPTS):
        before = await project_state(db_url, project)
        await asyncio.sleep(_SETTLE_S)
        if await project_state(db_url, project) == before:
            return
    raise AssertionError(f"project {project} kept changing after its System was released")


async def _provision_target(
    run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str
) -> LaneTarget:
    name, entry = lane_image()
    project = f"cov-{secrets.token_hex(4)}"
    target = CellRun(run.cell, run.writer)
    seen: list[str] = []

    async def observe(op: LiveStackClient, system_id: str, _owned: list[str]) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            await observe_guest(target, op, system_id, Path(scratch), entry)
        seen.append(system_id)

    await on_catalog_system(
        target, base_url, issuer, db_url, project=project, image=name, body=observe
    )
    allocation = await _allocation_of(db_url, seen[0])
    await _settled(db_url, project)
    cleanup = target.assertions["cleanup"]
    summary = run.writer.artifact(
        {"lane_target": {"system": "torn_down", "allocation": "released"}, "cleanup": cleanup}
    )
    return LaneTarget(project, allocation, seen[0], dict(target.observed), (cleanup, summary))


async def lane_target(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> LaneTarget:
    """The stack's rejection target, provisioned, observed and reclaimed on first use.

    Every role check of the System tools runs before any state check, so a torn-down System and
    a released Allocation are valid targets that no background work changes. A failed or
    blocked preparation is remembered and repeated for every later cell rather than retried.
    """
    cached = _TARGETS.get(base_url)
    if cached is None:
        try:
            cached = await _provision_target(run, base_url, issuer, db_url)
        except Exception as exc:  # noqa: BLE001 - remembered and re-raised for every cell
            cached = exc
        _TARGETS[base_url] = cached
    if isinstance(cached, ScenarioStop):
        raise ScenarioStop(cached.outcome, str(cached))
    if isinstance(cached, Exception):
        raise AssertionError(f"the lane target could not be prepared: {cached!r}") from cached
    run.observed |= cached.observed
    run.artifacts.extend(cached.artifacts)
    return cached
```

6. Run the focused command; expect all four new tests and the existing ones to pass. Run the
   existing frame tests `uv run python -m pytest tests/integration/live_stack -q` (unit tests,
   no stack): pass. `just lint`, `just type`; commit
   `test(live): bind and frame provider-lane tool cells`.

## Task 3 — the System tool-cell carrier and its bindings

Files: create `tests/integration/test_system_tool_cells_live.py`; modify
`scripts/coverage_campaign/obligations.toml` (`[implementations]`, 30 rows appended after the
operator rows), `tests/scripts/test_coverage_contract.py`.

Interfaces: consumes every Task 2 interface plus `HttpCaller`, `Grants`, `Rejection`,
`boundary_of`, `one`, `project_state`, `prove_rejection`, `run_tool_cell`, `tool_cells`;
`scenario.catalog_profile`, `domain_xml`, `authorize_ssh`, `ssh_probe`, `Provision`;
`image_smoke.Endpoint`, `os_matches`, `ssh`; `spine.await_system_state`, `drain_job`;
`cleanup.domain_disks`, `disk_absent`; `tests.mcp.json_data.data_mapping`, `data_str`.

Verification:

- Contract: carrier bindings. `Mode: focused-test` — in `test_pending_cells_have_owned_assertions_but_no_invented_nodes`
  add: local cells of the six tools (both arches) are 240 and bind to `_SYSTEM_NODE`
  (`tests/integration/test_system_tool_cells_live.py::test_system_tool_cell`); their remote
  cells stay unbound; add `*_SYSTEM_TOOLS` to `bound`. Red: node is `None`. Green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.
- Contract: the live cells. `Mode: task-test-not-applicable` — each acts only against a live
  server, worker libvirt and guest; proven by Task 4's run and `qualify`.

Steps:

1. Write the contract assertion; run; expect red.
2. Append to `[implementations]` one row per scenario
   `tool/<tool>/default/<kind>` for the six tools and kinds `functional`, `authentication`,
   `authorization`, `project-isolation`, `validation`, each
   `= "tests/integration/test_system_tool_cells_live.py::test_system_tool_cell"`.
3. Create the carrier:

```python
"""Prove the x86_64 local-libvirt System lifecycle tool cells over HTTP (#3062, ADR-0722).

``live_stack``-marked. One parameter per native local-libvirt contract cell of the six
``systems.*`` lifecycle tools, framed by
:func:`~tests.integration.live_stack.tool_cells.run_tool_cell`. A functional cell provisions the
lane image in a fresh ``cov-<hex>`` project through
:func:`~tests.integration.live_stack.tool_cells.on_lane_system`, calls the tool in the cell's
exposure, proves its effect against the worker's libvirt domain, the guest over SSH and its
disks, and proves owned cleanup. A rejection cell aims at the stack's
:func:`~tests.integration.live_stack.tool_cells.lane_target`, a torn-down System and its
released Allocation. ``docs/operating/runbooks/live-testing.md`` covers the run.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import secrets
import socket
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import tempfile
import xml.etree.ElementTree as ET  # noqa: S405 - the worker's own domain XML  # nosec B405
from collections.abc import Awaitable, Callable
from functools import cache, partial
from pathlib import Path
from typing import cast
from uuid import uuid4

import libvirt
import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell
from tests.integration.live_stack.cleanup import disk_absent, domain_disks
from tests.integration.live_stack.image_smoke import Endpoint, os_matches, ssh
from tests.integration.live_stack.scenario import (
    CellRun,
    Provision,
    authorize_ssh,
    catalog_profile,
    domain_xml,
    provision_catalog,
    ssh_probe,
)
from tests.integration.live_stack.spine import await_system_state, drain_job
from tests.integration.live_stack.tool_cells import (
    IDENTITY_PROBE,
    Boundary,
    Exposure,
    Grants,
    Guest,
    HttpCaller,
    LaneTarget,
    Rejection,
    boundary_of,
    lane_image,
    lane_target,
    on_lane_system,
    one,
    project_state,
    prove_rejection,
    run_tool_cell,
    tool_cells,
)
from tests.mcp.json_data import data_mapping, data_str

pytestmark = pytest.mark.live_stack

TOOLS = (
    "systems.authorize_ssh_key",
    "systems.check_ssh_reachable",
    "systems.provision",
    "systems.reprovision",
    "systems.ssh_info",
    "systems.teardown",
)
# Each tool's project gate, and the role one rank below it (none below viewer).
_GATES = {
    "systems.authorize_ssh_key": "contributor",
    "systems.check_ssh_reachable": "viewer",
    "systems.provision": "contributor",
    "systems.reprovision": "contributor",
    "systems.ssh_info": "viewer",
    "systems.teardown": "admin",
}
_BELOW: dict[str, str | None] = {"viewer": None, "contributor": "viewer", "admin": "contributor"}
# The SSH tools answer an id outside the caller's projects with not_found, the others with
# configuration_error; both are byte-identical to an absent id.
_NOT_FOUND_TOOLS = frozenset(
    {"systems.authorize_ssh_key", "systems.check_ssh_reachable", "systems.ssh_info"}
)
_HOSTFWD = re.compile(r"hostfwd=tcp:127\.0\.0\.1:(\d+)-:22")
_MARKER = "/root/kdive-cov-marker"
_LANE_VCPUS, _LANE_MEMORY_KIB = 2, 2 * 1024 * 1024
_KEYS = "cat /root/.ssh/authorized_keys"
Body = Callable[[HttpCaller, str, Guest], Awaitable[dict[str, object]]]


def _grants(project: str, role: str | None) -> Grants:
    """A token for ``project`` holding ``role``, or membership with no role."""
    return Grants(f"{project}-{role or 'member'}", (project,), {project: role} if role else {})


def _stranger() -> Grants:
    other = f"cov-{secrets.token_hex(4)}"
    return Grants(f"{other}-operator", (other,), {other: "operator"})


def _keypair(directory: Path) -> tuple[Path, str]:
    """A fresh ed25519 key under ``directory``: its private path and public line."""
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / "id_ed25519"
    subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "tool-cell", "-f", str(key)],
        check=True,
        timeout=30.0,
    )
    return key, (directory / "id_ed25519.pub").read_text(encoding="utf-8").strip()


@cache
def _public_key() -> str:
    """One valid public key the rejected ``authorize_ssh_key`` calls carry."""
    with tempfile.TemporaryDirectory() as scratch:
        return _keypair(Path(scratch))[1]


def _key_id(line: str) -> str:
    """``<type> <base64>`` of an ``authorized_keys`` line, ignoring options and comment."""
    tokens = line.split()
    start = next(i for i, t in enumerate(tokens) if t.startswith(("ssh-", "ecdsa-", "sk-")))
    return " ".join(tokens[start : start + 2])


def _keys(result: subprocess.CompletedProcess[str]) -> set[str]:
    assert result.returncode == 0, f"reading authorized_keys exited {result.returncode}"
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return {_key_id(line) for line in lines if not line.startswith("#")}


def _inode(path: str) -> int:
    try:
        return os.stat(path).st_ino
    except PermissionError:
        out = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
            ["sudo", "-n", "stat", "-c", "%i", "--", path],
            capture_output=True,
            text=True,
            timeout=10.0,
            check=True,
        )
        return int(out.stdout.strip())


def _defined(system_id: str) -> bool:
    try:
        domain_xml(system_id)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            return False
        raise
    return True


def _banner(endpoint: Endpoint) -> str:
    with socket.create_connection((endpoint.host, endpoint.port), timeout=10.0) as conn:
        return conn.recv(256).decode("ascii", "replace").strip()


async def _call(caller: HttpCaller, tool: str, args: dict[str, object], token: str) -> ToolResponse:
    return one(await caller.call(tool, args, token, discover=True))


def _provisioner(caller: HttpCaller, token: str) -> Provision:
    """Provision through the cell's exposure: the tool under test is ``systems.provision``."""

    async def provision(op: LiveStackClient, allocation_id: str, profile: dict[str, object]) -> str:
        args = {"allocation_id": allocation_id, "profile": profile}
        system_id = data_str(await _call(caller, "systems.provision", args, token), "system_id")
        await await_system_state(op, "provision", system_id, "ready")
        return system_id

    return provision


async def _provision(_caller: HttpCaller, _token: str, guest: Guest) -> dict[str, object]:
    domain = ET.fromstring(domain_xml(guest.system_id))  # noqa: S314  # nosec B314
    vcpus = int(domain.findtext("vcpu") or 0)
    memory = domain.find("memory")
    assert memory is not None and memory.get("unit", "KiB") == "KiB", "domain memory unit"
    kib = int(memory.text or 0)
    assert (vcpus, kib) == (_LANE_VCPUS, _LANE_MEMORY_KIB), f"domain sized {vcpus}/{kib} KiB"
    return {"system": "ready", "boot_id_seen": True, "vcpus": vcpus, "memory_kib": kib}


async def _ssh_info(caller: HttpCaller, token: str, guest: Guest) -> dict[str, object]:
    env = await _call(caller, "systems.ssh_info", {"system_id": guest.system_id}, token)
    coords = data_mapping(env, "ssh")
    xml = domain_xml(guest.system_id)
    forwarded = _HOSTFWD.search(xml)
    assert forwarded, "the domain XML carries no loopback SSH forward"
    assert coords.get("port") == int(forwarded.group(1)), f"ssh_info port {coords.get('port')}"
    endpoint = Endpoint(str(coords["host"]), int(cast(int, coords["port"])))
    probe = await asyncio.to_thread(ssh_probe, endpoint, guest.key, IDENTITY_PROBE)
    uuid = ET.fromstring(xml).findtext("uuid") or ""  # noqa: S314  # nosec B314
    assert probe.get("product_uuid", "").lower() == uuid.lower(), "guest is not this domain"
    return {"user": coords.get("user"), "port_matches_domain": True, "guest_is_domain": True}


async def _authorize(caller: HttpCaller, token: str, guest: Guest) -> dict[str, object]:
    before = _keys(await asyncio.to_thread(ssh, guest.endpoint, guest.key, _KEYS))
    key, public = await asyncio.to_thread(_keypair, guest.scratch / "second")
    args = {"system_id": guest.system_id, "public_key": public}
    env = await _call(caller, "systems.authorize_ssh_key", args, token)
    await drain_job(guest.op, "authorize", env.object_id)
    after = _keys(await asyncio.to_thread(ssh, guest.endpoint, key, _KEYS))
    kept = _keys(await asyncio.to_thread(ssh, guest.endpoint, guest.key, _KEYS))
    assert after == kept == before | {_key_id(public)}, "authorized_keys changed beyond the key"
    return {"keys_before": len(before), "keys_after": len(after), "frame_key_kept": True}


async def _check(caller: HttpCaller, token: str, guest: Guest) -> dict[str, object]:
    env = await _call(caller, "systems.check_ssh_reachable", {"system_id": guest.system_id}, token)
    job = await drain_job(guest.op, "check", env.object_id)
    verdict = json.loads(job.refs["result"])
    assert verdict.get("reachable") is True, f"verdict {verdict}"
    banner = await asyncio.to_thread(_banner, guest.endpoint)
    assert banner.startswith("SSH-"), "the endpoint sent no SSH banner"
    active = await asyncio.to_thread(ssh, guest.endpoint, guest.key, "systemctl is-active sshd")
    assert active.stdout.strip() == "active", f"sshd is {active.stdout.strip()!r}"
    return {"verdict": "reachable", "banner": banner.split("-", 2)[1], "sshd": "active"}


async def _reprovision(caller: HttpCaller, token: str, guest: Guest) -> dict[str, object]:
    marked = await asyncio.to_thread(ssh, guest.endpoint, guest.key, f"touch {_MARKER}")
    assert marked.returncode == 0, f"marker write exited {marked.returncode}"
    old = list(guest.owned)
    inodes = {path: _inode(path) for path in old}
    profile = catalog_profile(guest.entry, guest.image, f"{guest.project}-unread")
    args = {"system_id": guest.system_id, "profile": profile}
    env = await _call(caller, "systems.reprovision", args, token)
    await drain_job(guest.op, "reprovision", env.object_id)
    await await_system_state(guest.op, "reprovision", guest.system_id, "ready")
    guest.owned.extend(p for p in domain_disks(domain_xml(guest.system_id)) if p not in old)
    kept = [p for p in old if not disk_absent(p) and _inode(p) == inodes[p]]
    assert not kept, f"owned disk(s) survived the reprovision: {kept}"
    endpoint, key = await authorize_ssh(guest.op, guest.system_id, guest.scratch / "after", "cov")
    probe = await asyncio.to_thread(ssh_probe, endpoint, key)
    assert probe.get("boot_id") != guest.probe.get("boot_id"), "the guest did not reboot"
    assert os_matches(guest.entry, probe), "the replacement guest is not the catalog image"
    marker = await asyncio.to_thread(ssh, endpoint, key, f"test -e {_MARKER}")
    assert marker.returncode == 1, "the old install's marker survived the reprovision"
    return {"ready": True, "boot_id_changed": True, "marker": "absent", "disks_replaced": len(old)}


async def _teardown(caller: HttpCaller, token: str, guest: Guest) -> dict[str, object]:
    env = await _call(caller, "systems.teardown", {"system_id": guest.system_id}, token)
    if env.status != "torn_down":
        await drain_job(guest.op, "teardown", env.object_id)
    await await_system_state(guest.op, "teardown", guest.system_id, "torn_down")
    assert not await asyncio.to_thread(_defined, guest.system_id), "the domain is still defined"
    surviving = [path for path in guest.owned if not disk_absent(path)]
    assert not surviving, f"owned disk(s) survived teardown: {surviving}"
    return {"system": "torn_down", "domain": "absent", "disks_absent": len(guest.owned)}


_FUNCTIONAL: dict[str, Body] = {
    "systems.authorize_ssh_key": _authorize,
    "systems.check_ssh_reachable": _check,
    "systems.provision": _provision,
    "systems.reprovision": _reprovision,
    "systems.ssh_info": _ssh_info,
    "systems.teardown": _teardown,
}


async def _functional(
    run: CellRun, caller: HttpCaller, base_url: str, issuer: OidcIssuer, db_url: str
) -> None:
    tool = run.cell.operation
    project = f"cov-{secrets.token_hex(4)}"
    token = caller.token(_grants(project, _GATES[tool]))
    provision = _provisioner(caller, token) if tool == "systems.provision" else provision_catalog
    await on_lane_system(
        run,
        base_url,
        issuer,
        db_url,
        project=project,
        body=partial(_FUNCTIONAL[tool], caller, token),
        provision=provision,
    )


def _target_args(
    tool: str, target: LaneTarget, system_id: str, allocation_id: str
) -> dict[str, object]:
    name, entry = lane_image()
    profile = catalog_profile(entry, name, f"{target.project}-unread")
    if tool == "systems.provision":
        return {"allocation_id": allocation_id, "profile": profile}
    args: dict[str, object] = {"system_id": system_id}
    if tool == "systems.authorize_ssh_key":
        args["public_key"] = _public_key()
    if tool == "systems.reprovision":
        args["profile"] = profile
    return args


def _rejection(tool: str, boundary: Boundary, target: LaneTarget) -> Rejection:
    args = _target_args(tool, target, target.system_id, target.allocation_id)
    gate = _GATES[tool]
    if boundary == "validation":
        key = "allocation_id" if tool == "systems.provision" else "system_id"
        return Rejection({**args, key: 7}, _grants(target.project, gate))
    if boundary == "authentication":
        # A viewer's issued-token control call is refused by role or readiness, writing nothing.
        return Rejection(args, _grants(target.project, "viewer"))
    if boundary == "authorization":
        return Rejection(args, _grants(target.project, _BELOW[gate]))
    category = (
        ErrorCategory.NOT_FOUND if tool in _NOT_FOUND_TOOLS else ErrorCategory.CONFIGURATION_ERROR
    )
    twin = _target_args(tool, target, str(uuid4()), str(uuid4()))
    return Rejection(args, _stranger(), frozenset({category.value}), absent_twin=twin)


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    caller = HttpCaller(cast(Exposure, run.cell.exposure), base_url, issuer)
    if run.cell.kind == "functional":
        await _functional(run, caller, base_url, issuer, db_url)
        return
    target = await lane_target(run, base_url, issuer, db_url)
    boundary = boundary_of(run.cell)
    snapshot = partial(project_state, db_url, target.project)
    await prove_rejection(
        run, caller, boundary, _rejection(run.cell.operation, boundary, target), snapshot
    )


def _cells() -> list[Cell]:
    host = platform.machine()
    return [c for c in tool_cells(TOOLS) if c.provider == "local-libvirt" and c.guest_arch == host]


@pytest.mark.parametrize("cell", _cells(), ids=lambda cell: cell.id)
def test_system_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a System lifecycle tool."""
    run_tool_cell(cell, _scenario)
```

4. Run `uv run python -m pytest tests/scripts/test_coverage_contract.py -q` (green) and
   `uv run python -m pytest tests/integration/test_system_tool_cells_live.py -q` (every
   parameter deselected by the `live_stack` marker, no collection error). `just lint`,
   `just type`; commit `test(live): prove the local System lifecycle tool cells`.

## Task 4 — runbook and live proof

Files: modify `docs/operating/runbooks/live-testing.md` (new `#### System lifecycle tool cells
(#3062)` after the #2812 section).

Verification:

- Contract: runbook. `Mode: task-test-not-applicable` — prose; `just docs-check` covers links.
- Contract: the 120 live cells. `Mode: task-test-not-applicable` — live stack only; evidence is
  the two-lane run and `qualify` rows.

Steps:

1. Write the section: what the carrier proves, the lane image staging
   (`examples/local-libvirt/build-image.sh fedora-kdive-ready-44`), the environment (sourced
   `env.sh`, `KDIVE_DATABASE_URL="$KDIVE_MIGRATION_DATABASE_URL"`, exported
   `KDIVE_SYSTEMS_TOML`), the two-lane command block (bindings after staging; same evidence
   root), what cells leave behind, the four expected `gateway` validation failures, and a
   "Last run" record.
2. On the lab guest: push the branch head, check it out, `just build-capture-bootstrap-manifest`,
   bindings, then per lane `~/up-2811.sh <lane>`, check `/readyz` `ready` on the worker, run the
   carrier, assemble, `qualify`; grep the 120 rows. Wipe the stack (`demo-down.sh --wipe --yes`)
   and confirm no domain remains.
3. Fill the "Last run" record with candidate SHA and outcome counts; commit
   `docs(runbook): record the System tool-cell run`. If the record commit changes HEAD, rerun
   is not required (docs only), and the record names the proven candidate.
