# Capture workflows have moved

This path remains because decision records and existing diagnostics cite it. The former
manual four-method procedure used retired tool arguments and no longer describes a runnable
workflow. Use these maintained owners:

- [Control](../../guide/toolsets/control.md): crash methods, watches, and state/evidence limits.
- [Postmortem](../../guide/toolsets/postmortem.md): Run-addressed capture and core analysis.
- [Debug](../../guide/toolsets/debug.md): GDB attachment and live inspection.
- [Remote live-stack capstone](remote-live-stack.md#6-four-method-capture-capstone): the current
  automated four-method proof and its prerequisites.
- [Live testing](live-testing.md): test-tier selection and environment contracts.

## Wire the worker venv (drgn + libguestfs)

The old checkout-venv repair is not the current worker setup. Fixed lifecycle workers run
`/opt/kdive-live-worker-lifecycle/.venv/bin/python`, while checkout commands and direct tests
may use a different interpreter. Follow the
[fixed worker lifecycle contract](../../../deploy/systemd/README.md#fixed-live-worker-lifecycle-contract)
and its host provisioning. A successful import check in `KDIVE_PYTHON` alone does not prove
that the installed worker has drgn/libguestfs. Reprovision the owning environment rather than
copying incompatible native bindings between Python versions.
