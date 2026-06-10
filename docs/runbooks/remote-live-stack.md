# Runbook: remote live-stack end-to-end bring-up

Operator guide for driving the kdive spine against a **genuinely remote** `qemu+tls://`
libvirt/QEMU host the server/worker tier does not share a filesystem with. It mirrors the
[local live-stack runbook](live-stack.md) and runs the same `live_stack` suite
(`tests/integration/test_remote_live_stack.py`), but adds what a remote host needs over the
local one: worker→host TLS, the gdbstub-port ACL, and object-store reachability for the
two-phase vmcore upload. See [ADR-0042](../adr/0042-live-stack-e2e-mcp-http.md) (the operator-run
e2e shape), [ADR-0076](../adr/0076-remote-libvirt-provider-package.md) (the provider package +
portability gate), [ADR-0079](../adr/0079-remote-live-debug-transport.md) (the gdbstub ACL +
in-guest debug), and [ADR-0084](../adr/0084-remote-control-two-phase-vmcore-retrieve.md) (the
two-phase KDUMP capture). The design is in
[the spec](../superpowers/specs/2026-06-09-remote-live-stack-e2e-207.md).

This is **operator-run, not CI**: the suite is `live_stack`-marked and CI deselects it. The
preflight skips cleanly — naming the missing variable — unless every prerequisite below is
present, so `just test-live-stack` is safe to run on any host.

## Prerequisites

- A reachable libvirt/QEMU host exporting `qemu+tls://…/system`, with x509 mutual TLS configured
  on `libvirtd` (server cert signed by a CA the worker trusts; `no_verify` is forbidden).
- An **operator-staged base-OS qcow2 volume** on the remote host's storage pool, carrying:
  qemu-guest-agent (enabled), a kdump-capable base OS, `drgn`, and a matching
  `vmlinux`/debuginfo. Provisioning verifies the volume **exists**, not its contents — these
  image-content obligations are the operator's (ADR-0078/0079).
- The local stack backends up (Postgres + MinIO + mock OIDC) and the host
  `server`/`worker`/`reconciler` running, exactly as in the [local runbook](live-stack.md)
  steps 1–4. The remote variant changes only the **target** of provisioning, not the control
  plane.
- The repo set up (`just setup`).

## 1. Worker → host TLS reachability

The remote provider is **opt-in**: composition registers it only when the host URI is set
(`providers/remote_libvirt/config.py`). The TLS client cert, key, and CA are
**secrets-by-reference** — the worker resolves the refs, materializes them into a private per-op
`pkipath`, points the URI at it, and deletes them on every exit path. Export the provider config:

| var | value | role |
|-----|-------|------|
| `KDIVE_REMOTE_LIBVIRT_URI` | `qemu+tls://host.example/system` | the opt-in gate + control transport |
| `KDIVE_REMOTE_LIBVIRT_CLIENT_CERT_REF` | a `SecretBackend` ref | mutual-TLS client cert |
| `KDIVE_REMOTE_LIBVIRT_CLIENT_KEY_REF` | a `SecretBackend` ref | mutual-TLS client key | <!-- pragma: allowlist secret -->
| `KDIVE_REMOTE_LIBVIRT_CA_CERT_REF` | a `SecretBackend` ref | CA to verify the libvirtd server cert |
| `KDIVE_REMOTE_LIBVIRT_STORAGE_POOL` | e.g. `default` | pool holding the base image + overlays |

Confirm the worker host can actually reach libvirtd over TLS before running the spine:

```bash
virsh -c "$KDIVE_REMOTE_LIBVIRT_URI" list --all
```

A failure here surfaces in the spine as a `transport_failure` at the provision or discovery
phase — fix the URI, the cert chain, or host/CA hostname mismatch first.

## 2. The gdbstub-port ACL

