# Remote debug tool cells on x86_64 (#3113) — plan

Goal: implement [the spec](../specs/2026-10-20-remote-debug-tool-cells-design.md). The local
debug carrier runs the 440 x86_64 `remote-libvirt` `debug.*` cells on #3116's remote lane, adds a
gdbstub transport observer at both ends, and binds the cells. Python 3.14, uv, just, pytest,
libvirt-python, psycopg.

Global Constraints:

- No product source (`src/`), ADR, migration or `contract.py` change. ADR slot 0791 stays unused.
  The matrix digest moves only by the new bindings.
- Test-only environment inputs stay unprefixed (`REMOTE_PROVIDER_SSH`); add no `KDIVE_` variable.
- Local-libvirt cells keep their observed values. Every new branch keys on
  `provider == "remote-libvirt"`.
- Evidence and artifacts carry no host name, address or SSH destination (ADR-0715). The gdbstub
  address is used for the socket query only and never recorded.
- Prerequisite: the branch's base is a `main` that contains #3116 (PR #3287).
  Tasks 1–3 consume its `tool_cells.Connect`, `Lane.connect`, `Lane.installed_kernel` and
  `power_snapshots.domain_defined(system_id, connect)`. They also need its
  `capture_cells.boot` using `guest.lane.installed_kernel`. Nothing here depends on #3114.

Expected implementation size: 430–560 changed lines (L) — `debug_cells.py` ~110, live test ~25,
`remote_lifecycle.py` rename ~6, unit tests ~150, `obligations.toml` 110, contract test ~5,
runbook ~70.

Guardrails per task: `just lint`, `just type`, and the focused pytest named in the task. The
pre-push hook runs `just ci` once before the push.

## Task 0 — refresh the base onto #3116

Steps:

1. `git fetch origin && git merge-base --is-ancestor <#3287 merge commit> origin/main` exits 0.
2. `git merge --no-edit origin/main` (the repository's base-refresh practice; it never needs a
   force push, whether or not the design commit was published).
3. `rg -n "installed_kernel=guest.lane.installed_kernel" tests/integration/live_stack/capture_cells.py`
   prints one line, and `rg -n "def domain_defined\(system_id: str, connect" tests/integration/live_stack/power_snapshots.py` prints one line.

Verification: `task-test-not-applicable` — a base refresh changes no contract of this issue; step 3 is
the structural check that the consumed names exist.

## Task 1 — the carrier runs a remote cell

Files: `tests/integration/live_stack/debug_cells.py`,
`tests/integration/live_stack/remote_lifecycle.py`,
`tests/integration/live_stack/test_debug_cells.py`.

Interfaces produced: `REMOTE = "remote-libvirt"`, `REMOTE_DRGN_BLOCKED: str`,
`variant_of(cell: Cell) -> str`,
`gdbstub_endpoint(xml: str) -> tuple[str, int]`, `Transport(dest: str, addr: str, port: int)`,
`sockets(t: Transport) -> dict[str, int]`, `settle(t: Transport, expected: int) -> dict[str, int]`,
`transport_of(guest: Guest) -> Transport`, `released(s: Session, t: Transport) -> dict[str, object]`,
`snapshot_of(db_url: str, target: DebugTarget, connect: Connect) -> Snapshot`;
`remote_lifecycle.host_ssh(dest: str, command: str, timeout: float)`.
Consumed: `tool_cells.Connect`, `Lane.xml`, `Lane.provider`, `power_snapshots.domain_defined`,
`capture_cells.named_jobs(db_url, *ids)`, `tool_cells.project_state(db_url, project)`,
`remote_lifecycle.destination()`, `HOST_SSH_ENV`, `kdive.providers.shared.libvirt_xml.QEMU_NS`.

Verification:

- `focused-test` — remote profile unchanged: `test_gdbstub_profile_leaves_a_remote_profile_alone`
  fails red with `KeyError: 'local-libvirt'`; green
  `uv run python -m pytest tests/integration/live_stack/test_debug_cells.py -q`.
