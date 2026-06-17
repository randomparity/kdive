# remote-libvirt walkthrough

End-to-end setup for the remote-libvirt provider, where the KDIVE worker drives QEMU/KVM
guests on a separate TLS target host. For the provider's prerequisites and config see
[the remote-libvirt provider reference](remote-libvirt.md); this page is the linear path from
a prepared target host to a verified run.

> **Deployment:** a Helm/k8s control plane drives a separate TLS target host. The worker needs
> no local KVM — the guest runs on the target host. Provisioning that target (PKI,
> `virtproxyd`, firewall ACL, guest image) is a prerequisite, covered by the
> [remote-libvirt host setup runbook](../runbooks/remote-libvirt-host-setup.md).

## 1. Prepare

Provision the target host per the
[remote-libvirt host setup runbook](../runbooks/remote-libvirt-host-setup.md), then run the
read-only preflight from where the worker will connect:

```bash
just check-remote-libvirt HOST USER qemu+tls://HOST/system
```

## 2. Install

Deploy the control plane with the chart and attach the provider (see
[Kubernetes (Helm)](../kubernetes.md) and the
[Kubernetes deploy runbook](../runbooks/kubernetes-deploy.md)):

```bash
helm install kdive deploy/helm/kdive -n kdive-demo -f deploy/helm/kdive/values-demo.yaml --wait
```

## 3. Onboard the project

The chart seeds build-configs but **not** quota or budget, so the first `allocations.request`
dead-ends on `quota_exceeded` (this is issue #497). Onboard the demo project through the
audited admin tools. The in-cluster server is ClusterIP-only, so port-forward its MCP
endpoint first:

Run this from the repo checkout with the project venv active (or set
`KDIVE_PYTHON=/opt/kdive/.venv/bin/python`), so `fastmcp` resolves:

```bash
kubectl port-forward -n kdive-demo svc/kdive-kdive-server 8000:8000 &
export KDIVE_MCP_BASE=http://127.0.0.1:8000/mcp
just setup-remote-libvirt HOST root qemu+tls://HOST/system
```

The script mints a project-`admin` token in-cluster (`scripts/demo-token.sh`) and calls
`accounting.set_quota` + `accounting.set_budget`. Supply your own token with `KDIVE_TOKEN` to
skip the demo mint. See [Project onboarding](../project-onboarding.md) for the production
path.

## 4. Test the lifecycle

With the project onboarded, request an allocation and drive a System through its lifecycle:

```bash
# allocations.request → provision → build → boot → verify → teardown → release
```

Issue these as MCP tool calls against the port-forwarded endpoint. For the deep
build→boot→debug steps and the canonical dcache `dhash_entries` verification, follow the
[remote live stack](../runbooks/remote-live-stack.md) and
[four-method live run](../runbooks/four-method-live-run.md) runbooks. The full
build→boot→verify needs the real target hardware; a successful run reaches at least a ready
System via `provision`.
