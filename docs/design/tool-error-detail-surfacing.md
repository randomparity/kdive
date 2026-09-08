# Tool-error detail surfacing — historical audit (#450, ADR-0123)

> **Historical record.** This preserves the original decision or dated evidence.
> Commands, status, paths and capabilities below describe that context; they are not
> current operating guidance. Start with the [current documentation](../README.md).
> This bounded historical audit is not a current security assessment.

- **Issue:** [#450](https://github.com/randomparity/kdive/issues/450) (work item B, error detail)
- **Epic:** [#449](https://github.com/randomparity/kdive/issues/449)
- **ADR:** [`0123`](../adr/0123-tool-error-detail-surfacing.md)

### No-leak audit (bounded)

The ADR requires a one-time audit of diagnostic-category raise sites for secret/path/hostname
interpolation, bounded to categories that reach `detail`. Findings:

- The secret-bearing `CONFIGURATION_ERROR` messages — ssh credential-ref resolution
  (`providers/shared/build_host/ssh_transport.py:118`), remote-libvirt TLS secret-ref resolution
  (`providers/remote_libvirt/transport.py:114`), object-store `presign_get` key
  (`store/objectstore.py:320`) — are all raised in the **provider/worker plane**. They reach the
  wire through the async worker's `failure_context` path (`jobs/worker.py:255`, redacted by
  `SecretRegistry`) and `ToolResponse.from_job`, **not** through `failure_from_error`/`detail`.
  They are therefore out of the `detail` audit scope; the worker keeps its own redaction.
- The synchronous diagnostic-category raise sites that *do* reach `failure_from_error` — chiefly
  `ProvisioningProfile.parse()` (`"invalid provisioning profile"`), admission sizing/quota, and
  the `_common`/`_runtime_resolution` config errors — carry author-controlled messages with no
  secret/path/hostname/object-key interpolation. No raise-site message edits are required for
  the `detail` egress in this change.
