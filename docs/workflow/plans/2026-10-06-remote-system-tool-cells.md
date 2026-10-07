# Remote provider-lane tool cells and x86_64 remote System tool cells (#3080) — implementation plan

Goal: route the remote lifecycle tool cells as the operator split them, make the provider-lane
tool-cell frame run on a `remote-libvirt` provider host, and bind and prove the 120 x86_64 remote
cells of the six `systems.*` lifecycle tools in both configurations and both exposures.

Architecture: a two-line route in `contract.py`; a `Lane` value in
`tests/integration/live_stack/tool_cells.py` that the frame (`on_lane_system`, `lane_target`,
`Guest`, `bindings`) takes from `lane_for(cell)`; one optional `provision` argument and two small
observer helpers in `remote_lifecycle.py`; the existing System carrier parametrized over the
remote cells too; 30 `[implementations]` rows; a runbook section. Design:
[spec](../specs/2026-10-06-remote-system-tool-cells-design.md); ADR-0722, ADR-0715.

Tech stack: Python 3.14, pytest, libvirt-python, fastmcp client — all existing; nothing added.

Expected implementation size: 400–550 changed lines (L) — frame and bindings (~150), carrier
(~40), remote helpers (~30), unit tests (~150), contract and its tests (~20), 30 bindings, runbook
(~60).

## Global Constraints

- No product source change, no new dependency, no ADR, no migration.
- ADR-0722 governs exposure, configuration proof, rejection rules and the protected-state
  snapshot; ADR-0715 governs evidence identity: the provider destination never enters evidence.
- Remote scenario IDs stay provider-qualified (`tool/remote-libvirt/<tool>/<variant>/<kind>`,
  `contract.py` `_tool_cells`).
- Existing callers keep their behaviour: `on_remote_system`'s deep-lifecycle caller passes no new
  argument; local cells bind and run as before.
- Line length 100; `just lint`, `just type` and focused pytest green before each commit;
  `git fetch origin main && just records` before pushing.
- Live mutations only on disposable lab hosts, at a committed deployed head; wipe the stack and
  check the provider afterwards. Public text names no host, address, user or lab identifier.

## File map

| File | Today | After |
|---|---|---|
| `scripts/coverage_campaign/contract.py` | remote cells of 3062 and 3119 → 3080 | 3062 → 3080, 3119 → 3120 |
| `tests/scripts/test_coverage_contract.py` | 3080 owns 216; remote System cells unbound | 3080 120, 3120 96; remote System cells bound |
| `scripts/coverage_campaign/obligations.toml` | 30 local System bindings | + 30 `tool/remote-libvirt/systems.*` bindings |
| `tests/integration/live_stack/remote_lifecycle.py` | frame provisions with `provision_to_ready`; private `_remote_xml` | `provision` argument; public `remote_xml`, `remote_volume_absent`, `staged_base_volume` |
| `tests/integration/live_stack/tool_cells.py` | local-only frame and bindings | `Lane`, `lane_for`, remote bindings, per-provider target memo, `--remote` CLI |
| `tests/integration/test_system_tool_cells_live.py` | local cells; worker libvirt | local and remote cells; observers through `guest.lane` |
| `tests/integration/live_stack/test_tool_cells.py`, `test_remote_lifecycle.py` | local frame tests | + remote bindings, lane selection, skip, memo, observer helper |
| `docs/operating/runbooks/remote-live-stack.md` | §7 remote deep lifecycle | + §8 remote System tool cells and the recorded run |

No ownership transition: the frame keeps its owner and gains a provider seam.

## Task 1 — owner routing

Files: `scripts/coverage_campaign/contract.py`, `tests/scripts/test_coverage_contract.py`.

