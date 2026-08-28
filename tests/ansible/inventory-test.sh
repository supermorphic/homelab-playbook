#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
inventory_test_root="$(mktemp -d)"
trap 'rm -rf -- "$inventory_test_root"' EXIT

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
assert "k3s_cluster" in frozen_k3s, (
    "frozen/k3s inventory must contain the k3s_cluster group"
)
assert not staging.get("_meta", {}).get("hostvars", {}), (
    "staging inventory must not contain named hosts"
)
PY
