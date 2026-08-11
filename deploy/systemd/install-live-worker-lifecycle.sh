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
_libvirt_tuple_action=""
_libvirt_tuple_pid=""
_libvirt_tuple_pid_live="false"
_libvirt_tuple_process_uid=""
_libvirt_tuple_process_name=""
_libvirt_tuple_socket_state="absent"
_libvirt_tuple_pid_identity=""
_libvirt_tuple_socket_identity=""

_libvirt_tuple_error() {
  local reason="$1" socket_path="$2" pid_path="$3"
  echo "contradictory selected libvirt tuple: $reason; $socket_path and $pid_path left untouched." \
    "Recovery: inspect those exact paths and their owning process, correct the evidence, then rerun." >&2
  return 1
}

_inspect_libvirt_pid() {
  local pid_path="$1" operator_uid="$2" socket_path="$3"
  local authority pid_mode

  [[ -e $pid_path || -L $pid_path ]] || return 0
  if [[ ! -f $pid_path || -L $pid_path ]]; then
    _libvirt_tuple_error "pid residue has wrong type" "$socket_path" "$pid_path"
    return
  fi
  authority="$(stat -c '%u:%a' "$pid_path")"
  pid_mode="${authority#*:}"
  if [[ ${authority%%:*} != "$operator_uid" ]] ||
    ((!(8#$pid_mode & 8#400) || (8#$pid_mode & 8#022))); then
    _libvirt_tuple_error "pid residue has wrong authority" "$socket_path" "$pid_path"
    return
  fi
  _libvirt_tuple_pid="$(<"$pid_path")"
  if [[ ! $_libvirt_tuple_pid =~ ^[1-9][0-9]*$ ]]; then
    _libvirt_tuple_error "pid residue is not one numeric pid" "$socket_path" "$pid_path"
    return
  fi
  _libvirt_tuple_pid_identity="$(stat -c '%d:%i:%u:%a' "$pid_path")"
  if ! kill -0 "$_libvirt_tuple_pid" 2>/dev/null; then
    return 0
  fi
  _libvirt_tuple_pid_live="true"
  if ! _libvirt_tuple_process_uid="$(stat -c '%u' "/proc/$_libvirt_tuple_pid")" ||
    ! IFS= read -r _libvirt_tuple_process_name <"/proc/$_libvirt_tuple_pid/comm"; then
    _libvirt_tuple_error "pid process changed during inspection" "$socket_path" "$pid_path"
  fi
}

_probe_libvirt_socket() {
  /usr/bin/python3 - "$1" <<'PY'
import socket
import sys

client = socket.socket(socket.AF_UNIX)
client.settimeout(0.5)
try:
    client.connect(sys.argv[1])
except ConnectionRefusedError:
    raise SystemExit(3)
except OSError:
    raise SystemExit(4)
finally:
    client.close()
PY
}

_inspect_libvirt_socket() {
  local socket_path="$1" operator_uid="$2" group_gid="$3" pid_path="$4"
  local authority probe_status

  [[ -e $socket_path || -L $socket_path ]] || return 0
  if [[ ! -S $socket_path || -L $socket_path ]]; then
    _libvirt_tuple_error "socket residue has wrong type" "$socket_path" "$pid_path"
    return
  fi
  authority="$(stat -c '%u:%g:%a' "$socket_path")"
  if [[ $authority != "$operator_uid:$group_gid:770" ]]; then
    _libvirt_tuple_error "socket residue has wrong authority" "$socket_path" "$pid_path"
    return
  fi
  _libvirt_tuple_socket_identity="$(stat -c '%d:%i:%u:%g:%a' "$socket_path")"
  if _probe_libvirt_socket "$socket_path"; then
    _libvirt_tuple_socket_state="live"
    return 0
  else
    probe_status=$?
  fi
  if [[ $probe_status -eq 3 ]]; then
    _libvirt_tuple_socket_state="stale"
    return 0
  fi
  _libvirt_tuple_error "socket listener state is indeterminate" "$socket_path" "$pid_path"
}

_remove_stale_libvirt_tuple() {
  local socket_path="$1" pid_path="$2"
  local current_identity

  if [[ -n $_libvirt_tuple_pid_identity ]]; then
    current_identity="$(stat -c '%d:%i:%u:%a' "$pid_path" 2>/dev/null || :)"
    if kill -0 "$_libvirt_tuple_pid" 2>/dev/null ||
      [[ $current_identity != "$_libvirt_tuple_pid_identity" ]]; then
      _libvirt_tuple_error "pid residue changed during inspection" "$socket_path" "$pid_path"
      return
    fi
  fi
  if [[ -n $_libvirt_tuple_socket_identity ]]; then
    current_identity="$(stat -c '%d:%i:%u:%g:%a' "$socket_path" 2>/dev/null || :)"
    if [[ $current_identity != "$_libvirt_tuple_socket_identity" ]]; then
      _libvirt_tuple_error "socket residue changed during inspection" "$socket_path" "$pid_path"
      return
    fi
  fi
  [[ -z $_libvirt_tuple_pid_identity ]] || unlink "$pid_path"
  [[ -z $_libvirt_tuple_socket_identity ]] || unlink "$socket_path"
}

_reconcile_libvirt_tuple() {
  local socket_path="$1" pid_path="$2" daemon="$3" operator_uid="$4" group_gid="$5"

  _libvirt_tuple_action=""
  _libvirt_tuple_pid=""
  _libvirt_tuple_pid_live="false"
  _libvirt_tuple_process_uid=""
  _libvirt_tuple_process_name=""
  _libvirt_tuple_socket_state="absent"
  _libvirt_tuple_pid_identity=""
  _libvirt_tuple_socket_identity=""
  _inspect_libvirt_pid "$pid_path" "$operator_uid" "$socket_path" || return
  _inspect_libvirt_socket "$socket_path" "$operator_uid" "$group_gid" "$pid_path" || return

  if [[ $_libvirt_tuple_pid_live == true ]]; then
    if [[ $_libvirt_tuple_process_uid != "$operator_uid" ||
      $_libvirt_tuple_process_name != "$daemon" || $_libvirt_tuple_socket_state != live ]]; then
      _libvirt_tuple_error "live pid, daemon identity, and socket do not agree" \
        "$socket_path" "$pid_path"
      return
    fi
    _libvirt_tuple_action="adopt"
    return 0
  fi
  if [[ $_libvirt_tuple_socket_state == live ]]; then
    _libvirt_tuple_error "socket has a listener without the exact selected pid" \
      "$socket_path" "$pid_path"
    return
  fi
  _remove_stale_libvirt_tuple "$socket_path" "$pid_path" || return
  _libvirt_tuple_action="start"
}

_install_libvirt_runtime_directories() {
  local runtime_root="$1" operator="$2" group="$3"
  install -d -o "$operator" -g "$group" -m 0750 "$runtime_root" "$runtime_root/libvirt"
}

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
install -d -o root -g root -m 0755 /run/kdive
_install_libvirt_runtime_directories /run/kdive/live-libvirt "$operator" "$libvirt_group"
install -d -o "$operator" -g "$libvirt_group" -m 2770 \
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
IFS=: read -r _ _ libvirt_group_gid _ < <(getent group "$libvirt_group")
_reconcile_libvirt_tuple "$libvirt_socket_path" "$libvirt_pid_path" "$_libvirt_daemon" \
  "$operator_uid" "$libvirt_group_gid"
if [[ $_libvirt_tuple_action == start ]]; then
  runuser -u "$operator" -- env XDG_RUNTIME_DIR=/run/kdive/live-libvirt \
    "$_libvirt_executable" --daemon --config "/etc/kdive/$_libvirt_config" \
    --pid-file "$libvirt_pid_path"
fi
_reconcile_libvirt_tuple "$libvirt_socket_path" "$libvirt_pid_path" "$_libvirt_daemon" \
  "$operator_uid" "$libvirt_group_gid"
if [[ $_libvirt_tuple_action != adopt ]]; then
  echo "selected $_libvirt_family $_libvirt_daemon did not produce a complete live tuple;" \
    "inspect $libvirt_pid_path and $libvirt_socket_path, then rerun" >&2
  exit 1
fi

systemctl daemon-reload
systemctl enable --now kdive-live-worker-lifecycle.socket