Verification:
- owner routing — Mode: focused-test. `test_lifecycle_owners_follow_the_approved_split` expects
  `(_SYSTEM_TOOLS, "remote-libvirt", 3080)`, `(_RUN_TOOLS, "remote-libvirt", 3120)`, 3080 → 120,
  3120 → 96. Red before the route: `{3080} != {3120}`. Green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q` → all pass.

Steps:
1. In the test, replace `(_LIFECYCLE_TOOLS, "remote-libvirt", 3080),` with
   `(_SYSTEM_TOOLS, "remote-libvirt", 3080),` and `(_RUN_TOOLS, "remote-libvirt", 3120),`; replace
   `assert len([c for c in cells if c.owner == 3080]) == 216` with `== 120` and add
   `assert len([c for c in cells if c.owner == 3120]) == 96`. Run; expect the red above.
2. In `contract.py` `_tool_cells` replace the route with:
   ```python
                       # Lifecycle and break-glass ppc64le cells wait for the POWER lane (#2818);
                       # x86_64 remote System cells are #3080's, remote run/image cells #3120's.
                       if group.owner in (3062, 3112, 3119) and arch == "ppc64le":
                           owner = 2818
                       elif group.owner == 3062 and provider == "remote-libvirt":
                           owner = 3080
                       elif group.owner == 3119 and provider == "remote-libvirt":
                           owner = 3120
   ```
3. Run the green command; commit `test(coverage): route remote run/image cells to #3120`.

## Task 2 — remote observer helpers and the provision seam

File: `tests/integration/live_stack/remote_lifecycle.py`; test
`tests/integration/live_stack/test_remote_lifecycle.py`.

Interfaces (later tasks rely on):
- `remote_xml(dest: str, system_id: str) -> str` (rename of `_remote_xml`).
- `remote_volume_absent(dest: str, path: str) -> bool`.
- `staged_base_volume(image: RemoteImage) -> str` — raises `ScenarioStop(BLOCKED, ...)` when
  unstaged.
- `on_remote_system(..., body: CatalogBody, provision: Provision = provision_catalog)`.
  `Provision` and `provision_catalog` exist in `tests/integration/live_stack/scenario.py`
  (`Provision = Callable[[LiveStackClient, str, dict[str, object]], Awaitable[str]]`).

Verification:
- `remote_volume_absent` opens and closes its own observer — Mode: focused-test.
  `test_remote_volume_absent_closes_its_observer` with the existing `_Conn` fake: `_NoVolume`
  → `True`, `conn.closed` true. Red: `AttributeError` (no such function). Green:
  `uv run python -m pytest tests/integration/live_stack/test_remote_lifecycle.py -q`.
- `staged_base_volume` blocks when unstaged — Mode: focused-test.
  `test_unstaged_base_volume_blocks` monkeypatches `remote_lifecycle.staged_volume` to `None`
  and expects `ScenarioStop` with `Outcome.BLOCKED`. Same red and green.
- `provision` seam — Mode: task-test-not-applicable: the default is the call it replaces, and
  the frame acts only through a live stack client and provider host; exercised by the remote
  `systems.provision` cells.

Steps:
1. Add the two tests:
   ```python
   def test_remote_volume_absent_closes_its_observer(monkeypatch: pytest.MonkeyPatch) -> None:
       conn = _Conn(_NoVolume("gone"))
       monkeypatch.setattr(remote_lifecycle, "observer", lambda _dest: _conn(conn))
       assert remote_volume_absent("operator@provider.example", "/pool/overlay.qcow2")
       assert conn.closed


   def test_unstaged_base_volume_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
       monkeypatch.setattr(remote_lifecycle, "staged_volume", lambda _name: None)
       with pytest.raises(ScenarioStop) as stop:
           staged_base_volume(REMOTE_REPRESENTATIVES["fedora"])
       assert stop.value.outcome is Outcome.BLOCKED
   ```
   (import `remote_volume_absent`, `staged_base_volume` from `remote_lifecycle`). Run; red.
