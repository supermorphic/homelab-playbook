#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
test_root="$(mktemp -d)"
fake_bin="$test_root/bin"
mise_log="$test_root/mise.log"
mise_cwd_log="$test_root/mise.cwd"
uv_log="$test_root/uv.log"
mkdir -p "$fake_bin" "$test_root/outside"

cleanup() {
  rm -rf "$test_root"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains() {
  local needle="$1"
  local haystack="$2"
  [[ "$haystack" == *"$needle"* ]] || fail "expected output to contain: $needle"
}

assert_status() {
  local expected="$1"
  shift
  set +e
  "$@" >/dev/null 2>&1
  local actual=$?
  set -e
  [[ "$actual" -eq "$expected" ]] || fail "expected exit $expected, got $actual: $*"
}

assert_file_equals() {
  local expected="$1"
  local actual="$2"
  diff -u "$expected" "$actual" || fail "unexpected arguments in $actual"
}

cat >"$fake_bin/mise" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" >"$FAKE_MISE_LOG"
pwd >"$FAKE_MISE_CWD_LOG"
EOF
chmod +x "$fake_bin/mise"

cat >"$fake_bin/uv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" >>"$FAKE_UV_LOG"
printf '%s\n' '---' >>"$FAKE_UV_LOG"
if [[ "$*" == *'ansible-config dump --only-changed'* ]]; then
  printf '%s\n' "${FAKE_ANSIBLE_CONFIG_DUMP:-}"
fi
EOF
chmod +x "$fake_bin/uv"

output="$("$repo_root/scripts/playbook.sh")"
assert_contains 'Usage:' "$output"
assert_contains 'Available playbooks:' "$output"
assert_contains 'pihole' "$output"

output="$("$repo_root/scripts/playbook.sh" pihole)"
assert_contains "Available actions for playbook 'pihole':" "$output"
assert_contains 'install' "$output"
assert_contains 'update' "$output"

output="$("$repo_root/scripts/playbook.sh" pihole update)"
assert_contains 'Available inventories:' "$output"
assert_contains 'production' "$output"
assert_contains 'staging' "$output"
assert_contains 'frozen/k3s' "$output"

: >"$uv_log"
assert_status 2 "$repo_root/scripts/playbook.sh" unknown
[[ ! -s "$uv_log" ]] || fail 'unknown playbook invoked uv'
assert_status 2 "$repo_root/scripts/playbook.sh" pihole unknown
[[ ! -s "$uv_log" ]] || fail 'unknown action invoked uv'
assert_status 2 "$repo_root/scripts/playbook.sh" pihole update unknown
[[ ! -s "$uv_log" ]] || fail 'unknown inventory invoked uv'

(cd "$test_root/outside" && \
  PATH="$fake_bin:$PATH" FAKE_MISE_LOG="$mise_log" \
  FAKE_MISE_CWD_LOG="$mise_cwd_log" \
  "$repo_root/run-playbook" pihole update production --limit p1 --check -vv)
printf '%s\n' \
  run \
  playbook \
  -- \
  pihole \
  update \
  production \
  --limit \
  p1 \
  --check \
  -vv >"$test_root/expected-mise.log"
assert_file_equals "$test_root/expected-mise.log" "$mise_log"
assert_file_equals <(printf '%s\n' "$repo_root") "$mise_cwd_log"

: >"$uv_log"
(cd "$test_root/outside" && \
  PATH="$fake_bin:$PATH" FAKE_UV_LOG="$uv_log" \
  "$repo_root/scripts/playbook.sh" pihole update production --limit p1 --check -vv)
printf '%s\n' \
  run \
  --frozen \
  --no-sync \
  python \
  scripts/dependencies.py \
  verify \
  --- \
  run \
  --frozen \
  --no-sync \
  ansible-playbook \
  -i \
  "$repo_root/inventory/production" \
  "$repo_root/playbooks/pihole/update.yml" \
  --limit \
  p1 \
  --check \
  -vv \
  --- >"$test_root/expected-uv.log"
assert_file_equals "$test_root/expected-uv.log" "$uv_log"

for unsafe_args in \
  '-k' \
  '--ask-pass' \
  '--connection-password-file /tmp/connection-password' \
  '--conn-pass-file=/tmp/connection-password' \
  '-K' \
  '--ask-become-pass' \
  '--become-password-file /tmp/become-password' \
  '--become-pass-file=/tmp/become-password' \
  '--start-at-task Run-the-update' \
  '--tags update' \
  '--skip-tags preflight' \
  '--step'; do
  : >"$uv_log"
  read -r -a unsafe_argv <<<"$unsafe_args"
  assert_status 2 env \
    PATH="$fake_bin:$PATH" \
    FAKE_UV_LOG="$uv_log" \
    "$repo_root/scripts/playbook.sh" \
    os maintain production "${unsafe_argv[@]}"
  [[ ! -s "$uv_log" ]] || fail "unsafe OS argument invoked uv: $unsafe_args"
done

for unsafe_config in \
  'DEFAULT_ASK_PASS(env: ANSIBLE_ASK_PASS) = True' \
  'DEFAULT_BECOME_ASK_PASS(env: ANSIBLE_BECOME_ASK_PASS) = True' \
  'CONNECTION_PASSWORD_FILE(env: ANSIBLE_CONNECTION_PASSWORD_FILE) = /tmp/connection-password' \
  'BECOME_PASSWORD_FILE(env: ANSIBLE_BECOME_PASSWORD_FILE) = /tmp/become-password' \
  "TAGS_RUN(env: ANSIBLE_RUN_TAGS) = ['update']" \
  "TAGS_SKIP(env: ANSIBLE_SKIP_TAGS) = ['preflight']"; do
  : >"$uv_log"
  assert_status 2 env \
    PATH="$fake_bin:$PATH" \
    FAKE_UV_LOG="$uv_log" \
    FAKE_ANSIBLE_CONFIG_DUMP="$unsafe_config" \
    "$repo_root/scripts/playbook.sh" os maintain production --check
  if rg -q '^ansible-playbook$' "$uv_log"; then
    fail "unsafe effective Ansible configuration invoked ansible-playbook: $unsafe_config"
  fi
done

: >"$uv_log"
(cd "$test_root/outside" && \
  PATH="$fake_bin:$PATH" FAKE_UV_LOG="$uv_log" \
  "$repo_root/scripts/playbook.sh" \
  os provision production --limit nuc4 --ask-vault-pass)
printf '%s\n' \
  run \
  --frozen \
  --no-sync \
  python \
  scripts/dependencies.py \
  verify \
  --- \
  run \
  --frozen \
  --no-sync \
  ansible-config \
  dump \
  --only-changed \
  --- \
  run \
  --frozen \
  --no-sync \
  ansible-playbook \
  -i \
  "$repo_root/inventory/production" \
  "$repo_root/playbooks/os/provision.yml" \
  --limit \
  nuc4 \
  --ask-vault-pass \
  --- >"$test_root/expected-uv.log"
assert_file_equals "$test_root/expected-uv.log" "$uv_log"

echo 'PASS: run-playbook operator contract'
