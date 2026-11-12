# Allocation stranded by an authority-routed fixed-worker System

This runbook releases an Allocation held by a System that the fixed worker provisioned on its own
libvirt daemon, but whose Run was later routed to the external-boot authority. The authority
cannot find the domain, so the activation stays `preparing`, and nothing ends it. Release and
lease expiry both refuse the Allocation, and `systems.teardown` routes the System to the authority,
which never held it. The recovery is `ops.recover_stranded_activation`
([ADR-0816](../../adr/0816-stranded-activation-settlement.md)). The tool proves where the domain is,
ends the activation, and then lets the worker tear the System down.

## When it applies

All of these hold:

- `allocations.release` or `runs.boot` is denied with `data.reason` `external_boot_restricted`
  and `data.activation_state` `preparing`. The denial's `data.activation_id` is the activation to
  recover, and its next actions name `ops.recover_stranded_activation` for a `platform_admin`.
- The System is not authority-owned: the fixed worker provisioned it. The usual cause is an
  `authority_instance` added to a Resource after the worker provisioned Systems on it
  ([ADR-0752](../../adr/0752-admit-authority-run-routing-by-resource-route.md) Consequences), or a
  `relocated_authority_instance` fixture whose relocation never ran.
- The System's provider is local-libvirt. Any other provider refuses with
  `stranded_observation_unsupported`, and no tool recovers it.
- The System is neither `tearing_down` nor `torn_down`, and no authority-marked job is queued or
  running for it. Do not run `systems.teardown` or `ops.force_teardown` before the settlement:
  either one puts the System in a state that settlement refuses.

An authority-owned System that failed to provision is a different shape, with a different
recovery: see [Exhausted authority System provision](exhausted-authority-system-provision.md).

**Upgrade order.** Use the tool only after the database migration, the authority host, every
worker, the reconciler and then every server run a build that contains it. An older server routes a
settled System's teardown to the authority, and an older reconciler expires the Allocation while
the worker's domain still runs.

## Recover

Each step needs `platform_admin` and a reason of 1 to 1024 bytes. Every observe request and every
refusal is written to the platform audit log, and a settlement writes its own row.

1. **Observe.** Run `ops.recover_stranded_activation` with `activation_id=<activation-id>`,
   `step=observe` and a `reason`. From the CLI:

   ```sh
   kdivectl ops recover-stranded-activation --activation-id <activation-id> \
     --step observe --reason "<why>" --yes
   ```

   The CLI asks for typed confirmation of this destructive call; `--yes` skips the prompt for
   non-interactive use.

   The response is `observation_requested`. Its `data.job_id` is a platform-internal job. Do not
   pass it to `jobs.wait`, which answers `not_found` for this kind. In the job, the fixed worker
   confirms that its daemon holds the System's domain. The authority then confirms that its own
   daemon holds no such domain and that its store holds none of the activation's objects.
2. **Settle.** Run the same call with `step=settle`. A pending observation returns
   `observation_pending` with the job id. Run `settle` again after a few seconds. On success the
   status is `settled` and `data.system_id` names the System. The activation is now `abandoned`
   with cleanup complete, and its reservation is released.
3. **Tear down on the worker.** Run `ops.force_teardown` for that System (`kdivectl ops
   force-teardown <system-id> --reason "<why>" --force`). A settled System's teardown runs on the
   worker that holds the domain, not on the authority. Wait for the job with `jobs.wait` until
   `systems.get` reports `torn_down`. Run no `runs.boot` on the System before this step. A new
   activation is unsettled, so every fence returns and the recovery starts over.
4. **Release.** Run `allocations.release` (project `contributor`) or `ops.force_release`
   (`platform_admin`). Release refuses with `external_boot_system_teardown_required` until the
   System is `torn_down`.

## Confirm the domain and the Allocation are gone

- `allocations.get` reports the Allocation `released`, and `systems.get` reports the System
  `torn_down`.
- On the worker host, the worker's daemon (the Resource's `host_uri`) has no `kdive-<system-id>`
  domain: `virsh -c '<host_uri>' list --all`.
- On the authority host, the authority account's daemon has no domain for the System:
  `sudo -n -u kdive-provider-authority virsh -c
  'qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock' list --all`.

## Refusals

A refusal returns `status: error`, names the reason in `data.reason`, changes nothing and is
audited. Evidence stands for one hour. A later refusal does not withdraw it, so after any refusal
run `observe` again rather than `settle` on the older evidence.

| `data.reason` | Step | Meaning and action |
|---|---|---|
| `not_found` | either | No such activation or System. Check the id from the denial. |
| `authority_owned` | either | The System has an authority ownership row, so the authority owns it. This tool does not apply. |
| `not_preparing` | observe, settle | The activation has left `preparing`. Read it with `runs.get`. A settled replay returns `settled` again. |
| `system_ended` | settle | The System is `tearing_down` or `torn_down`, so a teardown ran before settlement. See [Stuck `tearing_down` System](stuck-tearing-down-system.md). |
| `job_live` | settle | An authority-marked job for the System is queued or running. Wait for it to end, then `observe` again. |
| `observation_missing` | settle | No observation was requested. Run `observe` first. |
| `observation_pending` | settle | The observation job is still queued or running. Run `settle` again. |
| `worker_domain_absent` | settle | The worker's daemon holds no domain named for the System, so this is not the worker-owned shape. Check whether a relocation moved it to the authority. |
| `authority_domain_present` | settle | The authority's daemon holds the System's domain: this is a relocated System, not a stranded one. End that domain through the authority before observing again. |
| `authority_objects_present` | settle | The authority's store still holds objects of the activation. The message names the recovery reference and each object. Nothing in kdive removes them yet (#3587), so remove them on the authority host, then `observe` again. |
| `stranded_observation_unsupported` | settle | The System's provider, or this worker's runtime, has no stranded observation. Only local-libvirt carries one. |
| `authority_route_missing`, `authority_route_mismatch` | settle | The worker has no authority route for the System's Resource, or its route names another instance. Fix the worker's inventory, restart the worker, then `observe` again. |
| `worker_evidence_superseded` | settle | The database refused the worker's record: its job, attempt or worker incarnation no longer matches. Run `observe` again. |
| `authority_evidence_missing`, `worker_evidence_missing` | settle | An owner's record is absent. The observation did not finish: `observe` again. |
| `evidence_stale` | settle | A record is over an hour old. Run `observe` again. |
| `authority_instance_mismatch` | settle | The authority that recorded is not the activation's route. Check the Resource's `authority_instance`. |
| `authority_journal_moved` | settle | The authority's journal for the System moved after it observed. Wait for its lane to finish, then `observe` again. |
| `objects_unproven` | settle | The authority's record does not cover every object the activation names. Run `observe` again. A repeat means the store is not proven clear. |
| `reservation_missing` | settle | The activation has no reservation to release. This is not the stranded shape. Stop and report it. |
| any other reason | settle | The observation job failed on a reason the authority or its transport returned, for example while the authority's lane for the System was busy. Read `journalctl -u kdive-external-boot-authority.service` on the authority host, then `observe` again. |

A configuration error, such as a malformed id, a missing reason or an unknown `step`, is not
audited.

## Limits

- **No manual state writes.** Do not write `external_boot_activations`, `systems` or
  `allocations` directly. A direct write skips the evidence that keeps an Allocation from being
  released while a domain still runs on either daemon.
- **Remote-libvirt.** A stranded remote-libvirt System stays stranded
  (`stranded_observation_unsupported`).
