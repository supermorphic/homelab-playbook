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

echo 'PASS: run-playbook operator contract'
