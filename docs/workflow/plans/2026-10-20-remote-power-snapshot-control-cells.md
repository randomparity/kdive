# Remote power, snapshot and control tool cells (#3116) — implementation plan

Goal: bind and prove the 160 x86_64 remote-libvirt cells of the eight power, snapshot, traffic,
SysRq and crash-watch tools by running the merged local drivers on the remote provider lane.

Architecture: the provider `Lane` (`tests/integration/live_stack/tool_cells.py`) gains the three
provider-specific observations the local drivers hard-code (native libvirt connection, capture
spool path, installed-kernel observer). The power/snapshot and control scenario modules read them
from `guest.lane`; the two existing carriers parametrize the remote cells too; the control carrier
gets a remote rejection target. Spec:
[2026-10-20-remote-power-snapshot-control-cells-design.md](../specs/2026-10-20-remote-power-snapshot-control-cells-design.md).

Tech stack: Python 3.14, pytest, libvirt-python, `uv`, `just`.

Expected implementation size: 450–650 changed lines (L) — the file map below: four helper modules
(~120), two carriers (~40), 40 binding rows, five unit-test files (~250), one runbook section (~60).

## Global Constraints

- No product source (`src/`), ADR, migration or `contract.py` change; the matrix digest moves
  only by the new bindings. ADR slot 0793 stays unused.
- Test-only env inputs stay unprefixed (`REMOTE_PROVIDER_SSH`); never add a `KDIVE_` variable.
- Local-libvirt cells keep their observed values: the moved `domain_kernel`, `worker_connect` and
  `pcap_dir` spool are the same functions or values the local drivers use today.
- Evidence never carries the provider destination, pool name or a host path beyond what the
  existing remote frame already records (ADR-0715).
- Ruff line length 100, lint `E,F,I,UP,B,SIM`; `just type` is whole-tree; prose avoids
  "critical", "robust", "comprehensive", "elegant".
- Guardrails per task: `just lint`, `just type`, the named focused tests; `just coverage-check`
  after Task 4; `just docs-links docs-paths` after Task 5. The pre-push `just ci` runs once at
  delivery.

## File map

| File | Today | After |
|---|---|---|
| `tests/integration/live_stack/tool_cells.py` | `Lane` with xml/absent | + `connect`, `spool`, `installed_kernel`; owns `worker_connect`, `remote_pcap_path`, `domain_kernel` (moved) |
| `tests/integration/live_stack/power_snapshots.py` | local native reads | native reads take `connect`; kernel from lane |
| `tests/integration/live_stack/capture_cells.py` | `boot` with local kernel | `boot` with lane kernel |
| `tests/integration/live_stack/control_cells.py` | local filters, spool, gateway | lane `connect`/`spool`; remote gateway; `run_target`, `target_state` |
| `tests/integration/test_power_snapshot_tool_cells_live.py` | local cells | + remote x86_64 cells |
| `tests/integration/test_control_tool_cells_live.py` | local cells | + remote x86_64 cells, remote target |
| `scripts/coverage_campaign/obligations.toml` | local rows | + 40 remote rows |
| `tests/scripts/test_coverage_contract.py` | remote power/control unbound | remote bound |
| `docs/operating/runbooks/remote-live-stack.md` | §1–§10 | + §11 |

No compatibility path is retained: `power_snapshots.domain_kernel` moves to `tool_cells` and its
one importer (`capture_cells`) migrates in Task 3.

## Task 1 — Lane fields

Files: modify `tests/integration/live_stack/tool_cells.py`,
`tests/integration/live_stack/power_snapshots.py` (remove the moved `domain_kernel`),
`tests/integration/live_stack/capture_cells.py` (its `domain_kernel` import); test
`tests/integration/live_stack/test_tool_cells.py`.

Interfaces (later tasks rely on):

- `tool_cells.worker_connect() -> libvirt.virConnect`
- `tool_cells.remote_pcap_path(dest: str, pool: str, system_id: str, job_id: str) -> str`
- `tool_cells.domain_kernel(system_id: str, _endpoint: Endpoint, _key: Path, _release: str) -> tuple[str, str]`
- `Lane.connect: Callable[[], libvirt.virConnect]`, `Lane.spool: Callable[[str, str], str]`,
  `Lane.installed_kernel: InstalledKernel` (`deep_lifecycle.InstalledKernel`)

Verification:

- Contract: remote pcap path. Mode: focused-test.
  `test_tool_cells.py::test_remote_pcap_path_joins_the_pool_target` and
  `::test_remote_pcap_path_needs_a_pool_target`; red: `AttributeError` (no
  `remote_pcap_path`); green:
  `uv run python -m pytest tests/integration/live_stack/test_tool_cells.py -q -k pcap`.
- Contract: lane fields. Mode: focused-test.
  `test_tool_cells.py::test_local_lane_observes_the_worker` and
  `::test_remote_lane_observes_the_provider_host`; red: `AttributeError: 'Lane' object has no
  attribute 'connect'`; green:
  `uv run python -m pytest tests/integration/live_stack/test_tool_cells.py -q -k "lane_observes"`.

Steps:

