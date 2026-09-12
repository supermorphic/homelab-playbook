#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
inventory_test_root="$(mktemp -d)"
trap 'rm -rf -- "$inventory_test_root"' EXIT

printf '%s\n' '[defaults]' > "$inventory_test_root/ansible.cfg"
export ANSIBLE_CONFIG="$inventory_test_root/ansible.cfg"
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

mkdir -p \
  "$inventory_test_root/production/group_vars/os_managed" \
  "$inventory_test_root/production/group_vars/podman" \
  "$inventory_test_root/production/group_vars/reverse_proxy" \
  "$inventory_test_root/production/group_vars/tls_issuer" \
  "$inventory_test_root/production/host_vars/nuc4" \
  "$inventory_test_root/staging/group_vars/semaphore" \
  "$inventory_test_root/staging-semaphore/group_vars/semaphore" \
  "$inventory_test_root/frozen/k3s/group_vars/k3s_cluster" \
  "$inventory_test_root/frozen/k3s/group_vars/k3s_server"

cp "$repository_root/inventory/production/hosts.yml" \
  "$inventory_test_root/production/hosts.yml"
cp "$repository_root/inventory/production/group_vars/os_managed/vars.yml" \
  "$inventory_test_root/production/group_vars/os_managed/vars.yml"
cp "$repository_root/inventory/production/group_vars/podman/vars.yml" \
  "$inventory_test_root/production/group_vars/podman/vars.yml"
cp "$repository_root/inventory/production/group_vars/reverse_proxy/vars.yml" \
  "$inventory_test_root/production/group_vars/reverse_proxy/vars.yml"
cp "$repository_root/inventory/production/group_vars/tls_issuer/vars.yml" \
  "$inventory_test_root/production/group_vars/tls_issuer/vars.yml"
cp "$repository_root/inventory/production/host_vars/nuc4/vars.yml" \
  "$inventory_test_root/production/host_vars/nuc4/vars.yml"

cp "$repository_root/inventory/staging/hosts.yml" \
  "$inventory_test_root/staging/hosts.yml"
cp "$repository_root/inventory/staging/group_vars/semaphore/vars.yml" \
  "$inventory_test_root/staging/group_vars/semaphore/vars.yml"
cp "$repository_root/inventory/staging/group_vars/semaphore/versions.yml" \
  "$inventory_test_root/staging/group_vars/semaphore/versions.yml"

cp "$repository_root/inventory/staging/group_vars/semaphore/vars.yml" \
  "$inventory_test_root/staging-semaphore/group_vars/semaphore/vars.yml"
cp "$repository_root/inventory/staging/group_vars/semaphore/versions.yml" \
  "$inventory_test_root/staging-semaphore/group_vars/semaphore/versions.yml"
printf '%s\n' \
  '---' \
  'all:' \
  '  children:' \
  '    semaphore:' \
  '      hosts:' \
  '        semaphore-test:' \
  '          ansible_connection: local' \
  > "$inventory_test_root/staging-semaphore/hosts.yml"

cp "$repository_root/inventory/frozen/k3s/hosts.yml" \
  "$inventory_test_root/frozen/k3s/hosts.yml"
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
uv run --frozen --no-sync ansible-inventory \
  --inventory "$inventory_test_root/staging-semaphore" --list \
  > "$inventory_test_root/staging-semaphore.json"

python3 - \
  "$inventory_test_root/production.json" \
  "$inventory_test_root/frozen-k3s.json" \
  "$inventory_test_root/staging.json" \
  "$inventory_test_root/staging-semaphore.json" <<'PY'
import json
import sys


def load_inventory(path):
    with open(path, encoding="utf-8") as inventory_file:
        return json.load(inventory_file)


production = load_inventory(sys.argv[1])
frozen_k3s = load_inventory(sys.argv[2])
staging = load_inventory(sys.argv[3])
staging_semaphore = load_inventory(sys.argv[4])

assert production["os_managed"].get("hosts", []) == ["nuc4"]
assert production["podman"].get("hosts", []) == ["nuc4"]
assert production["reverse_proxy"].get("hosts", []) == ["nuc4"]
assert production["tls_issuer"].get("hosts", []) == ["nuc4"]
assert production["semaphore"].get("hosts", []) == ["nuc4"]
for retired_group in ("servers", "pihole", "ansible"):
    assert retired_group not in production
host_variables = production.get("_meta", {}).get("hostvars", {}).get("nuc4", {})
assert host_variables.get("ansible_user") == "ansible"
assert host_variables.get("host_identity_hostname") == "nuc4"
semaphore_account = {
    "name": "svc-semaphore", "uid": 2001, "gid": 2001,
    "subuid_start": 200000, "subuid_count": 65536,
    "subgid_start": 200000, "subgid_count": 65536,
}
assert host_variables.get("podman_foundation_accounts") == [semaphore_account]
assert host_variables.get("semaphore_foundation_account") == semaphore_account
assert host_variables.get("semaphore_hostname") == "semaphore.infra.supermorphic.com"
assert host_variables.get("semaphore_backend_port") == 18080
assert host_variables.get("semaphore_certificate_name") == "infra"
assert host_variables.get("semaphore_controller_enabled") is False
assert host_variables.get("semaphore_controller_identity_enabled") is False
assert host_variables.get("tls_automation_timer_enabled") is False
assert host_variables.get("tls_automation_issuer_uid") == 2010
assert host_variables.get("tls_automation_issuer_gid") == 2010
assert host_variables.get("reverse_proxy_deferred_certificates") == ["infra"]
assert host_variables.get("reverse_proxy_bind_addresses") == []
assert host_variables.get("reverse_proxy_client_sources") == []
assert host_variables.get("reverse_proxy_routes") == []
for protected_variable in (
    "host_identity_timezone",
    "security_baseline_authorized_keys",
    "security_baseline_management_sources",
):
    assert protected_variable not in host_variables
assert "k3s_cluster" not in production, (
    "production inventory must not contain the k3s_cluster group"
)
assert "k3s_cluster" in frozen_k3s, (
    "frozen/k3s inventory must contain the k3s_cluster group"
)
assert not staging.get("_meta", {}).get("hostvars", {}), (
    "staging inventory must not contain named hosts"
)
semaphore_variables = staging_semaphore.get("_meta", {}).get("hostvars", {}).get(
    "semaphore-test", {}
)
required_semaphore_variables = {
    "db_dump_cron",
    "db_dump_retention",
    "db_dump_target",
    "mysql_version",
    "semaphore_db_name",
    "semaphore_version",
}
missing_semaphore_variables = required_semaphore_variables.difference(
    semaphore_variables
)
assert not missing_semaphore_variables, (
    "synthetic Semaphore staging host is missing retained public variables: "
    f"{sorted(missing_semaphore_variables)}"
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
