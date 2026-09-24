#!/usr/bin/env bash
# shellcheck disable=SC1091
lifecycle_node_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

source "$lifecycle_node_dir/../lib/node-lifecycle-state.sh"

longhorn_kubectl() {
  local kubeconfig="$1"
  shift
  "${NODE_KUBECTL:-kubectl}" --kubeconfig "$kubeconfig" \
    --namespace longhorn-system "$@"
}

read_longhorn_node() {
  local kubeconfig="$1"
  local node="$2"
  longhorn_kubectl "$kubeconfig" get nodes.longhorn.io "$node" --output json
}

build_maintenance_lifecycle_record_from_state() {
  local state="$1"
  local allow eviction
  allow="$(yq -r '.spec.allowScheduling' - <<<"$state")"
  eviction="$(yq -r '.spec.evictionRequested' - <<<"$state")"
  [[ "$allow" =~ ^(true|false)$ && "$eviction" =~ ^(true|false)$ ]] || {
    echo 'Longhorn node has invalid scheduling state.' >&2
    return 1
  }
  ALLOW="$allow" EVICTION="$eviction" \
    yq --null-input --output-format json '
      {
        "schemaVersion": 1,
        "kind": "maintenance",
        "longhorn": {
          "allowScheduling": {
            "before": (strenv(ALLOW) == "true"),
            "during": false
          },
          "evictionRequested": {
            "before": (strenv(EVICTION) == "true"),
            "during": true
          }
        }
      }
    '
}

build_maintenance_lifecycle_record() {
  local kubeconfig="$1"
  local node="$2"
  local state
  state="$(read_longhorn_node "$kubeconfig" "$node")" || return 1
  build_maintenance_lifecycle_record_from_state "$state"
}

replace_longhorn_node_state() {
  local kubeconfig="$1"
  local replacement="$2"
  printf '%s\n' "$replacement" |
    longhorn_kubectl "$kubeconfig" replace --filename - >/dev/null
}

verify_longhorn_node_values() {
  local kubeconfig="$1"
  local node="$2"
  local expected_allow="$3"
  local expected_eviction="$4"
  local state actual
  state="$(read_longhorn_node "$kubeconfig" "$node")" || return 1
  actual="$(yq -r '[.spec.allowScheduling, .spec.evictionRequested] | join(" ")' - <<<"$state")"
  [[ "$actual" == "$expected_allow $expected_eviction" ]] || {
    echo "Longhorn node $node state is '$actual', expected '$expected_allow $expected_eviction'." >&2
    return 1
  }
}

apply_longhorn_maintenance_state() {
  local kubeconfig="$1"
  local node="$2"
  local record="$3"
  local state current_allow current_eviction before_allow before_eviction replacement
  [[ "$(lifecycle_record_kind "$record")" == 'maintenance' ]] || return 1
  state="$(read_longhorn_node "$kubeconfig" "$node")" || return 1
  current_allow="$(yq -r '.spec.allowScheduling' - <<<"$state")"
  current_eviction="$(yq -r '.spec.evictionRequested' - <<<"$state")"
  before_allow="$(yq -r '.longhorn.allowScheduling.before' - <<<"$record")"
  before_eviction="$(yq -r '.longhorn.evictionRequested.before' - <<<"$record")"
  [[ "$current_allow" == "$before_allow" && "$current_eviction" == "$before_eviction" ]] || {
    echo "Longhorn node $node changed before maintenance settings were applied." >&2
    return 1
  }
  replacement="$(yq --output-format json '
    .spec.allowScheduling = false |
    .spec.evictionRequested = true
  ' <<<"$state")"
  replace_longhorn_node_state "$kubeconfig" "$replacement" || return 1
  verify_longhorn_node_values "$kubeconfig" "$node" false true
}

restore_longhorn_maintenance_state() {
  local kubeconfig="$1"
  local node="$2"
  local record="$3"
  local state current_allow current_eviction before_allow before_eviction
  local allow_action eviction_action replacement
  [[ "$(lifecycle_record_kind "$record")" == 'maintenance' ]] || return 1
  state="$(read_longhorn_node "$kubeconfig" "$node")" || return 1
  current_allow="$(yq -r '.spec.allowScheduling' - <<<"$state")"
  current_eviction="$(yq -r '.spec.evictionRequested' - <<<"$state")"
  before_allow="$(yq -r '.longhorn.allowScheduling.before' - <<<"$record")"
  before_eviction="$(yq -r '.longhorn.evictionRequested.before' - <<<"$record")"
  allow_action="$(compare_owned_value "$current_allow" "$before_allow" false)" || return 1
  eviction_action="$(compare_owned_value "$current_eviction" "$before_eviction" true)" || return 1
  if [[ "$allow_action" == 'noop' && "$eviction_action" == 'noop' ]]; then
    return 0
  fi
  replacement="$(ALLOW="$before_allow" EVICTION="$before_eviction" \
    yq --output-format json '
      .spec.allowScheduling = (strenv(ALLOW) == "true") |
      .spec.evictionRequested = (strenv(EVICTION) == "true")
    ' <<<"$state")"
  replace_longhorn_node_state "$kubeconfig" "$replacement" || return 1
  verify_longhorn_node_values "$kubeconfig" "$node" "$before_allow" "$before_eviction"
}

