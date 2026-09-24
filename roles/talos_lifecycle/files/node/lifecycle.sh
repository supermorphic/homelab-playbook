#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2153,SC2154
set -euo pipefail

lifecycle_node_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$lifecycle_node_dir/../lib/lease.sh"
source "$lifecycle_node_dir/../lib/node-lifecycle-state.sh"
source "$lifecycle_node_dir/common.sh"
source "$lifecycle_node_dir/drain.sh"
source "$lifecycle_node_dir/longhorn.sh"
source "$lifecycle_node_dir/recovery.sh"

lifecycle_talosctl() {
  "${NODE_TALOSCTL:-talosctl}" --context "${TALOS_LIFECYCLE_TALOS_CONTEXT:?}" "$@"
}

verify_expected_node_health() {
  local kubeconfig="$1"
  local nodes_json names states pressures
  nodes_json="$(node_kubectl "$kubeconfig" get nodes --output json)" || return 1
  names="$(yq -r '.items[].metadata.name' - <<<"$nodes_json" | sort)"
  [[ "$names" == $'node-a\nnode-b\nnode-c' ]] || {
    echo 'Expected exactly Kubernetes Nodes node-a, node-b, and node-c.' >&2
    return 1
  }
  states="$(yq -r '
    .items[] |
    .metadata.name + " " +
    ([.status.conditions[]? | select(.type == "Ready") | .status][0] // "Unknown") + " " +
    ((.spec.unschedulable // false) | tostring)
  ' <<<"$nodes_json" | sort)"
  [[ "$states" == $'node-a True false\nnode-b True false\nnode-c True false' ]] || {
    printf 'All established Nodes must be Ready and schedulable:\n%s\n' "$states" >&2
    return 1
  }
  # shellcheck disable=SC2016  # $node is a yq variable.
  pressures="$(yq -r '
    .items[] as $node |
    ["MemoryPressure", "DiskPressure", "PIDPressure"][] as $type |
    {
      "node": $node.metadata.name,
      "type": $type,
      "status": ([$node.status.conditions[]? | select(.type == $type) | .status][0] // "Missing")
    } |
    select(.status != "False") |
    .node + " " + .type + "=" + .status
  ' <<<"$nodes_json")"
  [[ -z "$pressures" ]] || {
    printf 'Node pressure blocks disruption:\n%s\n' "$pressures" >&2
    return 1
  }
}

verify_target_identity() {
  local kubeconfig="$1"
  local talosconfig="$2"
  local node="$3"
  local node_ip="$4"
  local kubernetes_uid hostname
  kubernetes_uid="$(node_kubectl "$kubeconfig" get node "$node" \
    --output jsonpath='{.metadata.uid}')" || return 1
  [[ -n "$kubernetes_uid" ]] || return 1
  hostname="$(lifecycle_talosctl get hostname --nodes "$node_ip" \
    --endpoints "$NODE_CLUSTER_ENDPOINTS" --talosconfig "$talosconfig" --output yaml)" || return 1
  [[ "$(yq -r '.spec.hostname' - <<<"$hostname")" == "$node" ]] || {
    echo "Talos endpoint $node_ip does not identify as $node." >&2
    return 1
  }
}

preflight_kubernetes_drain() {
  local kubeconfig="$1"
  local node="$2"
  local discovery
  discovery="$(drain_kubectl "$kubeconfig" get --raw /api/v1)" || {
    echo 'Cannot read Kubernetes core/v1 API discovery.' >&2
    return 1
  }
  [[ "$(yq -r '[.resources[]? | select(.name == "pods/eviction" and .kind == "Eviction")] | length' - <<<"$discovery")" -eq 1 ]] || {
    echo 'Kubernetes core/v1 does not advertise the pods/eviction resource.' >&2
    return 1
  }
  drain_kubectl "$kubeconfig" drain "$node" \
    --ignore-daemonsets \
    --delete-emptydir-data \
    --dry-run=client \
    --timeout="${NODE_DRAIN_PREFLIGHT_TIMEOUT:-2m}"
}

verify_survivor_capacity() {
  local kubeconfig="$1"
  local node="$2"
  local temp_dir nodes_file pods_file result
  temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/homelab-node-capacity.XXXXXX")"
  nodes_file="$temp_dir/nodes.json"
  pods_file="$temp_dir/pods.json"
  node_kubectl "$kubeconfig" get nodes --output json >"$nodes_file" || {
    rm -rf -- "$temp_dir"
    return 1
  }
  node_kubectl "$kubeconfig" get pods --all-namespaces --output json >"$pods_file" || {
    rm -rf -- "$temp_dir"
    return 1
  }
  "${NODE_PYTHON:-python}" "$lifecycle_node_dir/capacity.py" "$node" "$nodes_file" "$pods_file"
  result="$?"
  rm -rf -- "$temp_dir"
  return "$result"
}

run_disruption_preflight() {
  local kubeconfig="$1"
  local talosconfig="$2"
  local node="$3"
  local node_ip="$4"
  local inventory_file="$5"
  assert_cluster_disruption_admissible "$kubeconfig" || return 1
  verify_expected_node_health "$kubeconfig" || return 1
  verify_target_identity "$kubeconfig" "$talosconfig" "$node" "$node_ip" || return 1
  verify_etcd_recovery "$talosconfig" || return 1
  verify_survivor_capacity "$kubeconfig" "$node" || return 1
  capture_drain_inventory "$kubeconfig" "$node" "$inventory_file" || return 1
  preflight_kubernetes_drain "$kubeconfig" "$node" || return 1
  verify_short_absence_longhorn_safety "$kubeconfig" "$node" || return 1
}

repeat_disruption_safety() {
  local kubeconfig="$1"
  local talosconfig="$2"
  local node="$3"
  local holder="$4"
  require_current_lease "$kubeconfig" "$holder" || return 1
  assert_cluster_disruption_admissible "$kubeconfig" "$node" || return 1
  verify_etcd_recovery "$talosconfig" || return 1
  [[ "$(node_kubectl "$kubeconfig" get node "$node" \
    --output jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" == 'True' ]] || return 1
}

repeat_pre_containment_safety() {
  local kubeconfig="$1"
  local talosconfig="$2"
  local node="$3"
  local holder="$4"
  require_current_lease "$kubeconfig" "$holder" || return 1
  assert_cluster_disruption_admissible "$kubeconfig" || return 1
  verify_etcd_recovery "$talosconfig" || return 1
  [[ "$(node_kubectl "$kubeconfig" get node "$node" \
    --output jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" == 'True' ]] || return 1
}

send_talos_shutdown() {
  local talosconfig="$1"
  local node_ip="$2"
  lifecycle_talosctl shutdown --nodes "$node_ip" --endpoints "$NODE_CLUSTER_ENDPOINTS" \
    --talosconfig "$talosconfig" --force
}

send_talos_reboot() {
  local talosconfig="$1"
  local node_ip="$2"
  lifecycle_talosctl reboot --nodes "$node_ip" --endpoints "$NODE_CLUSTER_ENDPOINTS" \
    --talosconfig "$talosconfig" --wait=false
}

verify_node_offline() {
  local kubeconfig="$1"
  local talosconfig="$2"
  local node="$3"
  local node_ip="$4"
  local attempts="${NODE_OFFLINE_ATTEMPTS:-36}"
  local ready
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    ready="$(node_kubectl "$kubeconfig" get node "$node" \
      --output jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
    if [[ "$ready" != 'True' ]] && ! lifecycle_talosctl version --nodes "$node_ip" \
      --endpoints "$NODE_CLUSTER_ENDPOINTS" --talosconfig "$talosconfig" >/dev/null 2>&1; then
      return 0
    fi
    "${NODE_SLEEP:-sleep}" "${NODE_OFFLINE_POLL_SECONDS:-5}"
  done
  echo "Node $node did not reach verified offline state." >&2
  return 1
}

observe_node_reboot() {
  local kubeconfig="$1"
  local talosconfig="$2"
  local node="$3"
  local node_ip="$4"
  verify_node_offline "$kubeconfig" "$talosconfig" "$node" "$node_ip" || return 1
  local attempts="${NODE_REBOOT_RETURN_ATTEMPTS:-360}"
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    if lifecycle_talosctl version --nodes "$node_ip" --endpoints "$NODE_CLUSTER_ENDPOINTS" \
      --talosconfig "$talosconfig" >/dev/null 2>&1; then
      return 0
    fi
    "${NODE_SLEEP:-sleep}" "${NODE_RECOVERY_POLL_SECONDS:-5}"
  done
  echo "Talos API for $node did not return after reboot." >&2
  return 1
}

run_maintenance_enter_transaction() {
  local kubeconfig="$1" talosconfig="$2" node="$3" node_ip="$4"
  local holder="$5" record="$6" inventory_file="$7"
  repeat_pre_containment_safety "$kubeconfig" "$talosconfig" "$node" "$holder" || return 1
  require_current_lease "$kubeconfig" "$holder" || return 1
  persist_node_containment "$kubeconfig" "$node" "$record" || return 1
  require_current_lease "$kubeconfig" "$holder" || return 1
  apply_longhorn_maintenance_state "$kubeconfig" "$node" "$record" || return 1
  capture_drain_inventory "$kubeconfig" "$node" "$inventory_file" || return 1
  require_current_lease "$kubeconfig" "$holder" || return 1
  perform_kubernetes_drain "$kubeconfig" "$node" || return 1
  wait_for_workload_replacements "$kubeconfig" "$node" "$inventory_file" || return 1
  verify_no_drainable_workloads "$kubeconfig" "$node" || return 1
  evacuate_longhorn_replicas "$kubeconfig" "$node" || return 1
  repeat_disruption_safety "$kubeconfig" "$talosconfig" "$node" "$holder" || return 1
  require_current_lease "$kubeconfig" "$holder" || return 1
  send_talos_shutdown "$talosconfig" "$node_ip" || return 1
  verify_node_offline "$kubeconfig" "$talosconfig" "$node" "$node_ip" || return 1
}

run_reboot_transaction() {
  local kubeconfig="$1" talosconfig="$2" node="$3" node_ip="$4"
  local holder="$5" record="$6" inventory_file="$7"
  repeat_pre_containment_safety "$kubeconfig" "$talosconfig" "$node" "$holder" || return 1
  require_current_lease "$kubeconfig" "$holder" || return 1
  persist_node_containment "$kubeconfig" "$node" "$record" || return 1
  capture_drain_inventory "$kubeconfig" "$node" "$inventory_file" || return 1
  require_current_lease "$kubeconfig" "$holder" || return 1
  perform_kubernetes_drain "$kubeconfig" "$node" || return 1
  wait_for_workload_replacements "$kubeconfig" "$node" "$inventory_file" || return 1
  verify_no_drainable_workloads "$kubeconfig" "$node" || return 1
  verify_short_absence_longhorn_safety "$kubeconfig" "$node" || return 1
  repeat_disruption_safety "$kubeconfig" "$talosconfig" "$node" "$holder" || return 1
  require_current_lease "$kubeconfig" "$holder" || return 1
  send_talos_reboot "$talosconfig" "$node_ip" || return 1
  observe_node_reboot "$kubeconfig" "$talosconfig" "$node" "$node_ip" || return 1
  perform_recovery_acceptance "$kubeconfig" "$talosconfig" "$node" "$node_ip" \
    "$record" "$inventory_file" || return 1
  repeat_disruption_safety "$kubeconfig" "$talosconfig" "$node" "$holder" || return 1
  require_current_lease "$kubeconfig" "$holder" || return 1
  remove_node_containment_and_uncordon "$kubeconfig" "$node" "$record" recovery-accepted || return 1
}

run_maintenance_exit_transaction() {
  local kubeconfig="$1" talosconfig="$2" node="$3" node_ip="$4"
  local holder="$5" record="$6" inventory_file="${7:-}"
  require_current_lease "$kubeconfig" "$holder" || return 1
  assert_cluster_disruption_admissible "$kubeconfig" "$node" || return 1
  perform_recovery_acceptance "$kubeconfig" "$talosconfig" "$node" "$node_ip" \
    "$record" "$inventory_file" || return 1
  repeat_disruption_safety "$kubeconfig" "$talosconfig" "$node" "$holder" || return 1
  require_current_lease "$kubeconfig" "$holder" || return 1
  remove_node_containment_and_uncordon "$kubeconfig" "$node" "$record" recovery-accepted || return 1
}

node_lifecycle_main() (
  [[ "$#" -eq 1 ]] || {
    echo 'Usage: lifecycle.sh PREPARED_JSON' >&2
    return 2
  }
  local prepared_json="$1" action kubeconfig talosconfig holder record kind
  local temp_dir inventory_file renewal_failure status cleanup_status=0
  local lease_acquired=false
  [[ "$prepared_json" == /* && -f "$prepared_json" && ! -L "$prepared_json" ]] || return 2
  action="$(yq -r '.action' "$prepared_json")"
  NODE_NAME="$(yq -r '.node' "$prepared_json")"
  NODE_IP="$(yq -r '.address' "$prepared_json")"
  kubeconfig="$(yq -r '.talos_kubeconfig' "$prepared_json")"
  talosconfig="$(yq -r '.talos_talosconfig' "$prepared_json")"
  holder="$(yq -r '.holder' "$prepared_json")"
  NODE_CLUSTER_ENDPOINTS="$(yq -r '.talos_endpoints | join(",")' "$prepared_json")"
  TALOS_LIFECYCLE_KUBE_CONTEXT="$(yq -r '.talos_kube_context' "$prepared_json")"
  TALOS_LIFECYCLE_TALOS_CONTEXT="$(yq -r '.talos_talos_context' "$prepared_json")"
  NODE_KUBECTL="$(yq -r '.tools.kubectl' "$prepared_json")"
  NODE_TALOSCTL="$(yq -r '.tools.talosctl' "$prepared_json")"
  TEST_LEASE_KUBECTL="$NODE_KUBECTL"
  NODE_LIFECYCLE_KUBECTL="$NODE_KUBECTL"
  NODE_PYTHON="$(yq -r '.tools.python3' "$prepared_json")"
  TALOS_LIFECYCLE_PREPARED_JSON="$prepared_json"
  TALOS_LIFECYCLE_VERIFICATION_PY="$lifecycle_node_dir/../verification.py"
  export NODE_NAME NODE_IP NODE_CLUSTER_ENDPOINTS TALOS_LIFECYCLE_KUBE_CONTEXT
  export TALOS_LIFECYCLE_TALOS_CONTEXT NODE_KUBECTL NODE_TALOSCTL NODE_PYTHON
  export TEST_LEASE_KUBECTL NODE_LIFECYCLE_KUBECTL
  export TALOS_LIFECYCLE_PREPARED_JSON TALOS_LIFECYCLE_VERIFICATION_PY
  [[ -f "$kubeconfig" && -f "$talosconfig" ]] || {
    echo 'Missing validated node lifecycle credential reference.' >&2
    return 1
  }
  temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/homelab-node-lifecycle.XXXXXX")"
  inventory_file="$temp_dir/drain-inventory.json"
  renewal_failure="$temp_dir/lease-renewal-failed"
  TEST_LEASE_RENEWAL_FAILURE_MARKER="$renewal_failure"
  export TEST_LEASE_RENEWAL_FAILURE_MARKER
  cleanup() {
    local primary_status="$1"
    cleanup_status=0
    stop_test_lease_renewal || cleanup_status=1
    if [[ "$lease_acquired" == true ]]; then
      release_test_lease "$kubeconfig" "$holder" || cleanup_status=1
    fi
    rm -rf -- "$temp_dir" || cleanup_status=1
    if ((primary_status != 0)); then
      ((cleanup_status == 0)) || echo 'Talos lifecycle cleanup also failed.' >&2
      return "$primary_status"
    fi
    return "$cleanup_status"
  }
  if [[ "$action" == 'maintenance-check' ]]; then
    if ! run_disruption_preflight "$kubeconfig" "$talosconfig" "$NODE_NAME" "$NODE_IP" "$inventory_file"; then
      rm -rf -- "$temp_dir"
      return 1
    fi
    rm -rf -- "$temp_dir"
    echo "Maintenance preflight passed for $NODE_NAME; repeat it through maintenance-enter before mutation."
    return 0
  fi
  acquire_test_lease "$kubeconfig" "$holder"
  lease_acquired=true
  start_test_lease_renewal "$kubeconfig" "$holder" "$renewal_failure"
  trap '
    status=$?
    trap - EXIT INT TERM
    cleanup "$status"
    exit $?
  ' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  case "$action" in
    maintenance-enter)
      local longhorn_state
      run_disruption_preflight "$kubeconfig" "$talosconfig" "$NODE_NAME" "$NODE_IP" "$inventory_file"
      [[ "$(yq -r '.talos_confirmation' "$prepared_json")" == "enter:${NODE_NAME}:${NODE_IP}" ]] || return 1
      longhorn_state="$(read_longhorn_node "$kubeconfig" "$NODE_NAME")"
      record="$(build_maintenance_lifecycle_record_from_state "$longhorn_state")"
      run_maintenance_enter_transaction "$kubeconfig" "$talosconfig" "$NODE_NAME" \
        "$NODE_IP" "$holder" "$record" "$inventory_file"
      ;;
    reboot)
      run_disruption_preflight "$kubeconfig" "$talosconfig" "$NODE_NAME" "$NODE_IP" "$inventory_file"
      [[ "$(yq -r '.talos_confirmation' "$prepared_json")" == "reboot:${NODE_NAME}:${NODE_IP}" ]] || return 1
      record='{"schemaVersion":1,"kind":"reboot"}'
      run_reboot_transaction "$kubeconfig" "$talosconfig" "$NODE_NAME" "$NODE_IP" \
        "$holder" "$record" "$inventory_file"
      ;;
    abrupt-loss-test)
      run_disruption_preflight "$kubeconfig" "$talosconfig" "$NODE_NAME" "$NODE_IP" "$inventory_file"
      require_current_lease "$kubeconfig" "$holder"
      [[ "$(yq -r '.talos_test_confirmation' "$prepared_json")" == 'chaos:node-abrupt-loss' ]] || return 1
      [[ "$(yq -r '.talos_confirmation' "$prepared_json")" == "remove-power:${NODE_NAME}:${NODE_IP}" ]] || return 1
      "$NODE_PYTHON" "$lifecycle_node_dir/../scenarios/node_abrupt_loss.py" \
        "$prepared_json" "$(yq -r '.evidence_dir' "$prepared_json")"
      ;;
    maintenance-exit)
      assert_cluster_disruption_admissible "$kubeconfig" "$NODE_NAME"
      record="$(read_node_lifecycle_record "$kubeconfig" "$NODE_NAME")"
      kind="$(lifecycle_record_kind "$record")"
      [[ "$(yq -r '.talos_confirmation' "$prepared_json")" == "accept:${NODE_NAME}:${kind}" ]] || return 1
      run_maintenance_exit_transaction "$kubeconfig" "$talosconfig" "$NODE_NAME" \
        "$NODE_IP" "$holder" "$record"
      ;;
    *)
      echo "Unsupported node lifecycle action: $action" >&2
      return 2
      ;;
  esac

  trap - EXIT INT TERM
  cleanup 0
)

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  node_lifecycle_main "$@"
fi
