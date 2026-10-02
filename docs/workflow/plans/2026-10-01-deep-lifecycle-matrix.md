# Deep lifecycle matrix — implementation plan

Goal: produce ADR-0715 evidence for the eight x86_64 local deep-lifecycle cells and hand the
lifecycle tool cells and ppc64le cells to their approved owners
([spec](../specs/2026-10-01-deep-lifecycle-matrix-design.md)).

Architecture: the image smoke's per-cell frame moves into a shared `scenario.py`; a
`deep_lifecycle.py` module holds representatives, fixture inputs, guest probes and bindings; one
parametrized `live_stack` test drives each cell through the shared frame. The offline contract
routes ownership by provider and architecture.

Tech stack: Python 3.14, pytest, pydantic, libvirt bindings, `uv`, `just`.

Expected implementation size: 650–750 changed lines (M) — file map below: ~180 shared runner
(about 150 of it moved from image smoke), ~280 deep module with its body, ~40 live test, ~150 unit
tests, ~30 contract/manifest, ~50 docs.

## Global Constraints

- Ruff line length 100, lint `E,F,I,UP,B,SIM`; `ty` strict over src and tests (`just type`).
- No new dependencies. Test-only env vars stay under `tests/` (config-guard); reuse
  `KDIVE_FIXTURE_ROOT` (already read by `tests/integration/test_kernel_fixtures_live.py`).
- Evidence and artifacts carry no host names, paths or keys (ADR-0715); exception messages are
  recorded by type only.
- Prose: no "critical", "robust", "comprehensive", "elegant".
- Gates: `just lint`, `just type`, `just test-changed`, `just coverage-check`,
  `git fetch origin main && just records`; full `just ci > log 2>&1 < /dev/null` before push.

## File map

| File | Change | Owns after |
|---|---|---|
| `scripts/coverage_campaign/obligations.toml` | modify | tool group owner 3062; two deep implementations |
| `scripts/coverage_campaign/contract.py` | modify | lane-based owners; per-provider deep scenario ID |
| `tests/scripts/test_coverage_contract.py` | modify | ownership and node assertions |
| `tests/integration/live_stack/scenario.py` | new | shared cell frame (moved from image smoke) |
| `tests/integration/test_image_smoke_live.py` | modify | smoke assertions only, on the shared frame |
| `tests/integration/live_stack/deep_lifecycle.py` | new | representatives, fixture inputs, probes, bindings, `deep_body` |
| `tests/integration/live_stack/test_deep_lifecycle.py` | new | unit tests for that module |
| `tests/integration/test_deep_lifecycle_live.py` | new | parametrization + local frame + hook |
| `docs/operating/runbooks/live-testing.md` | modify | deep-lifecycle procedure |

## Task 1 — contract ownership and scenario identity

Verification:
- Mode: focused-test. Contract: owners per lane and deep scenario IDs.
  `tests/scripts/test_coverage_contract.py::test_lifecycle_owners_follow_the_approved_split` —
  red: `assert {...} == {3062}` fails on owner 2809. Green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.
- Mode: focused-test. Contract: deep local cells map to the live node; remote stay unmapped.
  `test_pending_cells_have_owned_assertions_but_no_invented_nodes` (updated) — red until the
  implementations exist. Green: same command.

Steps:
1. Add to `tests/scripts/test_coverage_contract.py`:

```python
_DEEP_NODE = "tests/integration/test_deep_lifecycle_live.py::test_deep_lifecycle"
_LIFECYCLE_TOOLS = {
    "images.publish",
    "runs.boot",
    "runs.cancel",
    "runs.install",
    "runs.release_external_boot",
    "systems.authorize_ssh_key",
    "systems.check_ssh_reachable",
    "systems.provision",
    "systems.reprovision",
    "systems.ssh_info",
    "systems.teardown",
}


def test_lifecycle_owners_follow_the_approved_split(inventory: Inventory) -> None:
    cells = build_contract(inventory=inventory).cells

    def owners(operations: set[str], provider: str, arch: str) -> set[int]:
        return {
            c.owner
            for c in cells
            if c.operation in operations and c.provider == provider and c.guest_arch == arch
        }

    deep = {"deep-lifecycle"}
    assert owners(_LIFECYCLE_TOOLS, "local-libvirt", "x86_64") == {3062}
    assert owners(_LIFECYCLE_TOOLS, "local-libvirt", "ppc64le") == {2818}
    assert owners(deep, "local-libvirt", "x86_64") == {2809}
    assert owners(deep, "local-libvirt", "ppc64le") == {2818}
    for arch in SUPPORTED_ARCHES:
        assert owners(_LIFECYCLE_TOOLS | deep, "remote-libvirt", arch) == {2810}
    assert len([c for c in cells if c.owner == 2809]) == 8
```

   and in `test_pending_cells_have_owned_assertions_but_no_invented_nodes` replace the
   all-`None` line with:

