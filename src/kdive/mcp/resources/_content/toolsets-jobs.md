# jobs toolset

Long-running operations return durable job handles. Use the job tools to observe or stop that work
instead of assuming a queued operation has completed. Read each tool's schema for filters, wait
limits, and terminal result fields.

- `jobs.wait` waits for a job ID and returns its current or terminal outcome. Reissue a bounded
  wait while the job remains queued or running; a transport retry does not create a second job.
- `jobs.list` finds jobs for a project or object and follows the response cursor when more results
  are available. Use it to recover a handle after a client restart or to inspect related work.
- `jobs.cancel` requests cancellation for a job that should no longer run. A cancellation request
  is not proof that provider-side cleanup has finished, so inspect the eventual terminal result.

Most toolsets return the job handle in `object_id` or a response reference. This guide explains the
workflow; the individual job schemas own exact parameters and result shape.
