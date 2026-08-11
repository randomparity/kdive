# KDIVE systemd units

Run the KDIVE processes (`server` / `worker` / `reconciler`) as host services. System-scope
units are in [`system/`](system/); user-scope units are in [`user/`](user/). Backends
(Postgres, S3, OIDC) are external and are not ordered by these units.

## System scope

```bash
sudo useradd --system --home-dir /opt/kdive --shell /usr/sbin/nologin kdive
sudo install -d -o kdive -g kdive /etc/kdive
sudo install -m 0640 -o kdive -g kdive kdive.env.example /etc/kdive/kdive.env
# edit /etc/kdive/kdive.env: fill in KDIVE_* values and credentials
sudo cp system/kdive-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now kdive-server kdive-worker kdive-reconciler
journalctl -u kdive-server -f
```

## User scope

```bash
install -d ~/.config/kdive
install -m 0640 kdive.env.example ~/.config/kdive/kdive.env
cp user/kdive-*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now kdive-server kdive-worker kdive-reconciler
```

Full prerequisites, the external-backend ordering note, and the env-file details are in
[the systemd operating guide](../../docs/operating/systemd.md).

## Fixed live-worker lifecycle contract

The live-VM workflows use the separate fixed-slot contract from ADR-0555. It installs eight
retained worker templates, a root socket-activated lifecycle witness, isolated slot accounts, and
a dedicated group-accessible session-libvirt endpoint. It does not convert the server or
reconciler into system units.

On a disposable hosted runner, install the contract after the checkout's `uv sync`. Supply the
lifecycle-witness member DSN on standard input so it is absent from the installer command line:

```bash
read -r -s witness_dsn
printf '%s\n' "$witness_dsn" | sudo env "PATH=$PATH" \
  deploy/systemd/install-live-worker-lifecycle.sh \
    --operator "$(id -un)" --source "$PWD"
unset witness_dsn
```

The installer is idempotent for one checkout on a fresh disposable host. Persistent self-hosted
runners receive the equivalent accounts, files, modes, socket, witness environment, revision
stamp, and session-libvirt resources from the `live_vm_host` Ansible role. The fixed endpoint is:

```text
qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/virtqemud-sock
```

Only the configured operator belongs to `kdive-live-control`. Worker accounts belong to
`kdive-live-libvirt` and never to the control, sudo, or Docker groups. The witness credential and
service configuration are root-only beneath `/etc/kdive`; per-slot state is root-owned beneath
`/var/lib/kdive/live-workers`.
