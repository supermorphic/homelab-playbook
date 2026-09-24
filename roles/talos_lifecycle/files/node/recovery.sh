#!/usr/bin/env bash
# shellcheck disable=SC1091
lifecycle_node_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

source "$lifecycle_node_dir/common.sh"
source "$lifecycle_node_dir/drain.sh"
source "$lifecycle_node_dir/longhorn.sh"

recovery_kubectl() {
  local kubeconfig="$1"
  shift
  "${NODE_KUBECTL:-kubectl}" --kubeconfig "$kubeconfig" "$@"
}

recovery_talosctl() {
  "${NODE_TALOSCTL:-talosctl}" "$@"
}

recovery_just() {
  "${NODE_JUST:-just}" "$@"
}

verify_returned_node_contained() {
  local kubeconfig="$1"
  local node="$2"
  local record="$3"
  local state actual_record
  state="$(recovery_kubectl "$kubeconfig" get node "$node" --output json)" || return 1
  actual_record="$(ANNOTATION="$NODE_LIFECYCLE_ANNOTATION" \
    yq -r '.metadata.annotations[strenv(ANNOTATION)] // ""' <<<"$state")"
  [[ "$actual_record" == "$record" &&
    "$(yq -r '.spec.unschedulable // false' - <<<"$state")" == 'true' &&
    "$(yq -r '[.status.conditions[]? | select(.type == "Ready") | .status][0] // "Unknown"' - <<<"$state")" == 'True' ]] || {
    echo "Node $node has returned but is not Ready with the expected containment." >&2
    return 1
  }
}

wait_for_returned_node_contained() {
  local kubeconfig="$1"
  local node="$2"
  local record="$3"
  local attempts="${NODE_RECOVERY_RETURN_ATTEMPTS:-360}"
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    verify_returned_node_contained "$kubeconfig" "$node" "$record" && return 0
    "${NODE_SLEEP:-sleep}" "${NODE_RECOVERY_POLL_SECONDS:-5}"
  done
  echo "Node $node did not return Ready under lifecycle containment." >&2
  return 1
}

verify_talos_recovery() {
  local talosconfig="$1"
  local node="$2"
  local node_ip="$3"
  local common hostname security volumes
  common=(--nodes "$node_ip" --endpoints "$NODE_CLUSTER_ENDPOINTS" --talosconfig "$talosconfig" --output yaml)
  hostname="$(recovery_talosctl get hostname "${common[@]}")" || return 1
  [[ "$(yq -r '.spec.hostname' - <<<"$hostname")" == "$node" ]] || return 1
  security="$(recovery_talosctl get securitystate "${common[@]}")" || return 1
  [[ "$(yq -r '.spec.secureBoot' - <<<"$security")" == 'true' ]] || return 1
  [[ "$(yq -r '.spec.bootedWithUKI' - <<<"$security")" == 'true' ]] || return 1
  volumes="$(recovery_talosctl get volumestatuses "${common[@]}")" || return 1
  for volume in STATE EPHEMERAL u-longhorn; do
    [[ "$(VOLUME="$volume" yq ea -r 'select(.metadata.id == strenv(VOLUME)) | .spec.phase' - <<<"$volumes")" == 'ready' ]] || return 1
  done
  for volume in STATE EPHEMERAL; do
    [[ "$(VOLUME="$volume" yq ea -r 'select(.metadata.id == strenv(VOLUME)) | .spec.encryptionProvider' - <<<"$volumes")" == 'luks2' ]] || return 1
  done
}

verify_etcd_recovery() {
  local talosconfig="$1"
  local members status alarms names rows leaders
  members="$(recovery_talosctl etcd members --nodes "$NODE_CLUSTER_ENDPOINTS" \
    --endpoints "$NODE_CLUSTER_ENDPOINTS" --talosconfig "$talosconfig")" || return 1
  names="$(awk 'NR > 1 && NF {print $3}' <<<"$members" | sort -u)"
  [[ "$names" == $'node-a\nnode-b\nnode-c' ]] || {
    printf 'Expected etcd members node-a, node-b, and node-c; found:\n%s\n' \
      "${names:-none}" >&2
    return 1
  }
  status="$(recovery_talosctl etcd status --nodes "$NODE_CLUSTER_ENDPOINTS" \
    --endpoints "$NODE_CLUSTER_ENDPOINTS" --talosconfig "$talosconfig")" || return 1
  rows="$(awk 'NR > 1 && NF {count++} END {print count + 0}' <<<"$status")"
  leaders="$(awk 'BEGIN {FS="[[:space:]][[:space:]]+"} NR > 1 && NF {print $5}' <<<"$status" | sort -u | awk 'NF {count++} END {print count + 0}')"
  [[ "$rows" == '3' && "$leaders" == '1' ]] || {
    echo "Expected three etcd status rows with one leader; found $rows rows and $leaders leaders." >&2
    return 1
  }
  alarms="$(recovery_talosctl etcd alarm list --nodes "$NODE_CLUSTER_ENDPOINTS" \
    --endpoints "$NODE_CLUSTER_ENDPOINTS" --talosconfig "$talosconfig")" || return 1
  [[ "$(awk 'NR > 1 && NF {count++} END {print count + 0}' <<<"$alarms")" == '0' ]] || {
    echo 'Active etcd alarms block node lifecycle operations.' >&2
    return 1
  }
}

