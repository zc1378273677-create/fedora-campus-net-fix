#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if (( $# == 0 )); then
  set -- --apply --open
fi
exec python3 ./campus-net-fix.py "$@"
