#!/bin/bash
set -euo pipefail
PATH=/usr/sbin:/usr/bin:/sbin:/bin

[[ $(id -u) == 0 ]] || {
  echo 'EL10 guestfs binding build must run as root' >&2
  exit 1
}

source_rpm="$(rpm -q --qf '%{SOURCERPM}' libguestfs)"
devel_source_rpm="$(rpm -q --qf '%{SOURCERPM}' libguestfs-devel)"
[[ $source_rpm == "$devel_source_rpm" ]] || {
  echo "libguestfs and libguestfs-devel source versions differ: $source_rpm / $devel_source_rpm" >&2
  exit 1
}
[[ $source_rpm =~ ^libguestfs-[[:alnum:]._-]+\.src\.rpm$ ]] || {
  echo "unexpected libguestfs source RPM name: $source_rpm" >&2
  exit 1
}
version="$(rpm -q --qf '%{VERSION}' libguestfs)"
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo "unexpected libguestfs version: $version" >&2
  exit 1
}

root=/opt/kdive-guestfs
target="$root/$source_rpm"
site="$(python3.14 -P -c 'import sysconfig; print(sysconfig.get_path("platlib"))')"
[[ $site == /usr/local/lib64/python3.14/site-packages ]] || {
  echo "unexpected Python 3.14 local site-packages: $site" >&2
  exit 1
}
for directory in "$root" "$site"; do
  [[ ! -L $directory ]] || {
    echo "guestfs binding directory must not be a symlink: $directory" >&2
    exit 1
  }
  install -d -o root -g root -m 0755 -- "$directory"
  [[ $(stat -c '%u:%a' -- "$directory") == 0:755 ]] || {
    echo "guestfs binding directory must be root-owned mode 0755: $directory" >&2
    exit 1
  }
done

lock="$root/.lock"
if [[ -e $lock || -L $lock ]]; then
  [[ -f $lock && ! -L $lock && $(stat -c '%u:%h' -- "$lock") == 0:1 ]] || {
    echo "guestfs binding lock must be a root-owned, single-link regular file: $lock" >&2
    exit 1
  }
else
  (
    umask 077
    : >"$lock"
  )
fi
chmod 0600 "$lock"
exec 9>>"$lock"
flock -x 9

verify_binding() {
  local path="$1"
  PYTHONPATH="$path" python3.14 -P - "$version" "$path" <<'PY'
import sys
from pathlib import Path
import guestfs

expected = tuple(map(int, sys.argv[1].split('.')))
binding_dir = Path(guestfs.__file__).resolve().parent
if binding_dir != Path(sys.argv[2]).resolve():
    raise SystemExit(f'guestfs imported from unexpected directory: {binding_dir}')
handle = guestfs.GuestFS(python_return_dict=True)
try:
    actual = handle.version()
finally:
    handle.close()
observed = (actual['major'], actual['minor'], actual['release'])
if observed != expected:
    raise SystemExit(f'guestfs binding reports libguestfs {observed}, expected {expected}')
PY
}

if [[ -e $target || -L $target ]]; then
  [[ -d $target && ! -L $target ]] || {
    echo "existing guestfs binding target is not a real directory: $target" >&2
    exit 1
  }
  [[ $(stat -c '%u:%a' -- "$target") == 0:755 ]] || {
    echo "existing guestfs binding must be root-owned mode 0755: $target" >&2
    exit 1
  }
  verify_binding "$target" || {
    echo "existing guestfs binding is invalid: $target; repair it before retrying" >&2
    exit 1
  }
  result=reused
else
  stage="$(mktemp -d "$root/.build.XXXXXXXX")"
  trap 'rm -rf -- "$stage"' EXIT
  source_nevr="${source_rpm%.src.rpm}"
  dnf -q download --source --enablerepo='*source*' --destdir "$stage" "$source_nevr"
  [[ -f $stage/$source_rpm ]] || {
    echo "source repository did not provide installed libguestfs source: $source_rpm" >&2
    exit 1
  }
  rpm -K --define '_pkgverify_level all' "$stage/$source_rpm"
  binary_signature="$(rpm -q --qf '%{RSAHEADER:pgpsig}' libguestfs)"
  devel_signature="$(rpm -q --qf '%{RSAHEADER:pgpsig}' libguestfs-devel)"
  source_signature="$(rpm -qp --qf '%{RSAHEADER:pgpsig}' "$stage/$source_rpm")"
  [[ $binary_signature =~ Key\ ID\ ([[:xdigit:]]{16})$ ]] || {
    echo "installed libguestfs has no recognizable distribution signing key" >&2
    exit 1
  }
  distro_key="${BASH_REMATCH[1]}"
  [[ $devel_signature =~ Key\ ID\ $distro_key$ && $source_signature =~ Key\ ID\ $distro_key$ ]] || {
    echo "libguestfs source and devel packages must use the installed runtime's signing key" >&2
    exit 1
  }
  mkdir -p "$stage/rpmbuild" "$stage/source" "$stage/binding"
  rpm -i --define "_topdir $stage/rpmbuild" "$stage/$source_rpm"
  source_tar="$stage/rpmbuild/SOURCES/libguestfs-$version.tar.gz"
  [[ -f $source_tar ]] || {
    echo "signed source RPM lacks libguestfs-$version.tar.gz" >&2
    exit 1
  }
  tar -xzf "$source_tar" -C "$stage/source"
  source_dir="$stage/source/libguestfs-$version"
  cp -- "$source_dir/python/guestfs.py" "$source_dir/python/actions.h" \
    "$source_dir/python/actions-"{0..6}.c "$source_dir/python/handle.c" \
    "$source_dir/python/module.c" "$source_dir/python/structs.c" \
    "$source_dir/common/utils/stringlists-utils.c" \
    "$source_dir/common/utils/guestfs-stringlists-utils.h" "$stage/binding/"
  : >"$stage/binding/config.h"
  flags_text="$(python3.14-config --cflags)"
  read -r -a python_flags <<<"$flags_text"
  suffix="$(python3.14 -P -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')"
  [[ $suffix =~ ^\.cpython-314-[[:alnum:]_.-]+\.so$ ]] || {
    echo "unexpected Python 3.14 extension suffix: $suffix" >&2
    exit 1
  }
  (
    cd "$stage/binding"
    gcc -shared -fPIC "${python_flags[@]}" -I. actions-{0..6}.c \
      handle.c module.c structs.c stringlists-utils.c -lguestfs -o "libguestfsmod$suffix"
  )
  verify_binding "$stage/binding"
  chown -R root:root "$stage/binding"
  chmod 0755 "$stage/binding"
  chmod 0644 "$stage/binding"/*
  mv -T -- "$stage/binding" "$target"
  result=built
fi

site_entry="$(mktemp "$site/.kdive-guestfs.XXXXXXXX")"
trap 'rm -f -- "$site_entry"; if [[ ${stage:-} ]]; then rm -rf -- "$stage"; fi' EXIT
printf '%s\n' "$target" >"$site_entry"
chmod 0644 "$site_entry"
mv -fT -- "$site_entry" "$site/kdive-guestfs.pth"
site_entry=""
env -u PYTHONPATH python3.14 -P - "$target" <<'PY' || {
import sys
from pathlib import Path
import guestfs

if Path(guestfs.__file__).resolve().parent != Path(sys.argv[1]).resolve():
    raise SystemExit('Python 3.14 imported guestfs from the wrong directory')
guestfs.GuestFS().close()
PY
  echo 'Python 3.14 cannot import the published guestfs binding' >&2
  exit 1
}
printf 'guestfs binding %s from %s\n' "$result" "$source_rpm"
