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
retained worker templates, a root socket-activated lifecycle witness, isolated slot accounts,
and the `kdive-live-libvirt` supplemental group wiring. It does not convert the server or
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

The installer is idempotent for one checkout on a fresh disposable host. It requires the
distro's `kvm` group and fails before activation when it is missing. Persistent self-hosted
runners receive the equivalent accounts, groups, files, modes, socket, witness environment,
revision stamp, and fixture catalog from the Ubuntu/Debian-only `live_vm_host` role; both paths
converge the same end state on a clean host.

Only the configured operator belongs to `kdive-live-control`. Worker accounts keep distinct
primary groups and receive the `kdive-live-libvirt` and `kvm` supplemental groups; they never
belong to the control, sudo, or Docker groups. The distro `kvm` authority lets every worker read
`root:kvm` mode-`0640` host kernels for libguestfs and use `/dev/kvm` without making either
world-accessible. The witness credential and service configuration are root-only beneath
`/etc/kdive`; per-slot state is root-owned beneath `/var/lib/kdive/live-workers`, and each slot
account can neither traverse nor replace a sibling slot.

The dedicated session-libvirt daemon configuration, its protected `/run/kdive/live-libvirt`
runtime hierarchy with no-follow stale-tuple recovery, and the published `KDIVE_LIBVIRT_URI`
endpoint land with the libvirt provider-authority change (#1937); until then workers hold the
group membership but there is no session-libvirt socket to connect to.

Adding the operator to `kdive-live-control` does not refresh an already-running process's kernel
group list. Interactive operators must start a new login session after installation before using
the installed socket.