```python
    deep = [c for c in contract.cells if c.operation == "deep-lifecycle"]
    assert {c.node_id for c in deep if c.provider == "local-libvirt"} == {_DEEP_NODE}
    assert all(c.node_id is None for c in deep if c.provider == "remote-libvirt")
    implemented = {"image-smoke", "deep-lifecycle"}
    assert all(c.node_id is None for c in contract.cells if c.operation not in implemented)
```

2. Run the green command; expect the two tests to fail (owner 2809; no deep node).
3. `obligations.toml`: the group at `owner = 2809` becomes `owner = 3062`; append to
   `[implementations]`:
   `"deep-lifecycle/local-libvirt/longterm"` and `"deep-lifecycle/local-libvirt/stable"` =
   `"tests/integration/test_deep_lifecycle_live.py::test_deep_lifecycle"`. `build_contract`
   rejects a node whose function does not exist, so create that file now with a `live_stack`
   marked `def test_deep_lifecycle() -> None: pytest.skip("implemented in Task 4")`; Task 4
   replaces it.
4. `contract.py` `_tool_cells`: replace the owner expression with

```python
                    owner = group.owner
                    if group.owner == 3062 and provider == "remote-libvirt":
                        owner = 2810
                    elif group.owner == 3062 and arch == "ppc64le":
                        owner = 2818
```

   `_matrix_cells` deep block: owner
   `2810 if provider == "remote-libvirt" else (2809 if arch == "x86_64" else 2818)` and
   `scenario_id=f"deep-lifecycle/{provider}/{baseline}"`.
5. Green: the contract tests pass; `just coverage-check` exits 0.
6. Commit `feat(coverage): route lifecycle cells to their approved owners (#2809)`.

## Task 2 — shared cell frame

Verification:
- Mode: focused-test. Contract: image smoke unchanged on the shared frame — its existing unit
  tests (`tests/integration/live_stack/test_image_smoke.py`) and collection of
  `test_image_smoke_live.py` (`uv run python -m pytest tests/integration/test_image_smoke_live.py
  --collect-only -q` lists one id per native image). Red observation: none expected (move);
  the bite is collection failing on a broken import.
- Mode: focused-test. Contract: `CellRun.context` merges host identity with observed fields.
  `tests/integration/live_stack/test_deep_lifecycle.py::test_cell_run_context_merges_observed`.

Create `tests/integration/live_stack/scenario.py` by moving, unchanged in behaviour, from
`test_image_smoke_live.py`: `_Stop` → `ScenarioStop`; `_Run` → `CellRun` whose image/guest/
accelerator fields become `observed: dict[str, object]` (default `{"accelerator": "none"}`) and
which gains `note(payload)` (append an artifact backing no assertion); `_prerequisites` →
`prerequisites`; `_acquire` → `acquire_image(op, name, arch) -> dict[str, object]` (returns the
`acquire` observation, raises `ScenarioStop(BLOCKED)` when unregistered, no `prove`); `_profile`
→ `catalog_profile(entry, name, unread_ref)`; `_domain_xml` → `domain_xml`; `_probe` →
`ssh_probe(port, key, command=PROBE)`; `_rebooted_probe` → `probe_new_boot(port, key, boot_id,
command=PROBE)`; `_authorize` → `authorize_ssh(op, system_id, directory, comment)`. The
accelerator observation (domain XML `type`) moves from the smoke body into `on_catalog_system`,
after provisioning, so every local cell records it.

New in the frame (the body of `_smoke`/`_cleanup_attempt` and of `test_image_smoke`):

