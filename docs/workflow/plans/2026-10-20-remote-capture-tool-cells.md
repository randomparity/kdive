# Remote crash capture and introspection tool cells (#3114) — implementation plan

Goal: bind and prove the 180 x86_64 remote-libvirt cells of the six capture and introspection
tools by running #2814's local drivers on the remote provider lane. The `vmcore.fetch` variants
the handler refuses are recorded as `unsupported`.

Architecture:

- The contract asks the `vmcore.fetch` handler which methods produce a core. Any other
  advertised method is an `unsupported` variant with no `capability` cell (ADR-0722, 2026-10-20
  amendment).
- `capture_cells.py` reads the variant from either provider's scenario id, writes the remote
  profile section, proves the handler refusal and observes the remote dump volume. It prepares one
  rejection target per stack and provider.
- The provider `Lane` gains `pool_volume`, and the capture carrier parametrizes the remote cells.

Spec: [2026-10-20-remote-capture-tool-cells-design.md](../specs/2026-10-20-remote-capture-tool-cells-design.md).

Tech stack: Python 3.14, pytest, libvirt-python, `uv`, `just`.

Expected implementation size: 480–680 changed lines (L). Derived from the file map below: the
contract rule and handler rename (~25), the lane field (~30), the capture-cell changes (~140), the
carrier (~30), 45 binding rows, three unit-test files (~200) and one runbook section (~50).

## Global Constraints

- Base: `origin/main` containing the merge of PR #3287 (#3116). It provides `Lane.connect`,
  `Lane.installed_kernel`, `tool_cells.worker_connect`, `tool_cells.Connect`,
  `tool_cells.remote_pcap_spool`, `remote_lifecycle.observer` imported into `tool_cells`, and
  `power_snapshots.domain_defined(system_id, connect=worker_connect)`. `capture_cells.boot`
  already passes `installed_kernel=guest.lane.installed_kernel`. Do not start Task 1 until
  Task 0 proves that base.
- Product source changes are limited to renaming `_VMCORE_METHODS` to `VMCORE_METHODS` in
  `src/kdive/mcp/tools/lifecycle/vmcore/handlers.py`, with no behaviour change. No migration. The
  ADR change is the 2026-10-20 amendment in ADR-0722. ADR slot `docs/adr/0792` stays unused.
- Local-libvirt cells keep their values: 164 owned by #2814 (28 functional, 4 unsupported). The
  local profile edits and the local rejection target are unchanged.
- Test-only env inputs stay unprefixed (`REMOTE_PROVIDER_SSH`); never add a `KDIVE_` variable.
- Evidence never carries the provider destination, pool name or host path beyond what the remote
  frame already records (ADR-0715).
- Ruff line length 100, lint `E,F,I,UP,B,SIM`. `just type` is whole-tree. Prose avoids
  "critical", "robust", "comprehensive", "elegant".
- Guardrails per task: `just lint`, `just type` and the named focused tests;
  `just coverage-check` after Tasks 1 and 4; `just docs-check` after Task 5. The pre-push
  `just ci` runs once, at delivery.

## File map

| File | Today | After |
|---|---|---|
| `src/kdive/mcp/tools/lifecycle/vmcore/handlers.py` | private `_VMCORE_METHODS` | public `VMCORE_METHODS` (rename) |
| `tests/providers/test_composition.py` | comment names `_VMCORE_METHODS` | names `VMCORE_METHODS` |
| `scripts/coverage_campaign/contract.py` | every advertised method a supported variant | handler-refused variants `unsupported`, no capability cell; `HANDLER_REFUSAL` |
| `tests/scripts/test_coverage_contract.py` | remote capture unbound, 4 local unsupported | handler rule, remote counts, remote bindings |
| `tests/integration/live_stack/tool_cells.py` | `Lane` with #3116 fields | + `pool_volume`; owns `pool_target` (shared with `remote_pcap_spool`) and `pool_volume_path` |
| `tests/integration/live_stack/test_tool_cells.py` | lane tests | + pool volume tests |
| `tests/integration/live_stack/capture_cells.py` | local by construction | `variant_of`, provider-aware profile, `refusal_of`, `refused_method`, dump-volume check, per-provider target, #3124 blocked route |
| `tests/integration/live_stack/test_capture_cells.py` | local oracles | + remote cases |
| `tests/integration/test_capture_tool_cells_live.py` | local cells | + remote x86_64 cells |
| `scripts/coverage_campaign/obligations.toml` | local capture rows | + 45 remote rows |
| `docs/operating/runbooks/remote-live-stack.md` | numbered sections | + one remote capture section |
| `tests/integration/live_stack/debug_cells.py` | binds `protected_state` with four arguments | unchanged reader; the new `connect` keyword defaults to the worker |

No compatibility path is retained. `lane_keys(observed)` keeps its local behaviour as the default
of its new keyword.

## Task 0 — Base on #3116's lane

Files: none changed.

Verification:

- Contract: base contains the lane fields. Mode: task-test-not-applicable — the check below
  inspects git ancestry and an existing name; no code changes.

Steps:

1. `git fetch origin && gh pr view 3287 --json state,mergeCommit --jq '.state,.mergeCommit.oid'`.
   Expect `MERGED` and a SHA. If it is not merged, stop and report to the orchestrator; do not
   copy #3116's code.
2. `git merge --no-edit origin/main`, then
   `git merge-base --is-ancestor <mergeCommit> HEAD && echo based`. Expect `based`.
3. `rg -n "installed_kernel: InstalledKernel|def worker_connect|def remote_pcap_spool" tests/integration/live_stack/tool_cells.py`.
   Expect three hits.

## Task 1 — Handler-refused capture variants

Files: modify `src/kdive/mcp/tools/lifecycle/vmcore/handlers.py`,
`tests/providers/test_composition.py` (comment), `scripts/coverage_campaign/contract.py`; test
`tests/scripts/test_coverage_contract.py`.

