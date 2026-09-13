# investigations toolset

An Investigation is the durable record that ties an experiment's allocations, Systems, and Runs
together. Create it before provisioning work, and use it as the place to preserve the question and
the resources that belong to it. Read each tool's schema for argument shapes and returned fields.

## Start and inspect

- `investigations.open` creates an Investigation for a project and returns its ID. Record the
  hypothesis or reproducer in the creation request so later readers can understand the work.
- `investigations.get` reads one Investigation, including its links and current metadata.
- `investigations.list` finds the project's open or historical investigations; follow its cursor
  when the response says more items are available.
- `investigations.set` updates the Investigation's recorded purpose or metadata without changing
  the resources already linked to it.

## Associate work

- `investigations.link` associates an existing Allocation, System, or Run with the Investigation.
  Link work once it is part of the same experiment rather than inferring membership from names.
- `investigations.unlink` removes an incorrect association; it does not delete or tear down the
  linked object.
- `investigations.complete_rootfs_upload` finalizes an Investigation-scoped rootfs upload after
  the signed upload completes. Use its checksum only with a System profile bound to this
  Investigation.
- `investigations.close` records the Investigation's conclusion when the work is finished. Close
  the record only after any evidence or resources that need it have been linked.

Use `allocations`, `systems`, and `runs` for the operations themselves. This guide explains when
to use each investigation tool; the tool schema owns parameters and response details.
