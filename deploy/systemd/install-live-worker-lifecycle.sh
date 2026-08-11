#!/bin/bash
set -euo pipefail

_created_source_link=""
_libvirt_family=""
_libvirt_daemon=""
_libvirt_executable=""
_libvirt_config=""
_libvirt_socket=""
_libvirt_pid=""
_libvirt_uri=""

_select_libvirt_tuple() {
  local os_release="$1"
  local ID="" ID_LIKE="" distro_tokens

  if [[ ! -r $os_release ]]; then
    echo "cannot read distro identity from $os_release" >&2
    return 1
  fi
  # shellcheck disable=SC1090 # The caller supplies the os-release path for isolated tests.
  source "$os_release"
  distro_tokens=" ${ID:-} ${ID_LIKE:-} "
  case "$distro_tokens" in
  *" debian "* | *" ubuntu "*)
    _libvirt_family="Debian-family"
    _libvirt_daemon="libvirtd"
    _libvirt_config="libvirtd-live.conf"
    _libvirt_socket="libvirt-sock"
    _libvirt_pid="libvirtd.pid"
    _libvirt_uri="qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/libvirt-sock"
    ;;
  *" rhel "* | *" fedora "* | *" centos "* | *" rocky "* | *" almalinux "*)
    _libvirt_family="Red Hat-family"
    _libvirt_daemon="virtqemud"
    _libvirt_config="virtqemud-live.conf"
    _libvirt_socket="virtqemud-sock"
    _libvirt_pid="virtqemud.pid"
    _libvirt_uri="qemu+unix:///session?socket=/run/kdive/live-libvirt/libvirt/virtqemud-sock"
    ;;
  *)
    echo "unsupported distro family in $os_release (ID=${ID:-unset}, ID_LIKE=${ID_LIKE:-unset})" >&2
    return 1
    ;;
  esac

  if ! _libvirt_executable="$(command -v "$_libvirt_daemon")"; then
    echo "selected $_libvirt_family $_libvirt_daemon executable is missing" >&2
    return 1
  fi
}

_prepare_source_link() {
  local source_root="$1" install_link="$2"
  if [[ -L $install_link ]] &&
    [[ $(readlink -f "$install_link") == "$(readlink -f "$source_root")" ]]; then
    return
  fi
  if [[ -e $install_link || -L $install_link ]]; then
    echo "$install_link already exists and does not name --source" >&2
    return 1
  fi
  ln -s "$source_root" "$install_link"
  _created_source_link="$install_link"
}

_cleanup_source_link() {
  if [[ -n $_created_source_link && -L $_created_source_link ]]; then
    unlink "$_created_source_link"
  fi
  _created_source_link=""
}

if [[ ${BASH_SOURCE[0]} != "$0" ]]; then
  return 0
fi

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
_select_libvirt_tuple /etc/os-release
IFS= read -r witness_dsn || [[ -n $witness_dsn ]]
[[ -n $witness_dsn ]] || {
  echo "witness DSN on standard input must be non-empty" >&2
  exit 1
}

control_group="kdive-live-control"
libvirt_group="kdive-live-libvirt"
state_root="/var/lib/kdive/live-workers"
credential_temp=""
config_temp=""
revision_temp=""
uri_temp=""

cleanup() {
  [[ -z $credential_temp || ! -e $credential_temp ]] || unlink "$credential_temp"
  [[ -z $config_temp || ! -e $config_temp ]] || unlink "$config_temp"
  [[ -z $revision_temp || ! -e $revision_temp ]] || unlink "$revision_temp"
  [[ -z $uri_temp || ! -e $uri_temp ]] || unlink "$uri_temp"
  _cleanup_source_link
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
  /run/kdive/live-libvirt /run/kdive/live-libvirt/libvirt \
  /var/lib/kdive/rootfs /var/lib/kdive/console \
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
install -o root -g root -m 0644 "$source_root/deploy/systemd/$_libvirt_config" \
  "/etc/kdive/$_libvirt_config"

uri_temp="$(mktemp /etc/kdive/.live-worker-libvirt.env.XXXXXX)"
printf 'KDIVE_LIBVIRT_URI=%s\n' "$_libvirt_uri" >"$uri_temp"
install -o root -g root -m 0644 "$uri_temp" /etc/kdive/live-worker-libvirt.env
unlink "$uri_temp"
uri_temp=""

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
  _prepare_source_link "$source_root" /opt/kdive
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

libvirt_socket_path="/run/kdive/live-libvirt/libvirt/$_libvirt_socket"
libvirt_pid_path="/run/kdive/live-libvirt/libvirt/$_libvirt_pid"
if [[ ! -S $libvirt_socket_path ]]; then
  runuser -u "$operator" -- env XDG_RUNTIME_DIR=/run/kdive/live-libvirt \
    "$_libvirt_executable" --daemon --config "/etc/kdive/$_libvirt_config" \
    --pid-file "$libvirt_pid_path"
fi
[[ -S $libvirt_socket_path ]] || {
  echo "selected $_libvirt_family $_libvirt_daemon did not create $libvirt_socket_path" >&2
  exit 1
}
[[ -r $libvirt_pid_path ]] || {
  echo "selected $_libvirt_family $_libvirt_daemon did not create $libvirt_pid_path" >&2
  exit 1
}
IFS= read -r libvirt_pid <"$libvirt_pid_path"
if [[ ! $libvirt_pid =~ ^[0-9]+$ ]] || ! kill -0 "$libvirt_pid"; then
  echo "selected $_libvirt_family $_libvirt_daemon pid file is not live" >&2
  exit 1
fi
[[ $(stat -c '%U:%G:%a' "$libvirt_socket_path") == "$operator:$libvirt_group:770" ]] || {
  echo "selected libvirt socket must be $operator:$libvirt_group mode 0770" >&2
  exit 1
}

systemctl daemon-reload
systemctl enable --now kdive-live-worker-lifecycle.socket
