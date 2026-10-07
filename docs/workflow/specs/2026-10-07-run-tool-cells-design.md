# The x86_64 local run and image tool cells (#3119)

Decision records: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
and its 2026-10-02 amendment; evidence identity per
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md); pinned kernel
fixtures per [ADR-0693](../../adr/0693-pinned-external-kernel-fixtures.md). Builds on the #3062
provider-lane frame ([spec](2026-10-06-system-tool-cells-design.md)) and the #2809 deep-lifecycle
body (`tests/integration/live_stack/deep_lifecycle.py`, `deep_body`).

## Problem

Group 3119 holds `runs.install`, `runs.boot`, `runs.cancel`, `runs.release_external_boot` and
`images.publish`. Their x86_64 local-libvirt cells are 96: 20 functional and 76 rejection.
No carrier binds them. The merged contract also gives every functional cell the `authority`
deployed role and the six kernel inputs (`authority = true`, `kernel = true` on the group).
The demo-up lane installs no provider authority: `deploy/ansible/playbooks/local-libvirt-host.yml`
runs no authority role. So `run_cell` records `missing:authority` and `qualify` reports
`deployed-role-missing` for all 20. `images.publish` uploads no kernel, so it could never
observe the kernel inputs either. The operator chose per-tool flags on 2026-10-07 (option B,
recorded in the issue's `WORK:SCOPE`).

## Scope

1. **Contract flags** in `scripts/coverage_campaign/obligations.toml`. Group 3119 becomes three
   provider groups, all owner 3119, each tool keeping its observation string:
   - `runs.boot`, `runs.cancel`, `runs.install`: `kernel = true`, `authority = false`;
   - `images.publish`: `kernel = false`, `authority = false`;
   - `runs.release_external_boot`: `kernel = true`, `authority = true`.

   `contract.py` is unchanged. Its owner-3119 routes still send remote cells to #3120 and
   ppc64le cells to #2818, and those cells take the same flags. The 96 local x86_64 cells stay
   owner 3119.
2. **Bindings** in `tests/integration/live_stack/tool_cells.py`: a native functional
   `images.publish` cell boots the image it publishes, not the lane image. `PUBLISHED_IMAGES`
   names that image per architecture: `fedora-kdive-ready-43` for x86_64, a catalog image no
   other carrier boots. `bindings()` gives these cells that entry's guest OS and architecture,
   the cell's accelerator, and `image_sha256` null: the published image is the cell's output,
   not an input. Every other cell binds as before. `_settled` becomes public `settled`, which
   the carrier reuses.
3. **`deep_body` seam** in `deep_lifecycle.py`: an optional `step` argument issues the
   `runs.install` and `runs.boot` calls, so a cell can make one of them through its exposure.
   The default is the operator call used today. `_upload` becomes public `upload_fixture`.
4. **Carrier** `tests/integration/test_run_tool_cells_live.py::test_run_tool_cell`. It is
   parametrized over the five tools' native local-libvirt cells and binds the 24 scenarios
   (5 functional; 4 rejection boundaries for each `runs.*` tool; 3 for `images.publish`, which
   has no project-isolation cell).
5. A runbook section in `docs/operating/runbooks/live-testing.md` and one live run of both
   configurations on the disposable lab host.

No product source, ADR or migration change.

### Functional effects

Each `runs.*` functional cell runs in a fresh project P, framed by `on_lane_system`. Its kernel
is the verified `longterm` fixture under `$KDIVE_FIXTURE_ROOT`, and the bindings are written
with `--kernel-baseline longterm`. An unset root or an unverifiable fixture stops the cell
`blocked` before any provisioning. The tool under test is called through the cell's exposure as
a contributor of P. Setup calls go through the frame's operator client: investigation, Run,
upload, the step that is not under test, and draining jobs.

- `runs.install` and `runs.boot`: `deep_body` runs on its own scratch `CellRun`, with the tool
  under test routed through the exposure. Its proofs become the cell's `effect` observation:
  - upload digests and build ID;
  - the domain's staged `<kernel>` equals the uploaded boot member, and that path joins the
    owned set;
  - reconnect with a new `boot_id`;
  - running release and GNU build ID equal the fixture's;
  - the loaded `loop` module equals the uploaded `modules_install` copy.

  Its observed kernel fields become the cell's context. The local install writes no config file
  into the guest or the staging root. The config claim is the uploaded `effective_config` digest,
  which `qualify` compares with the fixture manifest's `.config` digest, together with the
  running build ID of the kernel built from that config.
- `runs.cancel`: the cell opens an Investigation and creates a Run bound to the System. It
  uploads the fixture, reads the Run (`build_ref`, `steps`), the domain XML and the guest
  `boot_id`, then cancels through the exposure. Afterwards:
  - the Run is `canceled` and keeps the same `build_ref`;
  - no `install` or `boot` step succeeded;
  - the System is `ready`, its domain XML is unchanged, and the guest keeps its `boot_id`;
  - a second `runs.create` on the same System succeeds (the System is freed) and is then
    cancelled by the operator.

  The kernel context is the uploaded build's identity: boot-member digest, build ID,
  `effective_config` digest, and the manifest's source commit and toolchain.
- `runs.release_external_boot`: stops `blocked` before any stack mutation. The reason names the
  missing external-boot authority on the demo-up lane and the missing authority-lane System
  frame. It is never recorded as covered.
- `images.publish`: a platform operator calls `images.publish` (`local-libvirt`, the published
  image) through the exposure, then drains the build job with that token (deadline 3600 s).
  `on_catalog_system` then provisions the published image in P, observes the guest with
  `observe_guest` and proves the frame's cleanup. The `effect` asserts:
  - the catalog row is `registered` with a `sha256:` digest;
  - its provenance `arch`, `releasever` and `os_release` `ID`/`VERSION_ID` match the catalog
    entry;
  - the booted guest matches the entry.

  The published digest goes into the `effect` artifact, and the context's `image_sha256` is
  reset to null.

### Rejection cells

The `runs.*` target is one unbound Run per stack (ADR-0169). It is created in `lane_target`'s
project T through T's operator client, and the frame waits until T's snapshot is stable. Every
`runs.*` handler resolves the Run, checks project membership (`not_found`), then the contributor
role, before any binding or state check (`steps.py`, `cancel.py`,
`external_boot/recovery_requests.py`). So an unbound `created` Run is a valid target, and a
leaked cancel or install would change T's snapshot. `images.publish` rejection cells take only
`lane_target`'s observed context. Their snapshot is the `platform` project's
`project_state` plus the published image's `image_catalog` rows.

| Boundary | Grants | Accepted |
|---|---|---|
| authentication | `runs.*`: viewer of T; `images.publish`: operator of a fresh project with no platform role | HTTP 401 for the foreign signature; the issued token is not refused |
| authorization | `runs.*`: viewer of T; `images.publish`: operator of a fresh project with no platform role | `authorization_denied` |
| project-isolation | `runs.*` only: operator of a fresh project | `not_found`, identical to the answer for an absent `run_id` (`absent_twin`) |
| validation | the functional grants (contributor of T, or `platform_operator`), with `run_id` or `name` mistyped as an integer | ADR-0722 §3 as amended |

## Failure model

1. **Actors and deployments:**
   - an operator running the live tier on the disposable lab host, one demo-up stack per
     configuration (`recovery` with `KDIVE_WORKER_DEATH_VERIFIER=docker`), local-libvirt
     x86_64 only, no provider authority installed;
   - CI, which runs only the unit and contract tests.
2. **Invariants and assets at stake:**
   - honest per-cell outcomes: the four release functional cells are blocked, never covered;
   - a contract whose flags match the operator's option B;
   - the host left as found: every System a completing cell creates is torn down, its domain
     undefined, its disks and the staged kernel path removed, and its capacity returned, proven
     per cell.
3. **Accepted failure classes:**
   - The published `fedora-kdive-ready-43` catalog row stays registered after its cell, because
     no tool deletes a public image. It is the cell's product, and the post-run
     `demo-down.sh --wipe --yes` clears it.
   - The rejection target Run and its Investigation stay in T as history, like the torn-down
     target System.
   - A cell killed mid-run can leave a System, allocation, Run or image build behind. The
     post-run wipe clears them, as for the earlier carriers.
   - A failed lane target or Run target fails every rejection cell of the stack that needs it.
     It is not retried.
   - A background change to T or `platform` during a rejection call fails that cell. No other
     allocator or publisher runs on the stack.
4. **Covered elsewhere:** a configured local external-boot authority and its release frame
   (follow-up candidate), remote run/image cells (#3120), ppc64le cells (#2818), the capability
   boundary (#2814), and the System tools and frame (#3062, #3080).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| per-tool flags | focused-test | `test_coverage_contract.py`: owner 3119 still 96, 3120 still 96, ppc64le to 2818; functional roles and input counts per tool |
| carrier bindings | focused-test | `test_coverage_contract.py`: the 24 scenarios bind the run carrier node |
| published-image bindings | focused-test | `test_tool_cells.py`: a functional `images.publish` cell binds the published entry with a null digest; its rejection cells and other tools bind the lane |
| `deep_body` step seam | focused-test | `test_deep_lifecycle.py`: an injected `step` receives `install` then `boot` with the Run id |
| live cells | task-test-not-applicable | they act only against a live stack; proven by the two-configuration lab run and `qualify` |
| runbook | task-test-not-applicable | prose; `just docs-check` |
