#!/usr/bin/env bash

NODE_LIFECYCLE_ANNOTATION="${NODE_LIFECYCLE_ANNOTATION:-homelab.supermorphic.com/node-lifecycle}"

lifecycle_kubectl() {
  local kubeconfig="$1"
  shift
  "${NODE_LIFECYCLE_KUBECTL:-kubectl}" --kubeconfig "$kubeconfig" "$@"
}

validate_lifecycle_record() {
  local record="$1"
  yq --exit-status '
    (.schemaVersion == 1) and
    (
      (
        (.kind == "reboot" or .kind == "abrupt-loss") and
        ((. | keys | length) == 2)
      ) or
      (
        .kind == "maintenance" and
        ((. | keys | length) == 3) and
        ((.longhorn | keys | length) == 2) and
        ((.longhorn.allowScheduling | keys | length) == 2) and
        ((.longhorn.evictionRequested | keys | length) == 2) and
        (.longhorn.allowScheduling.before == true or
          .longhorn.allowScheduling.before == false) and
        .longhorn.allowScheduling.during == false and
        (.longhorn.evictionRequested.before == true or
          .longhorn.evictionRequested.before == false) and
        .longhorn.evictionRequested.during == true
      )
    )
  ' <<<"$record" >/dev/null 2>&1
}

lifecycle_record_kind() {
  local record="$1"
  validate_lifecycle_record "$record" || {
    echo 'Lifecycle record is malformed or unsupported.' >&2
    return 1
  }
  yq -r '.kind' <<<"$record"
}

compare_owned_value() {
  local current="$1"
  local before="$2"
  local during="$3"
  if [[ "$current" == "$during" ]]; then
    printf 'restore\n'
  elif [[ "$current" == "$before" ]]; then
    printf 'noop\n'
  else
    echo "Lifecycle state conflict: current '$current' is neither owned value '$during' nor prior value '$before'." >&2
    return 1
  fi
}

assert_no_active_lifecycle_records() {
  local kubeconfig="$1"
  local nodes_json node_json node_name record
  nodes_json="$(lifecycle_kubectl "$kubeconfig" get nodes --output json)" || {
    echo 'Cannot inspect persistent node lifecycle state through Kubernetes.' >&2
    return 1
  }
  while IFS= read -r node_json; do
    [[ -n "$node_json" ]] || continue
    node_name="$(yq -r '.metadata.name' <<<"$node_json")"
    record="$(ANNOTATION="$NODE_LIFECYCLE_ANNOTATION" \
      yq -r '.metadata.annotations[strenv(ANNOTATION)] // ""' <<<"$node_json")"
    [[ -z "$record" ]] || {
      echo "Node $node_name already has persistent lifecycle state." >&2
      return 1
    }
  done < <(yq --output-format json --indent 0 '.items[]' <<<"$nodes_json")
}

assert_cluster_disruption_admissible() {
  local kubeconfig="$1"
  local recovery_node="${2:-}"
  local nodes_json node_json node_name ready unschedulable record
  local recovery_found=false recovery_cordoned=false

  nodes_json="$(lifecycle_kubectl "$kubeconfig" get nodes --output json)" || {
    echo 'Cannot establish node lifecycle state through the Kubernetes API.' >&2
    return 1
  }

  while IFS= read -r node_json; do
    [[ -n "$node_json" ]] || continue
    node_name="$(yq -r '.metadata.name' <<<"$node_json")"
    ready="$(yq -r '[.status.conditions[]? | select(.type == "Ready") | .status][0] // "Unknown"' <<<"$node_json")"
    unschedulable="$(yq -r '.spec.unschedulable // false' <<<"$node_json")"
    record="$(ANNOTATION="$NODE_LIFECYCLE_ANNOTATION" \
      yq -r '.metadata.annotations[strenv(ANNOTATION)] // ""' <<<"$node_json")"

    if [[ -n "$record" ]]; then
      validate_lifecycle_record "$record" || {
        echo "Node $node_name has a malformed or unsupported lifecycle record." >&2
        return 1
      }
      if [[ -z "$recovery_node" || "$node_name" != "$recovery_node" ]]; then
        echo "Node $node_name already has active lifecycle containment." >&2
        return 1
      fi
      recovery_found=true
      [[ "$unschedulable" == 'true' ]] && recovery_cordoned=true
    fi

    if [[ "$unschedulable" == 'true' && "$node_name" != "$recovery_node" ]]; then
      echo "Node $node_name is unexpectedly cordoned." >&2
      return 1
    fi
    if [[ "$ready" != 'True' && "$node_name" != "$recovery_node" ]]; then
      echo "Node $node_name is unexpectedly $ready rather than Ready." >&2
      return 1
    fi
  done < <(yq --output-format json --indent 0 '.items[]' <<<"$nodes_json")

  if [[ -n "$recovery_node" ]]; then
    [[ "$recovery_found" == 'true' ]] || {
      echo "Node $recovery_node has no lifecycle record to recover." >&2
      return 1
    }
    [[ "$recovery_cordoned" == 'true' ]] || {
      echo "Node $recovery_node has lifecycle state but is not cordoned." >&2
      return 1
    }
  fi
}
