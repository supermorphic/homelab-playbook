#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
inventory_test_root="$(mktemp -d)"
trap 'rm -rf -- "$inventory_test_root"' EXIT

printf '%s\n' '[defaults]' > "$inventory_test_root/ansible.cfg"
export ANSIBLE_CONFIG="$inventory_test_root/ansible.cfg"
unset ANSIBLE_VAULT_IDENTITY_LIST ANSIBLE_VAULT_PASSWORD_FILE

mkdir -p \
  "$inventory_test_root/production/group_vars/pihole" \
  "$inventory_test_root/staging" \
  "$inventory_test_root/frozen/k3s/group_vars/k3s_cluster" \
  "$inventory_test_root/frozen/k3s/group_vars/k3s_server"

cp "$repository_root/inventory/production/hosts.ini" \
  "$inventory_test_root/production/hosts.ini"
cp "$repository_root/inventory/production/group_vars/pihole/vars.yml" \
  "$inventory_test_root/production/group_vars/pihole/vars.yml"

cp "$repository_root/inventory/staging/hosts.yml" \
  "$inventory_test_root/staging/hosts.yml"

cp "$repository_root/inventory/frozen/k3s/hosts.ini" \
  "$inventory_test_root/frozen/k3s/hosts.ini"
cp "$repository_root/inventory/frozen/k3s/group_vars/k3s_cluster/vars.yml" \
  "$inventory_test_root/frozen/k3s/group_vars/k3s_cluster/vars.yml"
cp "$repository_root/inventory/frozen/k3s/group_vars/k3s_cluster/versions.yml" \
  "$inventory_test_root/frozen/k3s/group_vars/k3s_cluster/versions.yml"
cp "$repository_root/inventory/frozen/k3s/group_vars/k3s_server/vars.yml" \
  "$inventory_test_root/frozen/k3s/group_vars/k3s_server/vars.yml"

uv run --frozen --no-sync ansible-inventory \
  --inventory "$inventory_test_root/production" --list \
  > "$inventory_test_root/production.json"
uv run --frozen --no-sync ansible-inventory \
  --inventory "$inventory_test_root/frozen/k3s" --list \
  > "$inventory_test_root/frozen-k3s.json"
uv run --frozen --no-sync ansible-inventory \
  --inventory "$inventory_test_root/staging" --list \
  > "$inventory_test_root/staging.json"

python3 - \
  "$inventory_test_root/production.json" \
  "$inventory_test_root/frozen-k3s.json" \
  "$inventory_test_root/staging.json" <<'PY'
import json
import sys


def load_inventory(path):
    with open(path, encoding="utf-8") as inventory_file:
        return json.load(inventory_file)


production = load_inventory(sys.argv[1])
frozen_k3s = load_inventory(sys.argv[2])
staging = load_inventory(sys.argv[3])

assert "pihole" in production, "production inventory must contain the pihole group"
assert "k3s_cluster" not in production, (
    "production inventory must not contain the k3s_cluster group"
)
pihole_hosts = production["pihole"].get("hosts", [])
assert pihole_hosts, "production inventory must resolve at least one pihole host"
required_pihole_variables = {
    "pihole_dns_list_file",
    "pihole_dnsmasq_listening",
    "pihole_pihole_dns_1",
    "unbound_listen_addresses",
}
for host in pihole_hosts:
    host_variables = production.get("_meta", {}).get("hostvars", {}).get(host, {})
    missing_variables = required_pihole_variables.difference(host_variables)
    assert not missing_variables, (
        f"production pihole host {host} is missing public dependency variables: "
        f"{sorted(missing_variables)}"
    )
assert "k3s_cluster" in frozen_k3s, (
    "frozen/k3s inventory must contain the k3s_cluster group"
)
assert not staging.get("_meta", {}).get("hostvars", {}), (
    "staging inventory must not contain named hosts"
)
PY

playbook_index=0
while IFS= read -r -d '' tracked_playbook; do
  relative_playbook="${tracked_playbook#playbooks/}"
  domain="${relative_playbook%%/*}"
  action_file="${relative_playbook#*/}"
  action="${action_file%.yml}"
  discovery_output="$inventory_test_root/playbook-actions-$playbook_index.txt"

  bash "$repository_root/scripts/playbook.sh" "$domain" > "$discovery_output"
  if ! grep -Fqx -- "$action" "$discovery_output"; then
    printf 'Playbook discovery did not list %s/%s.\n' "$domain" "$action" >&2
    exit 1
  fi

  playbook_index=$((playbook_index + 1))
done < <(
  git -C "$repository_root" ls-files -z -- ':(glob)playbooks/*/*.yml'
)
