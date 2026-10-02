# Catalog image smoke — implementation plan

Goal: produce version-1 coverage evidence for every native `image-smoke` cell (#2808), using a
reusable evidence seam.
Architecture: three helper modules under `tests/integration/live_stack/` hold the evidence seam
(`evidence.py`), the owned-cleanup check (`cleanup.py`), and the image-specific probes and
bindings (`image_smoke.py`). One `live_stack` test drives the public MCP surface over HTTP. The
offline qualifier (`python -m scripts.coverage_campaign qualify`) remains the judge.
Spec: [design](../specs/2026-10-01-catalog-image-smoke-design.md);
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md).

Expected implementation size: 450–600 changed lines (M) — three helper modules (~280), one live
test (~120), unit tests (~150), mapping/test/runbook edits (~40).

## Global Constraints

- Python 3.14; ruff line length 100; `ty` strict over the whole tree; no new dependency.
- Add no new `KDIVE_*` variable; reuse `KDIVE_ARTIFACT_DIR` (`spine.report_artifact_dir`).
- Records validate through `scripts.coverage_campaign.evidence.Evidence` and `read_results`.
  Compute `input_sha256` as `digest(context)`.
- Published evidence and artifacts hold no host name, address, key or credential.
  `host_os` and `guest_os` use the public `distro:version` form.
- Guardrails: `just lint`, `just type`, `just test-changed`, `just coverage-check`,
  `git fetch origin main && just records`. Pre-push: `just ci > log 2>&1 < /dev/null`.
- Prose rule: no "critical", "robust", "comprehensive", "elegant".

## File map

| File | Change | Owns | Criterion |
|---|---|---|---|
| `tests/integration/live_stack/evidence.py` | new | identity, writer, assembly | 4, 6 |
| `tests/integration/live_stack/cleanup.py` | new | release plus domain, disk and capacity checks | 2, 6 |
| `tests/integration/live_stack/image_smoke.py` | new | cell selection, guest probes, toolchain, bindings | 1, 3, 8 |
| `tests/integration/test_image_smoke_live.py` | new | the live scenario | 1-5 |
| `tests/integration/live_stack/test_evidence.py`, `test_image_smoke.py`, `test_cleanup.py` | new | focused unit tests | all |
| `scripts/coverage_campaign/obligations.toml` | `[implementations]` row | node binding | 7 |
| `tests/scripts/test_coverage_contract.py`, `test_coverage_cli.py` | edit | no longer assert zero implementations | 7 |
| `docs/operating/runbooks/live-testing.md` | section | run procedure and inventory | 8 |

`tests/live_vm/installed_local_authority_support._active_worker_readyz_urls` is reused for
the slot port map; it is not moved.

## Task 1 — evidence seam

