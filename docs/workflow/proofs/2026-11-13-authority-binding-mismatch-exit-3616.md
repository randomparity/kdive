# Authority binding-mismatch exit

Date: 2026-11-13, 15:30-16:20 UTC. Tracks #3616. An authority-owned System whose recorded
`authority_instance` matched no installed authority was torn down, and its Allocation released,
without rolling the lab host back. The authority was installed under the recorded instance with a
server certificate issued for that instance, the stack was restarted, and the System's pending
teardown job then succeeded.

## Candidate and topology

- Every deployed role ran `55bdf8fcad0ba827cebbb93eff6a4a85bf1e5e21` (main, which includes the
  #3617 preactivation teardown recycle). The server, reconciler and worker `/readyz` reported that
  commit, and `/opt/kdive-provider-authority/revision` held it.
- Lane `sys-L`: Ubuntu 26.04 x86_64 lab guest, started from a clean snapshot. Backends and schema,
  then `installed-local-authority.yml`, then `stack-services.sh` with one fixed worker, as in
  [Live authority lab](../../operating/runbooks/live-testing.md#live-authority-lab).
- Installed authority instance `<original-instance>`. The `systems.toml` entry for the same
  resource named `<recorded-instance>`, as in live cell C2 of #3584.
- Root: the staged `fedora-kdive-ready-44` image, SHA-256
  `9e8e99fcb5834d67fdd09da89bf5c4d9ece3c5554ad137c26ba422d17b2d4d3b`.

## Arms that ran

| Arm | Action | Result |
|---|---|---|
| A1 | `allocations.request`, then `systems.provision` with the mismatched inventory | System `provisioning`; ownership row records `<recorded-instance>`. Worker journal: `marked authority System job <provision-job> failed without a terminal receipt: authority: binding-mismatch`. |
| A2 | `systems.teardown` | Teardown job queued, then `... <teardown-job> failed without a terminal receipt: authority: binding-mismatch`. |
| A3 | `allocations.release` | `conflict`, reason `authority_system_preactivation_mutation_fenced`. |
| B1 | Copy of the lane PKI directory; `authority-pki.sh leaf --name server --usage server --days 30 --key existing --authority-instance <recorded-instance>` | New server certificate whose SAN is the recorded instance's `authority_server_name`. |
| B2 | `installed-local-authority.yml` with the lane vars changed to the recorded instance, a manifest copy naming it, and the new server certificate and key | Play recap `failed=0`. The running authority exited with `authority System manifest changed` when the manifest was replaced, and systemd restarted it. Installed authority and worker route files name `<recorded-instance>`; the authority unit is active. |
| B3 | Provision job retry before any stack restart | Still `authority: binding-mismatch`: the worker started before B2 kept the old instance. |
| B4 | Stack environment `KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE` and `..._STORE_IDENTITY` set to recorded-instance values; `stack-down.sh`, then `stack-services.sh` | All three `/readyz` ready at the same commit; the new worker process environment holds `KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE=<recorded-instance>`. |
| B5 | The pending teardown job's next attempt (`jobs.wait`) | `succeeded` at attempt 2 of 3, `result_ref` `sha256:291eb5cbaa895ef5b0dac8836c8089443ec5910d6af95ff9ea96ec8867d8fb4b`. System `torn_down`; ownership `torn-down`, journal phase `terminal`; one authority attempt, `preactivation-teardown`, `terminal`, disposition `preactivation-absent`, consumed. No domain on either libvirt daemon. |
| B6 | Allocation | `audit_log`: `systems.teardown` `provisioning->torn_down`, then `system:reconciler` `allocations.release` `active->releasing` and `releasing->released`. The operator's `allocations.release` then answered `released`. |
| B7 | Provision job | Attempt 3 ended `authority System: attempt-superseded`; after it lapsed the reconciler set the job `canceled` with the same `result_ref` as B5. |
| B8 | `installed-local-authority.yml` with the original vars file; original stack environment; `stack-down.sh`, then `stack-services.sh` | Play `failed=0`; authority active under `<original-instance>`; the new worker holds `<original-instance>`. |

The teardown in B5 was still inside its attempt budget, so the run did not exercise the #3617
restart of an exhausted teardown. A teardown that has spent its attempts is restarted by running
`systems.teardown` again after B4.

## Findings

- `renew-local-authority-tls.yml` was not tried. It installs credentials only and leaves the
  installed authority configuration, including its instance, unchanged, then restarts the
  authority; a certificate for the recorded instance under a configuration naming the original
  one is the start-up mismatch #3584 hit. `installed-local-authority.yml` changes the instance,
  the manifest and the credentials in one run, which is the step the exit needs.
- The fixed worker reads its authority route only at start, so the stack restart in B4 is
  required. Whether the stack environment change in B4 was also required was not tested.
- After B8 a fresh provision failed with `authority: provider-conflict` and
  `local authority root source is not installed`, because the original vars file's manifest did
  not list the staged image used here. That is the existing root-source shape in the runbook, not
  part of this exit; the runbook's step 6 now says the original manifest must list every base in
  use. The System it left was not recovered before the rollback.

## Lab state afterwards

The lab guest was rolled back to its pre-run snapshot and restarted, with no kdive checkout
present. The run's PKI copy, including the copied CA key, was shredded, and the candidate bundle
was deleted.
