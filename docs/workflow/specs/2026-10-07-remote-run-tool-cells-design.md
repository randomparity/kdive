# The x86_64 remote run and image tool cells (#3120)

Decision records: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
and its 2026-10-02 amendment; evidence identity per
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md); pinned kernel
fixtures per [ADR-0693](../../adr/0693-pinned-external-kernel-fixtures.md). Builds on the remote
provider lane of #3080 ([spec](2026-10-06-remote-system-tool-cells-design.md)) and the run and
image carrier of #3119 ([spec](2026-10-07-run-tool-cells-design.md)).

## Problem

The contract routes the x86_64 `remote-libvirt` cells of `runs.install`, `runs.boot`,
`runs.cancel`, `runs.release_external_boot` and `images.publish` to #3120: 96 cells, 20
functional and 76 rejection (owner routing in `scripts/coverage_campaign/contract.py`,
per-tool flags of operator option B in `obligations.toml`). No carrier binds them. The #3119
carrier parametrizes only local-libvirt cells and hard-codes `local-libvirt` as the provider of
its unbound target Run and of its `images.publish` calls.

Two of the five tools have no remote path to prove today:

- `images.publish` for `remote-libvirt` enqueues an `IMAGE_BUILD` job whose handler resolves a
  catalog build for `local-libvirt` only and answers every other provider with
  `configuration_error` "provider-specific image build request is not implemented"
  (`src/kdive/jobs/handlers/image_build.py`, `_resolve_catalog_build`). Remote base images are
  built and staged by the operator (`deploy/ansible/playbooks/image.yml`).
- `runs.release_external_boot` releases an external boot, which needs a remote provider
  authority: the `provider_authority_host` role on the provider host and the full authority
  tuple on the `[[remote_libvirt]]` instance (`src/kdive/providers/remote_libvirt/config.py`).
  No runbook provisions one. The role needs a reviewed source checkout with Python 3.14 and `uv`
  on the provider, a database DSN, a server PKI and worker client CA, and object-store
  credentials. An authority on the instance also routes `runs.install` and `runs.boot` of every
  remote cell through the external-boot path (`steps.py`, `server_authority_instance`), which
  the in-guest install proofs below do not observe.

## Scope

1. **Carrier** `tests/integration/test_run_tool_cells_live.py::test_run_tool_cell` also
   parametrizes the five tools' remote-libvirt cells whose guest architecture has a remote lane
   family (`REMOTE_LANE_FAMILIES`, x86_64 today), as the System carrier does. Every provider name
   the carrier sends or reads comes from the cell (`run.cell.provider`):
   - `runs.create` of the unbound target Run names `target_kind=<provider>`;
   - the target Run is memoized per stack and provider, as `lane_target` is;
   - `images.publish` arguments, its dedup-key job count and its catalog-row snapshot take the
     provider.