Verification:
- Mode: focused-test, `tests/integration/live_stack/test_evidence.py`.
  - `run_identity` resolves injected reports, omits unknown roles, omits `worker` when no slot
    runs (the helper's `AssertionError`), and reports a mismatched worker slot.
  - `identity_problems` names each missing or mismatched role and a dirty checkout.
  - `assemble` drops records for another candidate and counts them.
  - A record written by `EvidenceWriter.record` and passed through `assemble` round-trips via
    `read_results`.
  - `artifact` is content-addressed.
  - Red before the module exists (`ModuleNotFoundError`). Green:
    `uv run python -m pytest tests/integration/live_stack/test_evidence.py -q`.

Interfaces (later tasks rely on these exactly):
```python
REQUIRED_ROLES = ("server", "worker", "reconciler", "authority")
@dataclass(frozen=True)
class RunIdentity:
    candidate_sha: str; matrix_sha256: str; host_os: str; host_arch: str
    clean: bool; deployed_roles: dict[str, str]
def run_identity(base_url: str, *, git=_git, fetch=_fetch_version, resolve=_resolve,
                 read=_read_privileged, running_workers=_running_workers) -> RunIdentity
def identity_problems(identity: RunIdentity, roles: Iterable[str]) -> list[str]
def os_identity(os_release: str) -> str   # "ID:VERSION_ID"
class EvidenceWriter:
    def __init__(self, root: Path) -> None
    def artifact(self, payload: object) -> str        # canonical JSON -> sha256 hex
    def record(self, evidence: Evidence) -> Path       # records/<sha256(cell_id)>.json, atomic
def assemble(root: Path, candidate: str) -> tuple[list[Evidence], int]  # (kept, dropped)
def evidence_root() -> Path   # report_artifact_dir() / "coverage-evidence"
```
Steps:
1. Write the tests above with injected `fetch`, `resolve`, `read` and `running_workers`. Run
   them and expect a red import failure.
2. Implement. `run_identity` takes `head` from `git rev-parse HEAD` and `matrix_sha256` from
   `build_contract().matrix_sha256`. It maps the server and reconciler through
   `readyz_urls(base_url, {})` and the worker slots through `_active_worker_readyz_urls`. It
   reads `authority` through `read("/opt/kdive-provider-authority/revision")`, which runs
   `sudo -n cat`. It sets `clean` from an empty `git status --porcelain`. Resolve each revision with `resolve`, take `host_os` from `/etc/os-release`,
   and take `host_arch` from `platform.machine()`. `assemble` reads `records/*.json` and
   validates the list with `read_results` semantics, keeping only records for `candidate`. Its
   `__main__` takes `assemble DIR --candidate SHA --out FILE`.
3. Rerun the tests and expect green. Then commit with
   `test(live-stack): add coverage evidence seam (#2808)`.

## Task 2 — owned cleanup check

Verification:
- Mode: focused-test, `tests/integration/live_stack/test_cleanup.py`. A fake client returns
  `torn_down`, `released` and an availability total, and a fake libvirt connection raises
  not-found.
  - The verdict passes.
  - Each of these raises `AssertionError`: a surviving disk, a disk under an unsearchable parent
    (it must not count as absent), a still-defined domain, and an `in_use` total that did not
    return to its value before the allocation.
  - Green: `uv run python -m pytest tests/integration/live_stack/test_cleanup.py -q`.

Interface:
```python
def domain_disks(xml: str) -> list[str]           # <disk><source file=...> paths
async def capacity_in_use(client) -> int          # sum of resources.availability items' in_use
async def release_and_verify(client, *, allocation_id: str, system_id: str, domain: str,
                             disks: list[str], in_use_before: int,
                             connect=_worker_connect) -> dict[str, object]
```
Steps:
1. Write the failing tests.
2. Implement it.
   - Call `allocations.release`, then
     `await_system_state(client, "cleanup", system_id, "torn_down")`.
   - Poll `allocations.wait(allocation_id=…, timeout_s=0)` until the status is `released`,
     within the spine `DRAIN_DEADLINE_S`.
   - Assert that `lookupByName` raises `libvirt.libvirtError`.
   - Treat a disk as absent only when `os.stat` raises `FileNotFoundError`. On a
     `PermissionError`, run `sudo -n test -e`: exit 1 means absent, exit 0 means present, and
     anything else fails.
   - Assert that `capacity_in_use(client) == in_use_before`.
   - Return
     `{"system": "torn_down", "allocation": "released", "domain": "absent", "disks_absent": n,
     "in_use": [before, after]}`.
3. Expect green, then commit with `test(live-stack): verify owned cleanup and released capacity`.

## Task 3 — image-smoke helpers and bindings

Verification:
- Mode: focused-test, `tests/integration/live_stack/test_image_smoke.py`.
  - `native_cells` returns the x86_64 cells on an x86_64 host.
  - `os_matches` accepts `rocky` `9.8` for catalog `9` and rejects `10` for `1`.
  - It maps `centos` to `centos-stream`.
  - `parse_probe` extracts uid, boot_id, machine and the os-release fields.
  - `bindings` gives a staged image its file digest and gives an unstaged one null. It carries
    the full expected `Context`: host OS and architecture, `guest_os` as `distro:version`,
    `guest_arch`, and `accelerator=cell.accelerator`.
  - A success record built from that binding passes `results.qualify`. Its only reason is
    `deployed-role-missing` when `authority` is omitted.
  - `toolchain_command` for the `rhel` family lists every `packages("build")` entry.
  - Green: `uv run python -m pytest tests/integration/live_stack/test_image_smoke.py -q`.

Interface:
```python
PROBE = "id -u; cat /proc/sys/kernel/random/boot_id; uname -m; cat /etc/os-release"
def native_cells(arch: str | None = None) -> list[Cell]   # contract image-smoke cells
def parse_probe(stdout: str) -> dict[str, str]  # uid, boot_id, machine, ID, VERSION_ID
def os_matches(entry: RootfsCatalogEntry, probe: dict[str, str]) -> bool
def toolchain_command(entry: RootfsCatalogEntry) -> str
def ssh(port: int, key: Path, command: str, *, deadline_s: float = 300.0) -> CompletedProcess[str]
def bindings(candidate: str, identity: RunIdentity, *,
             staged: Callable[[str], Path | None]) -> InputBindings
```
`toolchain_command` checks packages with `rpm -q` for the `rhel` and `suse` families and with
`dpkg -s` for `debian`. It then runs
`printf 'int main(void){return 0;}\n' > /tmp/t.c && printf 't: t.c\n\tgcc -o t t.c\n' > /tmp/Makefile && make -C /tmp t && /tmp/t`.
`staged(name)` reads the `staged-path` source of the `local-libvirt` `[[image]]` declared in
`systems.toml` (`kdive.inventory.loader.load_inventory_optional(systems_toml_path())`).
Steps: write the failing tests, implement, expect green, and commit with
`test(live-stack): add image-smoke probes and bindings`.

## Task 4 — live scenario, mapping and runbook

Verification:
- `obligations.toml` maps the scenario to the node. Mode: focused-test.
  - `tests/scripts/test_coverage_contract.py::test_pending_cells_have_owned_assertions_but_no_invented_nodes`
    first goes red (the `node_id is None` assertion), then goes green once edited.
  - After the edit it requires that exactly the `image-smoke` cells carry
    `tests/integration/test_image_smoke_live.py::test_image_smoke`, and that the ppc64le ones
    keep owner 2818.
  - `test_coverage_cli.py::test_shipped_pending_set_reports_every_missing_result` counts
    `pending-implementation` over the cells with no node.
  - Green: `uv run python -m pytest tests/scripts/test_coverage_contract.py tests/scripts/test_coverage_cli.py -q`,
    and `just coverage-check`.
- `test_image_smoke` is live-only. Mode: task-test-not-applicable. It needs a running stack,
  KVM and staged images, so it is proved by the Task 5 run. Its pure parts are covered by
  Tasks 1-3.
- Runbook prose. Mode: task-test-not-applicable. It is guidance text with no executable
  consumer; `just docs-check` covers its links.

The test body:
1. Skip when `KDIVE_STACK_BASE_URL` is unset.
   - An unreachable issuer or a missing `KDIVE_DATABASE_URL` writes a `blocked` record with
     `missing-prerequisite`.
   - Read `run_identity` for this cell. A mismatch or a dirty checkout writes a `failure` record
     before any mutation.
2. Acquire through `images.list` and `images.describe`. An image that is not registered writes
   a `blocked` record with `missing-prerequisite`.
   - Read `capacity_in_use` before allocating.
3. Allocate, then provision with catalog rootfs. Read the console marker. Keep the domain XML
   to get the accelerator (`type=kvm` is `kvm`, `qemu` is `tcg`) and the disks.
4. Authorize a new key with `ssh-keygen -t ed25519 -N ''` into `tmp_path`, then run the probe.
5. Check the toolchain on build rows.
6. Run `control.power` `cycle`, then probe again.
7. Run `release_and_verify`.
8. In a `finally` block, if cleanup was not already proven, run `release_and_verify` as a best
   effort and record its result in a `cleanup-attempt` artifact.
   - Write the record with `cell.node_id`, `cell.scenario_id` and `cell.id`.
   - The outcome is `success` only when every cell assertion passed.
   - Then fail the test unless the outcome is `success` and `identity_problems` is empty.

Commit with `test(live-stack): smoke every native catalog image (#2808)`.

## Task 5 — live proof

On the lab lane, at a stack whose deployed revision is this branch's HEAD:
1. `build-image.sh` all 15 x86_64 rows.
2. Write the bindings.
3. Run `uv run python -m pytest tests/integration/test_image_smoke_live.py -m live_stack`.
4. Start from an empty `KDIVE_ARTIFACT_DIR`, then run `assemble --candidate`.
5. Run `python -m scripts.coverage_campaign qualify` and expect exit 1.
   - Every cell outside `image-smoke` is `not-run`.
   - On the demo-up lane every image cell is unqualified. Its reasons are
     `deployed-role-missing` (#3066) plus that image's own failures, if any.

Record the sanitized per-image inventory (outcome, qualifier reasons, duration, image digest) in
`live-testing.md` and the PR. File a needs-triage issue, deduplicated, for each product defect
the run finds.

## Deferrals

- Authority role on the proof lane: #3066.
- Unbuildable catalog rows: #3064 (stale pins) and #3065 (`fedora-kdive-ready-43`).
