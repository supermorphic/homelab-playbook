#!/usr/bin/env bash
set -euo pipefail

if ((BASH_VERSINFO[0] < 5)); then
  printf '%s\n' 'Talos lifecycle tests require Bash 5 or newer' >&2
  exit 1
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
export TALOS_LIFECYCLE_KUBE_CONTEXT=fixture
export TALOS_LIFECYCLE_TALOS_CONTEXT=fixture
bash tests/talos/lease-test.sh
bash tests/talos/node-lifecycle-test.sh
uv run --frozen --no-sync python -m unittest discover -s tests/talos -p 'test_*.py' -v
