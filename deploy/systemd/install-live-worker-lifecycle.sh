#!/bin/bash
set -euo pipefail

usage() {
  echo "usage: $0 --operator USER --source PATH" >&2
  exit 2
}

operator=""
source_root=""
while (($#)); do
  case "$1" in
  --operator)
    [[ $# -ge 2 ]] || usage
    operator="$2"
    shift 2
    ;;
  --source)
    [[ $# -ge 2 ]] || usage
    source_root="$2"
    shift 2
    ;;
  *) usage ;;
  esac
done

[[ $EUID -eq 0 ]] || {
  echo "install-live-worker-lifecycle.sh must run as root" >&2
  exit 1
}
[[ -n $operator && -d $source_root ]] || usage
operator_uid="$(id -u "$operator")"
IFS= read -r witness_dsn || [[ -n $witness_dsn ]]
[[ -n $witness_dsn ]] || {
  echo "witness DSN on standard input must be non-empty" >&2
  exit 1
}

control_group="kdive-live-control"
libvirt_group="kdive-live-libvirt"
state_root="/var/lib/kdive/live-workers"
source_link=""
credential_temp=""
config_temp=""
revision_temp=""

cleanup() {
  [[ -z $credential_temp || ! -e $credential_temp ]] || unlink "$credential_temp"
  [[ -z $config_temp || ! -e $config_temp ]] || unlink "$config_temp"
  [[ -z $revision_temp || ! -e $revision_temp ]] || unlink "$revision_temp"
  [[ -z $source_link || ! -L $source_link ]] || unlink "$source_link"
}
trap cleanup EXIT

getent group "$control_group" >/dev/null || groupadd --system "$control_group"
getent group "$libvirt_group" >/dev/null || groupadd --system "$libvirt_group"
usermod -a -G "$control_group,$libvirt_group" "$operator"

for slot in {1..8}; do
  worker="kdive-worker-${slot}"
  getent group "$worker" >/dev/null || groupadd --system "$worker"
  if ! getent passwd "$worker" >/dev/null; then
    useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
      --gid "$worker" --groups "$libvirt_group" "$worker"
  else
    usermod -G "$libvirt_group" "$worker"
  fi
done

install -d -o root -g root -m 0755 /usr/local/libexec /etc/kdive
install -d -o root -g root -m 0700 /etc/kdive/credentials
install -d -o root -g root -m 0755 \
  "$state_root" "$state_root/slots" /opt/kdive-live-worker-lifecycle
install -d -o "$operator" -g "$libvirt_group" -m 2770 \
  /run/kdive/live-libvirt /var/lib/kdive/rootfs /var/lib/kdive/console \
  /var/lib/kdive/pcap /var/lib/kdive/build /var/lib/kdive/install

for slot in {1..8}; do
  install -d -o root -g "kdive-worker-${slot}" -m 0750 "$state_root/slots/${slot}"
done

install -o root -g root -m 0755 \
  "$source_root/deploy/systemd/bin/kdive-live-worker-gate" \
  /usr/local/libexec/kdive-live-worker-gate
install -o root -g root -m 0755 \
  "$source_root/deploy/systemd/bin/kdive-live-worker-lifecycle" \
  /usr/local/libexec/kdive-live-worker-lifecycle
for unit in kdive-live-worker@.service kdive-live-worker-lifecycle.socket \
  kdive-live-worker-lifecycle@.service; do
  install -o root -g root -m 0644 "$source_root/deploy/systemd/system/$unit" \
    "/etc/systemd/system/$unit"
done
install -o root -g root -m 0644 "$source_root/deploy/systemd/virtqemud-live.conf" \
  /etc/kdive/virtqemud-live.conf

credential_temp="$(mktemp /etc/kdive/credentials/.live-worker-witness.dsn.XXXXXX)"
printf '%s\n' "$witness_dsn" >"$credential_temp"
install -o root -g root -m 0600 "$credential_temp" \
  /etc/kdive/credentials/live-worker-witness.dsn
unlink "$credential_temp"
credential_temp=""
config_temp="$(mktemp /etc/kdive/.live-worker-lifecycle.conf.XXXXXX)"
printf 'KDIVE_LIVE_WORKER_OPERATOR_UID=%s\n' "$operator_uid" >"$config_temp"
printf 'KDIVE_LIVE_WORKER_STATE_ROOT=%s\n' "$state_root" >>"$config_temp"
install -o root -g root -m 0600 "$config_temp" /etc/kdive/live-worker-lifecycle.conf
unlink "$config_temp"
config_temp=""

if [[ $source_root != /opt/kdive ]]; then
  if [[ -L /opt/kdive ]] && [[ $(readlink -f /opt/kdive) == "$(readlink -f "$source_root")" ]]; then
    source_link=/opt/kdive
  elif [[ -e /opt/kdive || -L /opt/kdive ]]; then
    echo "/opt/kdive already exists and does not name --source" >&2
    exit 1
  else
    ln -s "$source_root" /opt/kdive
    source_link=/opt/kdive
  fi
fi
uv venv --python /usr/bin/python3 /opt/kdive-live-worker-lifecycle/.venv
uv pip install --python /opt/kdive-live-worker-lifecycle/.venv/bin/python /opt/kdive
chown -R root:root /opt/kdive-live-worker-lifecycle
chmod 0755 /opt/kdive-live-worker-lifecycle

revision_temp="$(mktemp /opt/kdive-live-worker-lifecycle/.revision.XXXXXX)"
git -C "$source_root" rev-parse HEAD >"$revision_temp"
install -o root -g root -m 0444 "$revision_temp" \
  /opt/kdive-live-worker-lifecycle/revision
unlink "$revision_temp"
revision_temp=""

if [[ ! -S /run/kdive/live-libvirt/libvirt/virtqemud-sock ]]; then
  runuser -u "$operator" -- env XDG_RUNTIME_DIR=/run/kdive/live-libvirt \
    virtqemud --daemon --config /etc/kdive/virtqemud-live.conf
fi

systemctl daemon-reload
systemctl enable --now kdive-live-worker-lifecycle.socket