Interfaces (later tasks rely on):

- `kdive.mcp.tools.lifecycle.vmcore.handlers.VMCORE_METHODS: frozenset[CaptureMethod]`
- `scripts.coverage_campaign.contract.HANDLER_REFUSAL: str` =
  `"vmcore.fetch handler: method does not produce a vmcore"`

Verification:

- Contract: handler-refused variants. Mode: focused-test.
  `test_coverage_contract.py::test_handler_refused_capture_variants_are_unsupported`; red:
  `ImportError` (no `HANDLER_REFUSAL`); green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q -k "handler_refus or capture_owners or capability"`.
- Contract: the contract follows the handler set. Mode: focused-test.
  `::test_handler_refusal_follows_the_core_method_set`; red: same `ImportError`; green: same
  command.
- Contract: handler rename. Mode: task-test-not-applicable — a pure rename; `just type` and the
  existing `tests/mcp` vmcore tests fail on any missed reference, and no behaviour changes.

Steps:

1. In `tests/scripts/test_coverage_contract.py` extend the contract import with `HANDLER_REFUSAL`,
   add `from kdive.mcp.tools.lifecycle.vmcore.handlers import VMCORE_METHODS`, and append:

   ```python
   def test_handler_refused_capture_variants_are_unsupported(inventory: Inventory) -> None:
       # ADR-0722 (2026-10-20): vmcore.fetch refuses console/gdbstub itself, on every lane, so
       # those variants are unsupported and carry no capability refusal cell.
       cells = build_contract(inventory=inventory).cells
       ids = {c.id for c in cells}
       fetch = [c for c in cells if c.operation == "vmcore.fetch" and c.kind != "rejection"]
       refused = {c.id for c in fetch if c.unsupported_reason == HANDLER_REFUSAL}
       assert refused == {
           f"tool/vmcore.fetch/{method}/{configuration}/{exposure}/remote-libvirt/{arch}/functional"
           for method in ("console", "gdbstub")
           for configuration in ("default", "recovery")
           for exposure in ("direct", "gateway")
           for arch in inventory.arches
       }
       assert all(c.kind == "unsupported" for c in fetch if c.id in refused)
       assert not {i.removesuffix("/functional") + "/capability" for i in refused} & ids


   def test_handler_refusal_follows_the_core_method_set(inventory: Inventory) -> None:
       advertised = {m.value for m in inventory.capabilities["remote-libvirt"].capture_methods}
       cells = build_contract(inventory=inventory).cells
       refused = {
           c.scenario_id.split("/")[-2]
           for c in cells
           if c.provider == "remote-libvirt" and c.unsupported_reason == HANDLER_REFUSAL
       }
       assert refused == advertised - {m.value for m in VMCORE_METHODS} == {"console", "gdbstub"}
   ```

   In `test_capture_owners_follow_the_approved_split`, after the local assertion, add:

   ```python
       remote = [c for c in cells if c.owner == 3114]
       kinds = Counter(c.kind for c in remote)
       assert (len(remote), kinds["functional"], kinds["unsupported"]) == (180, 28, 8)
   ```

2. Run the green command. Expect `ImportError: cannot import name 'HANDLER_REFUSAL'`.
3. In `handlers.py` rename `_VMCORE_METHODS` to `VMCORE_METHODS` (definition and both uses). In
   `tests/providers/test_composition.py:1075` change the comment's `_VMCORE_METHODS` to
   `VMCORE_METHODS`. Check: `rg -n "_VMCORE_METHODS" src tests scripts` prints nothing.
4. In `contract.py` add the import
   `from kdive.mcp.tools.lifecycle.vmcore.handlers import VMCORE_METHODS` and, after
   `_ADMISSION_REFUSALS`:

   ```python
   # vmcore.fetch admits only core-producing methods; it refuses any other advertised method itself,
   # on every lane, with no capability_unsupported envelope (ADR-0722, 2026-10-20 amendment).
   _CORE_METHODS = frozenset(method.value for method in VMCORE_METHODS)
   HANDLER_REFUSAL = "vmcore.fetch handler: method does not produce a vmcore"
   ```

   In `_tool_cells`, directly after the fadump override line:

   ```python
               refused = row.tool == "vmcore.fetch" and supported and variant not in _CORE_METHODS
               if refused:
                   supported, source = False, HANDLER_REFUSAL
   ```

   and change the capability condition to
   `if group.capabilities.get(row.tool) and not supported and not refused:`.
5. Run the green command. Expect all selected tests to pass. Then run
   `uv run python -m pytest tests/scripts/test_coverage_contract.py tests/mcp -q -k "vmcore or coverage"`
   and expect a pass. Run `just coverage-check`, `just lint` and `just type`, all exit 0.
6. Commit: `test(coverage): record handler-refused vmcore.fetch variants as unsupported`.

Acceptance: the contract counts are 180/28/8 for #3114 and 164/28/4 for #2814; no remote capability
cell exists; the handler's behaviour is unchanged.

## Task 2 — Lane pool volume

Files: modify `tests/integration/live_stack/tool_cells.py`; test
`tests/integration/live_stack/test_tool_cells.py`.

Interfaces:

- consumes `tool_cells.observer` (imported from `remote_lifecycle` by #3116) and the `_PoolConn`
  fake and `_cell` helper already in `test_tool_cells.py` (added by #3116).
- provides `tool_cells.pool_target(dest: str, pool: str) -> PurePosixPath`,
  `tool_cells.pool_volume_path(dest: str, pool: str, name: str) -> str` and
  `Lane.pool_volume: Callable[[str], str] | None` (last field).

Verification:

- Contract: pool volume path. Mode: focused-test.
  `test_tool_cells.py::test_pool_volume_path_joins_the_pool_target` and
  `::test_pool_volume_path_needs_a_pool_target`; red: `AttributeError` (no `pool_volume_path`);
  green: `uv run python -m pytest tests/integration/live_stack/test_tool_cells.py -q -k "pool_volume or lane_observes"`.
- Contract: lane field. Mode: focused-test. The existing `::test_local_lane_observes_the_worker`
  and `::test_remote_lane_observes_the_provider_host` gain `pool_volume` assertions. Red:
  `AttributeError: 'Lane' object has no attribute 'pool_volume'`. Green: same command.

Steps:

1. Append to `test_tool_cells.py`:

   ```python
   def test_pool_volume_path_joins_the_pool_target(monkeypatch: pytest.MonkeyPatch) -> None:
       conn = _PoolConn("<pool><target><path>/srv/pool</path></target></pool>")
       monkeypatch.setattr(tool_cells, "observer", lambda _dest: conn)
       path = tool_cells.pool_volume_path("op@provider.example", "kdive", "kdive-host-dump-x.kdump")
       assert path == "/srv/pool/kdive-host-dump-x.kdump"
       assert (conn.named, conn.closed) == ("kdive", True)


   def test_pool_volume_path_needs_a_pool_target(monkeypatch: pytest.MonkeyPatch) -> None:
       conn = _PoolConn("<pool><target/></pool>")
       monkeypatch.setattr(tool_cells, "observer", lambda _dest: conn)
       with pytest.raises(AssertionError, match="no target path"):
           tool_cells.pool_volume_path("op@provider.example", "kdive", "v")
       assert conn.closed
   ```

   In `test_local_lane_observes_the_worker` add `assert lane.pool_volume is None`. In
   `test_remote_lane_observes_the_provider_host` add:

   ```python
       volume = lane.pool_volume
       assert isinstance(volume, partial) and volume.func is tool_cells.pool_volume_path
       assert volume.args == ("op@provider.example", "kdive")
   ```

2. Run the green command. Expect the red failures above.
3. In `tool_cells.py`, move `remote_pcap_spool`'s pool read into one helper that both namers
   use. The existing pcap tests (`test_remote_pcap_spool_*`) keep passing unchanged, because the
   read, the close and the assertion message are the same:

   ```python
   def pool_target(dest: str, pool: str) -> PurePosixPath:
       """The target directory of storage pool ``pool``, read through the operator's observer."""
       conn = observer(dest)
       try:
           xml = conn.storagePoolLookupByName(pool).XMLDesc(0)
       finally:
           conn.close()
       target = ET.fromstring(xml).findtext("./target/path")  # noqa: S314  # nosec B314
       assert target, f"storage pool {pool} has no target path"
       return PurePosixPath(target)


   def remote_pcap_spool(dest: str, pool: str, system_id: str) -> Callable[[str], str]:
       """Name the provider-host pcap volume a capture job of ``system_id`` writes into ``pool``.

       The remote capturer writes a per-job volume into the configured storage pool (ADR-0432), so
       the System owns it until the job reclaims it. The pool's target is read here, before any job
       exists; the returned namer does no I/O.
       """
       directory = pool_target(dest, pool)
       return lambda job_id: str(directory / pcap_volume_name(UUID(system_id), UUID(job_id)))


   def pool_volume_path(dest: str, pool: str, name: str) -> str:
       """The provider-host path of volume ``name`` in storage pool ``pool``."""
       return str(pool_target(dest, pool) / name)
   ```

   Add the `Lane` field `pool_volume: Callable[[str], str] | None` last. Extend its docstring with:
   "``pool_volume(name)`` is the provider-host path of a named pool volume, ``None`` where the
   provider has no storage pool." `_local_lane` passes `None`; `_remote_lane` passes
   `partial(pool_volume_path, host.dest, host.pool)`.
4. Run the green command, then the whole file:
   `uv run python -m pytest tests/integration/live_stack/test_tool_cells.py -q`. Expect a pass.
   Run `just lint` and `just type`.
5. Commit: `test(live): name provider pool volumes on the remote lane`.

## Task 3 — Provider-aware capture cells

Files: modify `tests/integration/live_stack/capture_cells.py`; test
`tests/integration/live_stack/test_capture_cells.py`.

Interfaces:

- consumes `Lane.pool_volume`, `Lane.absent`, `Lane.connect` (Task 2, #3116),
  `tool_cells.Connect` and `tool_cells.worker_connect`,
  `power_snapshots.domain_defined(system_id, connect)` and `HANDLER_REFUSAL` (Task 1);
- consumes
  `kdive.providers.remote_libvirt.retrieve.host_dump_capture.host_dump_volume_name(system_id: UUID) -> str`;
- provides `variant_of(cell: Cell) -> str`, `REFUSED_REASON: str`,
  `REMOTE_DEBUGINFO_BLOCKED: dict[str, str]`,
  `refusal_of(env: ToolResponse, method: str) -> dict[str, object]`,
  `refused_method(run, caller, tree, manifest, db_url, guest) -> dict[str, object]`,
  `lane_keys(observed, *, remote: bool = False)`, `CaptureTarget.connect: Connect` (default
  `worker_connect`),
  `protected_state(db_url, project, system_id, run_id, *, connect: Connect = worker_connect)`
  (keyword with the local default, so `debug_cells.snapshot_of`'s four-argument `partial`
  (`debug_cells.py:1151`) keeps working unchanged) and the unchanged `snapshot_of(db_url, target)`
  and `capture_target(...)` signatures.

Verification:

- Contract: variant parsing and options. Mode: focused-test.
  `::test_variant_of_reads_either_provider` and
  `::test_options_follow_each_remote_functional_and_unsupported_cell`; red: `ImportError`
  (`variant_of`); green:
  `uv run python -m pytest tests/integration/live_stack/test_capture_cells.py -q -k "variant or options or crash_profile or refusal or dump_volume or keys or target or snapshot or 3124"`.
- Contract: remote crash profile. Mode: focused-test.
  `::test_remote_crash_profile_edits_only_the_remote_section`; red:
  `KeyError: 'local-libvirt'`; green: same command.
- Contract: refusal check. Mode: focused-test. `::test_refusal_of_accepts_only_the_handler_refusal`;
  red: `ImportError` (`refusal_of`); green: same command.
- Contract: dump-volume observer. Mode: focused-test.
  `::test_remote_host_dump_capture_owns_and_checks_its_dump_volume` and
  `::test_local_host_dump_capture_owns_no_dump_volume`; red: an `owned` list without the volume
  path; green: same command.
- Contract: remote target. Mode: focused-test. `::test_lane_keys_keep_host_keys_remotely`,
  `::test_capture_target_is_prepared_per_provider` and
  `::test_failed_remote_target_replays_its_host_keys`; red: `TypeError` (unexpected keyword
  `remote`), a single cached target and a replay without host keys; green: same command.
- Contract: protected state reads the target's provider. Mode: focused-test.
  `::test_snapshot_of_reads_the_targets_connect`; red: the fake `connect` is never called; green:
  same command.
- Contract: blocked remote introspection (#3124). Mode: focused-test.
  `::test_remote_live_introspection_is_blocked_on_3124`; red: `options` returns `"live"`; green:
  same command.

Steps:

1. Add tests to `test_capture_cells.py`. New imports:
   `from scripts.coverage_campaign.contract import HANDLER_REFUSAL`,
   `from tests.integration.live_stack import capture_cells`,
   `from uuid import UUID`, and the names `REFUSED_REASON`, `REMOTE_DEBUGINFO_BLOCKED`,
   `refusal_of`, `snapshot_of`, `variant_of` and `vmcore_body` from `capture_cells`.

   ```python
   def _remote_cells(arch: str = "x86_64") -> list[Cell]:
       return [
           c
           for c in build_contract().cells
           if c.operation in TOOLS and c.provider == "remote-libvirt" and c.guest_arch == arch
       ]


   def test_variant_of_reads_either_provider() -> None:
       local = next(c for c in _cells() if c.operation == "vmcore.fetch" and c.kind == "functional")
       remote = next(c for c in _remote_cells() if c.operation == "vmcore.fetch")
       assert variant_of(local) == local.scenario_id.split("/")[2]
       assert variant_of(remote) == remote.scenario_id.split("/")[3]


   def test_options_follow_each_remote_functional_and_unsupported_cell() -> None:
       expected = {
           "control.force_crash": "kdump",
           "introspect.from_vmcore": "kdump",
           "postmortem.crash": "kdump",
           "vmcore.fetch/kdump": "kdump",
           "vmcore.fetch/host_dump": "host_dump",
           "vmcore.fetch/console": "host_dump",
           "vmcore.fetch/gdbstub": "host_dump",
       }
       seen = {}
       for cell in _remote_cells():
           if cell.kind == "rejection" or cell.operation in REMOTE_DEBUGINFO_BLOCKED:
               continue
           op = cell.operation
           seen[f"{op}/{variant_of(cell)}" if op == "vmcore.fetch" else op] = options(cell)
       assert seen == expected


   def test_remote_live_introspection_is_blocked_on_3124() -> None:
       cells = [
           c
           for c in _remote_cells()
           if c.kind == "functional" and c.operation in ("introspect.run", "introspect.script")
       ]
       assert len(cells) == 8
       for cell in cells:
           with pytest.raises(ScenarioStop, match="#3124") as stop:
               options(cell)
           assert stop.value.outcome is Outcome.BLOCKED
       local = [c for c in _cells() if c.operation == "introspect.run" and c.kind == "functional"]
       assert {options(c) for c in local} == {"live"}


   def _remote_profile() -> dict[str, object]:
       return {"arch": "x86_64", "provider": {"remote-libvirt": {"base_image_volume": "b"}}}


   @pytest.mark.parametrize(
       ("crash", "expected"),
       [
           ("kdump", {"destructive_ops": ["force_crash"], "crashkernel": "256M"}),
           ("host_dump", {"destructive_ops": ["force_crash"], "host_dump": True}),
           ("target", {"destructive_ops": ["force_crash"]}),
           ("live", {}),
       ],
   )
   def test_remote_crash_profile_edits_only_the_remote_section(
       crash: str, expected: dict[str, object]
   ) -> None:
       profile = _remote_profile()
       edited = crash_profile(profile, crash)
       assert edited["provider"] == {"remote-libvirt": {"base_image_volume": "b", **expected}}
       assert profile == _remote_profile(), "the lane profile was changed in place"


   def test_refusal_of_accepts_only_the_handler_refusal() -> None:
       data = {"method": "console", "reason": REFUSED_REASON}
       refusal = ToolResponse.failure("run-1", ErrorCategory.CONFIGURATION_ERROR, data=data)
       assert refusal_of(refusal, "console") == {
           "error_category": "configuration_error",
           "reason": REFUSED_REASON,
           "method": "console",
       }
       wrong = [
           ToolResponse.failure(
               "run-1", ErrorCategory.CONFIGURATION_ERROR, data={**data, "reason": "x"}
           ),
           ToolResponse.failure("run-1", ErrorCategory.NOT_FOUND, data=data),
           ToolResponse.success("run-1", "queued", refs={"job_id": "j"}),
       ]
       for env in wrong:
           with pytest.raises(AssertionError):
               refusal_of(env, "console")
       with pytest.raises(AssertionError):
           refusal_of(refusal, "gdbstub")
       assert HANDLER_REFUSAL.endswith(REFUSED_REASON)
   ```

   The dump-volume tests drive `vmcore_body` with a stub `Session`. Patch `raw_core`,
   `require_core` and `Session.crash`/`Session.capture`/`Session.armed` with `monkeypatch`:

   ```python
   _SYS = str(UUID(int=7))


   def _dump_session(
       monkeypatch: pytest.MonkeyPatch, *, remote: bool, present: bool, fails: bool = False
   ) -> Session:
       async def nothing(*_a: object, **_k: object) -> None:
           return None

       async def capture(*_a: object, **_k: object) -> list[str]:
           if fails:
               raise AssertionError("capture job failed")
           return ["artifact-1"]

       async def core(*_a: object) -> tuple[str, bytes]:
           return "0" * 64, b""

       monkeypatch.setattr(Session, "crash", nothing)
       monkeypatch.setattr(Session, "armed", nothing)
       monkeypatch.setattr(Session, "capture", capture)
       monkeypatch.setattr(capture_cells, "raw_core", core)
       monkeypatch.setattr(capture_cells, "require_core", lambda *_a: None)
       lane = SimpleNamespace(
           pool_volume=(lambda name: f"/srv/pool/{name}") if remote else None,
           absent=lambda _path: not present,
       )
       guest = SimpleNamespace(system_id=_SYS, lane=lane, owned=[], op=None)
       cell = SimpleNamespace(scenario_id="tool/remote-libvirt/vmcore.fetch/host_dump/functional")
       run = cast(CellRun, SimpleNamespace(cell=cell))
       caller = cast(HttpCaller, None)
       return Session(
           run, cast(Guest, guest), caller, Grants("s", ("p",)), _MANIFEST, "run-1", Endpoint("h", 22)
       )


   def test_remote_host_dump_capture_owns_and_checks_its_dump_volume(
       monkeypatch: pytest.MonkeyPatch,
   ) -> None:
       s = _dump_session(monkeypatch, remote=True, present=False)
       observed = asyncio.run(vmcore_body(s))
       path = f"/srv/pool/kdive-host-dump-{_SYS}.kdump"
       assert s.guest.owned == [path] and observed["dump_volume"] == "absent"
       leaked = _dump_session(monkeypatch, remote=True, present=True)
       with pytest.raises(AssertionError, match="outlived"):
           asyncio.run(vmcore_body(leaked))
       assert leaked.guest.owned == [path], "a leaked volume must stay owned for the cleanup"
       failed = _dump_session(monkeypatch, remote=True, present=True, fails=True)
       with pytest.raises(AssertionError, match="capture job failed"):
           asyncio.run(vmcore_body(failed))
       assert failed.guest.owned == [path], "a failed capture's volume must stay owned"


   def test_local_host_dump_capture_owns_no_dump_volume(monkeypatch: pytest.MonkeyPatch) -> None:
       s = _dump_session(monkeypatch, remote=False, present=True)
       observed = asyncio.run(vmcore_body(s))
       assert s.guest.owned == [] and "dump_volume" not in observed


   def test_lane_keys_keep_host_keys_remotely() -> None:
       observed = {"host_os": "rocky:10.2", "host_arch": "x86_64", "guest_os": "fedora:43"}
       assert lane_keys(observed) == {"guest_os": "fedora:43"}
       assert lane_keys(observed, remote=True) == observed


   def test_capture_target_is_prepared_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
       prepared: list[str] = []

       async def prepare(run: CellRun, *_a: object) -> CaptureTarget:
           prepared.append(run.cell.provider)
           return CaptureTarget("p", "s", "r", "x", {"host_os": run.cell.provider}, ())

       monkeypatch.setattr(capture_cells, "_TARGETS", {})
       monkeypatch.setattr(capture_cells, "_prepare_target", prepare)

       def run_of(provider: str) -> CellRun:
           cell = SimpleNamespace(provider=provider)
           return cast(CellRun, SimpleNamespace(cell=cell, writer=None, observed={}, artifacts=[]))

       for provider in ("local-libvirt", "remote-libvirt", "remote-libvirt"):
           run = run_of(provider)
           asyncio.run(
               capture_cells.capture_target(
                   run, "http://s", cast(Any, None), "db", lambda: (Path(), {})
               )
           )
           assert run.observed == {"host_os": provider}
       assert prepared == ["local-libvirt", "remote-libvirt"]


   def test_failed_remote_target_replays_its_host_keys(monkeypatch: pytest.MonkeyPatch) -> None:
       async def prepare(
           _run: CellRun, _url: str, _issuer: object, _db: str, _fx: object, scratch: CellRun
       ) -> CaptureTarget:
           scratch.observed |= {"host_os": "rocky:10.2", "host_arch": "x86_64"}
           raise AssertionError("boot failed")

       monkeypatch.setattr(capture_cells, "_TARGETS", {})
       monkeypatch.setattr(capture_cells, "_prepare_target", prepare)
       cell = next(c for c in _remote_cells() if c.kind == "rejection")
       for _ in range(2):
           run = CellRun(cell, cast(Any, None))
           with pytest.raises(AssertionError, match="could not be prepared"):
               asyncio.run(
                   capture_cells.capture_target(
                       run, "http://s", cast(Any, None), "db", lambda: (Path(), {})
                   )
               )
           assert run.observed["host_os"] == "rocky:10.2"


   def test_snapshot_of_reads_the_targets_connect(monkeypatch: pytest.MonkeyPatch) -> None:
       opened: list[str] = []

       async def rows(*_a: object) -> list[object]:
           return []

       def defined(system_id: str, connect: object) -> bool:
           opened.append(cast(Any, connect)())
           return True

       monkeypatch.setattr(capture_cells, "project_state", rows)
       monkeypatch.setattr(capture_cells, "named_jobs", rows)
       monkeypatch.setattr(capture_cells, "domain_defined", defined)
       target = CaptureTarget("p", "s", "r", "x", {}, (), connect=lambda: "provider")
       state = asyncio.run(snapshot_of("db", target)())
       assert opened == ["provider"] and state["domain_defined"] is True
   ```

   `CellRun` is a plain dataclass (`tests/integration/live_stack/scenario.py`), so
   `capture_target`'s `CellRun(run.cell, run.writer)` accepts the namespace cell. Add
   `from typing import Any` if missing.
2. Run the green command. Expect the red failures listed in Verification.
3. Implement in `capture_cells.py`:

   - imports: `from uuid import UUID` (beside `uuid4`),
     `from kdive.providers.remote_libvirt.retrieve.host_dump_capture import host_dump_volume_name`,
     `Connect` and `worker_connect` from `tool_cells`, and `one` (already imported);
   - constants after `_LANE_KEYS`:

     ```python
     _HOST_KEYS = ("host_os", "host_arch")
     # vmcore.fetch's refusal of a method outside its core-producing set (handlers.py).
     REFUSED_REASON = "method does not produce a vmcore"
     # Remote drgn-live runs in the guest, which never receives the Run's vmlinux: these cells
     # stay blocked on the open defect, not failed (#3124, blocked by #3121). Drop an entry when
     # its issue closes.
     REMOTE_DEBUGINFO_BLOCKED = {
         tool: "remote guests do not receive the uploaded vmlinux (#3124)"
         for tool in ("introspect.run", "introspect.script")
     }
     ```

   - helpers:

     ```python
     def variant_of(cell: Cell) -> str:
         """The tool variant of ``cell``, read from a local or a provider-qualified scenario id."""
         return cell.scenario_id.split("/")[-2]
     ```

     In `options`, after the ppc64le check, add:

     ```python
         if cell.provider == "remote-libvirt" and cell.operation in REMOTE_DEBUGINFO_BLOCKED:
             raise ScenarioStop(Outcome.BLOCKED, REMOTE_DEBUGINFO_BLOCKED[cell.operation])
     ```

     `options` runs before the carrier provisions anything, so a blocked cell mutates nothing.
     Then replace `variant = cell.scenario_id.split("/")[2]` with `variant = variant_of(cell)`
     and add, after the kdump/host_dump branch:

     ```python
         if cell.operation == "vmcore.fetch" and variant in ("console", "gdbstub"):
             return "host_dump"  # a crashed System the handler refuses to fetch from
     ```

   - `crash_profile` writes the one provider section:

     ```python
     def crash_profile(profile: Mapping[str, object], crash: str) -> dict[str, object]:
         """``profile`` with ``crash`` options in its provider section; the input is unchanged."""
         assert crash in _CRASH_OPTIONS, f"unknown crash options {crash!r}"
         edited = copy.deepcopy(dict(profile))
         sections = cast(dict[str, Any], edited["provider"])
         assert len(sections) == 1, f"the profile names {len(sections)} provider sections"
         ((provider, section),) = sections.items()
         if crash != "live":
             section["destructive_ops"] = ["force_crash"]
         if crash == "kdump":
             section["crashkernel"] = arch_traits(str(edited["arch"])).default_crashkernel
         if crash == "host_dump" and provider == "remote-libvirt":
             section["host_dump"] = True
         elif crash == "host_dump":
             section["debug"] = {"preserve_on_crash": True}
         return edited
     ```

   - `lane_keys`:

     ```python
     def lane_keys(observed: Mapping[str, object], *, remote: bool = False) -> dict[str, object]:
         """The lane context of ``observed``, with the provider host's keys on the remote lane."""
         keys = (*_LANE_KEYS, *_HOST_KEYS) if remote else _LANE_KEYS
         return {key: observed[key] for key in keys if key in observed}
     ```

   - in `vmcore_body`, replace `method = s.run.cell.scenario_id.split("/")[2]` with
     `method = variant_of(s.run.cell)`. Then replace the capture line
     `refs = await s.capture(method, exposed=True)` with:

     ```python
         path = None
         if method == "host_dump" and s.guest.lane.pool_volume is not None:
             # Owned before the capture: a failed capture can leave the deterministic volume.
             path = s.guest.lane.pool_volume(host_dump_volume_name(UUID(s.guest.system_id)))
             s.guest.owned.append(path)
         refs = await s.capture(method, exposed=True)
         dump: dict[str, object] = {}
         if path is not None:
             # The remote capture reclaims its per-System dump volume in its own finally.
             assert await asyncio.to_thread(s.guest.lane.absent, path), (
                 "the host_dump volume outlived its capture"
             )
             dump = {"dump_volume": "absent"}
     ```

     and add `**dump` to the returned mapping. Afterwards
     `rg -n 'split\("/"\)\[2\]' tests/integration/live_stack/capture_cells.py` prints nothing.

   - the refusal:

     ```python
     def refusal_of(env: ToolResponse, method: str) -> dict[str, object]:
         """``vmcore.fetch``'s refusal of a non-core ``method``, with no job (ADR-0722)."""
         refused = {
             "error_category": env.error_category,
             "reason": env.data.get("reason"),
             "method": env.data.get("method"),
         }
         expected = {
             "error_category": ErrorCategory.CONFIGURATION_ERROR.value,
             "reason": REFUSED_REASON,
             "method": method,
         }
         assert refused == expected, f"vmcore.fetch({method}) was not refused: {refused}"
         assert not env.refs, f"the refused vmcore.fetch({method}) returned refs"
         return refused


     async def refused_method(
         run: CellRun,
         caller: HttpCaller,
         tree: Path,
         manifest: Mapping[str, Any],
         db_url: str,
         guest: Guest,
     ) -> dict[str, object]:
         """A crashed fixture System's Run refuses a non-core capture method, writing no job."""
         method = variant_of(run.cell)
         run_id, endpoint = await boot(run, guest, tree, manifest)
         role = grants(guest.project, GATES["vmcore.fetch"])
         await Session(run, guest, caller, role, manifest, run_id, endpoint).crash()
         jobs = partial(named_jobs, db_url, guest.system_id, run_id)
         before = await jobs()
         args = {"run_id": run_id, "method": method}
         answer = await caller.call("vmcore.fetch", args, caller.token(role), discover=True)
         refused = refusal_of(one(answer), method)
         assert await jobs() == before, "the refused fetch changed the jobs of its System or Run"
         await await_system_state(guest.op, "refused", guest.system_id, "crashed")
         return {"tool": run.cell.operation, **refused, "jobs_unchanged": True, "system": "crashed"}
     ```

   - `CaptureTarget` gains the last field `connect: Connect = worker_connect`. Docstring: "and
     the provider libvirt its domain is read through".
   - `_prepare_target`: collect the lane in the body (`lanes.append(guest.lane)` next to
     `ids.update`), and build the target with
     `lane_keys(scratch.observed, remote=run.cell.provider == "remote-libvirt")` and
     `connect=lanes[0].connect`.
   - `capture_target`: key `_TARGETS` by `(base_url, run.cell.provider)` and type it
     `dict[tuple[str, str], CaptureTarget | _Failed]`. Docstring: "Each provider of a stack has its
     own target." `_Failed` gains a last field `observed: dict[str, object]`. The `except` branch
     stores `lane_keys(scratch.observed, remote=run.cell.provider == "remote-libvirt")` there, and
     the replay runs `run.observed |= cached.observed` before raising. A failed remote target then
     still names the provider host it reached.
   - `protected_state(db_url, project, system_id, run_id, *, connect: Connect = worker_connect)`
     reads `domain_defined(system_id, connect)`. `snapshot_of` passes `connect=target.connect`.
     `capability` and `debug_cells` keep the default. `debug_cells.py` is a reader and is not
     edited.
   - Update the module docstring's first line to "Crash capture and introspection tool cells
     over HTTP on both providers (#2814, #3114, ADR-0722)". Add one paragraph: a remote cell runs
     the same bodies on the remote lane; its profile edits the `remote-libvirt` section; a
     `console`/`gdbstub` `vmcore.fetch` cell proves the handler refusal; a remote `host_dump`
     capture owns its pool dump volume.
4. Run the green command, then
   `uv run python -m pytest tests/integration/live_stack/test_capture_cells.py -q`. Expect a pass,
   including the unchanged local tests (`test_crash_profile_adds_only_its_options`,
   `test_lane_keys_drop_kernel_fields`). Run `just lint` and `just type`.
5. Controlled fault: in `refusal_of` drop the `assert not env.refs` line. Run the green command
   and expect `test_refusal_of_accepts_only_the_handler_refusal` to fail. Restore it with
   `git checkout -p` on that hunk only, or commit first and use `git checkout -- <file>`.
6. Commit: `test(live): run the capture cells on the remote provider lane`.

## Task 4 — Carrier and bindings

Files: modify `tests/integration/test_capture_tool_cells_live.py`,
`scripts/coverage_campaign/obligations.toml`; test `tests/scripts/test_coverage_contract.py`.

Interfaces: consumes `variant_of`, `refused_method`, `HANDLER_REFUSAL`,
`tool_cells.REMOTE_LANE_FAMILIES`.

Verification:

- Contract: remote bindings. Mode: focused-test.
  `test_coverage_contract.py::test_remote_capture_cells_bind_the_capture_carrier` and the updated
  `::test_pending_cells_have_owned_assertions_but_no_invented_nodes`; red: `node_id` `None` for
  the remote cells; green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q -k "capture or pending_cells"`.
