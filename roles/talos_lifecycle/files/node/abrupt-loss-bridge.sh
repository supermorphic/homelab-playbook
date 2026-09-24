#!/usr/bin/env bash
# shellcheck disable=SC1091
set -euo pipefail

bridge_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$bridge_dir/../lib/lease.sh"
source "$bridge_dir/../lib/node-lifecycle-state.sh"
source "$bridge_dir/common.sh"
source "$bridge_dir/recovery.sh"

abrupt_survivors_safe() {
  local kubeconfig="$1" node="$2" nodes
  nodes="$(node_kubectl "$kubeconfig" get nodes --output json)" || return 1
  [[ "$(yq -r '.items | length' <<<"$nodes")" == 3 ]] || return 1
  [[ "$(NODE="$node" yq -r '[.items[] | select(.metadata.name != strenv(NODE)) | select(([.status.conditions[]? | select(.type == "Ready") | .status][0] // "Unknown") == "True") | select((.spec.unschedulable // false) == false)] | length' <<<"$nodes")" == 2 ]] || return 1
  [[ "$(NODE="$node" yq -r '[.items[] | select(.metadata.name == strenv(NODE)) | ([.status.conditions[]? | select(.type == "Ready") | .status][0] // "Unknown")][0]' <<<"$nodes")" != True ]] || return 1
  assert_no_active_lifecycle_records "$kubeconfig"
}

abrupt_loss_bridge_main() {
  [[ "$#" -eq 2 ]] || { echo 'Usage: abrupt-loss-bridge.sh admit|contain|recover PREPARED_JSON' >&2; return 2; }
  local action="$1" prepared="$2" kubeconfig talosconfig node node_ip holder record
  [[ "$prepared" == /* && -f "$prepared" && ! -L "$prepared" ]] || return 2
  kubeconfig="$(yq -r '.talos_kubeconfig' "$prepared")"
  talosconfig="$(yq -r '.talos_talosconfig' "$prepared")"
  node="$(yq -r '.node' "$prepared")"
  node_ip="$(yq -r '.address' "$prepared")"
  holder="$(yq -r '.holder' "$prepared")"
  NODE_CLUSTER_ENDPOINTS="$(yq -r '.talos_endpoints | join(",")' "$prepared")"
  NODE_EXPECTED_NAMES="$(yq -r '.nodes | keys | .[]' "$prepared" | sort)"
  TALOS_LIFECYCLE_KUBE_CONTEXT="$(yq -r '.talos_kube_context' "$prepared")"
  TALOS_LIFECYCLE_TALOS_CONTEXT="$(yq -r '.talos_talos_context' "$prepared")"
  NODE_KUBECTL="$(yq -r '.tools.kubectl' "$prepared")"
  NODE_TALOSCTL="$(yq -r '.tools.talosctl' "$prepared")"
  TEST_LEASE_KUBECTL="$NODE_KUBECTL"
  NODE_LIFECYCLE_KUBECTL="$NODE_KUBECTL"
  NODE_PYTHON="$(yq -r '.tools.python3' "$prepared")"
  TALOS_LIFECYCLE_PREPARED_JSON="$prepared"
  TALOS_LIFECYCLE_VERIFICATION_PY="$bridge_dir/../verification.py"
  export NODE_CLUSTER_ENDPOINTS NODE_EXPECTED_NAMES TALOS_LIFECYCLE_KUBE_CONTEXT TALOS_LIFECYCLE_TALOS_CONTEXT
  export NODE_KUBECTL NODE_TALOSCTL NODE_PYTHON TALOS_LIFECYCLE_PREPARED_JSON TALOS_LIFECYCLE_VERIFICATION_PY
  export TEST_LEASE_KUBECTL NODE_LIFECYCLE_KUBECTL

  join_test_lease "$kubeconfig" "$holder"
  require_current_lease "$kubeconfig" "$holder"
  record='{"schemaVersion":1,"kind":"abrupt-loss"}'
  case "$action" in
    admit)
      assert_cluster_disruption_admissible "$kubeconfig"
      [[ "$(node_kubectl "$kubeconfig" get node "$node" --output jsonpath='{.status.conditions[?(@.type=="Ready")].status}')" == True ]]
      ;;
    contain)
      abrupt_survivors_safe "$kubeconfig" "$node"
      require_current_lease "$kubeconfig" "$holder"
      persist_node_containment "$kubeconfig" "$node" "$record"
      ;;
    recover)
      assert_cluster_disruption_admissible "$kubeconfig" "$node"
      [[ "$(read_node_lifecycle_record "$kubeconfig" "$node")" == "$record" ]] || return 1
      perform_recovery_acceptance "$kubeconfig" "$talosconfig" "$node" "$node_ip" "$record"
      require_current_lease "$kubeconfig" "$holder"
      remove_node_containment_and_uncordon "$kubeconfig" "$node" "$record" recovery-accepted
      ;;
    *) echo "Unsupported abrupt-loss bridge action: $action" >&2; return 2 ;;
  esac
  require_current_lease "$kubeconfig" "$holder"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  abrupt_loss_bridge_main "$@"
fi