- `focused-test` — variant of either provider's id: `test_variant_reads_local_and_remote_ids`
  asserts `variant_of(c) in {"gdbstub", "drgn-live"}` for every `debug.start_session` cell of
  both providers, and `_session` built from a remote id gives `Session.variant == "drgn-live"`;
  red `ImportError` before `variant_of`, then red `AssertionError`
  (`'debug.start_session' != 'drgn-live'`) while `Session.variant` still reads index 2; same
  green command.
- `focused-test` — drgn-live symptom gate: `test_remote_drgn_live_stops_blocked_on_its_symptom`
  (remote `_session`, start answers `missing_debuginfo={"reason": "debuginfo_unloadable"}`)
  raises `ScenarioStop` with `Outcome.BLOCKED` naming #3124 and `debuginfo_unloadable`, and the
  started session is ended by `end_all`; `test_local_drgn_live_warning_still_fails` keeps the
  local `AssertionError`; `test_remote_drgn_live_without_warning_runs_the_assertions` reaches
  the `introspect.run` proof. Red: the remote case raises `AssertionError` before the gate; same.
- `focused-test` — `gdbstub_endpoint` reads `-gdb tcp:<addr>:<port>` and fails without it:
  `test_gdbstub_endpoint_reads_the_qemu_argument` red `ImportError`; same.
- `focused-test` — `released` passes on `{0,0}` then `{1,1}` then `{0,0}` with every row
  detached; it fails on leftover sockets, a calibration that never reaches 1 (server not on this
  host), or an open session row: `test_released_*` (four cases) with `sockets` and `_records`
  monkeypatched and `debug_cells._SETTLE_DELAY_S` set to 0; red `ImportError`; same.
- `focused-test` — a failed calibration still ends its session:
  `test_functional_ends_the_calibration_session_on_failure` drives `functional`'s remote tail
  with `settle` raising on `expected=1` and asserts `debug.end_session` was called for the
  calibration session; red (session left in `Session.sessions`) before the tail moves inside the
  `try`; same.
- `focused-test` — the remote target proves its transport: `test_remote_target_records_transport`
  runs `_prepare_target`'s body with fakes (`on_lane_system` calling the body with a remote
  `Guest`, `boot` returning a run id, `settle` recording calls) and asserts `settle` saw `1` then
  `0` and the summary artifact holds `transport`; red (no `settle` call); same.
- `focused-test` — target cache per provider and remote host keys:
  `test_debug_target_is_cached_per_provider` with `_prepare_target` monkeypatched to return two
  distinct targets for `local-libvirt` and `remote-libvirt` cells of one base URL; red (one
  target returned twice); same.
- `focused-test` — snapshot reads `domain_defined` through the given connect:
  `test_snapshot_reads_the_lane_connect` with `domain_defined`, `project_state`, `named_jobs`
  monkeypatched, asserting the connect passed through; red `TypeError` (old 2-arg signature).
- `focused-test` — rejection pairs cover every remote boundary:
  `test_rejection_covers_every_boundary_of_every_tool` parametrized over both providers, reading
  the variant with `variant_of(c)` and asserting `call.args["transport"] in {"gdbstub",
  "drgn-live"}` for `debug.start_session`; red `ImportError` before `variant_of`; same.

Steps (write each test, run it red, implement, run green, commit once at the end of the task):

1. `remote_lifecycle.py`: rename `_host_ssh` to `host_ssh` and its two callers (`host_probe`,
   `volume_sha256`). No other change.
2. `debug_cells.py` constants and helpers, after `_REANSWER_S`:

```python
REMOTE = "remote-libvirt"
# drgn-live runs kdive-drgn in a remote guest, which never receives the Run's vmlinux; the start
# envelope's missing_debuginfo warning is that defect's symptom (#3124, blocked by #3121).
REMOTE_DRGN_BLOCKED = "remote guests do not receive the uploaded vmlinux (#3124)"
_HOST_KEYS = ("host_os", "host_arch")
_GDB_TCP = re.compile(r"tcp:(?P<addr>[^:\s]+):(?P<port>\d+)")
_SETTLE_POLLS = 10
_SETTLE_DELAY_S = 1.0


def variant_of(cell: Cell) -> str:
    """The tool variant of a local (``tool/<t>/<v>/<k>``) or remote scenario id."""
    return cell.scenario_id.split("/")[-2]
```

