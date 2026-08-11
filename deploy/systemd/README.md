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

The installer is idempotent for one checkout on a fresh disposable host. It selects the host
distro's supported session daemon: Debian-family hosts use monolithic `libvirtd` and
`libvirt-sock`; Red Hat-family hosts use modular `virtqemud` and `virtqemud-sock`. An unsupported
distro or missing selected daemon fails before activation. Persistent self-hosted runners receive
the Debian-family tuple and the equivalent accounts, files, modes, socket, witness environment,
revision stamp, and session-libvirt resources from the Ubuntu/Debian-only `live_vm_host` role.

The selected non-secret endpoint is published as `KDIVE_LIBVIRT_URI` in the root-owned,
world-readable `/etc/kdive/live-worker-libvirt.env`. The possible values are:

```text
qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock
qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/virtqemud-sock
```

Exactly one daemon tuple is activated; the installer does not create a compatibility socket alias.
`/run/kdive` is root-owned mode `0755`. Its `live-libvirt` and `live-libvirt/libvirt`
subdirectories are operator-owned mode `0750`, so workers can traverse to the explicit mode-`0770`
libvirt socket but cannot unlink or replace either control socket. Only provider data directories
are group-writable mode `2770`.

An existing endpoint is adopted only when its pid file names a live operator-owned process with
the selected daemon identity and its socket has the selected owner, group, mode, and a live
listener. A dead pid and refused, correctly owned selected socket are removed as exact stale
residues before restart. Contradictory process, type, ownership, or listener evidence is left
untouched; inspect the two paths named by the installer, correct that evidence, and rerun.

Only the configured operator belongs to `kdive-live-control`. Worker accounts belong to
`kdive-live-libvirt` and never to the control, sudo, or Docker groups. The witness credential and
service configuration are root-only beneath `/etc/kdive`; per-slot state is root-owned beneath
`/var/lib/kdive/live-workers`.

Adding the operator to `kdive-live-control` does not refresh an already-running process's kernel
group list. The hosted workflow therefore enters one `sg kdive-live-control` context for its full
post-install spine, verifies the effective numeric group, and opens the installed socket before
bring-up. Interactive operators must start a new login session after installation.
