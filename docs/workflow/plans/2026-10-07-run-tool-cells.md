# Plan: the x86_64 local run and image tool cells (#3119)

**Goal.** Prove the 96 x86_64 local-libvirt cells of `runs.install`, `runs.boot`, `runs.cancel`,
`runs.release_external_boot` and `images.publish` with a new live carrier. The contract flags
change per tool (operator option B).
[Spec](../specs/2026-10-07-run-tool-cells-design.md).

**Architecture.** One `obligations.toml` edit splits group 3119 into three owner-3119 groups.
Two additive seams support the carrier:
- `tool_cells.bindings()` binds the functional `images.publish` cells to the image they publish,
  and `_settled` becomes public `settled`;
- `deep_lifecycle.deep_body` takes an optional `step` call;
- `spine.build_and_upload_kernel` takes `complete: bool = True`.

A new carrier `tests/integration/test_run_tool_cells_live.py` reuses `run_tool_cell`,
`on_lane_system`, `lane_target`, `prove_rejection` and `deep_body`.

**Tech stack.** Python 3.14, pytest, the repository's `live_stack` harness, `uv`.

Expected implementation size: 520–640 changed lines (L) — the file map below: carrier ~380,
frame seams ~50, contract and unit tests ~110, `obligations.toml` ~40, runbook ~60.

## Global Constraints

- No product source, ADR or migration change; no new dependency. A `KDIVE_`-prefixed variable
  is introduced nowhere.
- Ruff line length 100, lint `E,F,I,UP,B,SIM`; `ty` strict over the whole tree (`just type`).
- Prose rule: no "critical", "robust", "comprehensive", "elegant"; "Milestone", never "Sprint".
- Keep owner 3119, each tool's observation string, and `contract.py` unchanged. Edits to
  `tool_cells.py` and `obligations.toml` stay additive: another campaign edits them.
- Guardrails: `just lint`, `just type`, `just test-changed`; `just docs-check` for the runbook;
  `just records` needs `git fetch origin main` first.
- Commit format: Conventional Commits, imperative subject of 72 characters or fewer.
- Host x86_64; targets x86_64 and ppc64le. The carrier parametrizes only native local cells.

## File map

| File | Today | After |
|---|---|---|
| `scripts/coverage_campaign/obligations.toml` | one group 3119 (`kernel`, `authority`); no run/image rows | three owner-3119 groups with per-tool flags; 24 `[implementations]` rows |
| `tests/scripts/test_coverage_contract.py` | pins one role/input set for the five tools | pins the per-tool set; pins the run carrier's bound scenarios |
| `tests/integration/live_stack/tool_cells.py` | lane bindings for every native cell; private `_settled` | `PUBLISHED_IMAGES`, `PUBLISH_TOOL`; published-image bindings; public `settled` |
| `tests/integration/live_stack/test_tool_cells.py` | authority test uses `runs.install` | uses `runs.release_external_boot`; one published-image binding test |
| `tests/integration/live_stack/deep_lifecycle.py` | install/boot through the operator client | optional `step` |
| `tests/integration/live_stack/spine.py` | `build_and_upload_kernel` always completes the build | `complete: bool = True`; `False` skips `runs.complete_build` |
| `tests/integration/live_stack/test_spine.py` | upload tests | one test of `complete=False` |
| `tests/integration/live_stack/test_deep_lifecycle.py` | no step test | one test of the `step` seam |
| `tests/integration/test_run_tool_cells_live.py` | absent | the carrier |
| `docs/operating/runbooks/live-testing.md` | System cells section | adds a run/image cells section with the lab result |

## Task 1 — Per-tool contract flags