3. `gdbstub_profile`: after the deep copy, `sections = cast(dict[str, Any], edited["provider"])`;
   `if REMOTE in sections: return edited` with the comment `# remote provisions a gdbstub on
   every System and resolves GDBSTUB, so the install pins nokaslr (ADR-0083)`; the local branch
   is unchanged. Docstring: "with the local-libvirt gdbstub on; a remote-libvirt profile is
   returned as a copy".
4. `Session.variant` returns `variant_of(self.run.cell)`. In `start_body`'s drgn-live branch,
   replace the `missing_debuginfo` assertion with:

```python
        warning = env.data.get("missing_debuginfo")
        if warning is not None and s.run.cell.provider == REMOTE:
            reason = cast(dict[str, Any], warning).get("reason")
            raise ScenarioStop(Outcome.BLOCKED, f"{REMOTE_DRGN_BLOCKED}: {reason}")
        assert warning is None, "drgn-live attached without debuginfo"
```

   The session id was already appended to `s.sessions`, so `functional`'s `finally` ends it.
5. Transport observer, a new section `# --- remote transport ---` before `# --- functional cells`:

```python
def gdbstub_endpoint(xml: str) -> tuple[str, int]:
    """The ``-gdb tcp:<addr>:<port>`` endpoint a domain's qemu command line records."""
    root = ET.fromstring(xml)  # noqa: S314 - the provider's own domain XML  # nosec B314
    args = [arg.get("value") for arg in root.iter(f"{{{QEMU_NS}}}arg")]
    for flag, value in zip(args, args[1:], strict=False):
        found = _GDB_TCP.fullmatch(value or "") if flag == "-gdb" else None
        if found is not None:
            return found["addr"], int(found["port"])
    raise AssertionError("the domain records no -gdb tcp:<addr>:<port> argument")


@dataclass(frozen=True)
class Transport:
    """A remote System's gdbstub: the provider host (``dest``) and the endpoint gdb dials."""

    dest: str
    addr: str
    port: int


def _lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip())


def sockets(t: Transport) -> dict[str, int]:
    """ESTABLISHED sockets on the gdbstub port at the provider host and at this host's server."""
    command = f"ss -Htn state established '( sport = :{t.port} )'"
    provider = host_ssh(t.dest, command, 60.0)
    assert provider.returncode == 0, f"provider-host ss exited {provider.returncode}"
    local = subprocess.run(  # noqa: S603,S607 - fixed argv  # nosec B603 B607
        ["ss", "-Htn", "state", "established", "dst", f"{t.addr}:{t.port}"],
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )
    assert local.returncode == 0, f"ss exited {local.returncode}"
    return {"provider": _lines(provider.stdout), "server": _lines(local.stdout)}


async def settle(t: Transport, expected: int) -> dict[str, int]:
    """Poll until both ends count ``expected`` sockets; fail after ``_SETTLE_POLLS`` reads."""
    seen: dict[str, int] = {}
    for _ in range(_SETTLE_POLLS):
        seen = await asyncio.to_thread(sockets, t)
        if set(seen.values()) == {expected}:
            return seen
        await asyncio.sleep(_SETTLE_DELAY_S)
    raise AssertionError(f"gdbstub sockets read {seen}, expected {expected} at each end")


def transport_of(guest: Guest) -> Transport:
    """The remote System's gdbstub, from its domain XML on the provider host."""
    dest = destination()
    assert dest, f"{HOST_SSH_ENV} is unset on a remote cell"
    addr, port = gdbstub_endpoint(guest.lane.xml(guest.system_id))
    return Transport(dest, addr, port)


async def _all_detached(s: Session) -> None:
    rows = await _records(s.db_url, s.run_id)
    left = [row for row in rows if row[1] != "detached"]
    assert rows and not left, f"session rows still open: {left}"


async def released(s: Session, t: Transport) -> dict[str, object]:
    """The body's sessions are closed at both ends; a fresh attach is then seen and released.

    The attach comes after the body, so a listing body sees only its own rows. It proves the
    observer sees this System's transport: a server on another host fails it.
    """
    after = await settle(t, 0)
    await _all_detached(s)
    session_id = await s.start()
    await s.register(session_id)
    attached = await settle(t, 1)
    await s.end(session_id)
    await settle(t, 0)
    await _all_detached(s)
    return {"after": after, "attached": attached, "sessions_open": 0}
```

