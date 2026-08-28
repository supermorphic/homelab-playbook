#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

unset ANSIBLE_VAULT_IDENTITY_LIST ANSIBLE_VAULT_PASSWORD_FILE

uv run --frozen --no-sync python scripts/dependencies.py verify
bash tests/ansible/inventory-test.sh
bash tests/ansible/vault-test.sh
uv run --frozen --no-sync python -m unittest -v tests/ansible/test_source_contracts.py
uv run --frozen --no-sync ansible-lint --profile production
