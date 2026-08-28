#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

while IFS= read -r -d '' candidate_source; do
  [[ -f "$candidate_source" ]] || continue
  case "$candidate_source" in
    inventory/frozen/k3s/group_vars/k3s_cluster/vault.yml) continue ;;
    inventory/production/group_vars/pihole/vault.yml) continue ;;
  esac

  printf '%s\0' "$candidate_source"
done < <(
  git ls-files -z --cached --others --exclude-standard -- \
    ':(glob)playbooks/**/*.yml' \
    ':(glob)playbooks/**/*.yaml' \
    ':(glob)roles/**/*.yml' \
    ':(glob)roles/**/*.yaml' \
    ':(glob)overrides/ansible-galaxy/**/*.yml' \
    ':(glob)overrides/ansible-galaxy/**/*.yaml' \
    ':(glob)inventory/**/*.yml' \
    ':(glob)inventory/**/*.yaml' \
    requirements.yml \
    tests/fixtures/vault/playbook.yml
)