2. In `remote_lifecycle.py`:
   ```python
   def staged_base_volume(image: RemoteImage) -> str:
       """The staged base volume of ``image``; an unstaged image is a blocked cell."""
       volume = staged_volume(image.name)
       if volume is None:
           raise ScenarioStop(
               Outcome.BLOCKED,
               f"{image.name} is not a staged remote-libvirt [[image]]; build it with "
               "deploy/ansible/playbooks/image.yml",
           )
       return volume


   def remote_volume_absent(dest: str, path: str) -> bool:
       """:func:`volume_absent` over the test's own observer connection to the provider host."""
       conn = observer(dest)
       try:
           return volume_absent(conn, path)
       finally:
           conn.close()
   ```
   Rename `_remote_xml` to `remote_xml`. In `on_remote_system` add the keyword parameter
   `provision: Provision = provision_catalog`, replace the inline staged-volume check with
   `volume = staged_base_volume(image)`, and replace the `provision_to_ready(...)` call with
   `system_id = await provision(op, allocation, remote_profile(image.arch, volume))`. Drop the
   now-unused `provision_to_ready` import; import `Provision`, `provision_catalog` from
   `scenario`. Docstring: "``provision`` creates the System; a cell whose tool under test is the
   provision passes its own."
3. Green command, `just lint`, `just type`; commit
   `test(live-stack): add remote observer helpers and a provision seam`.

## Task 3 — the `Lane` and the remote frame and bindings

File: `tests/integration/live_stack/tool_cells.py`; test
`tests/integration/live_stack/test_tool_cells.py`.

Interfaces (Task 4 relies on): `REMOTE_LANE_FAMILIES: dict[str, str]`; `Lane` with fields
`provider`, `entry: GuestIdentity`, `profile: Callable[[str], dict[str, object]]`,
`xml: Callable[[str], str]`, `absent: Callable[[str], bool]`, `frame: LaneFrame`;
`lane_for(cell: Cell) -> Lane`; `Guest(op, project, system_id, lane, endpoint, key, probe, owned,
observed, scratch)`; `LaneTarget(project, allocation_id, system_id, observed, artifacts,
profile)` with `profile` defaulting to `{}`. `GuestIdentity` exists in `image_smoke.py`;
`catalog_profile`, `domain_xml`, `on_catalog_system`, `CatalogBody` in `scenario.py`;
`disk_absent` in `cleanup.py`; `RemoteHost`, `REMOTE_REPRESENTATIVES`, `remote_host`,
`remote_profile`, `staged_volume`, `volume_sha256`, `HOST_SSH_ENV` in `remote_lifecycle.py`.

Verification (all focused-test; green:
`uv run python -m pytest tests/integration/live_stack/test_tool_cells.py -q`):
- remote bindings — `test_remote_bindings_carry_the_provider_host`: with
  `remote=RemoteHost("operator@provider.example", "default", "rocky:10.2", "x86_64", "kvm")`,
  `digest=lambda *_: "f" * 64`, `volumes=lambda name: f"{name}.qcow2"`, the bound x86_64 remote
  cell gets `("rocky:10.2", "x86_64", "fedora:43", "x86_64", "kvm", "f" * 64)`; a remote ppc64le
  cell stays unbound; without `remote` no remote cell is bound (existing test). Red: `TypeError`
  (unexpected keyword `remote`).
- lane selection — `test_lane_for_selects_the_provider`: a local cell gives
  `provider == "local-libvirt"` and `entry.distro == "fedora"`; a remote cell with
  `REMOTE_PROVIDER_SSH` unset raises `ScenarioStop` `BLOCKED`. Red: `AttributeError`.
- remote skip — `test_remote_cell_is_not_skipped_by_control_plane_arch`: with
  `platform.machine` patched to `aarch64`, a remote x86_64 cell reaches `require_stack` (patched
  to raise a sentinel). Red: `pytest.skip` raised instead.
- memo per provider — `test_lane_target_prepares_once_per_provider`: one URL, a service cell and
  a local cell, two preparations; a second call per provider reuses. Red: one preparation.