6. `functional`: the remote tail runs inside the `try`, so the `finally` also ends a calibration
   session whose check failed:

```python
    try:
        observed = await BODIES[run.cell.operation](session)
        if guest.lane.provider == REMOTE:
            await session.end_all()
            transport = await asyncio.to_thread(transport_of, guest)
            observed["transport"] = await released(session, transport)
    finally:
        await session.end_all()
```

   Docstring gains: "A remote cell then proves the gdbstub transport released at both ends."
7. Rejection target: `_TARGETS: dict[tuple[str, str], DebugTarget | _Failed]`, keyed
   `(base_url, run.cell.provider)` in `debug_target`. In `_prepare_target`, the returned
   `observed` is `lane_keys(scratch.observed)` plus, when `run.cell.provider == REMOTE`,
   `{key: scratch.observed[key] for key in _HOST_KEYS if key in scratch.observed}`. In its
   `body`, on the remote lane (`guest.lane.provider == REMOTE`), take
   `transport = await asyncio.to_thread(transport_of, guest)` after the boot; after the
   `target-attach` read, record `attached = await settle(transport, 1)`; after the
   `ended.status` assertion, record `after = await settle(transport, 0)`. Keep both in a local
   `proof: dict[str, object]` and add `"transport": proof` to the summary artifact's payload when
   it is set (the local target's summary is unchanged).
8. Replace `snapshot_of` and drop the `protected_state` import:

```python
async def protected_state(db_url: str, target: DebugTarget, connect: Connect) -> dict[str, object]:
    """The target project's rows, its System and Run jobs, and whether its provider defines it."""
    return {
        "project": await project_state(db_url, target.project),
        "jobs": await named_jobs(db_url, target.system_id, target.run_id),
        "domain_defined": await asyncio.to_thread(domain_defined, target.system_id, connect),
    }


def snapshot_of(db_url: str, target: DebugTarget, connect: Connect) -> Snapshot:
    return partial(protected_state, db_url, target, connect)
```

9. Module docstring: "Local and remote GDB debug tool cells over HTTP (#2813, #3113,
   ADR-0722)", plus one paragraph naming the remote lane, the drgn-live block and the transport
   proof.
10. Tests in `test_debug_cells.py`, with the remote cells from
    `build_contract()` (`provider == "remote-libvirt" and guest_arch == "x86_64"`). The scripted
    debugger and `_session` gain a `lane=SimpleNamespace(provider=..., xml=lambda _s: XML)`
    where needed. XML fixture:
    `<domain xmlns:qemu="http://libvirt.org/schemas/domain/qemu/1.0"><qemu:commandline>`
    `<qemu:arg value="-gdb"/><qemu:arg value="tcp:192.0.2.10:5901"/></qemu:commandline></domain>`.
11. Run `uv run python -m pytest tests/integration/live_stack/test_debug_cells.py
    tests/integration/live_stack/test_remote_lifecycle.py -q`: all pass. Then `just lint` and
    `just type`: exit 0. Commit
    `test(live): run the debug cells on the remote lane with a transport proof`.

Acceptance: local cells' bodies, rejection calls and snapshots are unchanged except for the
explicit `connect`; every new remote branch is unit-covered red then green.

## Task 2 — the live test carries and binds the remote cells

Files: `tests/integration/test_debug_tool_cells_live.py`,
`scripts/coverage_campaign/obligations.toml`, `tests/scripts/test_coverage_contract.py`.

