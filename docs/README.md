# KDIVE documentation

KDIVE coordinates a Linux kernel build, boot, debug, and crash-analysis workflow over MCP.
You build kernels in your own environment and upload the artifacts; KDIVE provisions the
VMs, installs and boots kernels, and manages debugging, artifacts, and resource lifetimes.
Local-libvirt and remote-libvirt are implemented providers. See
[platform support](operating/platform-support.md) for host and guest requirements.

## Use KDIVE — users and agents

Start here if someone already operates a KDIVE server for you.

1. [Connect an MCP client](guide/agents/index.md) and verify access.
2. [Learn the domain concepts](guide/concepts.md): resources, allocations, systems,
   investigations, runs, and debug sessions.
3. [Follow the core reproduce/verify path](guide/core-path.md).
4. Read [responses](guide/response-envelope.md), [async jobs](guide/async-jobs.md),
   [errors and recovery](guide/errors.md), and [permissions](guide/safety-and-rbac.md)
   as you encounter them.

The [generated tool reference](guide/reference/index.md) owns exact parameters and tool
contracts.
The [agent workflow index](guide/agent-index.md) and toolset guides are also served over MCP.

## Run KDIVE — operators

Start with [installation and run modes](operating/install.md), then choose a provider:

- [Local-libvirt setup](../examples/local-libvirt/README.md): run KDIVE
  as host processes on a Linux KVM/libvirt host.
- [Remote-libvirt setup](operating/providers/remote-libvirt.md): prepare
  a separate libvirt host and connect it to the control plane.

The [operating index](operating/index.md) maps deployment, project onboarding, maintenance,
and recovery procedures. Use [live testing](operating/runbooks/live-testing.md) when validating
an installation or a code change against real infrastructure.

## Develop KDIVE — contributors

1. [Set up a checkout and run a focused test](../CONTRIBUTING.md).
2. Read the [architecture overview](../ARCHITECTURE.md), then the
   [current architecture and code map](design/top-level-design.md).
3. Consult [cross-platform development](development/cross-platform.md) for x86_64 and ppc64le,
   [mutation testing](development/mutation-testing.md) for test effectiveness, and
   [releasing](development/releasing.md) when preparing a release.

## Current guidance and historical records

The guides above describe the checkout you are reading. For a released installation, read
these files at that release's Git tag and use the matching image or package version.

- **Current behavior:** tool and configuration references are generated from implementation.
  Procedures belong in user, operating, or contributor guides. The top-level design explains
  current architecture; it does not replace the tool contracts.
- **Decision history:** [ADRs](adr/README.md) explain why decisions were made. Read their status
  and later amendments or superseding records before applying them. [Debt records](debt/)
  describe known deferred concerns.
- **Working designs and history:** dated specs, implementation plans, proof records, and
  [archived material](archive/) describe a particular change or past checkout. Their presence
  does not establish that a feature is available today. Use the current guides to install,
  operate, and contribute.
