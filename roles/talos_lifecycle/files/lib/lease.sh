#!/usr/bin/env bash

TEST_LEASE_NAMESPACE=flux-system
TEST_LEASE_NAME=homelab-test-run-lock
TEST_LEASE_DURATION_SECONDS=90
TEST_LEASE_RENEW_INTERVAL_SECONDS=30
export TEST_LEASE_NAMESPACE TEST_LEASE_NAME TEST_LEASE_DURATION_SECONDS
export TEST_LEASE_RENEW_INTERVAL_SECONDS

lease_expiry_epoch() {
  local renew_time="$1" duration="$2"
  python3 - "$renew_time" "$duration" <<'PY'
import datetime as dt
import sys
value = dt.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
print(int(value.timestamp()) + int(sys.argv[2]))
PY
}

lease_record_valid_holder() {
  local document="$1" expected="$2"
  python3 - "$document" "$expected" "$TEST_LEASE_DURATION_SECONDS" <<'PY'
import json
import sys
value = json.loads(sys.argv[1])
spec = value.get("spec", {})
raise SystemExit(0 if spec.get("holderIdentity") == sys.argv[2]
                 and spec.get("leaseDurationSeconds") == int(sys.argv[3])
                 and isinstance(spec.get("renewTime"), str) else 1)
PY
}

require_current_lease() {
  local kubeconfig="$1" context="$2" holder="$3" kubectl_bin="${4:-kubectl}"
  local value
  value="$($kubectl_bin --kubeconfig "$kubeconfig" --context "$context" \
    --namespace "$TEST_LEASE_NAMESPACE" get lease "$TEST_LEASE_NAME" -o json)" || return
  lease_record_valid_holder "$value" "$holder"
}
