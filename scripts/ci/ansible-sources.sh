#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

candidate_manifest="$(mktemp)"
trap 'rm -f -- "$candidate_manifest"' EXIT

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
  tests/fixtures/vault/playbook.yml > "$candidate_manifest"

ansible_sources=()
while IFS= read -r -d '' candidate_source; do
  if [[ -L "$candidate_source" ]]; then
    printf 'refusing Ansible source symlink: %s\n' "$candidate_source" >&2
    exit 1
  fi
  case "$candidate_source" in
    inventory/frozen/k3s/group_vars/k3s_cluster/vault.yml) continue ;;
    inventory/production/group_vars/pihole/vault.yml) continue ;;
  esac
  [[ -f "$candidate_source" ]] || continue

  ansible_sources+=("$candidate_source")
done < "$candidate_manifest"

if ((${#ansible_sources[@]} == 0)); then
  printf '%s\n' 'no explicit Ansible sources were discovered' >&2
  exit 1
fi

printf '%s\0' "${ansible_sources[@]}"
