#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

uv sync --frozen
mkdir -p .ansible/roles
uv run --frozen --no-sync ansible-galaxy role install \
  --role-file requirements.yml \
  --roles-path .ansible/roles \
  --force
install -m 0644 \
  overrides/ansible-galaxy/l3d.unbound/tasks/configure.yml \
  .ansible/roles/l3d.unbound/tasks/configure.yml
uv run --frozen --no-sync python scripts/dependencies.py write-fingerprint
uv run --frozen --no-sync python scripts/dependencies.py verify
