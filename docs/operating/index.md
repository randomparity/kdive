# Operating KDIVE

KDIVE's portable core runs as three processes — `server`, `worker`, `reconciler` — plus a
`migrate` one-shot, on top of operator-provided backends (Postgres, an S3-compatible object store,
and an OIDC issuer). Kubernetes adds a dedicated fourth `lifecycle-witness` workload; Compose and
systemd retain the portable core, with Compose using an operator-side lifecycle wrapper. These pages
cover how to install the code, the three deployment shapes, the libvirt providers, and the live
runbooks.

## Install and run modes

| Page | What it covers |
|---|---|
| [Install](install.md) | Install paths, host prerequisites, and the run modes |
| [Docker Compose](../../deploy/compose/README.md) | App tier plus dev backends in one graph |
| [Kubernetes (Helm)](runbooks/kubernetes-deploy.md) | The chart for four long-running workloads and the migrate Job |
| [systemd](systemd.md) | Running the processes as host services |
| [Platform and architecture support](platform-support.md) | Supported arches, accelerators, and per-distro customize-boot tiers |

## Providers

| Page | What it covers |
|---|---|
| [Local libvirt](../../examples/local-libvirt/README.md) | Host preparation, lifecycle setup, guest images, and client connection |
| [Remote libvirt](providers/remote-libvirt.md) | Setup sequence, connection requirements, and CPU expectations |
| [Build lane](external-build-upload.md) | The build lane: build the kernel locally and upload it (no operator-staged source tree or build host) |

## Tenancy

| Page | What it covers |
|---|---|
| [Project onboarding](project-onboarding.md) | Establishing a project's budget and quota in production via the audited admin tools |

## Runbooks

Step-by-step procedures for live runs and operational tasks.

| Runbook | What it covers |
|---|---|
| [Live stack](runbooks/live-stack.md) | Bring up the HTTP live-stack against compose backends |
| [Remote live stack](runbooks/remote-live-stack.md) | Live stack driving a remote libvirt host |
| [Remote libvirt host setup](runbooks/remote-libvirt-host-setup.md) | Preparing a remote libvirt host |
| [Four-method live run](runbooks/four-method-live-run.md) | Exercising all four crash-capture methods |
| [Image lifecycle](runbooks/image-lifecycle.md) | Building, publishing, and pruning base images |
| [kdivectl](runbooks/kdivectl.md) | Operating the admin CLI |
| [Build-use recovery](../guide/reference/ops.md#opsrecover_build_use) | Pin listing and recovery after durable worker termination |
| [Diagnostic verification](runbooks/doctor-exit-criterion.md) | Seeded-fault evidence and the limits of doctor checks |
| [MCP coverage campaign rerun](runbooks/mcp-coverage-campaign-rerun.md) | Re-running the MCP tool coverage sweep |
| [Live testing](runbooks/live-testing.md) | How to run each live test tier (`live_stack`, `live_vm`, `live_vm_tcg`) and its environment contract |

## Investigation

| Guide | What it covers |
|---|---|
| [Race debugging](race-debugging.md) | Observing a kernel value under race load without halting the VM — drgn-live + tracepoints over root SSH |
