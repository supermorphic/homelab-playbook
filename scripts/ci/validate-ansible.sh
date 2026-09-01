#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

ansible_validation_root="$(mktemp -d)"
trap 'rm -rf -- "$ansible_validation_root"' EXIT

ansible_config="$ansible_validation_root/ansible.cfg"
lint_output="$ansible_validation_root/ansible-lint.txt"
printf '[defaults]\nroles_path = %s/.ansible/roles:%s/roles\ncollections_path = %s/.ansible/collections\n' \
  "$repo_root" "$repo_root" "$repo_root" > "$ansible_config"
export ANSIBLE_CONFIG="$ansible_config"
unset \
  ANSIBLE_ASK_VAULT_PASS \
  ANSIBLE_VAULT_ENCRYPT_IDENTITY \
  ANSIBLE_VAULT_ENCRYPT_SALT \
  ANSIBLE_VAULT_IDENTITY \
  ANSIBLE_VAULT_IDENTITY_LIST \
  ANSIBLE_VAULT_ID_MATCH \
  ANSIBLE_VAULT_PASSWORD_FILE

uv run --frozen --no-sync python scripts/dependencies.py verify
bash tests/ansible/inventory-test.sh
bash tests/ansible/vault-test.sh
uv run --frozen --no-sync python -m unittest -v \
  tests/ansible/test_ansible_sources.py \
  tests/ansible/test_molecule_contract.py \
  tests/ansible/test_os_baseline_verify_controls.py \
  tests/ansible/test_platform_control_contracts.py \
  tests/ansible/test_source_contracts.py

ansible_source_manifest="$ansible_validation_root/ansible-sources.bin"
bash scripts/ci/ansible-sources.sh > "$ansible_source_manifest"

ansible_sources=()
while IFS= read -r -d '' ansible_source; do
  ansible_sources+=("$ansible_source")
done < "$ansible_source_manifest"

if ((${#ansible_sources[@]} == 0)); then
  printf '%s\n' 'no explicit Ansible sources are available for ansible-lint' >&2
  exit 1
fi

set +e
NO_COLOR=1 uv run --frozen --no-sync ansible-lint --profile production \
  "${ansible_sources[@]}" > "$lint_output" 2>&1
lint_status=$?
set -e

cat "$lint_output"
if ((lint_status != 0)); then
  exit "$lint_status"
fi
if grep -Eq '(^|[[:space:]])WARNING[[:space:]]' "$lint_output"; then
  printf '%s\n' 'ansible-lint emitted warnings' >&2
  exit 1
fi
