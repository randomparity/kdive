# Prepare and register a remote-libvirt host

Use this guide to connect a prepared libvirt host to a KDIVE deployment. The
[Ansible guide](../../../deploy/ansible/README.md) owns host packages, mutual-TLS PKI, storage,
firewall configuration, and guest-image builds. This page owns the deployment-side inventory,
secret mapping, object-store reachability, and checks before a lifecycle test.

## 1. Provision the host

Follow the [Ansible usage sequence](../../../deploy/ansible/README.md#usage), starting with your
inventory and per-host settings. Set the worker network (`worker_cidr`), host FQDN, gdbstub
listen address/range, capacity, and selected images before generating certificates or applying
host changes. The playbooks require a Linux KVM host and privilege to install system services.

The resulting host needs:

- A mutually authenticated `qemu+tls` listener, normally port 16514. The role selects `libvirtd`
  or modular `virtproxyd` according to the host distribution.
- A **directory (`dir`) storage pool** and guest network. The remote module-restoration volume
  naming contract assumes a dir pool ([ADR-0588](../../adr/0588-remote-module-volume-ownership-lives-in-the-volume-name.md)).
- Firewall rules admitting the worker to TLS and the gdbstub range. The gdbstub uses raw TCP;
  the libvirt TLS connection does not protect it. Verify both an allowed worker connection and
  refusal from outside the configured worker network.

The Ansible roles own the distro-specific libguestfs prerequisites, including readable host
kernels and the Debian-family appliance networking workarounds. Diagnose those through
[`guest_image_prereqs`](../../../deploy/ansible/roles/guest_image_prereqs/tasks/main.yml).
Host libvirt security confinement is retained by default; `disable_security_driver` is an
explicit Ansible opt-in, separate from the guest SELinux policy below.

## 2. Prepare guest images

Select and build images through the [Ansible image catalog](../../../deploy/ansible/README.md#image-catalog-inventorygroup_varsallyml--host_vars).
Remote-libvirt boots a complete disk image with a bootloader and qemu-guest-agent. Each image
must carry the [in-guest helpers and their dependencies](../../../deploy/remote-libvirt-guest-helpers/README.md).
The image playbook installs the selected helpers with root ownership and the required labels.

Guest SELinux must be **permissive**, as required by
[ADR-0484](../../adr/0484-guest-images-ship-selinux-permissive.md). A per-domain permissive rule
for `virt_qemu_ga_t` does not cover all helper child processes. This guest policy does not imply
disabling the host's libvirt security driver.

Choose image content for the intended method: kdump needs a crash-capable kernel and configured
capture service; live drgn needs the running kernel's matching debuginfo. A staged volume alone
proves neither. The Ansible guide records which image families support kernel installation and
which still need hardware validation.

After `playbooks/image.yml`, **run `site.yml` again** to regenerate the per-host inventory
fragments. The facts role emits image entries only for staged volumes that pass its guest-userland
check; a fragment marked `INCOMPLETE` must be resolved before registration. The volume name comes
from the selected catalog entry; there is no single mandatory Fedora volume name.

## 3. Register remote-libvirt on the deployment

The provider is opt-in through `[[remote_libvirt]]` entries in `systems.toml`. Use the generated
`deploy/ansible/artifacts/<host>-systems.toml` fragments. Each host's `base_image` names an
`[[image]]` entry, whose staged `volume` names the actual volume in that host's storage pool.

Deploy the control plane using the [Helm runbook](kubernetes-deploy.md), or use the
[host-process live stack](live-stack.md) for testing. For Helm, provide the inventory ConfigMap
through `systems.configMapName` and the TLS Secret through `secrets.secretName`, using the
runbook's installation or upgrade procedure. For host processes, set `KDIVE_SYSTEMS_TOML` and
`KDIVE_SECRETS_ROOT` in the maintained process environment.

The worker resolves `client_cert_ref`, `client_key_ref`, and `ca_cert_ref` as filenames under
`KDIVE_SECRETS_ROOT`. The Kubernetes Secret **keys** must match those inventory refs exactly.
For refs named `clientcert.pem`, `clientkey.pem`, and `cacert.pem`, create the Secret from the
Ansible client bundle (from the repository root):

```sh
kubectl -n kdive-demo create secret generic kdive-remote-tls \
  --from-file=clientcert.pem=deploy/ansible/artifacts/client/clientcert.pem \
  --from-file=clientkey.pem=deploy/ansible/artifacts/client/clientkey.pem \
  --from-file=cacert.pem=deploy/ansible/artifacts/client/cacert.pem
```

Adjust the namespace and Secret name to your deployment. If the emitted refs use names such as
`remote-clientcert.pem`, use those names on the left side of `--from-file` too. Mount the client
bundle and CA certificate; keep the CA signing key on the PKI controller.

The storage pool, network, and machine settings remain runtime configuration
(`KDIVE_REMOTE_LIBVIRT_STORAGE_POOL`, `KDIVE_REMOTE_LIBVIRT_NETWORK`,
`KDIVE_REMOTE_LIBVIRT_MACHINE`); see the [configuration reference](../../guide/reference/config.md).
Onboard the tenant's budget and quota using [project onboarding](../project-onboarding.md).

### Object-store reachability

Set `KDIVE_S3_ENDPOINT_URL` to an endpoint reachable by **both the worker and the remote guest**.
The guest downloads kernels with presigned GET URLs and uploads kdump cores with presigned PUT
URLs. The endpoint embedded in those URLs must resolve and route from the guest network.

Loopback names such as `localhost` point to the guest itself. A cluster-only service address can
also be unreachable from a remote guest. The worker rejects loopback endpoints before remote
install/kdump transfer; it cannot establish that every other address has a working guest route.
Verify routing and firewall rules for the actual guest-to-store path.

### Optional: offer the base image's kernel config

An image staged by Ansible does not automatically publish its `/boot/config-<version>` to KDIVE.
To offer it as a build starting point, declare and reconcile the image row first, then use a
local copy of the built qcow2 with `stage-volume`:

```sh
python -m kdive stage-volume \
  --provider remote-libvirt \
  --image fedora-kdive-remote-base-43 \
  --from /path/to/fedora-kdive-remote-base-43.qcow2
```

Run this from an installed KDIVE environment configured for the deployment's database, remote
inventory/TLS refs, and object store. This command currently requires **exactly one**
`[[remote_libvirt]]` instance in its process inventory. For a multi-host deployment, point this
command's `KDIVE_SYSTEMS_TOML` at an inventory containing the intended host and its image entries.
It uploads the volume to the configured pool and attempts to capture and attach the kernel
config. The config capture is advisory: absent or ambiguous kernel content can leave a successfully staged image with no config offer. Check
`images.describe` for `has_kernel_config`; a successful volume upload alone does not prove it.

## 4. Validate the deployment

Start with `kdivectl doctor --provider remote-libvirt` and inspect individual checks. A passing
secret-backend check does not by itself prove that the three TLS refs resolve or that a remote
connection succeeds. The [kdivectl runbook](kdivectl.md) owns authentication and exit codes.

For the default Helm release/namespace, the following probe uses the worker's mounted Secret.
Adjust the StatefulSet name, namespace, inventory hostname, pool, volume, and ref filenames to
match your deployment. The destination filenames inside `pkipath` are fixed by libvirt.

```sh
kubectl -n kdive-demo exec -i statefulset/kdive-kdive-worker -c worker -- python - <<'PY'
import os
import shutil
import tempfile
from pathlib import Path

import libvirt

source = Path(os.environ["KDIVE_SECRETS_ROOT"])
with tempfile.TemporaryDirectory(prefix="kdive-tls-check-") as pki:
    for ref, name in {
        "cacert.pem": "cacert.pem",
        "clientcert.pem": "clientcert.pem",
        "clientkey.pem": "clientkey.pem",
    }.items():
        shutil.copyfile(source / ref, Path(pki) / name)
    connection = libvirt.openReadOnly("qemu+tls://HOST.FQDN/system?pkipath=" + pki)
    try:
        volumes = connection.storagePoolLookupByName("default").listVolumes()
        if "fedora-kdive-remote-base-43.qcow2" not in volumes:
            raise SystemExit("Base volume missing; verify image staging and the pool name.")
        print("Mutual TLS connected; base volume present.")
    finally:
        connection.close()
PY
```

This proves only TLS connectivity and volume visibility from that worker. Separately inspect
`resources.list`/`images.list` for catalog registration and test the permitted/refused firewall
paths from step 1. Neither the TLS probe nor a catalog row proves the guest boots or captures a
core. The [remote live-stack runbook](remote-live-stack.md) defines the disposable host-process
lifecycle tests and their completion evidence.
