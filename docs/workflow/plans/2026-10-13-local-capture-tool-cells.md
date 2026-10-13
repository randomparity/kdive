# Local capture tool cells — implementation plan

Goal: bind and prove the 164 x86_64 local-libvirt cells of the six capture and
introspection tools over HTTP (#2814), per
`docs/workflow/specs/2026-10-13-local-capture-tool-cells-design.md`.

Architecture: a contract edit routes group 2814 and drops its `authority`
flag; one scenario module (`tests/integration/live_stack/capture_cells.py`)
holds the functional bodies, provision options, rejection arguments and the two
cached targets; one carrier (`tests/integration/test_capture_tool_cells_live.py`)
parametrizes the contract cells through `run_tool_cell`.

Tech stack: Python 3.14, pytest, psycopg, httpx, libvirt-python, the
`tests/integration/live_stack` frame.

Expected implementation size: 900–1300 changed lines (L) — the file map below:
scenario module ~550, its unit tests ~300, carrier ~110, contract and contract
tests ~90, runbook ~60, `deep_body` ~5.

## Global Constraints

- Guardrails: `just lint`, `just type` (whole tree), focused
  `just test-verbose <paths>`; full `just ci > <file> 2>&1 < /dev/null` before
  push.
- No new dependency. No product source, migration or ADR change.
- Evidence records carry digests, booleans, counts and fixture identities, never
  console text, transcript text or core bytes (ADR-0715).
- Shell commands sent to the guest are fixed strings or interpolate only
  generated hex tokens.
- New names under `scripts/` carry no `KDIVE_` prefix; this plan adds none.

## File map

| File | Change | Owns after |
|---|---|---|
| `scripts/coverage_campaign/obligations.toml` | modify | group 2814 without `authority`; `[implementations]` rows for the 2814 local scenarios |
| `scripts/coverage_campaign/contract.py` | modify | 2814 → 2818 (ppc64le), 2814 → 3114 (remote) routing |
| `tests/scripts/test_coverage_contract.py` | modify | owner, role and binding assertions for group 2814 |
| `tests/integration/live_stack/deep_lifecycle.py` | modify | `deep_body` returns the Run id it created |
| `tests/integration/live_stack/capture_cells.py` | create | capture scenarios, targets, options, rejection arguments |
| `tests/integration/live_stack/test_capture_cells.py` | create | unit tests of the pure helpers |
| `tests/integration/test_capture_tool_cells_live.py` | create | the carrier test function |
| `docs/operating/runbooks/live-testing.md` | modify | how to run the capture cells; last-run record |

## Task 1 — contract routing and the authority flag

Verification:

- Contract: group 2814 roles exclude `authority`. Mode: focused-test.
  `tests/scripts/test_coverage_contract.py::test_capture_owners_follow_the_approved_split`;
  red before the edit (roles carry `authority`, owners are all 2814); green:
  `just test-verbose tests/scripts/test_coverage_contract.py`.
- Contract: ppc64le → 2818, remote-libvirt → 3114, local x86_64 → 2814 (164
  cells: 28 functional, 4 unsupported, 132 rejection). Mode: focused-test, same
  test.

Steps:

1. Add the test:

   ```python
   _CAPTURE_TOOLS = {
       "control.force_crash",
       "introspect.from_vmcore",
       "introspect.run",
       "introspect.script",
       "postmortem.crash",
       "vmcore.fetch",
   }


   def test_capture_owners_follow_the_approved_split(inventory: Inventory) -> None:
       # Operator split (2026-10-06) and decision D1-A (2026-10-13): #2814 proves the x86_64
       # local cells without the authority role; remote is #3114's, ppc64le the POWER lane's.
       cells = [c for c in build_contract(inventory=inventory).cells if c.operation in _CAPTURE_TOOLS]
       assert all("authority" not in c.roles for c in cells)
       for cell in cells:
           expected = (
               2818
               if cell.guest_arch == "ppc64le"
               else 3114
               if cell.provider == "remote-libvirt"
               else 2814
           )
           assert cell.owner == expected, cell.id
       local = [c for c in cells if c.owner == 2814]
       kinds = Counter(c.kind for c in local)
       assert (len(local), kinds["functional"], kinds["unsupported"]) == (164, 28, 4)
   ```

2. Run it: red (authority in roles; owner 2814 everywhere).
3. In `obligations.toml`, delete `authority = true` from the `owner = 2814`
   group.
4. In `contract.py` `_tool_cells`, extend the routing: `group.owner in (2814,
   2815, 3062, 3112, 3119) and arch == "ppc64le"` → 2818, and a new branch
   `elif group.owner == 2814 and provider == "remote-libvirt": owner = 3114`;
   update the routing comment.
5. Run the test: green. Run the whole contract test file: green (the matrix
   digest is not pinned by any test; if one asserts the digest, update it from
   the failure message).
6. Commit `test(coverage): route #2814's cells by the approved split`.

## Task 2 — `deep_body` returns its Run

Verification: Mode: task-test-not-applicable — `deep_body` only runs against a
live stack; its return value is consumed and asserted by the capture carrier
(Task 4), and `just type` checks that its four existing callers
(`test_deep_lifecycle_live.py`, `test_remote_deep_lifecycle_live.py`,
`test_run_tool_cells_live.py`, `live_stack/power_snapshots.py`) ignore it.

Steps: change `deep_body`'s return annotation to `str`, keep `run_id` defined
before the `try`, and `return run_id` after the `finally`. Update the docstring
first line to say it returns the Run id. Commit with Task 3.

## Task 3 — the scenario module

Interfaces it provides (used by Task 4):

```python
TOOLS: tuple[str, ...]                     # the six tools
def options(cell: Cell) -> str  # "kdump" | "host_dump" | "live"; ppc64le: blocked stop
def crash_profile(profile: dict[str, object], options: str) -> dict[str, object]
def grants(project: str, role: str | None) -> Grants   # reused from power_snapshots
async def functional(run, caller, token, tree, manifest, guest) -> dict[str, object]
async def unsupported(run, tree, manifest, guest) -> dict[str, object]
async def capture_target(run, base_url, issuer, db_url) -> CaptureTarget
def rejection(tool: str, boundary: Boundary, target: CaptureTarget) -> Rejection
def snapshot_of(db_url: str, target: CaptureTarget) -> Snapshot
async def capability(run, caller, base_url, issuer, db_url) -> None  # lane frame
```

Pure helpers (unit-tested):

- `vmcoreinfo(core: BinaryIO) -> bytes` reads the VMCOREINFO block only: for
  an ELF64 core (`\x7fELF`), the `PT_NOTE` note named `VMCOREINFO`; for a
  kdump-compressed core (`KDUMP   ` signature), the block at the sub-header's
  `offset_vmcoreinfo`/`size_vmcoreinfo` (sub-header at `block_size`, the
  `int` at header offset 428; the two fields at sub-header offsets 32 and 40).
  Any other format, or no block, raises `AssertionError`.
- `require_core(info: bytes, manifest)` requires exactly one `BUILD-ID=` line
  equal to `manifest["build_id"]` and one `OSRELEASE=` line equal to
  `manifest["release"]`.
- `lane_keys(observed) -> dict[str, object]` keeps `guest_os`, `guest_arch`,
  `accelerator` and `image_sha256` only.
- `panic_seen(text: str, release: str) -> bool`: the text contains
  `Kernel panic - not syncing` and `release`.
- `transcript_names(transcript, *facts) -> list[str]`: the facts missing from
  a postmortem transcript.
- `rejection(...)` arguments: id arguments per tool (`system_id`; `run_id`;
  `session_id` plus `helper="sysinfo"` or `script="print(1)"`), gate roles
  `{"control.force_crash": "admin", "introspect.from_vmcore": "viewer"}`, every
  other tool `contributor`, and the one-below table `{"admin": "operator",
  "contributor": "viewer", "viewer": None}`; isolation categories as the spec's
  table.

Verification:

- Contract: the VMCOREINFO reader on synthetic ELF and kdump-compressed cores
  (a stray `OSRELEASE=%s` outside the note is ignored; an unknown format and a
  mismatched build ID fail). Mode: focused-test,
  `tests/integration/live_stack/test_capture_cells.py::test_vmcoreinfo_*`;
  red before the module exists (import error); green:
  `just test-verbose tests/integration/live_stack/test_capture_cells.py`.
- Contract: per-tool provision options and the profile edit. Mode:
  focused-test, `test_crash_profile_*`, same command.
- Contract: rejection arguments, grants and categories for every tool and
  boundary. Mode: focused-test, `test_rejection_*`, same command.
- Contract: console panic and transcript matching. Mode: focused-test,
  `test_panic_seen_*`, `test_transcript_names_*`, same command.
- Contract: a rejection record carries no kernel field. Mode: focused-test,
  `test_lane_keys_drop_kernel_fields`, same command.
- Contract: `missing_dependency` stops as blocked. Mode: focused-test,
  `test_missing_dependency_blocks`, same command (`require_tool(env, phase)`
  raises `ScenarioStop(BLOCKED)` on that category, `AssertionError` on any other
  error).
- Contract: live bodies (crash, capture, introspection, sessions, targets).
  Mode: task-test-not-applicable — they drive a real stack and guest; Task 4's
  live run is their proof, and the unit tests above cover every pure decision
  they make.

Steps: write the unit tests, observe the import-error red, write the module,
observe green, `just lint` and `just type`, commit
`test(live): add the capture tool-cell scenarios (#2814)`.

Functional bodies follow the spec's table. Shared steps:

- `boot(run, guest, tree, manifest)` runs `deep_body` in a scratch directory,
  appends its assertions' artifacts to `run.artifacts`, returns the Run id and
  a fresh SSH endpoint.
- `sentinel(endpoint, key) -> tuple[str, int]` copies `/usr/bin/sleep` to
  `/root/kcov<hex>`, starts it detached for 3600 s and returns its name and pid.
- `armed(endpoint, key)` polls `cat /sys/kernel/kexec_crash_loaded` every 5 s
  for 600 s until `1`.
- `crash(op_admin, system_id)` calls `control.force_crash`, drains the job to
  `succeeded` and waits for `crashed` (`spine.drain_job`,
  `spine.await_system_state`).
- `capture(client, run_id, method)` calls `vmcore.fetch`, drains the job,
  requires `succeeded`, returns `spine.captured_vmcore_refs` and asserts
  `spine.raw_vmcore_refs` is empty.
- `raw_core(op, run_id, scratch) -> tuple[str, bytes]` calls
  `artifacts.fetch_raw(run_id, asset="vmcore")` twice, streams each
  `refs.download_uri` with `httpx` to a scratch file while hashing, requires
  equal SHA-256 and sizes equal to `data.size_bytes`, and returns the digest and
  `vmcoreinfo` of the file.

`capture_target` follows `tool_cells.lane_target`: a module-level cache keyed by
`base_url`, a `_Failed` replay, the frame `on_lane_system` with `target`
options run on a scratch `CellRun`, a body that boots the fixture, opens a
drgn-live session and ends it, then `tool_cells.settled` on the project after
the frame's cleanup. The cached `observed` is `lane_keys(scratch.observed)`.

`capability` runs `on_catalog_system(run, ..., image=LANE_IMAGES[arch], body,
provision=<host_dump wrapper>)`. The body calls
`tool_cells.observe_guest(run, op, system_id, scratch, entry)`, opens an
investigation, `runs.create(investigation_id=..., system_id=...,
build_profile=spine.build_profile("x86_64"))`, crashes through an admin client,
waits on `tool_cells.settled(db_url, project)`, then
`tool_cells.prove_rejection(run, caller, "capability", Rejection({"run_id":
run_id, "method": "fadump"}, grants(project, "contributor")), snapshot)` where
the snapshot is the Run's jobs and artifacts and the System's state; finally it
closes the investigation. The frame proves `cleanup`.

## Task 4 — the carrier and the bindings

Verification:

- Contract: all 324 local capture cells (164 x86_64, 160 ppc64le, which share
  the scenario ids) bind the carrier node; remote cells stay unbound. Mode: focused-test, extend
  `test_pending_cells_have_owned_assertions_but_no_invented_nodes` with the
  capture count and node, and add the six tools to its `bound` set; red before
  the `[implementations]` rows; green with the contract test command.
- Contract: the live cells. Mode: task-test-not-applicable for CI — the carrier
  is `live_stack`-marked and runs only against a candidate stack; its first
  real run is recorded in Task 5.

Steps:

1. Write `tests/integration/test_capture_tool_cells_live.py` on the pattern of
   `test_power_snapshot_tool_cells_live.py`: `_fixture()` blocked when
   `KDIVE_FIXTURE_ROOT` is unset or unverified; `_scenario` dispatches
   `functional`, `unsupported`, `capability` or the rejection path by cell kind
   and boundary; `_cells()` filters `provider == "local-libvirt"` and
   `guest_arch == platform.machine()`; one parametrized
   `test_capture_tool_cell(cell)`.
2. Add one `[implementations]` row per local scenario id of the six tools
   (functional, each boundary, and `capability` for fadump), all naming
   `tests/integration/test_capture_tool_cells_live.py::test_capture_tool_cell`.
3. Extend the contract test; run it green; `just lint`, `just type`.
4. Commit `test(live): prove local capture tool cells over HTTP (#2814)`.

## Task 5 — runbook and live proof

Verification: Mode: task-test-not-applicable — prose in a runbook, with no
executable consumer; the live run itself is the proof.

Steps: add a "Capture and introspection tool cells" subsection to
`docs/operating/runbooks/live-testing.md` beside the power/snapshot one: the
bindings command (`python -m tests.integration.live_stack.tool_cells bindings
--candidate SHA --out FILE --kernel-baseline longterm`), the carrier command,
the lane image and fixture prerequisites, and the last-run record (date,
candidate, configuration, per-kind counts, any blocked or failing cells).
Run both configurations on the assigned lab x86_64 host; record results.
Commit `docs(live-testing): run the capture tool cells (#2814)`.
