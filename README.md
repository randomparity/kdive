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

- **The kernel workflow through MCP.** Build a kernel in your own environment and upload it;
  use KDIVE to provision, install, boot, debug, capture, retrieve, and clean up.
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

## Start here

| What you want to do | Read next |
|---|---|
| Connect to an existing KDIVE server | [Agent onboarding](docs/guide/agents/index.md), then the [core workflow](docs/guide/core-path.md) |
| Run KDIVE on a local Linux KVM/libvirt host | [Local-libvirt walkthrough](docs/operating/providers/local-libvirt-walkthrough.md) |
| Run a control plane with separate libvirt hosts | [Remote-libvirt walkthrough](docs/operating/providers/remote-libvirt-walkthrough.md) |
| Change KDIVE itself | [Contributing](CONTRIBUTING.md), then the [architecture overview](ARCHITECTURE.md) |

The local walkthrough includes a Debian/Ubuntu setup script and the first-VM procedure.
The remote walkthrough covers host preparation, control-plane deployment, and verification.
See [platform support](docs/operating/platform-support.md) for x86_64 and ppc64le requirements
and [installation](docs/operating/install.md) for packaging and deployment choices.

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

The [documentation index](docs/README.md) gives ordered learning paths for users, operators,
and contributors, and explains how current guidance differs from historical designs.

## Project links

- [Documentation](docs/README.md)
- [Operating guide](docs/operating/index.md)
- [Architecture decisions](docs/adr/)
- [Release process](docs/development/releasing.md)
- [Security policy](SECURITY.md)

## License

KDIVE is licensed under the [Apache License 2.0](LICENSE).
