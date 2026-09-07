<p align="center">
  <img src="docs/assets/kdive-logo.png" alt="KDIVE" width="260">
</p>

<h1 align="center">Kernel Debug, Inspect, Validate, Explore</h1>

<p align="center">
  An MCP platform for the complete Linux kernel build, boot, debug, and crash-analysis loop.
</p>

<p align="center">
  <a href="LICENSE">
    <img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="Apache-2.0 license">
  </a>
  <a href="docs/guide/index.md">
    <img src="https://img.shields.io/badge/MCP-streamable_HTTP-5b4bdb" alt="MCP over streamable HTTP">
  </a>
  <a href="docs/development/cross-platform.md">
    <img src="https://img.shields.io/badge/hosts-x86__64_%7C_ppc64le-2f855a"
         alt="x86_64 and ppc64le hosts">
  </a>
</p>

KDIVE gives coding agents one durable workflow for kernel development: acquire capacity, provision
a guest, build and install a kernel, boot it, attach debugging tools, trigger and inspect failures,
and retrieve artifacts such as vmcores. The service coordinates the lifecycle across local and
remote libvirt resources while keeping state, access control, accounting, and long-running work
outside the agent session.

## What KDIVE provides

- **The full kernel loop through MCP.** Provision, build, install, boot, debug, introspect, capture,
  retrieve, and clean up through a consistent tool surface.
- **Durable investigations.** Resources, allocations, systems, investigations, runs, and debug
  sessions have explicit lifecycles backed by Postgres; large artifacts live in an S3-compatible
  object store.
- **Async work that survives the request.** Provisioning, builds, installs, and captures run as
  durable jobs. Agents receive a job handle and poll for a terminal result.
- **Local and remote KVM.** Use libvirt on the worker host for the shortest path, or connect to
  operator-managed remote libvirt hosts over TLS.
- **Kernel debugging and crash analysis.** Drive GDB and drgn workflows, collect console evidence,
  force policy-gated crashes, and retrieve vmcores by reference instead of flooding the MCP
  response with logs.
- **Multi-user controls.** OIDC identity, project RBAC, quotas, budgets, audit attribution, secret
  references, mandatory output redaction, and guarded destructive operations are part of the
  service boundary.
- **Failure-aware orchestration.** Workers execute long operations while a reconciler repairs
  drift, expires leases, tears down orphans, and detaches dead debug sessions.

KDIVE currently ships production runtimes for **local-libvirt** and **remote-libvirt**. The
fault-injection provider is an opt-in test facility. Cloud, bare-metal, and PowerVM providers are
future targets, not installable provider paths today. See the
[provider architecture](docs/design/top-level-design.md#provider-model) for the extension model.

## Choose a provider

| Provider | Best fit | Where KDIVE runs | Start here |
|---|---|---|---|
| **local-libvirt** | Development, dedicated lab hosts, and the shortest route to a live VM | The server, worker, and reconciler run as host processes on the KVM/libvirt host | [Local-libvirt quick start](#local-libvirt-quick-start) |
| **remote-libvirt** | Shared labs and deployments where the control plane is separate from the VM hosts | A Kubernetes control plane drives an operator-prepared libvirt host over TLS | [Remote-libvirt walkthrough](docs/operating/providers/remote-libvirt-walkthrough.md) |

### Local-libvirt quick start

You need a Linux host with KVM/libvirt, Python 3.14, the provider host packages, Docker for the
development backends, and a kernel source tree to drive. Clone, build the venv, and run the
read-only preflight; it reports each missing prerequisite with the command that fixes it:

```bash
git clone https://github.com/randomparity/kdive.git
cd kdive
uv sync
./scripts/operations/check-local-libvirt.sh
```

Then bring the stack up with the example scripts. One command starts the backends (Postgres,
MinIO, mock OIDC), migrates the database, seeds the `demo` project, installs a `.mcp.json` into
your kernel tree (`~/src/linux` by default, or `KDIVE_KERNEL_SRC`), and starts the three host
processes:

```bash
examples/local-libvirt/up.sh
export KDIVE_TOKEN=$(examples/local-libvirt/mint-token.sh)
cd ~/src/linux && claude          # or any MCP client that reads .mcp.json
examples/local-libvirt/down.sh    # stop the processes when you are done
```

Booting a System also needs a guest image at `/var/lib/kdive/rootfs/local/`; build it once with
`python -m kdive build-fs --image fedora-kdive-ready-44`. The
[example README](examples/local-libvirt/README.md) documents the scripts, their variables, and
the token lifecycle; the [local-libvirt walkthrough](
docs/operating/providers/local-libvirt-walkthrough.md) is the step-by-step reference for host
packages, worker privileges, the guest-image build, and the first allocation.

### Remote-libvirt quick start

Prepare a target host with libvirt TLS, firewall policy, guest storage, and KDIVE's provider
authority. From the machine that will connect to it, run the read-only preflight:

```bash
just check-remote-libvirt HOST USER qemu+tls://HOST/system
```

Deploy the control plane and verify the chart:

```bash
helm install kdive deploy/helm/kdive \
  -n kdive-demo \
  -f deploy/helm/kdive/values-demo.yaml
helm test kdive -n kdive-demo
```

Start with the [remote-libvirt host setup runbook](
docs/operating/runbooks/remote-libvirt-host-setup.md), then follow the
[remote-libvirt walkthrough](docs/operating/providers/remote-libvirt-walkthrough.md) to attach the
provider, onboard a project, and run the lifecycle. The remote path needs real target hardware; it
is not a local Docker-only demo.

## How it fits together

```text
MCP client
    |
    v
server  ──────>  Postgres + S3-compatible object store
    |
    v
durable job queue  ──────>  worker  ──────>  local or remote libvirt
                                ^
                                |
                            reconciler
```

The server owns the MCP surface, authentication, authorization, state transitions, and admission
control. Workers perform provider operations. The reconciler repairs lifecycle drift. Kubernetes
deployments add a lifecycle witness for worker-termination evidence. Read the
[architecture overview](ARCHITECTURE.md) for the concise model or the
[top-level design](docs/design/top-level-design.md) for the authoritative design and rationale.

## Connect an agent

KDIVE serves MCP over streamable HTTP. An agent follows the `suggested_next_actions` returned in
each structured response and uses `jobs.wait` for long-running operations.

- [Agent onboarding and MCP client configuration](docs/guide/agents/index.md)
- [Core reproduce and verify path](docs/guide/core-path.md)
- [Tool reference](docs/guide/reference/index.md)
- [Response envelope and recovery model](docs/guide/response-envelope.md)

## Develop KDIVE

KDIVE uses Python 3.14 and [`uv`](https://docs.astral.sh/uv/). Install the task runner and hook
manager before running setup:

```bash
uv tool install rust-just prek
just setup
just test
```

`libvirt-python` builds against the system libvirt and Python headers, so install your
distribution's `libvirt-dev` and `python3-dev` equivalents first. On `ppc64le`, Rust and several
source-build prerequisites are also required. See the
[installation guide](docs/operating/install.md) and
[x86_64/ppc64le development guide](docs/development/cross-platform.md).

Contributor workflow and guardrails live in [CONTRIBUTING.md](CONTRIBUTING.md). The complete
documentation map is [docs/README.md](docs/README.md).

## Project links

- [Documentation](docs/README.md)
- [Operating guide](docs/operating/index.md)
- [Architecture decisions](docs/adr/)
- [Release process](docs/development/releasing.md)
- [Security policy](SECURITY.md)

## License

KDIVE is licensed under the [Apache License 2.0](LICENSE).
