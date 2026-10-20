# Remote crash capture and introspection tool cells on x86_64 (#3114)

Decision records: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
with its 2026-10-13 amendment (capability cells follow the support verdict) and the 2026-10-20
amendment this change adds (handler-refused capture variants), and
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md) (evidence identity).
Builds on the local carrier of #2814 (`capture_cells.py`, `test_capture_tool_cells_live.py`), the
remote provider lane of #3080 and the lane fields of #3116 (`Lane.connect`,
`Lane.installed_kernel`, `domain_defined(system_id, connect)`).

## Problem

The contract routes 180 x86_64 `remote-libvirt` cells of six tools to #3114: `control.force_crash`,
`vmcore.fetch`, `introspect.run`, `introspect.script`, `introspect.from_vmcore` and
`postmortem.crash`. That is 9 tool variants × `default`/`recovery` × `direct`/`gateway` × {functional,
authentication, authorization, project-isolation, validation}: 36 functional and 144 rejection
cells. No remote capture cell is bound.

Two things stand in the way.

1. **Eight functional cells cannot pass.** Remote-libvirt advertises capture methods `kdump`,
   `host_dump`, `gdbstub` and `console` (`providers/remote_libvirt/composition.py`), and
   `_capability_variants` turns each into a `vmcore.fetch` variant. But `vmcore.fetch` accepts only
   its core-producing set `{host_dump, kdump, fadump}` (`_VMCORE_METHODS`,
   `mcp/tools/lifecycle/vmcore/handlers.py`). Any other method gets `configuration_error` with
   `data = {"method": <m>, "reason": "method does not produce a vmcore"}`. It checks this after the
   Run, role, Run-binding and CRASHED-state checks, and creates no job. So
   `tool/remote-libvirt/vmcore.fetch/{console,gdbstub}/functional` claim coverage that no run can
   produce.
2. **The local carrier is local by construction:**
   - `crash_profile` writes only the `local-libvirt` profile section;
   - `options`, and the carrier's rejection `method`, read the variant as `scenario_id.split("/")[2]`.
     A remote scenario id carries the provider (`tool/remote-libvirt/<tool>/<variant>/<kind>`),
     so that index is the tool name there;
   - `capture_target` is cached per stack URL, keeps only the lane keys (no provider host) and
     reads domain definedness from the worker's libvirt;
   - `test_capture_tool_cells_live._cells` selects only local cells and loads the fixture for
     `platform.machine()`.

## Scope

