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
   names that image per architecture and exposure: on x86_64, `fedora-kdive-ready-43-cloud` for
   `direct` and `rocky-kdive-ready-9` for `gateway`. Both are single-kernel cloud-image rows
   that no other cell of this carrier boots. Each exposure needs its own image because
   `images.publish` deduplicates on provider and name and never recycles a finished job
   (`build_publish.py`, `queue.enqueue`), so a second publish of one name in a stack would
   return the first cell's job. `bindings()` gives these cells their entry's guest OS and
   architecture, the cell's accelerator, and `image_sha256` null: the published image is the
   cell's output, not an input. Every other cell binds as before, and the CLI's
   unstaged-image warning skips these deliberate null digests. `_settled` becomes public
   `settled`, which the carrier reuses.
3. **`deep_body` seam** in `deep_lifecycle.py`: an optional `step` argument issues the
   `runs.install` and `runs.boot` calls, so a cell can make one of them through its exposure.
   The default is the operator call used today.
   `spine.build_and_upload_kernel` gains `complete: bool = True`; `False` uploads the
   artifacts and skips `runs.complete_build`, leaving the Run `created`.
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
- `runs.cancel`: only a `created` or `running` Run is cancelable; `runs.complete_build` moves a
  Run to `succeeded`, which `runs.cancel` answers with `conflict` (`cancel.py`). The cell opens
  an Investigation and creates a Run bound to the System. It uploads the fixture's artifacts
  without completing the build (`complete=False`), so the Run stays `created`. It then records
  the domain's `<os>` kernel and cmdline and the guest `boot_id`, and checks that a second
  `runs.create` on the System is refused with `system_has_live_run`. Then it cancels through
  the exposure. Afterwards:
  - the Run is `canceled`, with no `build_ref` and no succeeded `install` or `boot` step;
  - `runs.complete_build` on it is refused, and it stays `canceled`;
  - the System is `ready`, its `<os>` kernel and cmdline are unchanged, and the guest keeps its
    `boot_id`;
  - a new `runs.create` on the same System succeeds (the System is freed) and is then cancelled
    by the operator.

  The kernel context is the uploaded artifacts' identity: the boot member of the uploaded
  bundle, the uploaded `vmlinux` build ID, the uploaded `effective_config` digest, and the
  manifest's source commit and toolchain.
- `runs.release_external_boot`: stops `blocked` before any stack mutation. The reason names the
  missing external-boot authority on the demo-up lane and the missing authority-lane System
  frame. It is never recorded as covered.
- `images.publish`: a platform operator calls `images.publish` (`local-libvirt`, the cell's
  published image) through the exposure. First the cell checks that no job with that image's
  dedup key exists; one would be returned again, so a prior publication in the stack stops the
  cell `blocked`, naming the wipe. The build job's authorizing project is `platform`, so a viewer of `platform` drains it
  (deadline 3600 s). `on_catalog_system` then provisions the published image in P, observes the
  guest with `observe_guest` and proves the frame's cleanup. The `effect` asserts:
  - the catalog row is `registered` with a `sha256:` digest, and no row of that name is
    `pending`;
  - its provenance `os_release` and `arch` match the catalog entry (`image_smoke.os_matches`);
  - the booted guest matches the entry.

  The published digest goes into the `effect` artifact, and the context's `image_sha256` is
  reset to null. The cell's `cleanup` covers the boot frame: System, domain, disks and capacity.
  The worker's build workspace belongs to the image build plane and is not observed. The lab
  run reports what it finds there.

### Rejection cells

The `runs.*` target is one unbound Run per stack (ADR-0169), created with
`target_kind="local-libvirt"` in `lane_target`'s project T through T's operator client. The
frame then waits until T's snapshot is stable. Every `runs.*` handler resolves the Run and
checks project membership, then the contributor role, before any binding or state check
(`steps.py`, `cancel.py`, `external_boot/recovery_requests.py`). So an unbound `created` Run is
a valid target. A leaked cancel would change T's snapshot. A leaked install, boot or release on
that Run writes nothing, so those cells rest on the refusal category and the absent twin.
`images.publish` rejection cells take only `lane_target`'s observed context. Their snapshot is
the `platform` project's `project_state`, the cell's published image's `image_catalog` rows, and
the count of jobs with that image's dedup key.

| Boundary | Grants | Accepted |
|---|---|---|
| authentication | `runs.*`: viewer of T; `images.publish`: operator of a fresh project with no platform role | HTTP 401 for the foreign signature; the issued token is not refused |
| authorization | `runs.*`: viewer of T; `images.publish`: operator of a fresh project with no platform role | `authorization_denied` |
| project-isolation | `runs.*` only: operator of a fresh project | `configuration_error` for `runs.install` and `runs.boot`, `not_found` for `runs.cancel` and `runs.release_external_boot`; in each case identical to the answer for an absent `run_id` (`absent_twin`) |
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
   - The published catalog rows stay registered after their cells, because no tool deletes a
     public image. They are the cells' product, and `demo-down.sh --wipe --yes` clears them.
     The run wipes between the two configurations, so each stack's publish cells start with no
     prior job for their image.
   - The rejection target Run and its Investigation stay in T as history, like the torn-down
     target System.
   - A cell killed mid-run can leave a System, allocation, Run or image build behind. The
     post-run wipe clears them, as for the earlier carriers.
   - A failed lane target or Run target fails every rejection cell of the stack that needs it.
     It is not retried.
   - A background change to T or `platform` during a rejection call fails that cell. No other
     allocator or publisher runs on the stack. The carrier's own publish whose drain timed out
     is the exception: its build keeps running and can fail the stack's later `images.publish`
     cells. They are not retried.
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
| upload without completion | focused-test | `test_spine.py`: `complete=False` uploads and never calls `runs.complete_build` |
| live cells | task-test-not-applicable | they act only against a live stack; proven by the two-configuration lab run and `qualify` |
| runbook | task-test-not-applicable | prose; `just docs-check` |
