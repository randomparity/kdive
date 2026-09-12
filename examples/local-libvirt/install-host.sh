#!/usr/bin/env bash
# Compatibility entry point; docs/operating/install.md owns host preparation.
set -euo pipefail

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
cd "$repo_root"
exec just prepare-local-libvirt-host "$@"
