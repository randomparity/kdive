# Driving a kdive investigation

Start here when driving KDIVE through MCP. This index maps an investigation to tools and
served guides. Read a tool's current schema for its parameters and return fields.

## Reaching tools

By default, agent clients see a small core catalog. Use `tools.search` to discover other
capabilities, then request the chosen tool's schema with `detail="full"` and a small `limit`.
When you know the names, `tools.search(names=["runs.install"])` returns their full descriptions
and input schemas directly (1–10 names per call). Call a tool through
`tools.invoke(name, arguments)`, or directly when your client exposes it.

The verified operator CLI receives the direct catalog; disabling `KDIVE_MCP_TOOL_GATEWAY`
also restores the direct catalog for agents. `tools.search` and `tools.invoke` remain
available in either mode. Discovery is filtered by your roles, and both invocation paths
enforce the same authorization and state checks. A listed tool is not permission to use it
on every project or object.

The full catalog contains ~130 tools; your visible subset depends on your roles and client.
The server negotiates MCP protocol revision 2025-11-25; clients offering an older supported
revision negotiate that revision.

## The typical session

These stages describe the external-build investigation path. Debugging and crash capture
are optional; select them for the question you are investigating.

1. **Orient and discover** — `session.whoami` shows your projects and roles.
   Use `resources.list`, `resources.availability`, and `shapes.list` to find capacity,
   then `accounting.estimate` to estimate a shape and lease window in KCU.
   Use `investigations.open` to group related Runs.
2. **Acquire capacity** — `allocations.request`, then `allocations.wait` until granted.
   Inspect the returned state before provisioning; a wait response need not be a grant.
3. **Provision a system** — `images.describe` checks the base image's capabilities; see
   resource://kdive/docs/guide/toolsets/images.md and the checklist below. Call
   `systems.provision` with the profile inline, poll its job with `jobs.wait`, and require
   success. Use the returned `data.system_id` with `systems.get` to confirm readiness.
4. **Build and upload** — `runs.create` starts an external-build Run. Read
   resource://kdive/contracts/external-build and
   resource://kdive/docs/operating/external-build-upload.md before building. Submit the
   **complete artifact manifest in one** `artifacts.create_run_upload` call: another call
   replaces that manifest. PUT every object using its returned URL and required headers,
   then call `runs.complete_build` to validate the upload. This completion call is synchronous;
   a succeeded Run means the build is complete, not that it has booted. For target-specific
   packaging, read resource://kdive/docs/guide/kernel-build-per-arch.md.
5. **Install and boot** — `runs.install`, wait for its job to succeed, then `runs.boot`
   and wait for that job to succeed. Confirm the Run's boot result before using the guest.
6. **Reproduce in the guest** — `systems.authorize_ssh_key`, then `jobs.wait` until its
   job succeeds. Get connection details from `systems.ssh_info`; a `worker_loopback`
   endpoint needs access through the worker host. Compile in-guest or cross-compile and
   `scp` your reproducer, then run it over SSH. See the capture loop below.
7. **Observe evidence** — `runs.get` exposes `refs.latest_console`; request
   `include_console_artifacts=true` for `data.console_artifacts`. Use `artifacts.get`
   to read evidence and `artifacts.list` to find other System artifacts.
8. **Debug live** — `debug.start_session` opens a debugging session. For non-halting
   introspection, explicitly select `transport="drgn-live"`, then pass that session to
   `introspect.run` or `introspect.script`. End it with `debug.end_session` when done.
   Read the debug and introspect guides for prerequisites.
9. **Capture and triage** — `control.force_crash` is an optional, deliberate capture test,
   requiring the admin role, profile opt-in, and a READY System. It is not evidence that
   your reproducer triggered a bug. For a crashed guest, confirm the System is CRASHED
   before `vmcore.fetch`; wait for capture success, then use `postmortem.crash` with the
   Run ID. Do not force another crash to repair state or replace uncaptured evidence.
   See the control and postmortem guides.