```python
CatalogBody = Callable[[LiveStackClient, str, list[str]], Awaitable[None]]
Scenario = Callable[[CellRun, str, OidcIssuer, str], Awaitable[None]]


async def on_catalog_system(
    run: CellRun,
    base_url: str,
    issuer: OidcIssuer,
    db_url: str,
    *,
    project: str,
    image: str,
    body: CatalogBody,
) -> None:
    """Acquire ``image``, provision it to ready, run ``body``, then prove ``cleanup``.

    ``body(op, system_id, owned)`` may append host paths it made the System own; cleanup then
    proves them absent with the domain's disks. On failure a cleanup attempt is recorded.
    """
    entry = load_rootfs_catalog()[image]
    token = mint_role_token(
        issuer, project=project, agent_session=f"{project}-sess", role="operator"
    )
    async with LiveStackClient.over_http(base_url, token) as op:
        await seed_metering(db_url, project)
        run.acquired = await acquire_image(op, image, entry.arch)
        run.observed["image_sha256"] = str(run.acquired["digest"]).removeprefix("sha256:")
        ...  # allocation, provision_to_ready, owned = domain_disks(...), body, cleanup:
        # identical to today's _smoke/_cleanup/_cleanup_attempt


def run_cell(cell: Cell, scenario: Scenario) -> None:
    """Identity → prerequisites → scenario → one record; fail pytest unless success."""
    # body of today's test_image_smoke after `base_url = require_stack()`
```

`CellRun` also carries `acquired: dict[str, object]` (empty until acquisition). Image smoke
keeps `acquire` as an assertion: `_smoke` becomes
`on_catalog_system(..., project="image-smoke", image=cell.image, body=…)` whose body first calls
`run.prove("acquire", run.acquired)`. `test_image_smoke` becomes `run_cell(cell, partial(_smoke, tmp=tmp_path))`.

Steps: write `test_cell_run_context_merges_observed` (red: import error), move the code, run the
unit tests and the collection command, `just lint && just type`, commit
`refactor(live-stack): share the catalog cell frame (#2809)`.

## Task 3 — deep inputs, probes and bindings

Verification (all `focused-test`, file `tests/integration/live_stack/test_deep_lifecycle.py`,
green `uv run python -m pytest tests/integration/live_stack/test_deep_lifecycle.py -q`):
- `test_every_deep_cell_has_a_family_representative` — for every `deep-lifecycle` local cell of
  both arches, the representative exists, its `image_family` is the cell family and its arch the
  cell arch. Red: missing name.
- `test_gnu_build_id_reads_the_gnu_note` — a synthetic notes blob with a non-GNU note before the
  GNU type-3 note returns the GNU desc hex; a blob without one returns `None`.
- `test_staged_module_stays_under_modstage` — `/usr/lib/modules/R/kernel/x.ko` and
  `/lib/modules/R/kernel/x.ko` map to `modstage/lib/modules/R/kernel/x.ko`; a path with `..` or no
  `/lib/modules/` raises `AssertionError`.
- `test_kernel_inputs_come_from_the_manifest` — synthetic tree with `arch/x86/boot/bzImage` and
  a manifest dict; asserts the five fields.
- `test_bindings_bind_every_native_cell` — fake `staged` and fake fixture loader; eight x86_64
  cells, each `Context` has `guest_os` of its representative and the kernel fields; an unbuilt
  baseline yields null kernel fields.

Module `tests/integration/live_stack/deep_lifecycle.py` (public names; Task 4 adds `deep_body`):

```python
FIXTURE_ROOT_ENV = "KDIVE_FIXTURE_ROOT"
MODULE = "loop"  # CONFIG_BLK_DEV_LOOP=m in fixtures/kernel/debug.config
REPRESENTATIVES: dict[tuple[str, str], str]  # (family, arch) -> catalog name, per the spec
KERNEL_PROBE = PROBE + '; printf "release=%s\\nnotes=%s\\n" "$(uname -r)" ' \
    '"$(base64 -w0 /sys/kernel/notes)"'
MODULE_PROBE = (f"modprobe {MODULE} && p=$(modinfo -n {MODULE}) && printf "
    '"initstate=%s\\npath=%s\\nvermagic=%s\\nsha256=%s\\n" '
    f'"$(cat /sys/module/{MODULE}/initstate)" "$p" "$(modinfo -F vermagic {MODULE})" '
    '"$(sha256sum "$p" | cut -d" " -f1)"')

def native_cells(arch: str | None = None) -> list[Cell]  # local-libvirt deep cells for arch
def baseline(cell: Cell) -> str                          # last scenario_id segment
def representative(cell: Cell) -> str
def load_fixture(root: Path, name: str, arch: str) -> tuple[Path, dict[str, Any]]  # verify()
def kernel_inputs(tree: Path, manifest: dict[str, Any]) -> dict[str, str]
def gnu_build_id(notes: bytes) -> str | None   # little-endian ELF notes (x86_64, ppc64le)
def staged_module(modstage: Path, guest_path: str) -> Path
def file_sha256(path: str | Path) -> str       # sudo -n sha256sum on PermissionError
def bindings(candidate: str, *, root: Path | None, host_os: str, host_arch: str, matrix: str,
             staged: Callable[[str], Path | None] = staged_image,
             fixture: Callable[[Path, str, str], tuple[Path, dict[str, Any]]] = load_fixture,
             ) -> InputBindings
def main(argv: list[str] | None = None) -> int  # `bindings --candidate SHA --out FILE`
```