1. Add to `test_tool_cells.py` (new imports: `from functools import partial`,
   `from uuid import UUID`, `from kdive.providers.ports.traffic import pcap_volume_name`,
   `from kdive.providers.shared.runtime_paths import pcap_dir`,
   `from tests.integration.live_stack.remote_lifecycle import guest_boot_kernel`; `RemoteHost`,
   `tool_cells`, `pytest` and `_cell` are already there):

   ```python
   class _Pool:
       def __init__(self, xml: str) -> None:
           self.xml = xml

       def XMLDesc(self, _flags: int) -> str:  # noqa: N802 - libvirt binding name
           return self.xml


   class _PoolConn:
       def __init__(self, xml: str) -> None:
           self.pool, self.closed, self.named = _Pool(xml), False, ""

       def storagePoolLookupByName(self, name: str) -> _Pool:  # noqa: N802
           self.named = name
           return self.pool

       def close(self) -> None:
           self.closed = True


   _SYSTEM, _JOB = str(UUID(int=1)), str(UUID(int=2))


   def test_remote_pcap_path_joins_the_pool_target(monkeypatch: pytest.MonkeyPatch) -> None:
       conn = _PoolConn("<pool><target><path>/srv/pool</path></target></pool>")
       monkeypatch.setattr(tool_cells, "observer", lambda _dest: conn)
       path = tool_cells.remote_pcap_path("op@provider.example", "kdive", _SYSTEM, _JOB)
       assert path == f"/srv/pool/{pcap_volume_name(UUID(_SYSTEM), UUID(_JOB))}"
       assert (conn.named, conn.closed) == ("kdive", True)


   def test_remote_pcap_path_needs_a_pool_target(monkeypatch: pytest.MonkeyPatch) -> None:
       conn = _PoolConn("<pool><target/></pool>")
       monkeypatch.setattr(tool_cells, "observer", lambda _dest: conn)
       with pytest.raises(AssertionError, match="no target path"):
           tool_cells.remote_pcap_path("op@provider.example", "kdive", _SYSTEM, _JOB)
       assert conn.closed


   def test_local_lane_observes_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
       monkeypatch.setattr(tool_cells.platform, "machine", lambda: "x86_64")
       lane = tool_cells.lane_for(_cell("control.power", "local-libvirt", "x86_64"))
       assert lane.connect is tool_cells.worker_connect
       assert lane.spool(_SYSTEM, _JOB) == str(pcap_dir(UUID(_SYSTEM)))
       assert lane.installed_kernel is tool_cells.domain_kernel


   def test_remote_lane_observes_the_provider_host(monkeypatch: pytest.MonkeyPatch) -> None:
       host = RemoteHost("op@provider.example", "kdive", "rocky:10.2", "x86_64", "kvm")
       monkeypatch.setattr(tool_cells, "remote_host", lambda: host)
       monkeypatch.setattr(tool_cells, "staged_base_volume", lambda _image: "base.qcow2")
       seen: list[str] = []
       monkeypatch.setattr(tool_cells, "observer", lambda dest: seen.append(dest) or "conn")
       lane = tool_cells.lane_for(_cell("control.power", "remote-libvirt", "x86_64"))
       assert lane.connect() == "conn" and seen == ["op@provider.example"]
       spool = lane.spool
       assert isinstance(spool, partial) and spool.func is tool_cells.remote_pcap_path
       assert spool.args == ("op@provider.example", "kdive")
       kernel = lane.installed_kernel
       assert isinstance(kernel, partial) and kernel.func is guest_boot_kernel
       assert kernel.keywords == {"family": "fedora"}
   ```

   Run both green commands; expect `AttributeError` as stated.