verify_short_absence_longhorn_safety() {
  local kubeconfig="$1"
  local node="$2"
  local policy volumes replicas affected volume state robustness desired actual off_target
  policy="$(longhorn_kubectl "$kubeconfig" get settings.longhorn.io node-drain-policy \
    --output jsonpath='{.value}')" || return 1
  [[ "$policy" == 'block-if-contains-last-replica' ]] || {
    echo "Longhorn node-drain-policy '$policy' does not provide the required last-replica protection." >&2
    return 1
  }
  volumes="$(longhorn_kubectl "$kubeconfig" get volumes.longhorn.io --output json)" || return 1
  replicas="$(longhorn_kubectl "$kubeconfig" get replicas.longhorn.io --output json)" || return 1
  affected="$(NODE="$node" yq -r '
    .items[] |
    select(.spec.nodeID == strenv(NODE)) |
    select(.spec.failedAt == null or .spec.failedAt == "") |
    .spec.volumeName
  ' <<<"$replicas" | sort -u)"
  while IFS= read -r volume; do
    [[ -n "$volume" ]] || continue
    state="$(VOLUME="$volume" yq -r '.items[] | select(.metadata.name == strenv(VOLUME)) | .status.state' - <<<"$volumes")"
    robustness="$(VOLUME="$volume" yq -r '.items[] | select(.metadata.name == strenv(VOLUME)) | .status.robustness' - <<<"$volumes")"
    desired="$(VOLUME="$volume" yq -r '.items[] | select(.metadata.name == strenv(VOLUME)) | .spec.numberOfReplicas' - <<<"$volumes")"
    actual="$(VOLUME="$volume" yq -r '
      [.items[] |
        select(.spec.volumeName == strenv(VOLUME)) |
        select(.spec.failedAt == null or .spec.failedAt == "")] | length
    ' <<<"$replicas")"
    case "$state:$robustness" in
      attached:healthy) ;;
      detached:unknown)
        [[ "$desired" =~ ^[1-9][0-9]*$ && "$actual" =~ ^[0-9]+$ ]] || {
          echo "Detached Longhorn volume $volume has an invalid replica count." >&2
          return 1
        }
        [[ "$actual" -ge "$desired" ]] || {
          echo "Detached Longhorn volume $volume does not have all desired replicas." >&2
          return 1
        }
        ;;
      *)
        echo "Longhorn volume $volume is not healthy enough for a node disruption." >&2
        return 1
        ;;
    esac
    off_target="$(NODE="$node" VOLUME="$volume" yq -r '
      [.items[] |
        select(.spec.volumeName == strenv(VOLUME)) |
        select(.spec.nodeID != strenv(NODE)) |
        select(.spec.failedAt == null or .spec.failedAt == "")] | length
    ' <<<"$replicas")"
    [[ "$off_target" -ge 1 ]] || {
      echo "Longhorn volume $volume has no healthy replica away from $node." >&2
      return 1
    }
  done <<<"$affected"
}

longhorn_evacuation_complete() {
  local kubeconfig="$1"
  local node="$2"
  local volumes replicas remaining volume desired actual
  volumes="$(longhorn_kubectl "$kubeconfig" get volumes.longhorn.io --output json)" || return 1
  replicas="$(longhorn_kubectl "$kubeconfig" get replicas.longhorn.io --output json)" || return 1
  remaining="$(NODE="$node" yq -r '
    .items[] |
    select(.spec.nodeID == strenv(NODE)) |
    select(.spec.failedAt == null or .spec.failedAt == "") |
    .metadata.name
  ' <<<"$replicas")"
  [[ -z "$remaining" ]] || return 1
  while IFS= read -r volume; do
    [[ -n "$volume" ]] || continue
    case "$(VOLUME="$volume" yq -r '.items[] | select(.metadata.name == strenv(VOLUME)) | [.status.state, .status.robustness] | join(":")' - <<<"$volumes")" in
      attached:healthy|detached:unknown) ;;
      *) return 1 ;;
    esac
    desired="$(VOLUME="$volume" yq -r '.items[] | select(.metadata.name == strenv(VOLUME)) | .spec.numberOfReplicas' - <<<"$volumes")"
    actual="$(VOLUME="$volume" yq -r '
      [.items[] |
        select(.spec.volumeName == strenv(VOLUME)) |
        select(.spec.failedAt == null or .spec.failedAt == "")] | length
    ' <<<"$replicas")"
    [[ "$desired" =~ ^[1-9][0-9]*$ && "$actual" -ge "$desired" ]] || return 1
  done < <(yq -r '.items[].metadata.name' - <<<"$volumes")
}

evacuate_longhorn_replicas() {
  local kubeconfig="$1"
  local node="$2"
  local attempts="${NODE_LONGHORN_EVACUATION_ATTEMPTS:-180}"
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    longhorn_evacuation_complete "$kubeconfig" "$node" && return 0
    "${NODE_SLEEP:-sleep}" "${NODE_LONGHORN_POLL_SECONDS:-10}"
  done
  echo "Longhorn did not fully evacuate replicas from $node." >&2
  return 1
}