1. **Contract** (`scripts/coverage_campaign/contract.py`). A `vmcore.fetch` variant outside the
   handler's core-producing set is `unsupported`. Its `unsupported_reason` is
   `"vmcore.fetch handler: method does not produce a vmcore"`. The set comes from the handler: the
   handler's constant becomes public `VMCORE_METHODS` (a rename with no behaviour change) and the
   contract imports it. This verdict generates no `capability` cell. The refusal is
   `configuration_error` with `reason` "method does not produce a vmcore", not ADR-0209's
   `capability_unsupported`, so a capability cell would demand an answer the handler never gives.
   The rule follows the handler, which does not depend on provider or architecture. So it also
   turns the 8 ppc64le remote `console`/`gdbstub` cells (owner #2818, unchanged) unsupported. Cell
   identities and owners are unchanged. #3114 keeps 180 cells: 28 functional, 8 unsupported and
   144 rejection.
2. **Unsupported-variant proof** (`capture_cells.refused_method`). An unsupported
   `console`/`gdbstub` cell needs the handler's own preconditions. It provisions with `host_dump`
   options, boots the fixture through `boot` and crashes the System by the operator path
   (`Session.crash`). Then it calls `vmcore.fetch(run_id, method=<variant>)` through the cell's
   exposure as a `contributor`. It requires:
   - `error_category` `configuration_error`;
   - `data.reason` "method does not produce a vmcore" and `data.method` equal to the variant;
   - no job reference;
   - the System still `crashed`;
   - an unchanged `named_jobs` digest for its System and Run.

   The frame proves cleanup.
3. **Provider-aware carrier** (`capture_cells.py`).
   - `variant_of(cell)` is `scenario_id.split("/")[-2]`. `options`, `vmcore_body` and the
     rejection `method` use it.
   - Remote `introspect.run` and `introspect.script` functional cells stop `blocked` on #3124
     before provisioning. #3124 is open and blocked by #3121: remote guests never receive the
     Run's `vmlinux` (`remote_libvirt/lifecycle/install.py` presigns only the kernel), and the
     in-guest drgn cannot resolve symbols. `REMOTE_DEBUGINFO_BLOCKED` names them and loses its
     entries when #3124 closes. Their rejection cells still run: the target's `debug.start_session`
     does not resolve symbols.
   - `crash_profile` writes the profile's own provider section. Remote: `kdump` sets `crashkernel`
     (the arch default) and `destructive_ops`; `host_dump` sets `host_dump: true` and
     `destructive_ops`; `target` sets `destructive_ops`; `live` changes nothing.
     `console`/`gdbstub` provision with `host_dump`.
   - `vmcore_body` with `host_dump` on a lane that has `pool_volume` appends the per-System dump
     volume `kdive-host-dump-<system>.kdump` to `guest.owned` before the capture. A failed capture
     can leave the volume, and the frame's cleanup still proves it absent. After a successful
     capture the volume must already be absent, because the capture's own `finally` reclaims it.
   - The drgn-live session tools prove detach, as locally. A remote drgn-live session runs in the
     guest over the guest agent (`RemoteLibvirtLiveIntrospect`) and holds no provider-host
     resource. The domain's undefinition proves the guest is gone.
4. **Remote rejection target.** `capture_target` is cached per `(stack, provider)`. On the remote
   lane it provisions the representative through `on_lane_system` with `target` options, boots the
   fixture, opens and ends a drgn-live session and tears the System down. Its context keeps the
   lane keys plus the provider-host keys (`host_os`, `host_arch`). The protected state reads domain
   definedness through the lane's `connect`, which `protected_state` takes as a keyword
   defaulting to the worker's libvirt. `debug_cells`, a reader of `protected_state`, therefore
   keeps its local behaviour. A failed preparation is replayed, not retried. The replay carries
   the provider-host keys the target had already observed. The local target is unchanged.
5. **Lane** (`tool_cells.py`). `Lane` gains `pool_volume: Callable[[str], str] | None`. It is
   `None` locally. Remotely it is `pool_volume_path(dest, pool, name)`, which joins the pool's
   target path. That path is read through the observer by `pool_target`, a helper that #3116's
   `remote_pcap_spool` also uses after this change, so the pool read is not duplicated.
6. **Carrier** (`test_capture_tool_cells_live.py`):
   - parametrize the remote cells whose guest architecture is in `REMOTE_LANE_FAMILIES`;
   - load the fixture for the cell's guest architecture;
   - send a remote unsupported cell to `refused_method` and a local one to `unsupported`;
   - prove a `capability` rejection only on the local lane, where it remains.
7. **Bindings.** `[implementations]` binds the 45 `tool/remote-libvirt/<tool>/<variant>/<kind>`
   scenarios to `test_capture_tool_cells_live.py::test_capture_tool_cell`. The ppc64le remote cells
   share these ids but are never parametrized. `tool_cells bindings --remote --kernel-baseline
   longterm` writes their contexts unchanged.
8. **Runbook** section in `docs/operating/runbooks/remote-live-stack.md`, and one operator-granted
   live run of both configurations against a separate x86_64 provider host.

Cells and their deployed roles stay `server`/`worker`/`reconciler`. The capture group declares no
authority role (decision D1-A, 2026-10-13, asserted by `test_capture_owners_follow_the_approved_split`).
So "deployed-role identity" is those roles' identity. Installed-authority scenarios stay #3185/#3186's.

Considered and rejected:

- *Keep `console`/`gdbstub` functional and prove the refusal in the functional cell*: a
  functional cell would then count as coverage, which the issue forbids.
- *Emit `capability` cells for the handler refusal*: the refusal is not ADR-0209's
  `capability_unsupported`, so `refused_for_capability` would fail every one by construction.
- *Restrict the rule to x86_64 or remote*: the handler refuses `console`/`gdbstub` on every lane,
  so a narrower rule would leave the ppc64le remote cells claiming impossible functional coverage.
- *Stop advertising `console`/`gdbstub` as remote capture methods*: a product change to
  `systems.get` and the descriptor that other tools read, outside this test issue's surface.
- *Check the dump volume by name (`storageVolLookupByName`) rather than by path*: the frame's
  cleanup proves `owned` paths absent through `Lane.absent`. A name check would need its own
  post-release step outside the frame.
- *A frame-wide "no volume names the System" sweep in `remote_cleanup`*: it strengthens every remote
  carrier's cleanup (#3080, #3116, #3120) beyond this issue's surface. The named dump volume covers
  the one provider-host artifact these tools add.

## Success

- `test_capture_tool_cell` parametrizes the 180 x86_64 remote cells (owner #3114) besides the 164
  local ones.
- On the granted live run, each configuration records its 90 #3114 cells:
  - 18 System cells: 10 functional cells `success`, 4 handler-refusal cells `unsupported`, 4
    `introspect.run`/`introspect.script` cells `blocked` on #3124, or a truthful `fail` or
    `blocked` reported as a finding;
  - 72 rejection cells `rejection`, or a truthful `fail`.
- Every #3114 cell that reached the provider-host probe carries the provider-host identity, and
  every System cell that booted the fixture carries the kernel identity its binding declares.
- For those 90 cells per configuration, `qualify` against the run's bindings reports no
  `matrix-mismatch`, `input-binding-missing` or `missing-result`.
- The unit and contract tests listed under Validation pass under `just test`, and `just lint` is
  clean.

## Failure model

1. **Actors and deployments:**
   - an operator running `live_stack` from a control plane whose stack runs the committed candidate
     (`demo-up.sh`; `recovery` with `KDIVE_WORKER_DEATH_VERIFIER=docker`). It runs against one
     separate, disposable x86_64 `remote-libvirt` provider host that this lane alone uses;
   - CI and `just test`, which run only the unit and contract tests.
2. **Invariants and assets at stake:**
   - no cell counts without its observed effect and its provider-host cleanup. A handler refusal
     is `unsupported`, never `success`;
   - the provider host is left as found. Every System a completing cell creates is torn down, its
     domain undefined, its overlays and dump volume gone, and no new `kdive-*` domain left;
   - the provider destination and pool never enter evidence (ADR-0715);
   - local cells keep their behaviour and their 164-cell count.
3. **Accepted failure classes:**
   - a killed cell can leave a System, allocation, domain or volume on the provider host.
     `demo-down.sh --wipe --yes` plus the runbook's provider check clear them;
   - a failed or blocked `capture_target` fails or blocks every rejection cell of that provider
     lane. It is not retried;
   - a remote product defect found live (kdump capture, console capture, dump-volume leak) is a
     failing cell, reported as a finding and never fixed or waived here. A known open defect
     (#3124) is `blocked`;
   - a cell blocked before the provider-host probe (unset `REMOTE_PROVIDER_SSH`, unstaged image)
     records the control-plane host;
   - torn-down Systems, terminal Runs, stored cores and audit rows remain as history;
   - concurrent allocators on the stack or the provider host are not modelled.
4. **Covered elsewhere:** local cells (#2814); ppc64le and POWER qualification, including the 8
   ppc64le remote unsupported cells' proof (#2818); installed-authority scenarios (#3185/#3186);
   the remote lane fields (#3116).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| handler-refused variants | focused-test | `test_coverage_contract.py`: remote `console`/`gdbstub` `vmcore.fetch` cells are `unsupported` with the handler reason, on both architectures; no `capability` cell for them; #3114 counts 180/28/8/144; local counts unchanged |
| contract follows the handler | focused-test | `test_coverage_contract.py`: the unsupported variants are exactly the advertised methods outside `VMCORE_METHODS` |
| variant parsing | focused-test | `test_capture_cells.py`: `variant_of` reads local and remote ids; `options` maps remote `console`/`gdbstub` to `host_dump` |
| remote crash profile | focused-test | `test_capture_cells.py`: each option edits only the remote section with the listed keys; the input is unchanged |
| refused method | focused-test | `test_capture_cells.py`: `refusal_of` accepts the exact refusal and fails on refs, another reason, category or method |
| dump-volume observer | focused-test | `test_capture_cells.py`: a remote `host_dump` capture owns the volume path, even when the capture fails, and fails if the volume is present; a local lane owns nothing |
| remote target | focused-test | `test_capture_cells.py`: one preparation per stack and provider; host keys kept remotely, including on a replayed failure |
| pool volume path | focused-test | `test_tool_cells.py`: the pool target joined with the name; no target path fails; local lane has none; the pcap namer still passes over the shared read |
| blocked remote introspection | focused-test | `test_capture_cells.py`: the 8 remote `introspect.run`/`script` functional cells stop `blocked` naming #3124; local cells unchanged |
| protected state through the target | focused-test | `test_capture_cells.py`: `snapshot_of` reads the target's `connect` |
| bindings | focused-test | `test_coverage_contract.py`: the 180 remote cells bind `test_capture_tool_cell` |
| carrier selection and dispatch | task-test-not-applicable | a `live_stack` module that runs only against a stack; the live run and `qualify`'s `missing-result` prove it |
| live cells | task-test-not-applicable | act only against a live stack and provider host; proven by the lab run and `qualify` |
| runbook | task-test-not-applicable | prose with no executable consumer; `just docs-check` |