10. **Wind down** — `systems.teardown`, wait for successful cleanup, then
    `allocations.release`, then `investigations.close`. Teardown does not release the
    Allocation. A FAILED System currently cannot take the ordinary teardown transition:
    use the returned recovery guidance and involve the operator to confirm provider cleanup.
    Releasing capacity alone does not prove the guest is gone.

For a returned job handle, poll with `jobs.wait` and check its terminal status before
starting dependent work. Observation jobs are different: run the reproducer while the
watch is active. See resource://kdive/docs/guide/async-jobs.md for polling and recovery.

## Decide before you provision

Choose the image, capacity, and debugging needs before spending the lease on setup:

- **Base image** — inspect `images.describe` and its `capability_signals` for the planned
  boot, kdump, and live-introspection workflow. An `unverified` signal is not proof that a
  capability works. The images guide owns image selection and uploaded-rootfs workflows.
- **Shape and disk** — size CPU, memory, and guest disk for builds, reproducers, and captures
  when requesting capacity. Package installation and traces also consume disk headroom.
- **Kernel config** — enable the debugging options needed by your investigation in the
  kernel you build and upload. Changing that config needs a new build and install/boot cycle;
  it is not a provision-time switch.

### Provisioning for debugging

For local-libvirt, start with `systems.profile_examples` and choose these profile flags
before provisioning:

- `provider.local-libvirt.debug.gdbstub: true` enables the stub for GDB attachment.
- `provider.local-libvirt.debug.preserve_on_crash: true` configures the domain to remain
  stopped after a reported panic for post-panic inspection.

Changing these flags requires `systems.reprovision`. Live drgn uses the session workflow
above and does not require the gdbstub flag. Check the introspect guide for guest-tool and
kernel requirements; an SSH endpoint alone does not establish readiness for introspection.

## Guest access and the reproduce-and-capture loop

After your public-key authorization job succeeds, you can use root SSH into the guest.
Your private key stays with you; KDIVE uses a separate bootstrap key for its own access.
Use the guest package manager for tools supported by your image and kernel. Local-libvirt
has no outbound guest egress by default, so fetching packages requires operator-enabled
egress or a prepared image. See resource://kdive/docs/guide/toolsets/systems.md for access.

A panic can drop SSH, including the reproducer process and any observation over that
connection. Read the persisted serial-console evidence through `runs.get` and the artifact
tools. For a repeat-until-crash loop, start `control.watch_for_crash` before running the
reproducer and poll its job while the workload runs. The watch observes console output
from worker pickup until its window ends; its `refs.result` reports `fired` or `not_fired`.
Neither a successful job nor a dropped SSH connection proves a panic. `not_fired` means no
matching signature was observed in that window: inspect the console and System state.
The watch does not mark the System CRASHED; check state before attempting capture.

A snapshot can help repeat experiments when the provider supports it, but `systems.restore`
requires a READY System. It is not recovery for a CRASHED or FAILED System. See the systems
guide for snapshot and restore requirements.

## Toolset guides

| Toolset | What it is for | Guide |
|---|---|---|
| runs | Build, install, and boot lifecycle of a kernel test run | resource://kdive/docs/guide/toolsets/runs.md |
| artifacts | Fetch run evidence and upload builds | resource://kdive/docs/guide/toolsets/artifacts.md |
| debug | GDB and live-introspection sessions | resource://kdive/docs/guide/toolsets/debug.md |
| systems | Provision, reprovision, snapshot, and reach a system over SSH | resource://kdive/docs/guide/toolsets/systems.md |
| images | Pick a base image and read its capabilities | resource://kdive/docs/guide/toolsets/images.md |
| introspect | Non-halting live or offline drgn introspection | resource://kdive/docs/guide/toolsets/introspect.md |
| control | Watch for crashes, induce a crash, send a SysRq, or drive power | resource://kdive/docs/guide/toolsets/control.md |
| postmortem | Capture a vmcore, then triage or analyze it | resource://kdive/docs/guide/toolsets/postmortem.md |

For tool results, read resource://kdive/docs/guide/response-envelope.md. Clients that list
MCP prompts also have `start_investigation`, `build_boot_debug`, and `triage_panic`.