verify_cilium_recovery() {
  local kubeconfig="$1"
  local node="$2"
  local daemonset pods
  daemonset="$(recovery_kubectl "$kubeconfig" --namespace kube-system \
    get daemonset cilium --output json)" || return 1
  [[ "$(yq -r '[.status.desiredNumberScheduled, .status.numberReady, (.status.numberUnavailable // 0)] | join(" ")' - <<<"$daemonset")" == '3 3 0' ]] || return 1
  pods="$(recovery_kubectl "$kubeconfig" --namespace kube-system get pods \
    --selector k8s-app=cilium --field-selector "spec.nodeName=$node" --output json)" || return 1
  [[ "$(yq -r '[.items[] | select([.status.conditions[]? | select(.type == "Ready") | .status][0] == "True")] | length' - <<<"$pods")" == '1' ]] || return 1
}

verify_longhorn_convergence() {
  local kubeconfig="$1"
  local nodes volumes replicas unhealthy replica_shortfall volume desired actual
  nodes="$(recovery_kubectl "$kubeconfig" --namespace longhorn-system \
    get nodes.longhorn.io --output json)" || return 1
  [[ "$(yq -r '[.items[] | select([.status.conditions[]? | select(.type == "Ready") | .status][0] == "True")] | length' - <<<"$nodes")" == '3' ]] || return 1
  volumes="$(recovery_kubectl "$kubeconfig" --namespace longhorn-system \
    get volumes.longhorn.io --output json)" || return 1
  unhealthy="$(yq -r '
    .items[] |
    {"name": .metadata.name, "health": ([.status.state, .status.robustness] | join(":"))} |
    select(.health != "attached:healthy" and .health != "detached:unknown") | .name
  ' <<<"$volumes")"
  [[ -z "$unhealthy" ]] || {
    printf 'Longhorn volumes have not converged:\n%s\n' "$unhealthy" >&2
    return 1
  }
  replicas="$(recovery_kubectl "$kubeconfig" --namespace longhorn-system \
    get replicas.longhorn.io --output json)" || return 1
  replica_shortfall="$(VOLUMES="$volumes" yq -r '
    .items[] | select(.spec.failedAt == null or .spec.failedAt == "") | .spec.volumeName
  ' <<<"$replicas" | sort | uniq -c)"
  while IFS= read -r volume; do
    [[ -n "$volume" ]] || continue
    desired="$(VOLUME="$volume" yq -r '.items[] | select(.metadata.name == strenv(VOLUME)) | .spec.numberOfReplicas' - <<<"$volumes")"
    actual="$(awk -v name="$volume" '$2 == name {print $1}' <<<"$replica_shortfall")"
    [[ "$desired" =~ ^[1-9][0-9]*$ && "${actual:-0}" -ge "$desired" ]] || {
      echo "Longhorn volume $volume has not reached its desired replica count." >&2
      return 1
    }
  done < <(yq -r '.items[].metadata.name' - <<<"$volumes")
}

perform_recovery_acceptance() {
  local kubeconfig="$1"
  local talosconfig="$2"
  local node="$3"
  local node_ip="$4"
  local record="$5"
  local inventory_file="${6:-}"
  local kind
  kind="$(lifecycle_record_kind "$record")" || return 1
  wait_for_returned_node_contained "$kubeconfig" "$node" "$record" || return 1
  verify_talos_recovery "$talosconfig" "$node" "$node_ip" || return 1
  if [[ "$kind" == 'maintenance' ]]; then
    verify_returned_node_contained "$kubeconfig" "$node" "$record" || return 1
    restore_longhorn_maintenance_state "$kubeconfig" "$node" "$record" || return 1
  fi
  verify_longhorn_convergence "$kubeconfig" || return 1
  verify_etcd_recovery "$talosconfig" || return 1
  verify_cilium_recovery "$kubeconfig" "$node" || return 1
  if [[ -n "$inventory_file" ]]; then
    wait_for_workload_replacements "$kubeconfig" "$node" "$inventory_file" || return 1
  fi
  recovery_just kube foundation-verify || return 1
}