- Contract: carrier selection and dispatch. Mode: task-test-not-applicable — the module is
  `live_stack`-marked and only runs against a stack. Its selection is the same predicate the System
  carrier uses, and the live run plus `qualify`'s `missing-result` check prove it.

Steps:

1. Append to `test_coverage_contract.py`. It reuses the module's existing `_CAPTURE_NODE`
   (line 149), so do not redefine it:

   ```python
   def test_remote_capture_cells_bind_the_capture_carrier(inventory: Inventory) -> None:
       cells = [c for c in build_contract(inventory=inventory).cells if c.owner == 3114]
       assert len(cells) == 180 and {c.node_id for c in cells} == {_CAPTURE_NODE}
   ```

   In `test_pending_cells_have_owned_assertions_but_no_invented_nodes`, replace
   `assert {c.node_id for c in capture if c.provider == "remote-libvirt"} == {None}` with:

   ```python
       remote_capture = [c for c in capture if c.provider == "remote-libvirt"]
       # A remote scenario id names no architecture either: the ppc64le cells (#2818's) share it.
       assert len(remote_capture) == 360 and {c.node_id for c in remote_capture} == {_CAPTURE_NODE}
   ```

2. Run the green command. Expect `{None}` in the new test's failure.
3. In `obligations.toml`, after the local `tool/vmcore.fetch/...` capture rows, add 45 rows
   `"tool/remote-libvirt/<tool>/<variant>/<kind>" = "tests/integration/test_capture_tool_cells_live.py::test_capture_tool_cell"`,
   sorted like their neighbours, for these `<tool>/<variant>` pairs:
   - `control.force_crash/default`
   - `introspect.from_vmcore/offline-vmcore`
   - `introspect.run/live`
   - `introspect.script/live-script`
   - `postmortem.crash/default`
   - `vmcore.fetch/console`, `vmcore.fetch/gdbstub`, `vmcore.fetch/host_dump`, `vmcore.fetch/kdump`

   each crossed with these `<kind>` values: `authentication`, `authorization`, `functional`,
   `project-isolation`, `validation`. Generate the rows, rather than type them:

   ```bash
   uv run python - <<'EOF'
   from scripts.coverage_campaign.contract import build_contract
   node = "tests/integration/test_capture_tool_cells_live.py::test_capture_tool_cell"
   ids = sorted({c.scenario_id for c in build_contract().cells if c.owner == 3114})
   print(len(ids)); print("\n".join(f'"{i}" = "{node}"' for i in ids))
   EOF
   ```

   Expect `45` and then the rows.