The gdb-MI debug tier connects **directly over TCP** from the worker to the host's QEMU gdbstub
port — `qemu+tls://` does not tunnel it. The gdbstub is unauthenticated and unencrypted, so the
**ACL is the auth**: bind it to the worker pool's source only, and one System's port must be
unreachable by other tenants/guests. Each running System gets a distinct port the provisioning
profile allocates and records in the domain XML; the Connect port reads it back.

| var | value | role |
|-----|-------|------|
| `KDIVE_REMOTE_LIBVIRT_GDB_ADDR` | the ACL'd listen address (e.g. `10.0.0.5`) | **no default**; the security boundary |
| `KDIVE_REMOTE_LIBVIRT_GDB_PORT_MIN` | e.g. `47000` | per-System port range floor |
| `KDIVE_REMOTE_LIBVIRT_GDB_PORT_MAX` | e.g. `47099` | per-System port range ceiling |

`KDIVE_REMOTE_LIBVIRT_GDB_ADDR` has **no default** and provisioning **fails closed** without it,
so the remote preflight requires it — an unset address skips the suite rather than letting it
fail at the provision phase. Restrict the address + port range to the worker pool's source at the
host firewall; this is a security boundary, not a convenience note.

## 3. Object-store reachability for the presigned PUT

The object store is the only bulk artifact channel. The kernel flows **to** the target via a
presigned **GET** the target pulls in-guest; the vmcore flows **back** in two phases (ADR-0084):
on crash, kdump writes the core to the guest's local dump storage; on the **next normal boot**
the in-guest agent uploads it to a presigned **PUT** URL whose lifetime covers the
crash→reboot→upload window. So the **guest** (not just the worker) must reach the object-store
endpoint. No standing object-store credential lives in any guest — every URL is time-boxed and
scoped to a single object — and no host-side agent is deployed.

Ensure the guest's network can reach `KDIVE_S3_ENDPOINT_URL` (the same value the host processes
use, from the local runbook). If the guest cannot reach it, the capture phase fails at the
in-guest upload with an `infrastructure_failure`.

## 4. The base-image volume (a test/runbook input)

`KDIVE_REMOTE_BASE_IMAGE_VOLUME` names the operator-staged qcow2 volume the spine feeds into the
provision profile's `base_image_volume` field. It is a **test/runbook input**, not part of the
`KDIVE_REMOTE_LIBVIRT_*` provider-config surface — it parameterizes the e2e's profile, nothing
else.

```bash
export KDIVE_REMOTE_BASE_IMAGE_VOLUME=kdive-base-fedora.qcow2
```

## 5. Run the suite

```bash
just test-live-stack
```

This runs `pytest -m live_stack`, which now collects both the local
(`test_live_stack.py`) and the remote (`test_remote_live_stack.py`) spines. The remote spine
drives allocate(`remote-libvirt`) → provision(disk-image) → build → install → boot →
attach(gdb-MI direct TCP) → force-crash → two-phase KDUMP capture →
introspect(`from_vmcore`) → release → reconciler teardown → accounting report, each step under a
per-project role token.

Two operational notes:

- **Capture budget.** The capture phase drains a job that waits out a ~300s server-side readiness
  window while the guest reboots out of the kdump capture kernel, then uploads. The spine budgets
  900s for it; if the operator's reboot is slower, raise `_CAPTURE_DEADLINE_S` in the remote test.
- **Completion evidence.** A successful run writes `remote-accounting-report.json` to the artifact
  dir (`KDIVE_ARTIFACT_DIR`, or an out-of-tree temp default) — attach it as the record that the
  remote spine completed end-to-end.

## Non-goals

In-guest drgn-**live** MCP routing is a deferred follow-up (#215). The remote spine's introspect
phase uses the **worker-side** vmcore postmortem (`introspect.from_vmcore`), which fetches the
core from the object store and runs the report on the worker — no live in-guest reachability
needed. The portability gate (`just m2-gate`) and its committed report
(`docs/reports/m2-portability.md`) confirm the remote provider added no provider-specific logic
to core or `mcp/tools/*` beyond the ADR-0076 allowlist.