Steps:
1. Write the four tests (code below), run, see the reds.
   ```python
   def test_remote_bindings_carry_the_provider_host() -> None:
       remote = _bound(_cell("systems.ssh_info", "remote-libvirt", "x86_64"))
       foreign = _bound(_cell("systems.ssh_info", "remote-libvirt", "ppc64le"))
       host = RemoteHost("operator@provider.example", "default", "rocky:10.2", "x86_64", "kvm")
       inputs = bindings(
           "a" * 40,
           host_os="ubuntu:26.04",
           host_arch="x86_64",
           matrix="b" * 64,
           cells=[remote, foreign],
           staged=lambda _name: None,
           remote=host,
           digest=lambda *_: "f" * 64,
           volumes=lambda name: f"{name}.qcow2",
       )
       assert set(inputs.cells) == {remote.id}
       bound = inputs.cells[remote.id]
       assert (bound.host_os, bound.host_arch, bound.guest_os, bound.guest_arch) == (
           "rocky:10.2",
           "x86_64",
           "fedora:43",
           "x86_64",
       )
       assert (bound.accelerator, bound.image_sha256) == ("kvm", "f" * 64)


   def test_lane_for_selects_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
       monkeypatch.setattr(tool_cells.platform, "machine", lambda: "x86_64")
       local = tool_cells.lane_for(_cell("systems.ssh_info", "local-libvirt", "x86_64"))
       assert (local.provider, local.entry.distro) == ("local-libvirt", "fedora")
       monkeypatch.delenv("REMOTE_PROVIDER_SSH", raising=False)
       with pytest.raises(ScenarioStop) as stop:
           tool_cells.lane_for(_cell("systems.ssh_info", "remote-libvirt", "x86_64"))
       assert stop.value.outcome is Outcome.BLOCKED


   def test_remote_cell_is_not_skipped_by_control_plane_arch(
       monkeypatch: pytest.MonkeyPatch,
   ) -> None:
       def stack() -> str:
           raise RuntimeError("stack read")

       monkeypatch.setattr(tool_cells, "require_stack", stack)
       monkeypatch.setattr(tool_cells.platform, "machine", lambda: "aarch64")

       async def never(*_: object) -> None:
           raise AssertionError("the scenario must not run")

       with pytest.raises(RuntimeError, match="stack read"):
           tool_cells.run_tool_cell(_cell("systems.ssh_info", "remote-libvirt", "x86_64"), never)


   def test_lane_target_prepares_once_per_provider(
       tmp_path: Path, monkeypatch: pytest.MonkeyPatch
   ) -> None:
       prepared: list[str] = []
       issuer = cast(OidcIssuer, object())

       async def prepare(run: CellRun, *_: object) -> object:
           prepared.append(run.cell.provider)
           return tool_cells.LaneTarget("cov-t", "alloc", "sys", {}, ())

       monkeypatch.setattr(tool_cells, "_TARGETS", {})
       monkeypatch.setattr(tool_cells, "_provision_target", prepare)
       local = _cell("systems.ssh_info", "local-libvirt", "x86_64")
       for cell in (_run(tmp_path, "authentication").cell, local, local):
           asyncio.run(
               tool_cells.lane_target(CellRun(cell, EvidenceWriter(tmp_path)), "u", issuer, "db")
           )
       assert prepared == ["service", "local-libvirt"]
   ```
   (`_cell(tool, provider, arch)` is the file's existing helper; import `RemoteHost` from
   `remote_lifecycle`.)
