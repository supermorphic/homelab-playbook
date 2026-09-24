#!/usr/bin/env bash

# Cluster-wide serialization for disruptive transactions. Acquisition and renewal use
# resourceVersion-guarded create/replace operations; release succeeds only while the
# Lease still names this run as its holder.

TEST_LEASE_NAMESPACE="${TEST_LEASE_NAMESPACE:-flux-system}"
TEST_LEASE_NAME="${TEST_LEASE_NAME:-homelab-test-run-lock}"
TEST_LEASE_DURATION_SECONDS="${TEST_LEASE_DURATION_SECONDS:-90}"
TEST_LEASE_RENEW_INTERVAL_SECONDS="${TEST_LEASE_RENEW_INTERVAL_SECONDS:-30}"
TEST_LEASE_RENEW_PID=''

lease_kubectl() {
  local kubeconfig="$1"
  shift
  "${TEST_LEASE_KUBECTL:-kubectl}" --kubeconfig "$kubeconfig" "$@"
}

lease_now() {
  # coordination.k8s.io Lease timestamps use Kubernetes MicroTime, whose API
  # decoder requires exactly six fractional digits.
  date -u +%Y-%m-%dT%H:%M:%S.000000Z
}

lease_timestamp_epoch() {
  local timestamp="$1"
  if [[ "$timestamp" == *.*Z ]]; then
    timestamp="${timestamp%%.*}Z"
  fi
  if date -u -d "$timestamp" +%s >/dev/null 2>&1; then
    date -u -d "$timestamp" +%s
  else
    date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$timestamp" +%s
  fi
}

lease_is_expired() {
  local lease_json="$1"
  local renew_time duration renew_epoch now_epoch
  renew_time="$(yq -r '.spec.renewTime // .spec.acquireTime // ""' - <<<"$lease_json")"
  duration="$(yq -r '.spec.leaseDurationSeconds // 0' - <<<"$lease_json")"
  [[ -n "$renew_time" && "$duration" =~ ^[0-9]+$ && "$duration" -gt 0 ]] || return 0
  renew_epoch="$(lease_timestamp_epoch "$renew_time" 2>/dev/null)" || return 0
  now_epoch="$(date -u +%s)"
  (( now_epoch >= renew_epoch + duration ))
}

lease_manifest() {
  local holder="$1"
  local now="$2"
  local resource_version="${3:-}"
  local manifest
  manifest="$(HOLDER="$holder" \
  NOW="$now" \
  LEASE_NAME="$TEST_LEASE_NAME" \
  LEASE_NAMESPACE="$TEST_LEASE_NAMESPACE" \
  LEASE_DURATION="$TEST_LEASE_DURATION_SECONDS" \
    yq --null-input --output-format json '{
      "apiVersion": "coordination.k8s.io/v1",
      "kind": "Lease",
      "metadata": {
        "name": strenv(LEASE_NAME),
        "namespace": strenv(LEASE_NAMESPACE)
      },
      "spec": {
        "holderIdentity": strenv(HOLDER),
        "leaseDurationSeconds": (strenv(LEASE_DURATION) | tonumber),
        "acquireTime": strenv(NOW),
        "renewTime": strenv(NOW)
      }
    }')"
  if [[ -n "$resource_version" ]]; then
    RESOURCE_VERSION="$resource_version" \
      yq '.metadata.resourceVersion = strenv(RESOURCE_VERSION)' <<<"$manifest"
  else
    printf '%s\n' "$manifest"
  fi
}

acquire_test_lease() {
  local kubeconfig="$1"
  local holder="$2"
  local attempts="${3:-5}"
  local lease_json existing_holder resource_version now operation_error=''

  [[ "$holder" =~ ^[a-zA-Z0-9_.:-]+$ ]] || {
    echo "Unsafe test Lease holder identity: $holder" >&2
    return 2
  }
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if lease_json="$(lease_kubectl "$kubeconfig" --namespace "$TEST_LEASE_NAMESPACE" \
      get lease "$TEST_LEASE_NAME" --output json 2>/dev/null)"; then
      existing_holder="$(yq -r '.spec.holderIdentity // ""' - <<<"$lease_json")"
      if [[ -n "$existing_holder" && "$existing_holder" != "$holder" ]] &&
        ! lease_is_expired "$lease_json"; then
        echo "State-changing test Lease is held by '$existing_holder'." >&2
        return 1
      fi
      resource_version="$(yq -r '.metadata.resourceVersion // ""' - <<<"$lease_json")"
      [[ -n "$resource_version" ]] || return 1
      now="$(lease_now)"
      if operation_error="$(
        lease_manifest "$holder" "$now" "$resource_version" |
          lease_kubectl "$kubeconfig" replace --filename - 2>&1
      )"; then
        return 0
      fi
    else
      now="$(lease_now)"
      if operation_error="$(
        lease_manifest "$holder" "$now" |
          lease_kubectl "$kubeconfig" create --filename - 2>&1
      )"; then
        return 0
      fi
    fi
  done
  [[ -z "$operation_error" ]] || echo "$operation_error" >&2
  echo "Could not acquire test Lease $TEST_LEASE_NAMESPACE/$TEST_LEASE_NAME." >&2
  return 1
}

