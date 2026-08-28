#!/usr/bin/env bash

set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
vault_test_root="$(mktemp -d)"
trap 'rm -rf -- "$vault_test_root"' EXIT

password_file="$vault_test_root/password"
vars_file="$vault_test_root/vars.yml"
view_file="$vault_test_root/view.yml"
ansible_config="$vault_test_root/ansible.cfg"

umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > "$password_file"
chmod 0600 "$password_file"
printf '%s\n' 'fixture_secret: ephemeral-ci-value' > "$vars_file"
printf '%s\n' '[defaults]' > "$ansible_config"
export ANSIBLE_CONFIG="$ansible_config"
unset ANSIBLE_VAULT_IDENTITY_LIST ANSIBLE_VAULT_PASSWORD_FILE

uv run --frozen --no-sync ansible-vault encrypt \
  --vault-password-file "$password_file" \
  "$vars_file"
uv run --frozen --no-sync ansible-vault view \
  --vault-password-file "$password_file" \
  "$vars_file" > "$view_file"

grep -Fqx 'fixture_secret: ephemeral-ci-value' "$view_file"

uv run --frozen --no-sync ansible-playbook \
  --inventory 'localhost,' \
  --connection local \
  --vault-password-file "$password_file" \
  --extra-vars "@$vars_file" \
  "$repository_root/tests/fixtures/vault/playbook.yml"
