# postmortem toolset

Use `vmcore.fetch` to capture a core, then `postmortem.crash` to analyze it. Both take a
**Run ID** and require contributor access. The crash state belongs to the Run's bound
System; there is no requirement for a Run state named "crashed". Read each tool's schema
for parameters and returned fields.

## Capture the core

1. Check the bound System with `systems.get`: `vmcore.fetch` requires **CRASHED**. A console
   signature or a successful watch job does not establish that state. If state and evidence
   disagree, preserve the console and follow the returned recovery guidance; do not force
   another crash just to change state. See resource://kdive/docs/guide/toolsets/control.md.
2. Choose a provider-supported core method: `kdump`, `fadump`, or `host_dump`. Omitting
   `method` uses the System profile's choice only if it resolves to a supported core method;
   otherwise supply one explicitly. Console and gdbstub methods do not produce a vmcore.
   Kdump/fadump need prepared guest crash-capture support. Admission rejects known-negative
   kernel/rootfs evidence, but an unverified or absent signal can pass and does not prove
   capture readiness. External-boot admission also checks the owning Run and activation state.
3. Call `vmcore.fetch` and poll its job with `jobs.wait` until terminal. Require success
   before analysis. Repeating the same Run/method reuses its capture job, including a terminal
   job; a new idempotency key does not force recapture. Inspect the existing job's failure
   guidance instead of repeatedly submitting it or changing methods to bypass the failure.

A fresh capture publishes a **redacted artifact ID** in the completed job's `refs.result`;
`runs.get` also exposes it as `refs.vmcore` for non-failed Runs. For a failed Run, use the
completed capture job's reference. These are artifact IDs, not the Run ID passed to analysis
tools. Read this redacted log evidence with `artifacts.get`: local-libvirt extracts dmesg
text, not a sanitized binary core. A replay can lack the reference if the redacted sibling
was removed while the raw core remains; a missing redacted reference alone does not establish
that the raw capture is absent. Analysis tools can resolve the raw core directly by Run ID.

For external analysis, `artifacts.fetch_raw(run_id, asset="vmcore")` returns a presigned URL
to the sensitive raw core and requires contributor access. It does not return inline bytes.
The analysis tools resolve the raw core from the Run themselves. See
resource://kdive/docs/guide/toolsets/artifacts.md for artifact reads and downloads.

## Analyze the core

`postmortem.crash` needs the Run's captured core, a recorded build ID, and recorded matching
`vmlinux` debug information. The provider checks the core's build ID against the Run's build
before invoking crash(8). Analysis executes in the MCP server process, whose environment
needs the crash utility and provider analysis dependencies. Installing tools only in the
guest or worker does not supply these server dependencies.

Omit `commands` to run the standard `log`, `bt` batch. This tool returns its report directly;
it does not return a job to poll. Inspect `data.transcript` and `data.truncated`. The redacted
transcript can contain per-command errors even when the tool succeeds. Use a smaller,
targeted command batch if output is truncated. Custom commands must pass the allowlist and
shell/control-character checks described by the tool schema.

If required inputs are missing, inspect `data.reason`: `no_vmcore`, `no_debuginfo`, or
`no_build` identifies the gap. For a Run declared as an early-boot `console_crash` with no
core, `postmortem.crash` returns `expected_console_crash` and directs you to console evidence
through `runs.get`. It does not promise that every panic produces a dump.

For drgn analysis of the same captured core, use `introspect.from_vmcore` with the **Run ID**;
that path requires viewer access. See resource://kdive/docs/guide/toolsets/introspect.md.