4. In `test_capture_tool_cells_live.py`:
   - docstring: "Prove the x86_64 crash capture and introspection tool cells over HTTP on both
     providers (#2814, #3114)". Replace the remote sentence with: "A remote cell runs on the
     provider host of `docs/operating/runbooks/remote-live-stack.md`; its handler-refused
     `vmcore.fetch` variants prove the refusal (ADR-0722, 2026-10-20)."
   - `_fixture(arch: str)` loads `load_fixture(Path(root), BASELINE, arch)`;
   - `_scenario`:

     ```python
         cell = run.cell
         fixture = partial(_fixture, str(cell.guest_arch))
         caller = HttpCaller(cast(Exposure, cell.exposure), base_url, issuer)
         if cell.kind == "rejection":
             boundary = boundary_of(cell)
             if boundary == "capability":
                 assert cell.provider == "local-libvirt", f"{cell.id}: no remote capability cell"
                 await capability(run, caller, base_url, issuer, db_url)
                 return
             target = await capture_target(run, base_url, issuer, db_url, fixture)
             call = rejection(cell.operation, boundary, target, method=variant_of(cell))
             await prove_rejection(run, caller, boundary, call, snapshot_of(db_url, target))
             return
         crash = options(cell)
         tree, manifest = fixture()
         project = f"cov-{secrets.token_hex(4)}"
         body: LaneBody
         if cell.unsupported_reason == HANDLER_REFUSAL:
             body = partial(refused_method, run, caller, tree, manifest, db_url)
         elif cell.kind == "unsupported":
             body = partial(unsupported, run, caller, tree, manifest)
         else:
             body = partial(functional, run, caller, tree, manifest)
     ```

     The `on_lane_system` call is unchanged.
   - `_cells`:

     ```python
     def _cells() -> list[Cell]:
         host = platform.machine()
         return [
             c
             for c in tool_cells(TOOLS)
             if (c.provider == "local-libvirt" and c.guest_arch == host)
             or (c.provider == "remote-libvirt" and c.guest_arch in REMOTE_LANE_FAMILIES)
         ]
     ```

   - imports: `HANDLER_REFUSAL` from the contract; `refused_method` and `variant_of` from
     `capture_cells`; `REMOTE_LANE_FAMILIES` from `tool_cells`.
5. Run the green command, then
   `uv run python -m pytest tests/integration/test_capture_tool_cells_live.py --collect-only -q -m live_stack`.
   Expect 164 + 180 = 344 collected parameters on an x86_64 host. Run `just coverage-check`,
   `just lint` and `just type`.
6. Commit: `test(live): bind the remote capture cells to the capture carrier`.

## Task 5 — Runbook

Files: modify `docs/operating/runbooks/remote-live-stack.md`.

Verification:

- Contract: runbook section. Mode: task-test-not-applicable — operator prose with no executable
  consumer; `just docs-check` checks its links and paths.

Steps:

1. After the last numbered section (after Task 0's merge, the one after "Remote power, snapshot
   and control tool cells (#3116)"), add a section numbered one higher and titled
   `Remote capture and introspection tool cells (#3114)`. Refer to other sections by title, not
   by number. Content:
   - the carrier node id and the 180 cells: 28 functional, 8 unsupported and 144 rejection,
     selected with `-k remote-libvirt`;
   - the differences from the local cells, per the spec: the remote profile options;
     `console`/`gdbstub` proving the handler refusal on a crashed Run (ADR-0722, 2026-10-20);
     the `host_dump` pool volume `kdive-host-dump-<system>.kdump` being owned and proven absent;
     drgn-live running in the guest over the guest agent; one remote rejection target per stack;
   - prerequisites. Those of "Remote power, snapshot and control tool cells (#3116)" apply. In addition, the staged `fedora-kdive-remote-base-43` must be built
     after #3236 so its GRUB writes to the serial console that `control.force_crash` reads. A
     volume staged earlier is skipped by the image role, so rebuild it with
     `deploy/ansible/playbooks/image.yml` and force the rebuild;
   - that section's command block, with the carrier path
     `tests/integration/test_capture_tool_cells_live.py` and the same `bindings --remote
     --kernel-baseline longterm`;
   - each configuration records 90 cells and skips the other configuration's 90. The 4 functional
     cells of `introspect.run` and `introspect.script` per configuration record `blocked` on #3124
     until it closes. A remote kdump, console or dump-volume failure is a failing cell. The provider check after the run: no
     `kdive-` domain, and no `kdive-` overlay or `kdive-host-dump-` volume in the pool.
2. `just docs-check`, exit 0.
3. Commit: `docs(runbook): run the remote capture tool cells`.

## Task 6 — Live proof (operator-granted only)

Files: none. Evidence stays under the run's `KDIVE_ARTIFACT_DIR`; only public-safe summaries
are published.

Verification:

- Contract: live cells. Mode: task-test-not-applicable — act only against a live stack and a
  provider host; proven by the run and `qualify`.

Steps (only with an explicit grant naming the hosts, cap and cleanup):

1. Deploy the committed head on the granted control plane, and confirm the deployed revision
   equals `git rev-parse HEAD`. Then run the new runbook section's block for `default`, then
   `recovery`.
2. Assemble and `qualify`. Record the outcome counts per configuration. Every failing or blocked
   cell is a finding with its cell id, reported and not fixed here.
3. Cleanup: `demo-down.sh --wipe --yes`, the new section's provider check, shredding the scratch CA key,
   and the host restores the grant names.

## Deferrals and follow-ups

- Remote `introspect.run`/`introspect.script` functional cells: blocked on #3124 (blocked by
  #3121). When it closes, drop its `REMOTE_DEBUGINFO_BLOCKED` entries and prove the 8 cells.
