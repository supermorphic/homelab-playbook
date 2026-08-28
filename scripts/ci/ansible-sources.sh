#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

while IFS= read -r -d '' tracked_source; do
  case "$tracked_source" in
    inventory/frozen/k3s/group_vars/k3s_cluster/vault.yml) continue ;;
    inventory/production/group_vars/pihole/vault.yml) continue ;;
  esac

  printf '%s\0' "$tracked_source"
done < <(
  git ls-files -z -- \
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