2. **Remote functional cells** run on the #3080 lane (`on_lane_system` → `on_remote_system`,
   `fedora-kdive-remote-base-43` on the provider host), with the #3119 bodies:
   - `runs.install`, `runs.boot`: `deep_body` with the tool under test through the exposure.
     Remotely the install is in-guest, so the installed kernel is observed with #2810's
     `guest_boot_kernel` (the digest of the guest's `/boot/vmlinuz-<release>`) instead of the
     local `<os><kernel>` file. Every other proof is #3119's: upload digests and build ID,
     reconnect with a new `boot_id`, running release and GNU build ID, the uploaded `loop` module.
   - `runs.cancel`: #3119's cancel body unchanged; the domain XML comes from the provider host
     through `guest.lane.xml`. The remote domain boots its disk image, so its `<os>` kernel and
     cmdline are absent before and after; the unchanged `boot_id` and the System state carry the
     proof.
   - `runs.release_external_boot`: `blocked` before any stack mutation, naming the missing remote
     provider authority, the absent provisioning runbook and the install/boot rerouting above.
   - `images.publish`: `blocked` before any stack mutation, naming the handler's local-only
     catalog build. The lab run calls `images.publish` for `remote-libvirt` once outside the
     carrier and records the job's terminal state and category in the runbook.

   Each runs functional cell proves #3080's remote cleanup: the domain undefined, its volumes
   absent and no new `kdive-*` domain on the provider host, capacity returned.
3. **Remote rejection cells**: the `runs.*` target is one unbound `remote-libvirt` Run in the
   remote lane target's project, created after `lane_target` provisioned and reclaimed the
   remote target System. The `images.publish` target is the remote lane image's name
   (`fedora-kdive-remote-base-43`) with provider `remote-libvirt`: its snapshot is the `platform`
   project, that name's remote catalog rows and its dedup-key job count, so a leaked publish
   would show. The grants and accepted answers are #3119's table unchanged.
4. **Contract**: `[implementations]` binds the 24 `tool/remote-libvirt/<tool>/default/<kind>`
   scenarios to the run carrier node. Owners, flags and `contract.py` are unchanged.
5. **Bindings**: none new. `tool_cells bindings --remote --kernel-baseline longterm` already
   gives each remote cell the provider host, the remote base volume's digest and the declared
   kernel fields.
6. A runbook section in `docs/operating/runbooks/remote-live-stack.md` and one live run of both
   configurations against a separate x86_64 provider host.

No product source, ADR or migration change.

## Failure model

1. **Actors and deployments:**
   - an operator running the live tier from a control plane with the stack at the candidate,
     against one separate disposable x86_64 `remote-libvirt` provider host used by this lane
     alone, one wiped stack per configuration (`recovery` with
     `KDIVE_WORKER_DEATH_VERIFIER=docker`), no provider authority installed;
   - CI, which runs only the unit and contract tests.
2. **Invariants and assets at stake:**
   - honest per-cell outcomes: the eight blocked functional cells are never covered;
   - the provider host left as found: no new `kdive-*` domain, the owned volumes gone, capacity
     returned, proven per runs functional cell and for the remote lane target;
   - the provider destination never enters evidence (ADR-0715).
3. **Accepted failure classes:**
   - a killed cell can leave a System, allocation, Run, domain or overlay on the provider;
     `demo-down.sh --wipe --yes` plus the runbook's provider check clear them;
   - the target Runs, their Investigations and the lab's one remote `images.publish` job remain
     as history until the wipe;
   - a failed or blocked remote lane target or target Run fails or blocks every remote rejection
     cell of that stack that needs it; it is not retried;
   - a cell blocked before the provider host is observed records the control-plane host, so
     `qualify` adds context-mismatch reasons, as for #2810 and #3080;
   - the remote `images.publish` and `runs.release_external_boot` functional cells stay blocked
     until the follow-ups below land; a change to the handler or an installed authority does not
     turn them into evidence by itself;
   - concurrent allocators on the stack or the provider host are not modelled.
4. **Covered elsewhere:** local cells (#3062, #3119), remote System cells and the frame (#3080),
   ppc64le (#2818), capability boundary (#2814), remote host defects (#3081, #3082, #3083,
   #3087, #3093, #3101). Follow-up candidates (not filed): a remote `images.publish` catalog
   build, and a provisioned remote authority with an authority-lane frame for the release cells.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| carrier bindings | focused-test | `test_coverage_contract.py`: the 24 remote run/image scenarios bind the run carrier node; remote run/image cells 192, owners unchanged |
| remote parametrization | focused-test | `pytest --collect-only -m live_stack -k remote-libvirt tests/integration/test_run_tool_cells_live.py` collects 96 parameters on an x86_64 host |
| provider-qualified target Run, publish args and snapshot | task-test-not-applicable | the carrier acts only against a live stack and provider host; the remote rejection cells and their `qualify` result in the lab run prove it |
| remote installed-kernel observer | task-test-not-applicable | reuses #2810's `guest_boot_kernel` unchanged; proven by the remote install/boot cells |
| blocked release and publish cells | task-test-not-applicable | a `ScenarioStop` before any stack call; observed as `blocked` records in the lab run |
| runbook | task-test-not-applicable | prose; `just docs-check` |