**Verification.**
- Contract: functional roles and input counts per tool. Mode: focused-test.
  `tests/scripts/test_coverage_contract.py::test_lifecycle_owners_follow_the_approved_split`.
  Red: the new set assertion fails against the single group (`images.publish` has 6 inputs and
  `authority`). Green: `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.
- Contract: a provider cell declaring `authority` fails without it. Mode: focused-test.
  `tests/integration/live_stack/test_tool_cells.py::test_declared_authority_fails_without_it`.
  Red after step 3 (its `assert "authority" in cell.roles` fails for `runs.install`). Green after
  step 4: `uv run python -m pytest tests/integration/live_stack/test_tool_cells.py -q`.

Steps:

1. In `tests/scripts/test_coverage_contract.py`, replace the two lines
   ```python
       runs = {(c.roles, len(c.inputs)) for c in functional if c.operation in _RUN_TOOLS}
       assert runs == {(("server", "worker", "reconciler", "authority"), 6)}
   ```
   with
   ```python
   base = ("server", "worker", "reconciler")
   runs = {(c.operation, c.roles, len(c.inputs)) for c in functional if c.operation in _RUN_TOOLS}
   # Operator option B (#3119, 2026-10-07): only the release needs the provider authority,
   # and images.publish uploads no kernel.
   assert runs == {
       ("images.publish", base, 0),
       ("runs.boot", base, 6),
       ("runs.cancel", base, 6),
       ("runs.install", base, 6),
       ("runs.release_external_boot", (*base, "authority"), 6),
   }
   ```
   Run the focused command: expect `AssertionError` on `runs ==`.
2. In `scripts/coverage_campaign/obligations.toml`, replace the group block that starts
   `owner = 3119` (lines 159-169 at `12b76d2e5`) with:
   ```toml
   [[groups]]
   owner = 3119
   execution = "provider"
   kernel = true
   [groups.tools]
   "runs.boot" = "Wait for boot and compare the guest's running kernel/build identity with uploaded artifacts."
   "runs.cancel" = "Verify operation-specific cancellation leaves the documented system and protected build state."
   "runs.install" = "Wait for installation and compare staged kernel, config and module contents with upload digests."

   [[groups]]
   owner = 3119
   execution = "provider"
   [groups.tools]
   "images.publish" = "Wait for publication and verify the built image's content, catalog provenance and cleanup."

   [[groups]]
   owner = 3119
   execution = "provider"
   kernel = true
   authority = true
   [groups.tools]
   "runs.release_external_boot" = "Wait for release and verify external-boot state and owned artifacts are reclaimed."
   ```
   Rerun: the test passes, and so do the 96/96 owner counts in the same test.
3. Run `uv run python -m pytest tests/integration/live_stack/test_tool_cells.py -q`: expect
   `test_declared_authority_fails_without_it` to fail on `assert "authority" in cell.roles`.
4. In that test, change `_cell("runs.install", "local-libvirt", "x86_64")` to
   `_cell("runs.release_external_boot", "local-libvirt", "x86_64")`. Rerun: green. Also run
   `uv run python -m pytest tests/integration/live_stack/test_tool_cells.py::test_kernel_inputs_bind_only_declaring_cells -q`
   (it uses `runs.install`, which still declares kernel inputs): expect pass.
5. `just lint` and `just type`, then commit `test(coverage): split run/image contract flags per tool`.

Acceptance: owner 3119 keeps 96 cells and 3120 keeps 96; ppc64le still goes to 2818; the five
tools' functional cells carry the role and input sets of step 1.

## Task 2 — Frame seams

**Interfaces.** Later tasks consume:
- `tool_cells.PUBLISH_TOOL: str = "images.publish"`;
- `tool_cells.PUBLISHED_IMAGES: dict[tuple[str, str], str]`, keyed by (architecture, exposure):
  `("x86_64", "direct")` → `"fedora-kdive-ready-43-cloud"`, `("x86_64", "gateway")` →
  `"rocky-kdive-ready-9"`;
- `async def tool_cells.settled(db_url: str, project: str) -> None`;
- `deep_lifecycle.StepCall = Callable[[str, str], Awaitable[ToolResponse]]`;
- `deep_body(..., installed_kernel: InstalledKernel, step: StepCall | None = None) -> None`;
- `spine.build_and_upload_kernel(client, *, run_id, ..., evidence_dir=None, complete=True)`;
  with `complete=False` it uploads, writes `upload.json` with `"result": null`, and leaves the
  Run `created`.

**Verification.**
- Contract: a functional `images.publish` cell binds the published entry with a null digest; its
  rejection cell and other tools bind the lane. Mode: focused-test.
  `tests/integration/live_stack/test_tool_cells.py::test_published_image_binds_the_product`.
  Red: `AttributeError` or a context equal to the lane's. Green:
  `uv run python -m pytest tests/integration/live_stack/test_tool_cells.py -q`.
- Contract: `deep_body`'s install and boot go through an injected `step`. Mode: focused-test.
  `tests/integration/live_stack/test_deep_lifecycle.py::test_install_and_boot_use_the_step`.
  Red: `TypeError` (unexpected argument). Green:
  `uv run python -m pytest tests/integration/live_stack/test_deep_lifecycle.py -q`.
- Contract: `build_and_upload_kernel(complete=False)` uploads without completing. Mode:
  focused-test. `tests/integration/live_stack/test_spine.py::test_spine_upload_can_leave_the_build_open`.
  Red: `TypeError` (unexpected argument). Green:
  `uv run python -m pytest tests/integration/live_stack/test_spine.py -q`.
- Contract: the rename `_settled` → `settled`. Mode: task-test-not-applicable. It is a name
  only; `just type` resolves every caller.

Steps:

1. Append to `tests/integration/live_stack/test_tool_cells.py`:
   ```python
   def test_published_image_binds_the_product(tmp_path: Path) -> None:
       image = tmp_path / "image.qcow2"
       image.write_bytes(b"lane")
       cells = [c for c in build_contract().cells if c.operation == "images.publish"]
       publish = {
           c.exposure: _bound(c)
           for c in cells
           if c.kind == "functional" and c.provider == "local-libvirt" and c.guest_arch == "x86_64"
       }
       reject = _bound(_cell("images.publish", "local-libvirt", "x86_64", "rejection"))
       install = _bound(_cell("runs.install", "local-libvirt", "x86_64"))
       inputs = bindings(
           "a" * 40,
           host_os="fedora:44",
           host_arch="x86_64",
           matrix="b" * 64,
           cells=[*publish.values(), reject, install],
           staged=lambda _name: image,
       )
       guests = {e: inputs.cells[c.id].guest_os for e, c in publish.items()}
       assert guests == {"direct": "fedora:43", "gateway": "rocky:9"}
       for cell in publish.values():
           product = inputs.cells[cell.id]
           assert (product.guest_arch, product.accelerator, product.image_sha256) == (
               "x86_64",
               "kvm",
               None,
           )
       lane = hashlib.sha256(b"lane").hexdigest()
       assert inputs.cells[reject.id].guest_os == "fedora:44"
       assert inputs.cells[reject.id].image_sha256 == lane
       assert inputs.cells[install.id].image_sha256 == lane
   ```
   Run it: red (the published cell binds `fedora:44`).
2. In `tests/integration/live_stack/tool_cells.py`, below `REMOTE_LANE_FAMILIES`, add:
   ```python
   PUBLISH_TOOL = "images.publish"
   # The single-kernel catalog image a functional images.publish cell publishes and boots, per
   # (architecture, exposure). One image per exposure: images.publish never recycles a finished
   # job of the same name, so two cells of one stack cannot both publish one image.
   PUBLISHED_IMAGES = {
       ("x86_64", "direct"): "fedora-kdive-ready-43-cloud",
       ("x86_64", "gateway"): "rocky-kdive-ready-9",
   }
   ```
   In `bindings()`, replace
   ```python
       native = [c for c in bound if c.provider == "local-libvirt" and c.guest_arch == host_arch]
   ```
   with
   ```python
       native = [c for c in bound if c.provider == "local-libvirt" and c.guest_arch == host_arch]
       published = [c for c in native if c.operation == PUBLISH_TOOL and c.kind == "functional"]
       native = [c for c in native if c not in published]
       for cell in published:
           product = PUBLISHED_IMAGES.get((host_arch, cell.exposure))
           if product is not None:
               # The published image is the cell's output, so no input digest binds it.
               entry = load_rootfs_catalog()[product]
               contexts |= _lane_contexts([cell], host_os, host_arch, entry, None, kernel=kernel)
   ```
   Add one sentence to the `bindings` docstring: "A functional ``images.publish`` cell boots
   ``PUBLISHED_IMAGES[(host_arch, exposure)]``, the image it publishes, with a null
   ``image_sha256``."
   In `main()`, the unstaged count must not count a functional `images.publish` binding, whose
   null digest is deliberate. Replace its `unstaged = sum(...)` with
   ```python
   published = {c.id for c in contract.cells if c.operation == PUBLISH_TOOL and c.kind == "functional"}
   unstaged = sum(
       c.guest_arch is not None and c.image_sha256 is None
       for cell_id, c in inputs.cells.items()
       if cell_id not in published
   )
   ```
   Rename `async def _settled` to `async def settled` and its one call in `_provision_target`.
   Rerun step 1's test: green.
3. Append to `tests/integration/live_stack/test_deep_lifecycle.py`:
   ```python
   def test_install_and_boot_use_the_step() -> None:
       calls: list[tuple[str, str]] = []

       class _Op:
           async def call_tool(self, name: str, **args: object) -> ToolResponse:
               if name == "jobs.wait":
                   return ToolResponse.success(str(args["job_id"]), "succeeded")
               assert name == "runs.get", f"unexpected operator call {name}"
               steps = {"install": "succeeded", "boot": "succeeded"}
               return ToolResponse.success("r", "succeeded", data={"steps": steps})

       async def step(name: str, run_id: str) -> ToolResponse:
           calls.append((name, run_id))
           return ToolResponse.success(f"job-{name}", "queued")

       op = cast(LiveStackClient, _Op())
       steps = asyncio.run(deep_lifecycle._install_and_boot(op, "r", step))
       assert calls == [("install", "r"), ("boot", "r")]
       assert steps["boot"] == "succeeded"
   ```
   Add the imports it needs, if absent: `import asyncio`, `from typing import cast`,
   `from kdive.mcp.dev_harness import LiveStackClient`,
   `from kdive.mcp.responses import ToolResponse`,
   `from tests.integration.live_stack import deep_lifecycle`. Run it: red (`TypeError`).
4. In `tests/integration/live_stack/deep_lifecycle.py`:
   - add `from collections.abc import Awaitable, Callable, Mapping` (extend the existing import)
     and `from kdive.mcp.responses import ToolResponse`;
   - below `InstalledKernel`, add
     ```python
     # (step, run_id) -> the step tool's envelope; lets a caller route install or boot elsewhere.
     StepCall = Callable[[str, str], Awaitable[ToolResponse]]
     ```
   - give `deep_body` a last keyword parameter `step: StepCall | None = None`, documented in its
     docstring as "``step`` issues ``runs.install`` and ``runs.boot``; the default is ``op``.";
     change its call to `steps = await _install_and_boot(op, run_id, step)`;
   - replace `_install_and_boot` with
     ```python
     async def _install_and_boot(
         op: LiveStackClient, run_id: str, step: StepCall | None = None
     ) -> Mapping[str, object]:
         """Drain ``runs.install`` and ``runs.boot``; return the Run's read-back ``steps``."""
         for name in ("install", "boot"):
             call = step(name, run_id) if step else scalar(op, f"runs.{name}", run_id=run_id)
             env = ok(await call, name)
             await drain_job(op, name, env.object_id)
         steps = data_mapping(ok(await scalar(op, "runs.get", run_id=run_id), "read-back"), "steps")
         assert (steps.get("install"), steps.get("boot")) == ("succeeded", "succeeded"), steps
         return steps
     ```

   Rerun: green.
5. Append to `tests/integration/live_stack/test_spine.py`:
   ```python
   def test_spine_upload_can_leave_the_build_open(
       monkeypatch: pytest.MonkeyPatch, tmp_path: Path
   ) -> None:
       (tmp_path / ".config").write_bytes(_BOOT_CONFIG + b"CONFIG_VIRTIO_NET=y\n")
       kernel_tar = tmp_path / "kernel.tar"
       kernel_tar.write_bytes(b"tar")
       monkeypatch.setattr(
           spine, "accepted_run_upload_names", lambda _c: ["kernel", "effective_config"]
       )
       monkeypatch.setattr(spine, "combined_kernel_tar", lambda *_a, **_k: kernel_tar)
       calls: list[str] = []

       async def _scalar(client: object, name: str, **args: object) -> ToolResponse:
           calls.append(name)
           items = [_upload_item(n) for n in ("kernel", "effective_config")]
           return ToolResponse.collection("run-1", "pending", items)

       async def _put(item: ToolResponse, path: Path) -> None:
           return None

       monkeypatch.setattr(spine, "scalar", _scalar)
       monkeypatch.setattr(spine, "put_presigned", _put)
       client = SimpleNamespace(read_text_resource=AsyncMock(return_value="{}"))
       asyncio.run(
           spine.build_and_upload_kernel(
               cast(Any, client),
               run_id="run-1",
               kernel_tree=tmp_path,
               evidence_dir=tmp_path / "evidence",
               complete=False,
           )
       )
       assert calls == ["artifacts.create_run_upload"]
       assert json.loads((tmp_path / "evidence/upload.json").read_text())["result"] is None
   ```
   Run it: red (`TypeError`).
6. In `tests/integration/live_stack/spine.py` `build_and_upload_kernel`, add the keyword
   parameter `complete: bool = True` after `evidence_dir`. Document it in the docstring:
   "``complete=False`` uploads the artifacts and skips ``runs.complete_build``, so the Run
   stays ``created``." Replace
   ```python
       result = ok(await scalar(client, "runs.complete_build", run_id=run_id, **extra), phase_name)
   ```
   with
   ```python
       result = (
           ok(await scalar(client, "runs.complete_build", run_id=run_id, **extra), phase_name)
           if complete
           else None
       )
   ```
   and the record's `"result": result.model_dump(mode="json"),` with
   `"result": result.model_dump(mode="json") if result is not None else None,`. Rerun: green.
   `just lint`, `just type`. Commit `test(live): add run-cell seams to the frame`.

Acceptance: the three focused tests green. `test_bindings_cover_bound_tool_cells` and the deep
lifecycle tests are unchanged and green.

## Task 3 — The run/image carrier

**Interfaces.** Consumes Task 2's names and these existing ones, with the signatures they have
at `12b76d2e5`:
- `tool_cells`: `run_tool_cell(cell, scenario)`, `on_lane_system(run, base_url, issuer, db_url,
  *, project, body)`, `lane_target(run, base_url, issuer, db_url) -> LaneTarget`,
  `observe_guest(run, op, system_id, scratch, entry)`, `prove_rejection(run, caller, boundary,
  rejection, snapshot)`, `project_state(db_url, project)`, `boundary_of`, `tool_cells`, `one`,
  `Grants`, `Guest`, `HttpCaller`, `Rejection`, `Boundary`, `Exposure`;
- `scenario`: `CellRun`, `ScenarioStop`, `on_catalog_system(run, base_url, issuer, db_url, *,
  project, image, body)`;
- `spine`: `build_profile(arch)`, `drain_job(client, phase, job_id, *, deadline_s)`,
  `mint_role_token(issuer, *, project, agent_session, role)`, `ok`, `scalar`;
- `deep_lifecycle`: `FIXTURE_ROOT_ENV`, `boot_kernel_sha256(tree, arch)`, `file_sha256`,
  `load_fixture(root, name, arch)`;
- `spine.build_and_upload_kernel` (Task 2's `complete` flag);
- `image_smoke`: `Endpoint`, `ssh(endpoint, key, command)`, `os_matches(entry, probe)`;
- `scripts.kernel_fixtures.identity(toolchain)`.

**Verification.**
- Contract: the 24 scenarios bind the carrier node, local only. Mode: focused-test.
  `tests/scripts/test_coverage_contract.py::test_pending_cells_have_owned_assertions_but_no_invented_nodes`.
  Red: `len(local) == 96` fails (no rows). Green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.
- Contract: the carrier's live cells. Mode: task-test-not-applicable. Every body acts only through
  a live stack, a KVM guest and a kernel fixture. They are proven by Task 4's lab run and
  `qualify`, and `pytest --collect-only` shows the parametrization.

Steps:

1. In `tests/scripts/test_coverage_contract.py`, add below `_SYSTEM_NODE`:
   ```python
   _RUN_NODE = "tests/integration/test_run_tool_cells_live.py::test_run_tool_cell"
   ```
   In `test_pending_cells_have_owned_assertions_but_no_invented_nodes`, after the `remote`
   System assertion, add:
   ```python
       runs = [c for c in contract.cells if c.operation in _RUN_TOOLS]
       local_runs = [c for c in runs if c.provider == "local-libvirt"]
       assert len(local_runs) == 192 and {c.node_id for c in local_runs} == {_RUN_NODE}
       assert {c.node_id for c in runs if c.provider == "remote-libvirt"} == {None}
   ```
   (192 is 96 x86_64 plus the 96 ppc64le local cells that share the scenarios.) Add
   `*_RUN_TOOLS` to the `bound` set and change the final `node_id is None` comprehension to
   `if c.operation not in bound or (c.operation in _RUN_TOOLS and c.provider == "remote-libvirt")`.
   Run: red.
2. Append to `[implementations]` in `obligations.toml` the 24 rows below, each
   `= "tests/integration/test_run_tool_cells_live.py::test_run_tool_cell"`:
   `tool/images.publish/default/{authentication,authorization,functional,validation}` and,
   for each of `runs.boot`, `runs.cancel`, `runs.install`, `runs.release_external_boot`,
   `tool/<tool>/default/{authentication,authorization,functional,project-isolation,validation}`.
3. Create `tests/integration/test_run_tool_cells_live.py`:

```python
"""Prove the x86_64 local run and image tool cells over HTTP (#3119).

``live_stack``-marked (ADR-0722). One parameter per native local-libvirt contract cell of
``runs.install``, ``runs.boot``, ``runs.cancel``, ``runs.release_external_boot`` and
``images.publish``, framed by :func:`~tests.integration.live_stack.tool_cells.run_tool_cell`. A
``runs.*`` functional cell provisions the lane image in a fresh ``cov-<hex>`` project through
:func:`~tests.integration.live_stack.tool_cells.on_lane_system`, uploads the verified ``longterm``
kernel fixture and calls the tool in the cell's exposure. The functional ``images.publish`` cell
publishes its exposure's :data:`~tests.integration.live_stack.tool_cells.PUBLISHED_IMAGES` image
and boots it. The
release functional cells stop ``blocked``: the demo-up lane configures no external-boot
authority. A ``runs.*`` rejection cell aims at one unbound Run in the stack's lane-target project;
an ``images.publish`` one at the published image's name. ``docs/operating/runbooks/
live-testing.md`` covers the run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import secrets
import tempfile
import xml.etree.ElementTree as ET  # noqa: S405 - the worker's own domain XML  # nosec B405
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest

from kdive.domain.errors import ErrorCategory
from kdive.images.rootfs.catalog import RootfsCatalogEntry, load_rootfs_catalog
from kdive.mcp.dev_harness import LiveStackClient, OidcIssuer
from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from scripts.kernel_fixtures import identity
from tests.integration.live_stack.deep_lifecycle import (
    FIXTURE_ROOT_ENV,
    boot_kernel_sha256,
    deep_body,
    file_sha256,
    load_fixture,
)
from tests.integration.live_stack.image_smoke import Endpoint, os_matches, ssh
from tests.integration.live_stack.scenario import CellRun, ScenarioStop, on_catalog_system
from tests.integration.live_stack.spine import (
    build_and_upload_kernel,
    build_profile,
    drain_job,
    mint_role_token,
    ok,
    scalar,
)
from tests.integration.live_stack.tool_cells import (
    PUBLISH_TOOL,
    PUBLISHED_IMAGES,
    Boundary,
    Exposure,
    Grants,
    Guest,
    HttpCaller,
    Rejection,
    boundary_of,
    lane_target,
    observe_guest,
    on_lane_system,
    one,
    project_state,
    prove_rejection,
    run_tool_cell,
    settled,
    tool_cells,
)

pytestmark = pytest.mark.live_stack

TOOLS = (
    "images.publish",
    "runs.boot",
    "runs.cancel",
    "runs.install",
    "runs.release_external_boot",
)
# The kernel fixture every runs.* cell uploads; write the bindings with
# ``--kernel-baseline longterm``.
BASELINE = "longterm"
_PROVIDER = "local-libvirt"
_BUILD_DEADLINE_S = 3600.0
_BOOT_ID = "cat /proc/sys/kernel/random/boot_id"
_NO_AUTHORITY = (
    "runs.release_external_boot needs a configured local external-boot authority and an "
    "authority-lane System frame; the demo-up lane installs neither"
)


def _grants(project: str, role: str) -> Grants:
    return Grants(f"{project}-{role}", (project,), {project: role})


def _stranger() -> Grants:
    """An operator of a fresh project, holding no role in the target and no platform role."""
    other = f"cov-{secrets.token_hex(4)}"
    return _grants(other, "operator")


def _platform_operator() -> Grants:
    project = f"cov-{secrets.token_hex(4)}"
    return Grants(f"{project}-platform", (project,), {}, ("platform_operator",))


async def _call(
    caller: HttpCaller, tool: str, args: Mapping[str, object], token: str
) -> ToolResponse:
    return one(await caller.call(tool, args, token, discover=True))


def _fixture() -> tuple[Path, dict[str, Any]]:
    """The verified ``longterm`` fixture; ``blocked`` before any stack mutation when absent."""
    root = os.environ.get(FIXTURE_ROOT_ENV)
    if not root:
        raise ScenarioStop(
            Outcome.BLOCKED, f"{FIXTURE_ROOT_ENV} unset; build the {BASELINE} fixture"
        )
    try:
        return load_fixture(Path(root), BASELINE, platform.machine())
    except ValueError as exc:
        raise ScenarioStop(Outcome.BLOCKED, f"{BASELINE} fixture: {exc}") from None


async def _create_run(op: LiveStackClient, investigation: str, system_id: str | None) -> str:
    args: dict[str, object] = {
        "investigation_id": investigation,
        "build_profile": build_profile(platform.machine()),
    }
    # An unbound Run names the Resource kind it builds for; a bound one derives it.
    args |= {"system_id": system_id} if system_id else {"target_kind": _PROVIDER}
    return ok(await scalar(op, "runs.create", **args), "create-run").object_id


async def _open(op: LiveStackClient, project: str, title: str) -> str:
    return ok(
        await scalar(op, "investigations.open", project=project, title=title), "open"
    ).object_id


async def _close(op: LiveStackClient, investigation: str) -> None:
    closed = await scalar(
        op, "investigations.close", investigation_id=investigation, summary="done"
    )
    assert closed.status == "closed", f"investigation not closed: {closed.status}"


def _domain_kernel(
    xml: Callable[[str], str], system_id: str, _endpoint: Endpoint, _key: Path, _release: str
) -> tuple[str, str]:
    """The ``<os><kernel>`` file the domain boots (the install's staged kernel) and its digest."""
    kernel = ET.fromstring(xml(system_id)).findtext("./os/kernel")  # noqa: S314  # nosec B314
    assert kernel, "the installed domain names no direct kernel"
    return file_sha256(kernel), kernel


async def _step(
    run: CellRun,
    caller: HttpCaller,
    token: str,
    tree: Path,
    manifest: dict[str, Any],
    guest: Guest,
) -> dict[str, object]:
    """Upload, install and boot; the tool under test goes through the cell's exposure."""
    tool = run.cell.operation

    async def through(step: str, run_id: str) -> ToolResponse:
        if f"runs.{step}" == tool:
            return await _call(caller, tool, {"run_id": run_id}, token)
        return await scalar(guest.op, f"runs.{step}", run_id=run_id)

    # deep_body proves its own assertion names; they back this cell's `effect`, not its record.
    deep = CellRun(run.cell, run.writer, observed=run.observed)
    tmp = guest.scratch / "deep"
    tmp.mkdir()
    await deep_body(
        deep,
        guest.op,
        guest.system_id,
        guest.owned,
        project=guest.project,
        entry=guest.lane.entry,
        tree=tree,
        manifest=manifest,
        tmp=tmp,
        installed_kernel=partial(_domain_kernel, guest.lane.xml),
        step=through,
    )
    run.artifacts.extend(deep.assertions.values())
    return {"tool": tool, "proofs": dict(sorted(deep.assertions.items()))}


def _os_boot(xml: str) -> tuple[str | None, str | None]:
    os_element = ET.fromstring(xml).find("os")  # noqa: S314  # nosec B314
    assert os_element is not None, "the domain XML has no <os>"
    return os_element.findtext("kernel"), os_element.findtext("cmdline")


async def _boot_id(guest: Guest) -> str:
    result = await asyncio.to_thread(ssh, guest.endpoint, guest.key, _BOOT_ID)
    assert result.returncode == 0, f"boot_id read exited {result.returncode}"
    return result.stdout.strip()


async def _cancel(
    run: CellRun,
    caller: HttpCaller,
    token: str,
    tree: Path,
    manifest: dict[str, Any],
    guest: Guest,
) -> dict[str, object]:
    """Cancel an uploaded, uncompleted Run: its System is untouched and freed, its build closed.

    Only a ``created`` or ``running`` Run is cancelable; ``runs.complete_build`` would make it
    ``succeeded``, which ``runs.cancel`` answers with ``conflict``.
    """
    op = guest.op
    upload = guest.scratch / "upload"
    investigation = await _open(op, guest.project, "run cancel")
    try:
        run_id = await _create_run(op, investigation, guest.system_id)
        await build_and_upload_kernel(
            op,
            run_id=run_id,
            arch=manifest["arch"],
            kernel_tree=tree,
            evidence_dir=upload,
            with_vmlinux=True,
            require_network=True,
            root_fs="ext4",
            complete=False,
        )
        boot = _os_boot(guest.lane.xml(guest.system_id))
        boot_id = await _boot_id(guest)
        held = await scalar(
            op,
            "runs.create",
            investigation_id=investigation,
            system_id=guest.system_id,
            build_profile=build_profile(manifest["arch"]),
        )
        assert held.data.get("reason") == "system_has_live_run", f"second create: {held.data}"
        env = await _call(caller, "runs.cancel", {"run_id": run_id}, token)
        assert env.status == "canceled", f"runs.cancel answered {env.status}"
        after = ok(await scalar(op, "runs.get", run_id=run_id), "read-after")
        steps = cast(Mapping[str, object], after.data.get("steps") or {})
        ran = [s for s in ("install", "boot") if steps.get(s) == "succeeded"]
        assert after.status == "canceled" and not ran, f"Run {after.status}, steps ran {ran}"
        assert after.data.get("build_ref") is None, "the canceled Run carries a build"
        closed = await scalar(op, "runs.complete_build", run_id=run_id, build_id="0" * 40)
        assert closed.error_category is not None, "complete_build accepted a canceled Run"
        again = ok(await scalar(op, "runs.get", run_id=run_id), "read-again")
        assert again.status == "canceled", f"the Run became {again.status}"
        system = ok(await scalar(op, "systems.get", system_id=guest.system_id), "system")
        assert system.status == "ready", f"the System is {system.status}"
        assert _os_boot(guest.lane.xml(guest.system_id)) == boot, "the domain's boot changed"
        assert await _boot_id(guest) == boot_id, "the guest rebooted"
        freed = await _create_run(op, investigation, guest.system_id)
        ok(await scalar(op, "runs.cancel", run_id=freed), "cancel-freed")
    finally:
        await _close(op, investigation)
    record = json.loads((upload / "upload.json").read_text(encoding="utf-8"))
    run.observed |= {
        "kernel_sha256": boot_kernel_sha256(tree, manifest["arch"]),
        "kernel_build_id": record["build_id"],
        "kernel_source_sha": manifest["source"]["commit"],
        "kernel_config_sha256": file_sha256(upload / "effective_config"),
        "compiler_id": identity(manifest["toolchain"]),
    }
    return {
        "run": "canceled",
        "held_before_cancel": "system_has_live_run",
        "build": "never-completed",
        "complete_build_after": closed.error_category,
        "system": "ready",
        "guest_rebooted": False,
        "system_freed": True,
    }


async def _described(op: LiveStackClient, name: str, arch: str) -> ToolResponse:
    """``images.describe`` of local-libvirt catalog image ``name``."""
    cursor = None
    while True:
        args: dict[str, object] = {"request": {"cursor": cursor}} if cursor else {}
        listing = ok(await scalar(op, "images.list", **args), "describe")
        match = next(
            (
                item
                for item in listing.items
                if (item.data.get("provider"), item.data.get("name"), item.data.get("arch"))
                == (_PROVIDER, name, arch)
            ),
            None,
        )
        if match is not None:
            return ok(await scalar(op, "images.describe", image_id=match.object_id), "describe")
        cursor = listing.data.get("next_cursor") if listing.data.get("truncated") else None
        assert cursor, f"{name} is not in the catalog after its publication"


def _provenance_matches(described: ToolResponse, entry: RootfsCatalogEntry) -> None:
    """The build-recorded ``os_release`` and ``arch`` name the catalog row's platform."""
    provenance = cast(Mapping[str, Any], described.data.get("provenance") or {})
    release = {str(k): str(v) for k, v in (provenance.get("os_release") or {}).items()}
    built = {**release, "machine": str(provenance.get("arch"))}
    assert os_matches(entry, built), f"provenance names {built.get('ID')} {built.get('VERSION_ID')}"


async def _build_jobs(db_url: str, name: str) -> int:
    """The number of ``IMAGE_BUILD`` jobs ``images.publish`` enqueued for ``name``."""
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT count(*) FROM jobs WHERE dedup_key = %s", (f"image_build:{_PROVIDER}:{name}",)
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _pending_rows(db_url: str, name: str) -> int:
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT count(*) FROM image_catalog WHERE provider = %s AND name = %s "
            "AND state = 'pending'",
            (_PROVIDER, name),
        )
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _publish(
    run: CellRun, caller: HttpCaller, base_url: str, issuer: OidcIssuer, db_url: str
) -> None:
    """Publish the cell's image through its exposure, then boot it and prove the frame cleanup."""
    name = PUBLISHED_IMAGES.get((platform.machine(), run.cell.exposure))
    if name is None:
        raise ScenarioStop(Outcome.BLOCKED, f"no published image for {platform.machine()}")
    entry = load_rootfs_catalog()[name]
    # images.publish never recycles a job of one name: a prior job would be returned again, and
    # the cell would re-observe another publication.
    if await _build_jobs(db_url, name):
        raise ScenarioStop(
            Outcome.BLOCKED, f"{name} was already published on this stack; wipe it first"
        )
    token = caller.token(_platform_operator())
    env = await _call(caller, PUBLISH_TOOL, {"provider": _PROVIDER, "name": name}, token)
    # The build job's authorizing project is `platform`; a viewer there may wait on it.
    viewer = mint_role_token(issuer, project="platform", agent_session="cov-publish", role="viewer")
    async with LiveStackClient.over_http(base_url, viewer) as platform_client:
        await drain_job(platform_client, "publish", env.object_id, deadline_s=_BUILD_DEADLINE_S)
    project = f"cov-{secrets.token_hex(4)}"

    async def body(op: LiveStackClient, system_id: str, _owned: list[str]) -> None:
        described = await _described(op, name, entry.arch)
        digest = str(described.data.get("digest", ""))
        assert described.data.get("state") == "registered", "the published row is not registered"
        assert digest.startswith("sha256:") and len(digest) == 71, f"digest {digest!r}"
        assert await _pending_rows(db_url, name) == 0, f"a pending {name} row remains"
        _provenance_matches(described, entry)
        with tempfile.TemporaryDirectory() as scratch:
            await observe_guest(run, op, system_id, Path(scratch), entry)
        run.prove(
            "effect",
            {
                "exposure": run.cell.exposure,
                "job": {"enqueued": env.status, "drained": "succeeded"},
                "image": name,
                "state": "registered",
                "digest": digest,
                "provenance": "matches-catalog",
                "guest": "matches-catalog",
            },
        )

    await on_catalog_system(run, base_url, issuer, db_url, project=project, image=name, body=body)
    # The published image is the cell's output, recorded in `effect`; it binds no input digest.
    run.observed["image_sha256"] = None