2. Implement in `tool_cells.py`:
   ```python
   # The remote representative family every remote provider cell of a guest architecture boots.
   REMOTE_LANE_FAMILIES = {"x86_64": "fedora"}


   class LaneFrame(Protocol):
       """A provider's System frame: provision, run ``body``, then prove owned cleanup."""

       def __call__(
           self,
           run: CellRun,
           base_url: str,
           issuer: OidcIssuer,
           db_url: str,
           *,
           project: str,
           body: CatalogBody,
           provision: Provision,
       ) -> Awaitable[None]: ...


   @dataclass(frozen=True)
   class Lane:
       """How one provider frames, profiles and observes a provider cell's System.

       ``xml`` and ``absent`` read the provider's own libvirt: the worker's for local-libvirt,
       the provider host's through the operator's observer for remote-libvirt.
       """

       provider: str
       entry: GuestIdentity
       profile: Callable[[str], dict[str, object]]
       xml: Callable[[str], str]
       absent: Callable[[str], bool]
       frame: LaneFrame


   def _local_lane() -> Lane:
       name, entry = lane_image()
       return Lane(
           "local-libvirt",
           entry,
           partial(catalog_profile, entry, name),
           domain_xml,
           disk_absent,
           partial(on_catalog_system, image=name),
       )


   def _remote_lane(arch: str) -> Lane:
       family = REMOTE_LANE_FAMILIES.get(arch)
       if family is None:
           raise ScenarioStop(Outcome.BLOCKED, f"no remote lane image for {arch}")
       image = REMOTE_REPRESENTATIVES[family]
       host = remote_host()
       volume = staged_base_volume(image)
       return Lane(
           "remote-libvirt",
           image,
           lambda _ref: remote_profile(image.arch, volume),
           partial(remote_xml, host.dest),
           partial(remote_volume_absent, host.dest),
           partial(on_remote_system, family=family),
       )


   def lane_for(cell: Cell) -> Lane:
       """The lane of provider cell ``cell``: its provider host when remote, else this host."""
       if cell.provider == "remote-libvirt":
           return _remote_lane(str(cell.guest_arch))
       return _local_lane()
   ```
   - `run_tool_cell`: `if cell.provider != "remote-libvirt" and cell.host_arch not in (None,
     platform.machine()):`; docstring adds "a remote cell's host is the provider host its frame
     observes".
   - `observe_guest(..., entry: GuestIdentity)`.
   - `Guest`: replace `image: str` and `entry: RootfsCatalogEntry` with `lane: Lane`.
   - `on_lane_system`: `lane = lane_for(run.cell)`; `observe_guest(..., lane.entry)`;
     `Guest(op, project, system_id, lane, endpoint, key, probe, owned, run.observed, root)`;
     `await lane.frame(run, base_url, issuer, db_url, project=project, body=framed,
     provision=provision)`.
   - `LaneTarget`: add `profile: Mapping[str, object] = field(default_factory=dict)` last.
   - `_TARGETS: dict[tuple[str, str], LaneTarget | _Failed]`, keyed
     `(base_url, run.cell.provider)` in `lane_target`.
   - `_provision_target`: `lane = lane_for(run.cell)`; observe with `lane.entry`; `await
     lane.frame(target, base_url, issuer, db_url, project=project, body=observe,
     provision=provision_catalog)`; return `LaneTarget(project, allocation, seen[0],
     dict(target.observed), (*target.artifacts, cleanup, summary),
     lane.profile(f"{project}-unread"))`.
   - `bindings(..., remote: RemoteHost | None = None, digest: Callable[[str, str, str], str] =
     volume_sha256, volumes: Callable[[str], str | None] = staged_volume)`; the per-cell
     `Context` build moves to `_lane_contexts(cells, host_os, host_arch, entry, digest, kernel)`;
     with `remote`, the bound remote cells whose `guest_arch == remote.host_arch` and whose
     architecture has a `REMOTE_LANE_FAMILIES` entry get
     `_lane_contexts(cells, remote.host_os, remote.host_arch, image, sha, kernel)`, `sha` being
     `digest(remote.dest, remote.pool, volume)` or `None` when `volumes(image.name)` is `None`.
   - `main`: `--remote` (`action="store_true"`, help names `REMOTE_PROVIDER_SSH`); on it,
     `remote_host()`, printing `cannot bind the remote cells: <reason>` and returning 2 on
     `ScenarioStop`. The unstaged hint names both lane images.
   - Module docstring: the provider lanes and `--remote`.
3. Green command, `just lint`, `just type`; commit
   `test(live-stack): run provider-lane tool cells on remote-libvirt`.

## Task 4 — the carrier and its bindings

Files: `tests/integration/test_system_tool_cells_live.py`,
`scripts/coverage_campaign/obligations.toml`, `tests/scripts/test_coverage_contract.py`.

