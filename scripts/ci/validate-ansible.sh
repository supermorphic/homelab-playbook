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
# Isolate validation from operator plugins and credential retrieval commands.
export ANSIBLE_VARS_ENABLED=host_group_vars
export SOPS_ANSIBLE_AWX_DISABLE_VARS_PLUGIN_TEMPORARILY=true
for credential_variable in $(compgen -e); do
  case "$credential_variable" in
    SOPS_AGE_*|ANSIBLE_SOPS_*|ANSIBLE_VAULT_*|ANSIBLE_ASK_VAULT_PASS)
      unset "$credential_variable"
      ;;
  esac
done

uv run --frozen --no-sync python scripts/dependencies.py verify
uv run --frozen --no-sync python scripts/secrets/validate.py
bash tests/ansible/inventory-test.sh
mise run test:secrets
mise run test:tls
mise run test:semaphore -- unit
uv run --frozen --no-sync python -m unittest discover -s tests/ansible -p 'test_*.py' -v

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