async def _functional(
    run: CellRun, caller: HttpCaller, base_url: str, issuer: OidcIssuer, db_url: str
) -> None:
    tool = run.cell.operation
    if tool == "runs.release_external_boot":
        raise ScenarioStop(Outcome.BLOCKED, _NO_AUTHORITY)
    if tool == PUBLISH_TOOL:
        await _publish(run, caller, base_url, issuer, db_url)
        return
    tree, manifest = _fixture()
    project = f"cov-{secrets.token_hex(4)}"
    token = caller.token(_grants(project, "contributor"))
    body = _cancel if tool == "runs.cancel" else _step
    await on_lane_system(
        run,
        base_url,
        issuer,
        db_url,
        project=project,
        body=partial(body, run, caller, token, tree, manifest),
    )


@dataclass(frozen=True)
class _RunTarget:
    """An unbound ``created`` Run in the lane target's project T: what ``runs.*`` cells aim at."""

    project: str
    run_id: str


_RUN_TARGETS: dict[str, _RunTarget | Exception] = {}


async def _unbound_run(base_url: str, issuer: OidcIssuer, db_url: str, project: str) -> str:
    token = mint_role_token(
        issuer, project=project, agent_session=f"{project}-sess", role="operator"
    )
    async with LiveStackClient.over_http(base_url, token) as op:
        run_id = await _create_run(op, await _open(op, project, "run target"), None)
    await settled(db_url, project)
    return run_id


