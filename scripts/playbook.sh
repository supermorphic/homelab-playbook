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
uv run --frozen --no-sync python scripts/dependencies.py verify
exec uv run --frozen --no-sync ansible-playbook \
  -i "$inventory_path" \
  "$playbook_path" \
  "$@"