renew_test_lease() {
  local kubeconfig="$1"
  local holder="$2"
  local lease_json existing_holder resource_version now
  lease_json="$(lease_kubectl "$kubeconfig" --namespace "$TEST_LEASE_NAMESPACE" \
    get lease "$TEST_LEASE_NAME" --output json)" || return 1
  existing_holder="$(yq -r '.spec.holderIdentity // ""' - <<<"$lease_json")"
  [[ "$existing_holder" == "$holder" ]] || return 1
  resource_version="$(yq -r '.metadata.resourceVersion // ""' - <<<"$lease_json")"
  [[ -n "$resource_version" ]] || return 1
  now="$(lease_now)"
  HOLDER="$holder" NOW="$now" RESOURCE_VERSION="$resource_version" \
    yq --output-format json '
      .metadata.resourceVersion = strenv(RESOURCE_VERSION) |
      .spec.holderIdentity = strenv(HOLDER) |
      .spec.renewTime = strenv(NOW)
    ' <<<"$lease_json" |
    lease_kubectl "$kubeconfig" replace --filename - >/dev/null
}

verify_test_lease_holder() {
  local kubeconfig="$1"
  local holder="$2"
  local lease_json existing_holder

  [[ "$holder" =~ ^[a-zA-Z0-9_.:-]+$ ]] || {
    echo "Unsafe test Lease holder identity: $holder" >&2
    return 2
  }
  lease_json="$(lease_kubectl "$kubeconfig" --namespace "$TEST_LEASE_NAMESPACE" \
    get lease "$TEST_LEASE_NAME" --output json)" || return 1
  existing_holder="$(yq -r '.spec.holderIdentity // ""' - <<<"$lease_json")"
  [[ "$existing_holder" == "$holder" ]] || {
    echo "Expected test Lease holder '$holder', found '${existing_holder:-<none>}'." >&2
    return 1
  }
  if lease_is_expired "$lease_json"; then
    echo "Test Lease held by '$holder' is expired." >&2
    return 1
  fi
  return 0
}

start_test_lease_renewal() {
  local kubeconfig="$1"
  local holder="$2"
  local failure_marker="$3"
  (
    while "${TEST_LEASE_SLEEP:-sleep}" "$TEST_LEASE_RENEW_INTERVAL_SECONDS"; do
      if ! renew_test_lease "$kubeconfig" "$holder"; then
        : >"$failure_marker"
        exit 1
      fi
    done
  ) &
  TEST_LEASE_RENEW_PID="$!"
}

stop_test_lease_renewal() {
  if [[ -n "$TEST_LEASE_RENEW_PID" ]]; then
    kill "$TEST_LEASE_RENEW_PID" 2>/dev/null || true
    wait "$TEST_LEASE_RENEW_PID" 2>/dev/null || true
    TEST_LEASE_RENEW_PID=''
  fi
}

release_test_lease() {
  local kubeconfig="$1"
  local holder="$2"
  local lease_json existing_holder resource_version now
  stop_test_lease_renewal
  lease_json="$(lease_kubectl "$kubeconfig" --namespace "$TEST_LEASE_NAMESPACE" \
    get lease "$TEST_LEASE_NAME" --output json)" || return 1
  existing_holder="$(yq -r '.spec.holderIdentity // ""' - <<<"$lease_json")"
  [[ "$existing_holder" == "$holder" ]] || {
    echo "Refusing to release test Lease now held by '$existing_holder'." >&2
    return 1
  }
  resource_version="$(yq -r '.metadata.resourceVersion // ""' - <<<"$lease_json")"
  now="$(lease_now)"
  NOW="$now" RESOURCE_VERSION="$resource_version" \
    yq --output-format json '
      .metadata.resourceVersion = strenv(RESOURCE_VERSION) |
      .spec.holderIdentity = null |
      .spec.renewTime = strenv(NOW)
    ' <<<"$lease_json" |
    lease_kubectl "$kubeconfig" replace --filename - >/dev/null
}
