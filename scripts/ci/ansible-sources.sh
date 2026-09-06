#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

validate_vault_source() {
  local vault_source="$1"
  local vault_header=
  # The Vault marker is literal; shell expansion would change the format check.
  # shellcheck disable=SC2016
  local vault_header_pattern='^\$ANSIBLE_VAULT;1\.1;AES256$|^\$ANSIBLE_VAULT;1\.2;AES256;[[:alnum:]_.-]+$'

  if [[ ! -f "$vault_source" || -L "$vault_source" ]]; then
    printf 'registered Ansible Vault source is not a regular file: %s\n' \
      "$vault_source" >&2
    exit 1
  fi
  if ! IFS= read -r vault_header < "$vault_source"; then
    printf 'registered Ansible Vault source has no header: %s\n' \
      "$vault_source" >&2
    exit 1
  fi
  if [[ ! "$vault_header" =~ $vault_header_pattern ]]; then
    printf 'registered Ansible Vault source has an invalid header: %s\n' \
      "$vault_source" >&2
    exit 1
  fi
}

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
  candidate_prefix=
  candidate_remainder="$candidate_source"
  while :; do
    candidate_component="${candidate_remainder%%/*}"
    if [[ -n "$candidate_prefix" ]]; then
      candidate_prefix+="/$candidate_component"
    else
      candidate_prefix="$candidate_component"
    fi
    if [[ -L "$candidate_prefix" ]]; then
      printf 'refusing Ansible source symlink: %s\n' "$candidate_source" >&2
      exit 1
    fi
    [[ "$candidate_remainder" == */* ]] || break
    candidate_remainder="${candidate_remainder#*/}"
  done
  case "$candidate_source" in
    inventory/frozen/k3s/group_vars/k3s_cluster/vault.yml | \
      inventory/production/group_vars/os_managed/vault.yml | \
      inventory/staging/group_vars/semaphore/vault.yml)
      validate_vault_source "$candidate_source"
      continue
      ;;
  esac
  [[ -f "$candidate_source" ]] || continue

  ansible_sources+=("$candidate_source")
done < "$candidate_manifest"

if ((${#ansible_sources[@]} == 0)); then
  printf '%s\n' 'no explicit Ansible sources were discovered' >&2
  exit 1
fi

printf '%s\0' "${ansible_sources[@]}"