2. In `tool_cells.py`:
   - imports: `import libvirt`; `from pathlib import PurePosixPath` (beside `Path`);
     `from uuid import UUID`;
     `import xml.etree.ElementTree as ET  # noqa: S405 - libvirt XML  # nosec B405`;
     `from kdive.providers.ports.traffic import pcap_volume_name`;
     `from kdive.providers.shared.runtime_paths import pcap_dir`; add `InstalledKernel` to the
     `deep_lifecycle` import; add `guest_boot_kernel, observer` to the `remote_lifecycle`
     import; `from tests.integration.live_stack.spine import worker_libvirt_uri`.
   - before `class Lane`:

     ```python
     def worker_connect() -> libvirt.virConnect:
         """The worker's own libvirt: the local lane's provider."""
         return libvirt.open(worker_libvirt_uri())


     def domain_kernel(
         system_id: str, _endpoint: Endpoint, _key: Path, _release: str
     ) -> tuple[str, str]:
         """The ``<os><kernel>`` file the domain boots (the install's staged kernel) and its digest."""
         kernel = ET.fromstring(domain_xml(system_id)).findtext("./os/kernel")  # noqa: S314  # nosec B314
         assert kernel, "the installed domain names no direct kernel"
         return file_sha256(kernel), kernel


     def _local_spool(system_id: str, _job_id: str) -> str:
         return str(pcap_dir(UUID(system_id)))


     def remote_pcap_path(dest: str, pool: str, system_id: str, job_id: str) -> str:
         """The provider-host path of the pcap volume capture job ``job_id`` writes into ``pool``.

         The remote capturer writes a per-job volume into the configured storage pool (ADR-0432),
         so the System owns it until the job reclaims it.
         """
         conn = observer(dest)
         try:
             xml = conn.storagePoolLookupByName(pool).XMLDesc(0)
         finally:
             conn.close()
         target = ET.fromstring(xml).findtext("./target/path")  # noqa: S314  # nosec B314
         assert target, f"storage pool {pool} has no target path"
         return str(PurePosixPath(target) / pcap_volume_name(UUID(system_id), UUID(job_id)))
     ```

   - `Lane`: append three fields after `frame`
     (`connect: Callable[[], libvirt.virConnect]`, `spool: Callable[[str, str], str]`,
     `installed_kernel: InstalledKernel`) and extend its docstring: "``connect`` opens that
     libvirt for a native read; ``spool(system, job)`` is the provider-host path a traffic
     capture writes (the System's directory locally, the job's pool volume remotely);
     ``installed_kernel`` observes the kernel an install put in place (the domain's direct
     kernel locally, the guest's own ``/boot`` remotely)."
   - `_local_lane` adds the arguments `worker_connect, _local_spool, domain_kernel` after the
     frame; `_remote_lane` adds `partial(observer, host.dest)`,
     `partial(remote_pcap_path, host.dest, host.pool)` and
     `partial(guest_boot_kernel, family=family)` after the frame.
   - In `power_snapshots.py` delete `domain_kernel` and the imports only it used (`ET`,
     `file_sha256`, `domain_xml`); in `capture_cells.py` import `domain_kernel` from `tool_cells`
     instead of `power_snapshots` (Task 3 replaces that use with the lane's observer).
3. Run both green commands; expect all pass. Break the pool join once (append `/ "x"`), see
   `test_remote_pcap_path_joins_the_pool_target` red, revert. `just lint`, `just type` clean.
4. Commit: `test(live): give the provider lane native, spool and kernel observers`.

Acceptance: both lanes expose the three fields; local values are the same functions as before.

## Task 2 — Power and snapshot cells read through the lane

Files: modify `tests/integration/live_stack/power_snapshots.py`,
`tests/integration/test_power_snapshot_tool_cells_live.py`; test
`tests/integration/live_stack/test_power_snapshots.py`.

Interfaces: consumes `Lane.connect`, `Lane.installed_kernel`, `worker_connect`, `lane_for`,
`REMOTE_LANE_FAMILIES` (`tool_cells`). Provides
`domain_state(system_id: str, connect: Connect) -> str`,
`checkpoints(system_id: str, connect: Connect) -> list[str]`,
`domain_defined(system_id: str, connect: Connect = worker_connect) -> bool`,
`protected_state(db_url: str, target: LaneTarget, connect: Connect) -> dict[str, object]`,
`snapshot_of(db_url: str, target: LaneTarget, connect: Connect) -> Callable[[], Awaitable[object]]`,
where `Connect = Callable[[], libvirt.virConnect]`.

Verification:

- Contract: native reads use the given connection and close it. Mode: focused-test.
  `test_power_snapshots.py::test_native_reads_use_the_lane_connection`; red: `TypeError`
  (unexpected argument); green:
  `uv run python -m pytest tests/integration/live_stack/test_power_snapshots.py -q`.
- Contract: protected state reads definedness through the lane. Mode: focused-test.
  `::test_protected_state_adds_the_target_jobs_and_domain` (updated); red: the stub asserting the
  passed `connect` fails; same green command.
- Contract: the functional boot uses the lane's kernel observer. Mode: focused-test.
  `::test_functional_boots_with_the_lane_kernel`; red: `deep_body` receives `domain_kernel`
  instead of the stub; same green command.
- Contract: the carrier parametrizes the remote x86_64 cells. Mode: focused-test, in Task 4's
  `test_coverage_contract.py` (binding) plus `--collect-only` below.

Steps:

1. Add to `test_power_snapshots.py` (new import: `import libvirt`):

   ```python
   class _Snap:
       def __init__(self, name: str) -> None:
           self.name = name

       def getName(self) -> str:  # noqa: N802 - libvirt binding name
           return self.name


   class _Dom:
       def state(self) -> list[int]:
           return [libvirt.VIR_DOMAIN_PAUSED, 0]

       def listAllSnapshots(self) -> list[_Snap]:  # noqa: N802
           return [_Snap("b"), _Snap("a")]


   class _Conn:
       def __init__(self) -> None:
           self.closed = 0

       def lookupByName(self, _name: str) -> _Dom:  # noqa: N802
           return _Dom()

       def close(self) -> None:
           self.closed += 1


   def test_native_reads_use_the_lane_connection() -> None:
       conn = _Conn()
       system = "11111111-1111-1111-1111-111111111111"
       assert power_snapshots.domain_state(system, lambda: conn) == "paused"
       assert power_snapshots.checkpoints(system, lambda: conn) == ["a", "b"]
       assert power_snapshots.domain_defined(system, lambda: conn)
       assert conn.closed == 3
   ```

   Update `test_protected_state_adds_the_target_jobs_and_domain`: replace the `domain_defined`
   stub with one that records its arguments, call
   `power_snapshots.protected_state("db", _TARGET, connect)` with `connect = object()` cast as
   needed, and assert the stub saw `(_TARGET.system_id, connect)`. Update the three
   `test_domain_defined_*` tests to pass `lambda: conn` (or the refusing callable) as `connect`
   instead of monkeypatching `libvirt.open`/`worker_libvirt_uri`.

   Add:

   ```python
   def test_functional_boots_with_the_lane_kernel(
       tmp_path: Path, monkeypatch: pytest.MonkeyPatch
   ) -> None:
       cell = next(
           c
           for c in build_contract().cells
           if c.operation == "control.power" and c.kind == "functional"
       )
       seen: list[object] = []

       def kernel(*_: object) -> tuple[str, None]:
           return "d" * 64, None

       async def deep(*_: object, installed_kernel: object, **__: object) -> str:
           seen.append(installed_kernel)
           return "run"

       async def endpoint(*_: object) -> power_snapshots.Endpoint:
           return power_snapshots.Endpoint("h", 22)

       async def body(_s: object) -> dict[str, object]:
           return {}

       monkeypatch.setattr(power_snapshots, "deep_body", deep)
       monkeypatch.setattr(power_snapshots, "ssh_endpoint", endpoint)
       monkeypatch.setitem(power_snapshots.BODIES, "control.power", body)
       lane = type("L", (), {"entry": None, "installed_kernel": kernel})()
       guest = type(
           "G",
           (),
           {
               "op": None,
               "system_id": "s",
               "owned": [],
               "project": "p",
               "lane": lane,
               "scratch": tmp_path,
           },
       )()
       run = CellRun(cell, EvidenceWriter(tmp_path))
       asyncio.run(
           power_snapshots.functional(
               run, cast(HttpCaller, None), "t", tmp_path, {}, cast(Guest, guest)
           )
       )
       assert seen == [kernel]
   ```

   Run the green command; expect the three named tests to fail as stated.
2. In `power_snapshots.py`:
   - import `worker_connect` from `tool_cells`; drop the `worker_libvirt_uri` import;
     add `Connect = Callable[[], libvirt.virConnect]`.
   - replace `_domain`, `domain_state`, `checkpoints`, `domain_defined`:

     ```python
     def _domain(system_id: str, connect: Connect) -> tuple[libvirt.virConnect, libvirt.virDomain]:
         conn = connect()
         try:
             return conn, conn.lookupByName(domain_name_for(UUID(system_id)))
         except libvirt.libvirtError:
             conn.close()
             raise


     def domain_state(system_id: str, connect: Connect) -> str:
         """The provider's libvirt state of the System's domain: running, paused or shutoff."""
         conn, domain = _domain(system_id, connect)
         try:
             state = domain.state()[0]
         finally:
             conn.close()
         return _STATES.get(state, f"state-{state}")


     def checkpoints(system_id: str, connect: Connect) -> list[str]:
         """The domain's native snapshot names, sorted by name."""
         conn, domain = _domain(system_id, connect)
         try:
             return sorted(snap.getName() for snap in domain.listAllSnapshots())
         finally:
             conn.close()


     def domain_defined(system_id: str, connect: Connect = worker_connect) -> bool:
         """Whether the provider's libvirt defines the domain: absent only on VIR_ERR_NO_DOMAIN."""
         conn = connect()
         try:
             conn.lookupByName(domain_name_for(UUID(system_id)))
         except libvirt.libvirtError as exc:
             if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                 raise
             return False
         finally:
             conn.close()
         return True
     ```
   - every in-module call passes the guest's lane: `domain_state(s.guest.system_id,
     s.guest.lane.connect)` in `_memory_restore` (twice) and `power_body` (twice), and
     `checkpoints(self.guest.system_id, self.guest.lane.connect)` in `Session.snapshot` and
     `Session.delete`, `checkpoints(s.guest.system_id, s.guest.lane.connect)` in `list_body`.
   - `functional`: `installed_kernel=guest.lane.installed_kernel`.
   - `protected_state(db_url, target, connect)` passes `connect` to
     `asyncio.to_thread(domain_defined, target.system_id, connect)`;
     `snapshot_of(db_url, target, connect)` returns `partial(protected_state, db_url, target,
     connect)`.
   - module docstring: "Native observations read the provider's libvirt (the lane's
     ``connect``): the worker's locally, the provider host's through the operator's observer
     remotely (#3116)."
3. In `test_power_snapshot_tool_cells_live.py`:
   - import `REMOTE_LANE_FAMILIES, lane_for` from `tool_cells`;
   - `_fixture(arch: str)` loads `load_fixture(Path(root), BASELINE, arch)`; the functional
     branch calls `_fixture(str(run.cell.guest_arch))`;
   - the rejection branch: `snapshot_of(db_url, target, lane_for(run.cell).connect)`;
   - `_cells()` keeps local native cells and adds
     `or (c.provider == "remote-libvirt" and c.guest_arch in REMOTE_LANE_FAMILIES)`;
   - docstring: replace "The remote cells are #3116's" with "and the x86_64 remote-libvirt cells
     (#3116), observed on the provider host"; add a `remote-live-stack.md` pointer.
4. Run the green command; expect pass. Run
   `uv run python -m pytest tests/integration/test_power_snapshot_tool_cells_live.py --collect-only -q -m live_stack`
   on this x86_64 host; expect `200 tests collected` (100 local, 100 remote).
5. `just lint`, `just type`. Commit: `test(live): run the power and snapshot cells on the remote lane`.

Acceptance: no `worker_libvirt_uri` use remains in `power_snapshots.py`; remote cells collect.

## Task 3 — Control cells on the remote lane

Files: modify `tests/integration/live_stack/capture_cells.py`,
`tests/integration/live_stack/control_cells.py`,
`tests/integration/test_control_tool_cells_live.py`; test
`tests/integration/live_stack/test_control_cells.py`.

Interfaces: consumes `Lane.connect`, `Lane.spool`, `Lane.installed_kernel`, `domain_defined`
(Task 2), `capture_cells.boot`, `capture_cells.CaptureTarget`, `capture_cells.named_jobs`,
`tool_cells.on_lane_system`, `tool_cells.settled`, `tool_cells.project_state`, `lane_for`.
Provides `control_cells.capture_filters(system_id: str, connect: Connect) -> list[str]`,
`control_cells.gateway_of(probe: Mapping[str, str]) -> str`,
`control_cells.run_target(run, base_url, issuer, db_url, fixture) -> CaptureTarget`,
`control_cells.target_state(db_url, target, connect) -> Snapshot`.

Verification:

- Contract: `boot` uses the lane's kernel observer. Mode: focused-test.
  `test_control_cells.py::test_boot_uses_the_lane_kernel`; red: `deep_body` receives
  `domain_kernel`; green: `uv run python -m pytest tests/integration/live_stack/test_control_cells.py -q`.
- Contract: remote flow gateway. Mode: focused-test. `::test_gateway_is_an_ipv4_address`; red:
  `AttributeError` (no `gateway_of`); same command.
- Contract: run target memo and replay. Mode: focused-test.
  `::test_run_target_prepares_once_per_provider_and_replays`; red: `AttributeError`; same command.
- Contract: kept context keys. Mode: focused-test. `::test_run_target_keeps_host_and_lane_keys`;
  red: `AttributeError`; same command.
- Contract: remote protected state. Mode: focused-test.
  `::test_target_state_reads_jobs_and_domain_through_the_lane`; red: `AttributeError`; same command.
- Contract: capture filters through the lane. Mode: task-test-not-applicable — `capture_filters`
  wraps `libvirt_qemu.qemuMonitorCommand` on a live domain; a fake connection would only restate
  the call; the live capture cells observe it.

Steps:

1. Add to `test_control_cells.py` (imports `asyncio`, `Path`, `capture_cells`, `CellRun`,
   `EvidenceWriter` from `tests.integration.live_stack.evidence`, `ScenarioStop` from
   `tests.integration.live_stack.scenario`, `Outcome` from `scripts.coverage_campaign.evidence`,
   `Cell` from `scripts.coverage_campaign.contract`, `Guest` from
   `tests.integration.live_stack.tool_cells`, `OidcIssuer` from `kdive.mcp.dev_harness`, and
   `Any` beside `cast` from `typing`; `build_contract` is already imported):

   ```python
   def _functional_cell(provider: str) -> Cell:
       return next(
           c
           for c in build_contract().cells
           if c.operation == "control.watch_for_crash"
           and c.kind == "functional"
           and c.provider == provider
           and c.guest_arch == "x86_64"
       )


   def test_boot_uses_the_lane_kernel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
       seen: list[object] = []

       def kernel(*_: object) -> tuple[str, None]:
           return "d" * 64, None

       async def deep(*_: object, installed_kernel: object, **__: object) -> str:
           seen.append(installed_kernel)
           return "run-1"

       async def endpoint(*_: object) -> object:
           return "endpoint"

       monkeypatch.setattr(capture_cells, "deep_body", deep)
       monkeypatch.setattr(capture_cells, "ssh_endpoint", endpoint)
       lane = type("L", (), {"entry": None, "installed_kernel": kernel})()
       guest = type(
           "G",
           (),
           {
               "op": None,
               "system_id": "s",
               "owned": [],
               "project": "p",
               "lane": lane,
               "scratch": tmp_path,
           },
       )()
       run = CellRun(_functional_cell("remote-libvirt"), EvidenceWriter(tmp_path))
       assert asyncio.run(capture_cells.boot(run, cast(Guest, guest), tmp_path, {})) == (
           "run-1",
           "endpoint",
       )
       assert seen == [kernel]


   @pytest.mark.parametrize(
       ("probe", "ok"),
       [
           ({"gateway": "192.168.122.1"}, True),
           ({"gateway": ""}, False),
           ({"gateway": "fe80::1"}, False),
           ({"gateway": "10.0.2.2"}, False),
           ({}, False),
       ],
   )
   def test_gateway_is_an_ipv4_address(probe: dict[str, str], ok: bool) -> None:
       if ok:
           assert control_cells.gateway_of(probe) == probe["gateway"]
       else:
           with pytest.raises(AssertionError, match="libvirt-network gateway"):
               control_cells.gateway_of(probe)


   _RUN_TARGET = CaptureTarget("cov-t", "sys", "run", "", {"host_os": "rocky:10.2"}, ("a" * 64,))


   def test_run_target_prepares_once_per_provider_and_replays(
       tmp_path: Path, monkeypatch: pytest.MonkeyPatch
   ) -> None:
       calls: list[str] = []
       issuer = cast(OidcIssuer, object())

       async def prepare(run: CellRun, *rest: object) -> CaptureTarget:
           calls.append(run.cell.provider)
           if run.cell.provider == "local-libvirt":
               cast(CellRun, rest[-1]).artifacts.append("b" * 64)  # the scratch record
               raise ScenarioStop(Outcome.BLOCKED, "no lane")
           return _RUN_TARGET

       monkeypatch.setattr(control_cells, "_TARGETS", {})
       monkeypatch.setattr(control_cells, "_prepare_run_target", prepare)
       remote = CellRun(_functional_cell("remote-libvirt"), EvidenceWriter(tmp_path))
       for _ in range(2):
           target = asyncio.run(control_cells.run_target(remote, "u", issuer, "db", lambda: None))
           assert target is _RUN_TARGET
       local = CellRun(_functional_cell("local-libvirt"), EvidenceWriter(tmp_path))
       for _ in range(2):
           with pytest.raises(ScenarioStop):
               asyncio.run(control_cells.run_target(local, "u", issuer, "db", lambda: None))
       assert calls == ["remote-libvirt", "local-libvirt"]
       assert local.artifacts.count("b" * 64) == 2
       assert remote.observed["host_os"] == "rocky:10.2"


   def test_run_target_keeps_host_and_lane_keys() -> None:
       observed = {
           "host_os": "rocky:10.2",
           "host_arch": "x86_64",
           "guest_os": "fedora:43",
           "guest_arch": "x86_64",
           "accelerator": "kvm",
           "image_sha256": "f" * 64,
           "kernel_sha256": "c" * 64,
       }
       kept = control_cells.target_keys(observed)
       assert "kernel_sha256" not in kept and kept["host_os"] == "rocky:10.2"
       assert len(kept) == 6


   def test_target_state_reads_jobs_and_domain_through_the_lane(
       monkeypatch: pytest.MonkeyPatch,
   ) -> None:
       connect = object()
       seen: list[object] = []

       async def project_state(_db: str, project: str) -> dict[str, list[object]]:
           seen.append(project)
           return {}

       async def named_jobs(_db: str, *ids: str) -> list[object]:
           seen.append(ids)
           return [0, ""]

       def defined(system_id: str, conn: object) -> bool:
           seen.append((system_id, conn))
           return False

       monkeypatch.setattr(control_cells, "project_state", project_state)
       monkeypatch.setattr(control_cells, "named_jobs", named_jobs)
       monkeypatch.setattr(control_cells, "domain_defined", defined)
       snapshot = control_cells.target_state("db", _TARGET, cast(Any, connect))
       state = asyncio.run(snapshot())
       assert state["domain_defined"] is False
       assert seen == ["cov-t", (_TARGET.system_id, _TARGET.run_id), (_TARGET.system_id, connect)]
   ```

   Run the green command; expect the named tests to fail as stated.
2. `capture_cells.boot`: `installed_kernel=guest.lane.installed_kernel`; drop the
   `domain_kernel` import Task 1 pointed at `tool_cells`.
3. `control_cells.py`:
   - imports: `ipaddress`; `from dataclasses import dataclass`; `from collections.abc import
     Mapping` (present); `libvirt` (present); `from kdive.mcp.dev_harness import OidcIssuer`;
     `from tests.integration.live_stack.capture_cells import CaptureTarget, Session, boot,
     drained, named_jobs`; `from tests.integration.live_stack.power_snapshots import
     domain_defined, grants`; `from tests.integration.live_stack.scenario import CellRun,
     ScenarioStop`; `from tests.integration.live_stack.tool_cells import Boundary, Guest,
     HttpCaller, Rejection, Snapshot, on_lane_system, project_state, settled`; drop `pcap_dir`
     and `worker_libvirt_uri`; `Connect = Callable[[], libvirt.virConnect]`.
   - constants: `_ROUTE = "printf 'gateway=%s\\n' \"$(ip -4 route show default | awk '$3 !~ /^10[.]0[.]2[.]/ {print $3; exit}')\""`
     (the first default route whose gateway is outside the user-mode subnet) and
     `_SLIRP = ipaddress.ip_network("10.0.2.0/24")`
     and `_TARGET_KEYS = ("host_os", "host_arch", "guest_os", "guest_arch", "accelerator",
     "image_sha256")`.
   - functions:

     ```python
     def gateway_of(probe: Mapping[str, str]) -> str:
         """The guest's libvirt-network IPv4 default gateway from ``_ROUTE``'s answer.

         The user-mode SSH NIC's gateway is refused: that NIC is not the captured netdev.
         """
         value = probe.get("gateway", "")
         try:
             address = ipaddress.ip_address(value)
             assert address.version == 4 and address not in _SLIRP
         except AssertionError, ValueError:
             raise AssertionError(f"the guest has no libvirt-network gateway ({value!r})") from None
         return value


     def capture_filters(system_id: str, connect: Connect) -> list[str]:
         """The QOM objects named ``kdive-dump-*`` on the System's domain."""
         conn = connect()
         ...  # body unchanged below the open
     ```
   - `capture_body`:

     ```python
         marker = token()
         local = s.guest.lane.provider == "local-libvirt"
         # A remote capture taps the libvirt-network NIC, whose default route this is (ADR-0432).
         gateway = _GATEWAY if local else gateway_of(await s.shell(_ROUTE))
         flow = _FLOW.format(token=marker, gateway=gateway, port=FLOW_PORT)
         pid = await _background(s, flow)
         try:
             env = await s.exposed(run_id=s.run_id, duration_s=_CAPTURE_S)
             s.guest.owned.append(s.guest.lane.spool(s.guest.system_id, env.object_id))
             job = await drained(s.guest.op, "capture", env.object_id)
         finally:
             await _stop(s, pid)
         ...
         filters = await asyncio.to_thread(capture_filters, s.guest.system_id, s.guest.lane.connect)
     ```
   - rejection target:

     ```python
     def target_keys(observed: Mapping[str, object]) -> dict[str, object]:
         """The provider-host and lane context a rejection record carries; no kernel field."""
         return {key: observed[key] for key in _TARGET_KEYS if key in observed}


     @dataclass(frozen=True)
     class _Failed:
         error: Exception
         artifacts: tuple[str, ...]


     _TARGETS: dict[tuple[str, str], CaptureTarget | _Failed] = {}


     async def _prepare_run_target(
         run: CellRun,
         base_url: str,
         issuer: OidcIssuer,
         db_url: str,
         fixture: Callable[[], tuple[Path, dict[str, Any]]],
         scratch: CellRun,
     ) -> CaptureTarget:
         tree, manifest = fixture()
         project = f"cov-{secrets.token_hex(4)}"
         ids: dict[str, str] = {}

         async def body(guest: Guest) -> dict[str, object]:
             run_id, _endpoint = await boot(scratch, guest, tree, manifest)
             ids.update(system=guest.system_id, run=run_id)
             return {"run_target": "prepared"}

         await on_lane_system(scratch, base_url, issuer, db_url, project=project, body=body)
         await settled(db_url, project)
         summary = run.writer.artifact(
             {
                 "run_target": {"system": "torn_down", "run": "booted"},
                 "cleanup": scratch.assertions["cleanup"],
             }
         )
         return CaptureTarget(
             project,
             ids["system"],
             ids["run"],
             "",
             target_keys(scratch.observed),
             (*scratch.artifacts, *scratch.assertions.values(), summary),
         )


     async def run_target(
         run: CellRun,
         base_url: str,
         issuer: OidcIssuer,
         db_url: str,
         fixture: Callable[[], tuple[Path, dict[str, Any]]],
     ) -> CaptureTarget:
         """A torn-down lane System and its booted Run, per stack and provider (#3116).

         The remote rejection target: no crash options and no debug session, which none of the
         three tools reads. A failed or blocked preparation is replayed, with its artifacts, for
         every later cell rather than retried.
         """
         key = (base_url, run.cell.provider)
         cached = _TARGETS.get(key)
         if cached is None:
             scratch = CellRun(run.cell, run.writer)
             try:
                 cached = await _prepare_run_target(run, base_url, issuer, db_url, fixture, scratch)
             except Exception as exc:  # noqa: BLE001 - remembered and replayed for every cell
                 cached = _Failed(exc, (*scratch.artifacts, *scratch.assertions.values()))
             _TARGETS[key] = cached
         if isinstance(cached, _Failed):
             run.artifacts.extend(cached.artifacts)
             if isinstance(cached.error, ScenarioStop):
                 raise ScenarioStop(cached.error.outcome, str(cached.error))
             raise AssertionError(f"the run target could not be prepared: {cached.error!r}")
         run.observed |= cached.observed
         run.artifacts.extend(cached.artifacts)
         return cached


     def target_state(db_url: str, target: CaptureTarget, connect: Connect) -> Snapshot:
         """The project's rows, the jobs naming the System or Run, and the domain's definedness."""

         async def state() -> dict[str, object]:
             return {
                 "project": await project_state(db_url, target.project),
                 "jobs": await named_jobs(db_url, target.system_id, target.run_id),
                 "domain_defined": await asyncio.to_thread(domain_defined, target.system_id, connect),
             }

         return state
     ```

     Note: the test stubs `_prepare_run_target` with a callable that does not touch `scratch`;
     the memo test patches only that name.
   - module docstring: add a paragraph — "A remote cell (#3116) runs the same sequences on the
     provider lane: native reads and the capture spool come from the lane, the flow goes to the
     guest's libvirt-network gateway, and a rejection cell aims at :func:`run_target`."
4. `test_control_tool_cells_live.py`: `_fixture(arch)` as in Task 2; the rejection branch:

   ```python
       if cell.kind == "rejection":
           boundary = boundary_of(cell)
           fixture = partial(_fixture, str(cell.guest_arch))
           if cell.provider == "remote-libvirt":
               target = await run_target(run, base_url, issuer, db_url, fixture)
               snapshot = target_state(db_url, target, lane_for(cell).connect)
           else:
               target = await capture_target(run, base_url, issuer, db_url, fixture)
               snapshot = snapshot_of(db_url, target)
           await prove_rejection(
               run, caller, boundary, rejection(cell.operation, boundary, target), snapshot
           )
           return
   ```

   The functional branch uses `_fixture(str(cell.guest_arch))`. `_cells()` adds remote x86_64
   cells as in Task 2. Docstring updated as in Task 2.
5. Run the green command; expect pass. `uv run python -m pytest
   tests/integration/test_control_tool_cells_live.py --collect-only -q -m live_stack`: expect
   `120 tests collected` (60 local, 60 remote x86_64).
6. `just lint`, `just type`. Commit: `test(live): run the traffic, SysRq and watch cells on the remote lane`.

Acceptance: local control cells keep `capture_target`, `10.0.2.2` and the `pcap_dir` spool.

## Task 4 — Bindings

Files: modify `scripts/coverage_campaign/obligations.toml`,
`tests/scripts/test_coverage_contract.py`.

Verification:

- Contract: the remote cells bind their carriers. Mode: focused-test.
  `test_coverage_contract.py` (the assertion block with `_POWER_NODE`/`_CONTROL_NODE`); red:
  `{None} != {_POWER_NODE}` after changing the asserts first; green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.

Steps:

1. Replace the two remote asserts:

   ```python
       remote_power = [c for c in power if c.provider == "remote-libvirt"]
       # As for local: the remote ppc64le cells (#2818's) share the provider-qualified ids.
       assert len(remote_power) == 200 and {c.node_id for c in remote_power} == {_POWER_NODE}
   ```

   and

   ```python
       remote_control = [c for c in control if c.provider == "remote-libvirt"]
       assert len(remote_control) == 120
       assert {c.node_id for c in remote_control} == {_CONTROL_NODE}
   ```

   Also add `assert {c.node_id for c in contract.cells if c.owner == 3116} == {_POWER_NODE,
   _CONTROL_NODE}`. Run; expect red.
2. In `[implementations]`, after the local `control.power`…`systems.snapshot` block add 25 rows
   `"tool/remote-libvirt/<tool>/<variant>/<kind>" = "<power node>"` for tool/variant in
   (`control.power`/`default`, `systems.delete_snapshot`/`supports_snapshots`,
   `systems.list_snapshots`/`supports_snapshots`, `systems.restore`/`supports_snapshots`,
   `systems.snapshot`/`supports_snapshots`) × kind in (authentication, authorization, functional,
   project-isolation, validation); after the local control block add 15 rows for
   (`control.capture_traffic`/`supports_traffic_capture`,
   `control.diagnostic_sysrq`/`supports_diagnostic_sysrq`,
   `control.watch_for_crash`/`supports_crash_watch`) × the same kinds →
   `tests/integration/test_control_tool_cells_live.py::test_control_tool_cell`. Generate them with
   a throwaway loop over `build_contract()` owner-3116 scenario ids to avoid typos, keep the file
   sorted as its neighbours are, and confirm 40 distinct ids.
3. Run the green command and `just coverage-check`; expect pass.
4. Commit: `test(coverage): bind the remote power, snapshot and control cells`.

## Task 5 — Runbook

Files: modify `docs/operating/runbooks/remote-live-stack.md` (new `## 11. Remote power, snapshot
and control tool cells (#3116)` after §10).

Verification: Contract: runbook. Mode: task-test-not-applicable — prose with no executable
consumer; `just docs-links docs-paths` checks its links and paths.

Content (prose, mirrors §8/§9): the two carriers and `-k remote-libvirt`; what differs from the
local cells (provider-host observer for domain state, checkpoints, QMP filters and definedness;
pcap volume in the pool; flow to the guest's libvirt-network gateway; in-guest kernel
observation; `run_target` for control rejections); prerequisites (§7 topology, observer, the
§3 object-store reachability and the `DOCKER-USER` rule because the fixture install downloads
into the guest, `fedora-kdive-remote-base-43` staged, the verified `longterm` fixture); the
command block (bindings with `--remote --kernel-baseline longterm`, both bring-ups, both
carriers, assemble, qualify); per-lane counts (80 recorded, 80 skipped); provider cleanup check
(no `kdive-` domain, no `kdive-` overlay or `kdive-pcap-` volume). The "Last run" paragraph is
added only after the live run, with observed facts. Commit:
`docs(runbook): run the remote power, snapshot and control cells`.

## Task 6 — Live proof (operator grant required)

Not code. Runs only after team-lead grants the host, cap and window. Bring both configurations up
at the committed candidate, write bindings, run both carriers with `-k remote-libvirt`, assemble,
qualify, record the observed counts and every failing/blocked cell in the runbook's "Last run"
paragraph (one commit), and clean the provider host. Product defects (e.g. a remote disk-only
revert or a pcap fetch-back failure) stay failing and are reported to the orchestrator.

## Deferrals

None.
