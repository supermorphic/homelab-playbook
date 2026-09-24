#!/usr/bin/env bash
# shellcheck disable=SC1091

lifecycle_node_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$lifecycle_node_dir/../lib/node-lifecycle-state.sh"

# These globals are the validated result consumed by lifecycle coordinators.
# shellcheck disable=SC2034
NODE_CLUSTER_ENDPOINTS='192.0.2.10,192.0.2.11,192.0.2.12'
# shellcheck disable=SC2034
NODE_NAME=''
# shellcheck disable=SC2034
NODE_IP=''
export NODE_CLUSTER_ENDPOINTS NODE_NAME NODE_IP

resolve_node_target() {
  local requested="$1"
  case "$requested" in
    node-a) NODE_NAME='node-a'; NODE_IP='192.0.2.10' ;;
    node-b) NODE_NAME='node-b'; NODE_IP='192.0.2.11' ;;
    node-c) NODE_NAME='node-c'; NODE_IP='192.0.2.12' ;;
    *)
      echo 'Node must be one of: node-a, node-b, node-c.' >&2
      return 1
      ;;
  esac
}

require_operator_checkout() {
  [[ "${NODE_ALLOW_LINKED_WORKTREE_FOR_TESTS:-}" != 'true' ]] || return 0
  local git_dir git_common superproject
  git_dir="$(git rev-parse --path-format=absolute --git-dir)"
  git_common="$(git rev-parse --path-format=absolute --git-common-dir)"
  superproject="$(git rev-parse --show-superproject-working-tree 2>/dev/null || true)"
  if [[ -z "$superproject" && "$git_dir" != "$git_common" ]]; then
    echo 'Refusing node mutation from a linked worktree. Run this operator command from the primary checkout.' >&2
    return 1
  fi
}

require_exact_confirmation() {
  local variable="$1"
  local expected="$2"
  [[ "${!variable:-}" == "$expected" ]] || {
    echo "Refusing operation: set $variable='$expected' after reviewing the preflight." >&2
    return 1
  }
}

node_kubectl() {
  local kubeconfig="$1"
  shift
  "${NODE_KUBECTL:-kubectl}" --kubeconfig "$kubeconfig" \
    --context "${TALOS_LIFECYCLE_KUBE_CONTEXT:?}" "$@"
}

read_node_lifecycle_record() {
  local kubeconfig="$1"
  local node="$2"
  local node_json
  node_json="$(node_kubectl "$kubeconfig" get node "$node" --output json)" || return 1
  ANNOTATION="$NODE_LIFECYCLE_ANNOTATION" \
    yq -r '.metadata.annotations[strenv(ANNOTATION)] // ""' <<<"$node_json"
}

persist_node_containment() {
  local kubeconfig="$1"
  local node="$2"
  local record="$3"
  local node_json current_record replacement verified node_uid
  validate_lifecycle_record "$record" || {
    echo 'Refusing to persist an invalid lifecycle record.' >&2
    return 1
  }
  node_json="$(node_kubectl "$kubeconfig" get node "$node" --output json)" || return 1
  node_uid="$(yq -r '.metadata.uid // ""' - <<<"$node_json")"
  [[ -n "$node_uid" ]] || {
    echo "Node $node has no stable UID; refusing containment." >&2
    return 1
  }
  current_record="$(ANNOTATION="$NODE_LIFECYCLE_ANNOTATION" \
    yq -r '.metadata.annotations[strenv(ANNOTATION)] // ""' <<<"$node_json")"
  [[ -z "$current_record" ]] || {
    echo "Node $node already has lifecycle containment." >&2
    return 1
  }
  [[ "$(yq -r '.spec.unschedulable // false' - <<<"$node_json")" == 'false' ]] || {
    echo "Node $node is already cordoned outside this lifecycle transaction." >&2
    return 1
  }
  replacement="$(ANNOTATION="$NODE_LIFECYCLE_ANNOTATION" RECORD="$record" \
    yq --output-format json '
      .metadata.annotations[strenv(ANNOTATION)] = strenv(RECORD) |
      .spec.unschedulable = true
    ' <<<"$node_json")"
  printf '%s\n' "$replacement" |
    node_kubectl "$kubeconfig" replace --filename - >/dev/null || {
      echo "Could not atomically annotate and cordon $node; lifecycle did not claim the Node object." >&2
      return 1
    }
  verified="$(node_kubectl "$kubeconfig" get node "$node" --output json)" || return 1
  [[ "$(yq -r '.metadata.uid // ""' - <<<"$verified")" == "$node_uid" &&
    "$(ANNOTATION="$NODE_LIFECYCLE_ANNOTATION" \
    yq -r '.metadata.annotations[strenv(ANNOTATION)] // ""' <<<"$verified")" == "$record" &&
    "$(yq -r '.spec.unschedulable // false' - <<<"$verified")" == 'true' ]] || {
    echo "Node $node containment did not persist exactly as requested." >&2
    return 1
  }
}

remove_node_containment_and_uncordon() {
  local kubeconfig="$1"
  local node="$2"
  local expected_record="$3"
  local acceptance="$4"
  local node_json current_record replacement verified node_uid
  [[ "$acceptance" == 'recovery-accepted' ]] || {
    echo "Refusing to make $node schedulable before recovery acceptance." >&2
    return 1
  }
  validate_lifecycle_record "$expected_record" || return 1
  node_json="$(node_kubectl "$kubeconfig" get node "$node" --output json)" || return 1
  node_uid="$(yq -r '.metadata.uid // ""' - <<<"$node_json")"
  [[ -n "$node_uid" ]] || return 1
  current_record="$(ANNOTATION="$NODE_LIFECYCLE_ANNOTATION" \
    yq -r '.metadata.annotations[strenv(ANNOTATION)] // ""' <<<"$node_json")"
  [[ "$current_record" == "$expected_record" ]] || {
    echo "Lifecycle record on $node changed; preserving containment." >&2
    return 1
  }
  [[ "$(yq -r '.spec.unschedulable // false' - <<<"$node_json")" == 'true' ]] || {
    echo "Node $node became schedulable before acceptance; refusing final transition." >&2
    return 1
  }
  replacement="$(ANNOTATION="$NODE_LIFECYCLE_ANNOTATION" \
    yq --output-format json '
      del(.metadata.annotations[strenv(ANNOTATION)]) |
      .spec.unschedulable = false
    ' <<<"$node_json")"
  printf '%s\n' "$replacement" |
    node_kubectl "$kubeconfig" replace --filename - >/dev/null || {
      echo "Final lifecycle transition for $node conflicted; it remains contained." >&2
      return 1
    }
  verified="$(node_kubectl "$kubeconfig" get node "$node" --output json)" || return 1
  [[ "$(yq -r '.metadata.uid // ""' - <<<"$verified")" == "$node_uid" &&
    "$(ANNOTATION="$NODE_LIFECYCLE_ANNOTATION" \
    yq -r '.metadata.annotations[strenv(ANNOTATION)] // ""' <<<"$verified")" == '' &&
    "$(yq -r '.spec.unschedulable // false' - <<<"$verified")" == 'false' ]] || {
    echo "Final lifecycle transition for $node could not be verified." >&2
    return 1
  }
}