Interfaces consumed: Task 1's `variant_of`, `snapshot_of(db_url, target,
connect)`; `tool_cells.lane_for(cell).connect`, `tool_cells.REMOTE_LANE_FAMILIES`.

Verification:

- `focused-test` — contract: in
  `test_debug_owners_and_carrier_follow_the_approved_split`, replace the last line with
  `remote = [c for c in cells if c.provider == "remote-libvirt"]` and
  `assert len(remote) == 880 and {c.node_id for c in remote} == {_DEBUG_NODE}` (the remote
  ppc64le cells, #2818's, share the provider-qualified ids). Red before the bindings
  (`{None} != {_DEBUG_NODE}`); green
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.
- `task-test-not-applicable` — the live test's `_scenario`/`_cells`: they run only against a
  live stack. The collection itself is checked: `uv run python -m pytest
  tests/integration/test_debug_tool_cells_live.py --collect-only -q -m live_stack | rg -c
  remote-libvirt/x86_64` prints `440`.

Steps:

1. `obligations.toml`: after `"tool/debug.start_session/gdbstub/validation"`, add the 110 lines
   `"tool/remote-libvirt/<tool>/<variant>/<kind>" = "tests/integration/test_debug_tool_cells_live.py::test_debug_tool_cell"`
   generated from the contract, sorted:

```bash
uv run python - <<'EOF'
from scripts.coverage_campaign.contract import build_contract
node = "tests/integration/test_debug_tool_cells_live.py::test_debug_tool_cell"
ids = sorted({c.scenario_id for c in build_contract().cells if c.owner == 3113})
print("\n".join(f'"{i}" = "{node}"' for i in ids))
EOF
```

   It prints 110 lines.
2. Live test `_fixture(arch: str)` loads `load_fixture(Path(root), BASELINE, arch)`. `_scenario`:
   use `fixture = partial(_fixture,
   str(cell.guest_arch))`, `variant = variant_of(cell)`, and
   `snapshot_of(db_url, target, lane_for(cell).connect)`. `_cells()` adds
   `or (c.provider == "remote-libvirt" and c.guest_arch in REMOTE_LANE_FAMILIES)`. The module
   docstring names #3113 and the remote runbook section.
3. Contract test as above; run it green; `just lint`, `just type`; commit
   `test(coverage): bind the remote debug tool cells`.

## Task 3 — runbook and the live run

Files: `docs/operating/runbooks/remote-live-stack.md` (new section after #3116's).

Verification: `task-test-not-applicable` — runbook prose has no executable consumer;
`just docs-links` and `just docs-paths` are the structural gates.

Steps:

1. Add a section "Remote debug tool cells (#3113)". Cover the selection
   (`tests/integration/test_debug_tool_cells_live.py -k remote-libvirt`) and the bindings command
   (`tool_cells bindings --remote --kernel-baseline longterm`). Name the prerequisites: §9's
   fixture and object store, and the §2 gdbstub ACL from the control plane (whose MCP server
   holds the gdb session) to the provider's
   `gdbstub_range`. State the expected counts per configuration while #3124 is open (44
   functional: 42 `success`, 2 `blocked` drgn-live on #3124's `missing_debuginfo` symptom; 176
   `rejection`), and that `qualify` then qualifies 436 of 440 and exits non-zero by design. Cover
   the transport proof, the post-run provider check, and a "Last run" paragraph written from the
   live evidence.
2. Live run, only under the operator's grant. Use the remote pair after #3116/#3185 release it.
   Rebuild the provider image with `image.yml -e force_image_rebuild=true`. Run `default`, then
   `recovery` on a wiped stack, then `evidence assemble` and `qualify` (expected: 436 qualified,
   the 4 drgn-live cells listed `missing-prerequisite`). Afterwards, on the
   provider, `virsh list --all` shows no `kdive-` domain and the pool holds no `kdive-` overlay;
   then run `demo-down.sh --wipe --yes`. A failing cell is recorded and reported, not fixed here.
3. Commit `docs(runbook): run the remote debug tool cells`.