async def _run_target(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> _RunTarget:
    """The stack's target Run, created on first use; a failure is replayed, not retried.

    Every ``runs.*`` handler resolves the Run and checks membership and the contributor role
    before any binding or state check, so an unbound Run is a valid target.
    """
    target = await lane_target(run, base_url, issuer, db_url)
    cached = _RUN_TARGETS.get(base_url)
    if cached is None:
        try:
            run_id = await _unbound_run(base_url, issuer, db_url, target.project)
            cached = _RunTarget(target.project, run_id)
        except Exception as exc:  # noqa: BLE001 - remembered and replayed for every cell
            cached = exc
        _RUN_TARGETS[base_url] = cached
    if isinstance(cached, Exception):
        raise AssertionError(f"the target Run could not be prepared: {cached!r}")
    return cached


# install and boot answer a Run outside the caller's projects as configuration_error, cancel and
# release as not_found; each is identical to the answer for an absent run_id.
_ISOLATION = {
    "runs.boot": frozenset({ErrorCategory.CONFIGURATION_ERROR.value}),
    "runs.install": frozenset({ErrorCategory.CONFIGURATION_ERROR.value}),
    "runs.cancel": frozenset({ErrorCategory.NOT_FOUND.value}),
    "runs.release_external_boot": frozenset({ErrorCategory.NOT_FOUND.value}),
}


def _run_rejection(tool: str, boundary: Boundary, target: _RunTarget) -> Rejection:
    args = {"run_id": target.run_id}
    if boundary == "validation":
        return Rejection({"run_id": 7}, _grants(target.project, "contributor"))
    if boundary in ("authentication", "authorization"):
        # Viewer is one rank below the contributor gate; its issued-token call writes nothing.
        return Rejection(args, _grants(target.project, "viewer"))
    twin = {"run_id": str(uuid4())}
    return Rejection(args, _stranger(), _ISOLATION[tool], absent_twin=twin)


def _publish_rejection(boundary: Boundary, name: str) -> Rejection:
    args = {"provider": _PROVIDER, "name": name}
    if boundary == "validation":
        return Rejection({**args, "name": 7}, _platform_operator())
    return Rejection(args, _stranger())


async def _publish_state(db_url: str, name: str) -> dict[str, object]:
    """The ``platform`` snapshot, the image's catalog rows and its build jobs.

    ``jobs`` has no project column, so ``project_state`` cannot see a leaked publish's job; the
    dedup-key count can.
    """
    platform_state = await project_state(db_url, "platform")
    async with await psycopg.AsyncConnection.connect(db_url) as conn:
        await conn.set_read_only(True)
        cursor = await conn.execute(
            "SELECT count(*), coalesce(string_agg(t::text, ',' ORDER BY t::text), '') "
            "FROM image_catalog t WHERE t.provider = %s AND t.name = %s",
            (_PROVIDER, name),
        )
        row = await cursor.fetchone()
    assert row is not None
    rows = [row[0], hashlib.sha256(str(row[1]).encode()).hexdigest()]
    jobs = await _build_jobs(db_url, name)
    return {"platform": platform_state, "image_catalog": rows, "build_jobs": jobs}


async def _scenario(run: CellRun, base_url: str, issuer: OidcIssuer, db_url: str) -> None:
    caller = HttpCaller(cast(Exposure, run.cell.exposure), base_url, issuer)
    if run.cell.kind == "functional":
        await _functional(run, caller, base_url, issuer, db_url)
        return
    boundary = boundary_of(run.cell)
    if run.cell.operation == PUBLISH_TOOL:
        await lane_target(run, base_url, issuer, db_url)  # the record's observed context
        name = PUBLISHED_IMAGES[(platform.machine(), run.cell.exposure)]
        snapshot = partial(_publish_state, db_url, name)
        rejection = _publish_rejection(boundary, name)
    else:
        target = await _run_target(run, base_url, issuer, db_url)
        snapshot = partial(project_state, db_url, target.project)
        rejection = _run_rejection(run.cell.operation, boundary, target)
    await prove_rejection(run, caller, boundary, rejection, snapshot)


def _cells() -> list[Cell]:
    host = platform.machine()
    return [c for c in tool_cells(TOOLS) if c.provider == _PROVIDER and c.guest_arch == host]


@pytest.mark.parametrize("cell", _cells(), ids=lambda cell: cell.id)
def test_run_tool_cell(cell: Cell) -> None:
    """Prove one configuration × exposure × kind cell of a run or image lifecycle tool."""
    run_tool_cell(cell, _scenario)
```

4. Run `just format`, `just lint` and `just type`. Then run
   `uv run python -m pytest tests/integration/test_run_tool_cells_live.py --collect-only -q`:
   expect 96 tests collected on x86_64. Run
   `uv run python -m pytest tests/scripts/test_coverage_contract.py tests/integration/live_stack -q`:
   expect green. Commit `test(live): carry the x86_64 local run and image tool cells`.

Acceptance: 96 parameters on x86_64; contract tests green; the release functional body records
`blocked` and never `success`.

## Task 4 — Runbook and live proof

**Verification.**
- Contract: runbook prose. Mode: task-test-not-applicable. It is prose with no executable
  consumer; `just docs-check` checks links.
- Contract: the live cells. Mode: task-test-not-applicable. They are proven by the lab run below,
  whose `qualify` output is recorded.

Steps:

1. In `docs/operating/runbooks/live-testing.md`, after the "System lifecycle tool cells (#3062)"
   section, add "#### Run and image tool cells (#3119)". It states:
   - the carrier and its 96 cells, and the per-tool flags (option B), linking the spec;
   - the prerequisites of the System cells plus `KDIVE_FIXTURE_ROOT` holding a verified
     `longterm` fixture (the deep-lifecycle build command, `--baseline longterm`);
   - bindings written with
     `uv run python -m tests.integration.live_stack.tool_cells bindings --candidate "$sha" --out inputs.json --kernel-baseline longterm`;
   - the run command
     `uv run python -m pytest -m live_stack tests/integration/test_run_tool_cells_live.py`, once
     per lane, then assembly and `qualify` as in the System section;
   - the four `runs.release_external_boot` functional cells record `blocked`
     (`missing-prerequisite`) because the demo-up lane installs no external-boot authority;
   - each stack's two functional `images.publish` cells build `fedora-kdive-ready-43-cloud`
     (`direct`) and `rocky-kdive-ready-9` (`gateway`) on the worker, which needs network access
     to the pinned cloud-image URLs and the libguestfs build tools. Both rows stay registered,
     and the lane must be wiped (`demo-down.sh --wipe --yes`) before the other configuration,
     because a second publish of one name returns the first job;
   - the recorded lab result.
2. Lab run, on the disposable kdive-servers Fedora 44 guest (control plane, idle). Use a probe
   branch committed on the guest at the candidate, and bring each stack up with
   `examples/local-libvirt/demo-up.sh` (`KDIVE_WORKER_DEATH_VERIFIER=docker` for recovery).
   Rebuild the capture-bootstrap manifest, check worker `/readyz` `ready`, stage
   `fedora-kdive-ready-44`, build the `longterm` fixture, write the bindings, run one lane, wipe
   and bring up the other lane (restaging the lane image), run it, then assemble and `qualify`.
   Record whether the worker's image-build workspace holds leftovers after each publish. Expected: 92 qualified (16 `success`, 76 `rejection`) and the 4
   release functional cells `blocked`. Afterwards run `demo-down.sh --wipe --yes`, check that
   `virsh list --all` shows no `kdive-` domain, and remove the probe refs, scripts and bundles.
3. Write the result into the runbook section, run `just docs-check`, and commit
   `docs(runbook): record the run and image tool-cell lab run`.

Rollback: every change is test, contract or documentation. Reverting the commits restores the
merged frame and the single group 3119.
