# Core reproduce/verify path

Use this workflow to boot a kernel, collect evidence, and compare a candidate fix.
Start with a [connected client](agents/index.md) and the [domain concepts](concepts.md).
Read each tool's contract before calling it; the sequence below supplies the context
that the individual [tool references](reference/index.md) do not.

For a job-producing prerequisite such as provision, install, or boot, keep the job ID and
poll `jobs.wait`; advance after success. Failure or cancellation needs recovery. Observation
jobs are different: run the workload during the watch, as described below.
Use the [async-jobs guide](async-jobs.md) for polling and uncertain responses, and the
[envelope guide](response-envelope.md) to distinguish job IDs from target-object IDs.

## Acquire a suitable System

Choose the guest architecture, rootfs and capture/debug features before provisioning.
The provider, guest image and uploaded kernel must support the chosen capture method;
for kdump, follow the kernel-config and debug-artifact requirements in the
[external-build guide](../operating/external-build-upload.md).
Most experiment steps require contributor. A deliberate `control.force_crash` requires
admin plus the profile opt-in; teardown also requires admin. Check
[safety and RBAC](safety-and-rbac.md) before planning those operations.

| Tool | What to establish before continuing |
|---|---|
| `investigations.open` | Keep the Investigation ID to group the original and candidate-fix Runs. |
| `resources.list` / `resources.describe` | Select compatible capacity and supported guest features. |
| `allocations.request`, then `allocations.wait` | Obtain a grant; a queued request is not usable capacity. Keep its lease current through `allocations.renew`. |
| `systems.provision` | Supply the provisioning profile for that Allocation. Keep `data.system_id`, poll the returned job, then use `systems.get` to confirm the System is ready. |

## Upload, install, and boot

1. Create a bound Run with `runs.create`, supplying the Investigation ID, ready System ID,
   and a `build_profile` for the intended architecture. For a new build, omit `build_ref`.
2. Read `resource://kdive/contracts/external-build` and build the kernel in your own build
   environment. The [artifact recipe and upload flow](../operating/external-build-upload.md)
   define the required bytes and optional debug assets.
3. Call `artifacts.create_run_upload` with the complete artifact manifest. This returns
   upload URLs; it does not transfer your files. Perform the HTTP PUTs using each item's
   `refs.upload_url` and `data.required_headers`, following the returned upload instructions
   and deadlines. Then call `runs.complete_build` to validate and finalize the upload.
4. Call `runs.install` and wait for its job to succeed. Then call `runs.boot` and wait for
   the boot job's result. Inspect `runs.get` for readiness and console evidence. Run status
   `succeeded` describes build completion and does not establish a successful boot.

For compatible build reuse or creating an unbound Run before capacity is ready, follow the
[Run reference](reference/runs.md); those paths have different upload and binding steps.

## Reproduce and inspect

Run the bug's reproducer against the booted guest. If your SSH key is not already authorized,
call `systems.authorize_ssh_key` and wait for its job to succeed. `systems.ssh_info` provides
connection coordinates; follow their host scope so a worker-local endpoint is reached from
the right machine. The SSH workload runs through your client, outside the MCP tool call.

For console-based observation, enqueue `control.watch_for_crash` before starting the
reproducer, then run the workload without waiting for the watch to finish. Poll and read the
verdict afterward; follow the [watch contract](reference/control.md#controlwatch_for_crash)
for its timing limits. Both `fired` and
`not_fired` are successful watch-job outcomes: read the JSON verdict in `refs.result`.
A completed watch does not itself change the System to `crashed`, and a negative verdict
only covers the watched window and signatures. A lost SSH connection alone is not a verdict.

A deliberate `control.force_crash` can test the capture pipeline on a ready System when
authorized. It is not evidence that the bug was reproduced. Supply the owning Run ID when
required by the [control contract](reference/control.md#controlforce_crash); do not use it
to repair bookkeeping after an observed panic.

To analyze a core, first confirm with `systems.get` that the Run's bound System is `crashed`.
If an observed panic has not produced that state, preserve the console evidence and seek
operator triage; `vmcore.fetch` rejects a System that is not `crashed`.

1. Call `vmcore.fetch` with the Run ID and a supported capture method (or a profile that
   supplies one), then wait for its capture job to succeed.
2. The completed job's `refs.result`, also exposed as `runs.get`'s `refs.vmcore`, is the
   captured artifact ID. Call `postmortem.crash` with the Run ID, omitting `commands` for
   first-pass triage. Use `introspect.from_vmcore` with the Run ID for offline inspection.

For live introspection, open `debug.start_session` with `transport="drgn-live"`, then pass
its session ID to `introspect.run`. The default `gdbstub` session does not satisfy that
contract. Finish with `debug.end_session`; see the [introspection reference](reference/introspect.md).

## Verify and finish

Create another Run in the same Investigation for the candidate fix. Use a suitable ready
System or obtain replacement capacity through the documented lifecycle; a crashed or failed
System is not a ready target. Build/upload the candidate kernel, install and boot it, and
repeat the same reproducer and observation conditions. Record what ran and what was observed
with `runs.set(outcome_note=...)`; build or boot success alone does not establish a fix.

Preserve needed evidence before cleanup. Follow the [System](reference/systems.md) and
[allocation](reference/allocations.md) contracts for teardown/release, including the
[failed-System limitations](errors.md#an-incomplete-snapshot-restore). Close the Investigation
with a summary when finished; [closing can schedule artifact cleanup](concepts.md#separate-lifetimes).

## Curated MCP prompts

The server's MCP prompt catalog exposes shorter tool sequences for parts of this workflow:

| Prompt | Purpose |
|---|---|
| `start_investigation` | Orient and acquire a System. |
| `build_boot_debug` | Upload a build, install/boot, and attach live drgn introspection. |
| `triage_panic` | Capture and analyze a core, with deliberate crash induction only when appropriate and authorized. |

These prompts are guidance, not an automatic execution or fix-verification service. They tag
`partial` tool steps with the available maturity reason and reject unavailable `planned` steps
at registration. Availability and maturity do not establish that your guest or permissions
satisfy the tool's preconditions.