Verification:
- remote scenarios bound — Mode: focused-test. In the binding test replace
  `assert {c.node_id for c in systems if c.provider == "remote-libvirt"} == {None}` with
  `remote = [c for c in systems if c.provider == "remote-libvirt"]` and
  `assert len(remote) == 240 and {c.node_id for c in remote} == {_SYSTEM_NODE}`. Red: `{None}`.
  Green: `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.
- carrier collection — Mode: focused-test.
  `uv run python -m pytest tests/integration/test_system_tool_cells_live.py --collect-only -q`
  lists 240 items on x86_64 (120 local, 120 remote). Red before `_cells` changes: 120.
- remote cell behaviour — Mode: task-test-not-applicable: every body acts only against a live
  stack and provider host; proven by the Task 5 run and `qualify`.

Steps:
1. Edit the binding test; run; red.
2. Add, in sorted position (after the `tool/ops.tool_trail/...` rows), for each tool in
   `systems.authorize_ssh_key`, `systems.check_ssh_reachable`, `systems.provision`,
   `systems.reprovision`, `systems.ssh_info`, `systems.teardown` and each kind in
   `authentication`, `authorization`, `functional`, `project-isolation`, `validation`:
   `"tool/remote-libvirt/<tool>/default/<kind>" =
   "tests/integration/test_system_tool_cells_live.py::test_system_tool_cell"`.
3. In the carrier:
   - `_HOSTFWD = re.compile(r"hostfwd=tcp:([^:,\s]+):(\d+)-:22")`; `_ssh_info` reads
     `xml = guest.lane.xml(guest.system_id)` and asserts
     `(coords.get("host"), coords.get("port")) == (forwarded.group(1), int(forwarded.group(2)))`.
   - `_defined(xml: Callable[[str], str], system_id: str) -> bool` calls `xml(system_id)`.
   - `_provision` parses `guest.lane.xml(guest.system_id)`.
   - `_reprovision`: `profile = guest.lane.profile(f"{guest.project}-unread")`, new disks from
     `domain_disks(guest.lane.xml(guest.system_id))`, `os_matches(guest.lane.entry, probe)`.
   - `_teardown`: `_defined(guest.lane.xml, guest.system_id)` and
     `guest.lane.absent(path)`.
   - `_target_args`: `profile = dict(target.profile)`; drop `lane_image`, `catalog_profile`,
     `domain_xml` imports.
   - `_cells`: local cells with `guest_arch == platform.machine()` plus remote cells with
     `guest_arch in REMOTE_LANE_FAMILIES`.
   - Module docstring names both providers and the remote runbook.
4. Run both green commands, `just lint`, `just type`, then
   `uv run python -m pytest tests/integration/live_stack -q`; commit
   `test(live-stack): carry the x86_64 remote System tool cells`.

## Task 5 — runbook and live proof

File: `docs/operating/runbooks/remote-live-stack.md` (new §8).

Verification: runbook — Mode: task-test-not-applicable: prose; `just docs-check` covers links.
Live cells — Mode: task-test-not-applicable: proven by the run below.

Steps:
1. Write §8: the carrier, the lane image, the extra prerequisite over §7 (the
   `fedora-kdive-remote-base-43` `[[image]]`, `ssh_addr`/`ssh_range` reachable from the control
   plane), the commands (`tool_cells bindings --remote`, pytest `-k remote-libvirt`, assemble,
   qualify) per configuration, cleanup, and a "Last run" placeholder replaced in step 4.
2. Commit; lab: control plane and a separate x86_64 provider host (Fedora or Rocky) on the
   operator-approved Proxmox lab; provider prepared per §7; the control-plane stack at the
   committed candidate (`demo-up.sh`; recovery with `KDIVE_WORKER_DEATH_VERIFIER=docker`),
   worker `/readyz` ready.
3. Per configuration: write bindings with `--remote`, run
   `pytest -m live_stack tests/integration/test_system_tool_cells_live.py -k remote-libvirt`,
   assemble, `qualify`. Record per-cell outcomes; blocked or failing cells are reported as such.
4. Fill "Last run" (candidate, topology without identifiers, lab workarounds for #3083 and #3093
   as non-product steps, outcomes); commit; wipe the stack and check the provider has no
   `kdive-*` domain or overlay left; remove lab scratch refs.