Key logic:

```python
def kernel_inputs(tree: Path, manifest: dict[str, Any]) -> dict[str, str]:
    return {
        "kernel_sha256": boot_kernel_sha256(tree, manifest["arch"]),
        "kernel_source_sha": manifest["source"]["commit"],
        "kernel_config_sha256": manifest["artifacts"][".config"],
        "compiler_id": identity(manifest["toolchain"]),  # scripts.kernel_fixtures.identity
        "kernel_build_id": manifest["build_id"],
    }


def boot_kernel_sha256(tree: Path, arch: str) -> str:
    """The digest of the boot member ``combined_kernel_tar`` uploads for ``arch``."""
    if arch == "x86_64":
        return file_sha256(tree / "arch/x86/boot/bzImage")
    with tempfile.TemporaryDirectory() as scratch:  # ppc64le: the stripped vmlinux copy
        out = Path(scratch) / "vmlinuz"
        subprocess.run(["strip", "-s", str(tree / "vmlinux"), "-o", str(out)], check=True)
        return file_sha256(out)


def gnu_build_id(notes: bytes) -> str | None:
    offset = 0
    while offset + 12 <= len(notes):
        namesz, descsz, kind = struct.unpack_from("<III", notes, offset)
        name_at = offset + 12
        desc_at = name_at + (namesz + 3) // 4 * 4
        offset = desc_at + (descsz + 3) // 4 * 4
        if kind == 3 and notes[name_at : name_at + namesz] == b"GNU\0":
            return notes[desc_at : desc_at + descsz].hex()
    return None


def staged_module(modstage: Path, guest_path: str) -> Path:
    _, sep, rest = guest_path.partition("/lib/modules/")
    path = (modstage / "lib/modules" / rest).resolve()
    assert sep and path.is_relative_to(modstage.resolve()), f"module path {guest_path!r}"
    return path
```

`bindings` mirrors `image_smoke.bindings`: per native cell, `guest_os` and arch from the
representative's catalog row, `accelerator` from the cell, `image_sha256` from the staged image,
and `kernel_inputs` of `fixture(root, baseline(cell), arch)` cached per baseline; a `ValueError`
from the fixture (or `root is None`) leaves the kernel fields null. `main` reads
`FIXTURE_ROOT_ENV` for `root`.

Steps: write the five tests (red: import error), implement, green, `just lint && just type`,
commit `test(live-stack): add deep-lifecycle inputs, probes and bindings (#2809)`.

## Task 4 — the deep body and the live scenario

Verification:
- Mode: task-test-not-applicable for `deep_body` and the live test: they need a KVM host, a
  deployed stack and built fixtures; their proof is the live run recorded in the PR. Collection
  (`uv run python -m pytest tests/integration/test_deep_lifecycle_live.py --collect-only -q`
  lists eight ids on x86_64) and `just coverage-check` (node exists) guard the shape.

Add to `tests/integration/live_stack/deep_lifecycle.py` the provider-neutral body (#2810 calls
it from its remote frame):

```python
async def deep_body(
    run: CellRun,
    op: LiveStackClient,
    system_id: str,
    owned: list[str],
    *,
    project: str,
    entry: RootfsCatalogEntry,
    tree: Path,
    manifest: dict[str, Any],
    tmp: Path,
    staged_kernel: Callable[[str], str],
) -> None:
    """Upload → install → boot → reconnect → build identity → module load (spec item 4)."""
```

In order:
- `port, key = authorize_ssh(...)`; `before = ssh_probe(port, key)`; assert uid `"0"` and
  `os_matches(entry, before)`; set `guest_os`, `guest_arch`.
- `investigations.open` (`project`, title `deep lifecycle`); in
  `try`/`finally` close it with `investigations.close` (summary `deep lifecycle finished`).
