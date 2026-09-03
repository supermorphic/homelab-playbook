#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

usage() {
  cat <<'EOF'
Usage:
  mise run playbook -- <playbook> <action> <inventory> [ansible-args...]

Available playbooks:
EOF
  find "$repo_root/playbooks" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; | sort
}

error() {
  echo "Error: $1" >&2
  exit 2
}

valid_component() {
  [[ -n "$1" && "$1" != /* && "$1" != *"/"* && "$1" != '.' && "$1" != '..' ]]
}

if [[ $# -eq 0 ]]; then
  usage
  exit 0
fi

playbook="$1"
valid_component "$playbook" || error "invalid playbook selector: $playbook"
playbook_dir="$repo_root/playbooks/$playbook"
[[ -d "$playbook_dir" ]] || error "unknown playbook: $playbook"

if [[ $# -eq 1 ]]; then
  echo "Available actions for playbook '$playbook':"
  find "$playbook_dir" -mindepth 1 -maxdepth 1 -type f -name '*.yml' -exec basename {} .yml \; | sort
  exit 0
fi

action="$2"
valid_component "$action" || error "invalid action selector: $action"
playbook_path="$playbook_dir/$action.yml"
[[ -f "$playbook_path" ]] || error "unknown action for $playbook: $action"

if [[ $# -eq 2 ]]; then
  cat <<'EOF'
Available inventories:
production
staging
frozen/k3s
EOF
  exit 0
fi

inventory="$3"
case "$inventory" in
  production|staging|frozen/k3s) ;;
  *) error "unknown inventory: $inventory" ;;
esac
inventory_path="$repo_root/inventory/$inventory"
[[ -f "$inventory_path" || -d "$inventory_path" ]] || error "inventory is unavailable: $inventory"

shift 3

guarded_os_action=false
if [[ "$playbook" == 'os' && "$action" != 'inspect' ]]; then
  guarded_os_action=true
  for argument in "$@"; do
    case "$argument" in
      -k|-K|--ask-pass|--ask-become-pass|--step|\
      --connection-password-file|--conn-pass-file|\
      --become-password-file|--become-pass-file|\
      --start-at-task|-t|--tags|--skip-tags|\
      --connection-password-file=*|--conn-pass-file=*|\
      --become-password-file=*|--become-pass-file=*|\
      --start-at-task=*|-t?*|--tags=*|--skip-tags=*)
        error "password credentials and task-selection controls are not allowed for mutating OS actions"
        ;;
    esac
  done
fi

uv run --frozen --no-sync python scripts/dependencies.py verify

if [[ "$guarded_os_action" == true ]]; then
  effective_config="$(
    uv run --frozen --no-sync ansible-config dump --only-changed
  )"
  while IFS= read -r config_line; do
    case "$config_line" in
      DEFAULT_ASK_PASS*' = True'|\
      DEFAULT_BECOME_ASK_PASS*' = True'|\
      CONNECTION_PASSWORD_FILE*|\
      BECOME_PASSWORD_FILE*|\
      TAGS_RUN*|\
      TAGS_SKIP*)
        error "effective Ansible configuration enables password credentials or task selection for a mutating OS action"
        ;;
    esac
  done <<<"$effective_config"
fi

exec uv run --frozen --no-sync ansible-playbook \
  -i "$inventory_path" \
  "$playbook_path" \
  "$@"