- `runs.create(investigation_id, system_id, build_profile(arch))`;
  `build_and_upload_kernel(op, run_id=…, arch=arch, kernel_tree=tree,
  evidence_dir=tmp / "upload", with_vmlinux=True, require_network=True, root_fs="ext4")`
  (`with_vmlinux` sets the debuginfo reference that makes install inject `lib/modules`); read
  `upload.json`; assert `build_id == manifest["build_id"]` and the result status `succeeded`;
  prove `upload` with the declared digests and build ID.
- `runs.install` → `drain_job`; `runs.boot` → `drain_job`; `runs.get` steps `install` and
  `boot` are `succeeded`.
- `kernel = staged_kernel(system_id)`; `owned.append(kernel)`; `digest = file_sha256(kernel)`;
  assert equal to `kernel_inputs(tree, manifest)["kernel_sha256"]`; prove `install`.
- `ssh_info` again; `after = probe_new_boot(port, key, before["boot_id"], KERNEL_PROBE)`; uid
  `"0"`; prove `reconnect`.
- `build_id = gnu_build_id(base64.b64decode(after["notes"]))`; assert
  `after["release"] == manifest["release"]` and `build_id == manifest["build_id"]`; set the five
  kernel context fields (`kernel_sha256` the observed digest, `kernel_build_id` the observed ID,
  `kernel_config_sha256` = `file_sha256(tmp / "upload" / "effective_config")`, the other two from
  the manifest); prove `boot-identity`.
- `module = parse_probe(ssh(port, key, MODULE_PROBE).stdout)` (exit 0 asserted); assert
  `initstate == "live"`, `vermagic.split()[0] == release`, and
  `sha256 == file_sha256(staged_module(tmp / "upload" / "modstage", path))`; prove `modules`.

`tests/integration/test_deep_lifecycle_live.py` (replaces the Task 1 stub):

```python
pytestmark = pytest.mark.live_stack


@pytest.mark.parametrize("cell", native_cells(), ids=lambda c: f"{c.family}-{baseline(c)}")
def test_deep_lifecycle(cell: Cell, tmp_path: Path) -> None:
    """Upload → install → boot → reconnect → build identity → module load → cleanup."""
    run_cell(cell, partial(_deep, tmp=tmp_path))
```

`_deep(run, base_url, issuer, db_url, tmp)`: `KDIVE_FIXTURE_ROOT` unset, or `load_fixture`
raising `ValueError`, → `ScenarioStop(BLOCKED, …)`; then `on_catalog_system(run, …,
project="deep-lifecycle", image=representative(cell), body=…)` whose body calls `deep_body` with
`staged_kernel=_domain_kernel` (`ET.fromstring(domain_xml(id)).findtext("./os/kernel")`, asserted
non-empty); afterwards re-`verify` the fixture, a change being a failure.

Steps: implement, collect, `just lint && just type && just coverage-check`, commit
`test(live-stack): drive the deep lifecycle over representative guests (#2809)`.

## Task 5 — runbook and live proof

Verification: Mode: task-test-not-applicable — runbook prose; its proof is following it on the lab
host for the PR evidence.

Add `#### Deep lifecycle across representative guests (#2809)` after the image-smoke subsection:
- build both fixtures with `scripts/kernel_fixtures.py build` (native host; Debian-family builder
  until #3063); stage the four representatives with `examples/local-libvirt/build-image.sh`;
  `export KDIVE_FIXTURE_ROOT`; write bindings with
  `python -m tests.integration.live_stack.deep_lifecycle bindings`; run; `assemble`; `qualify`.
- blocked/failure meanings, as image smoke lists them.
- after an interrupted run: `allocations.release` the leftover allocation (or wait out the lease)
  and check `KDIVE_INSTALL_STAGING` for the run's staged kernel.
- the four ppc64le local cells report `missing-result` for #2818 until a native POWER host runs
  the same node.
- the sanitized per-cell outcome table from the live proof below.

Live proof: redeploy the lab host at the branch head (demo-down, prepare, demo-up; `/readyz`
commits equal `HEAD`). Run one cell first (`-k fedora-longterm`). If it shows a staged kernel
surviving teardown or a fixture kernel that cannot boot, file that issue (`status:needs-triage`,
linked) before running the other seven. Then run all eight, `assemble`, `qualify`, and record the
eight rows (sanitized) in the runbook table and the PR body, each failing row with its linked
issue. Commit `docs(live-testing): run the deep lifecycle cells (#2809)`.
