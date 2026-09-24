#!/usr/bin/env bash

drain_kubectl() {
  local kubeconfig="$1"
  shift
  "${NODE_KUBECTL:-kubectl}" --kubeconfig "$kubeconfig" "$@"
}

validate_drain_pods() {
  local pods_json="$1"
  local unmanaged
  # shellcheck disable=SC2016  # $kind is a yq variable.
  unmanaged="$(yq -r '
    .items[] |
    select(.metadata.annotations."kubernetes.io/config.mirror" == null) |
    ([.metadata.ownerReferences[]? | select(.controller == true) | .kind][0] // "") as $kind |
    select($kind != "DaemonSet" and $kind == "") |
    .metadata.namespace + "/" + .metadata.name
  ' <<<"$pods_json")"
  [[ -z "$unmanaged" ]] || {
    printf 'Drain is blocked by unmanaged Pods:\n%s\n' "$unmanaged" >&2
    return 1
  }
}

report_drain_local_data() {
  local pods_json="$1"
  local report
  # shellcheck disable=SC2016  # $kind, $namespace, and $pod are yq variables.
  report="$(yq -r '
    .items[] |
    select(.metadata.annotations."kubernetes.io/config.mirror" == null) |
    ([.metadata.ownerReferences[]? | select(.controller == true) | .kind][0] // "") as $kind |
    select($kind != "DaemonSet") |
    .metadata.namespace as $namespace |
    .metadata.name as $pod |
    .spec.volumes[]? |
    select(.emptyDir != null) |
    $namespace + "/" + $pod + " emptyDir/" + .name
  ' <<<"$pods_json")"
  if [[ -n "$report" ]]; then
    printf 'Node-local emptyDir data will be discarded:\n%s\n' "$report"
  fi
}

capture_drain_inventory() {
  local kubeconfig="$1"
  local node="$2"
  local output_file="$3"
  local pods_json claims_json='[]' namespace claim pvc pv volume_name identity
  pods_json="$(drain_kubectl "$kubeconfig" get pods --all-namespaces \
    --field-selector "spec.nodeName=$node" --output json)" || return 1
  validate_drain_pods "$pods_json" || return 1
  report_drain_local_data "$pods_json" >&2
  # shellcheck disable=SC2016  # yq program intentionally uses literal variables.
  while IFS=$'\t' read -r namespace claim; do
    [[ -n "$namespace" && -n "$claim" ]] || continue
    pvc="$(drain_kubectl "$kubeconfig" --namespace "$namespace" get pvc "$claim" \
      --output json)" || return 1
    volume_name="$(yq -r '.spec.volumeName // ""' - <<<"$pvc")"
    [[ -n "$volume_name" ]] || {
      echo "PVC $namespace/$claim is not bound." >&2
      return 1
    }
    pv="$(drain_kubectl "$kubeconfig" get pv "$volume_name" --output json)" || return 1
    identity="$(NAMESPACE="$namespace" CLAIM="$claim" \
      PVC_UID="$(yq -r '.metadata.uid // ""' - <<<"$pvc")" \
      PV_NAME="$volume_name" PV_UID="$(yq -r '.metadata.uid // ""' - <<<"$pv")" \
      yq --null-input --output-format json '{
        "namespace": strenv(NAMESPACE),
        "name": strenv(CLAIM),
        "uid": strenv(PVC_UID),
        "volumeName": strenv(PV_NAME),
        "volumeUid": strenv(PV_UID)
      }')"
    [[ "$(yq -r '.uid != "" and .volumeUid != ""' - <<<"$identity")" == 'true' ]] || {
      echo "PVC or PV identity is unavailable for $namespace/$claim." >&2
      return 1
    }
    claims_json="$(ITEM="$identity" yq \
      '. + [(strenv(ITEM) | from_json)]' <<<"$claims_json")"
  done < <(yq -r '
    .items[] |
    select(.metadata.annotations."kubernetes.io/config.mirror" == null) |
    ([.metadata.ownerReferences[]? | select(.controller == true) | .kind][0] // "") as $kind |
    select($kind != "" and $kind != "DaemonSet") |
    .metadata.namespace as $namespace |
    .spec.volumes[]? |
    select(.persistentVolumeClaim.claimName != null) |
    $namespace + "\t" + .persistentVolumeClaim.claimName
  ' <<<"$pods_json" | sort -u)
  CLAIMS="$claims_json" yq --output-format json \
    '.homelabLifecycle.claims = (strenv(CLAIMS) | from_json)' \
    <<<"$pods_json" >"$output_file"
}

perform_kubernetes_drain() {
  local kubeconfig="$1"
  local node="$2"
  local discovery
  discovery="$(drain_kubectl "$kubeconfig" get --raw /api/v1)" || {
    echo 'Cannot verify the Kubernetes core/v1 pods/eviction resource.' >&2
    return 1
  }
  [[ "$(yq -r '[.resources[]? | select(.name == "pods/eviction" and .kind == "Eviction")] | length' - <<<"$discovery")" -eq 1 ]] || {
    echo 'Refusing drain because the core/v1 pods/eviction resource is unavailable.' >&2
    return 1
  }
  drain_kubectl "$kubeconfig" drain "$node" \
    --ignore-daemonsets \
    --delete-emptydir-data \
    --timeout="${NODE_DRAIN_TIMEOUT:-15m}"
}

verify_no_drainable_workloads() {
  local kubeconfig="$1"
  local node="$2"
  local pods_json remaining
  pods_json="$(drain_kubectl "$kubeconfig" get pods --all-namespaces \
    --field-selector "spec.nodeName=$node" --output json)" || return 1
  # shellcheck disable=SC2016  # $kind is a yq variable.
  remaining="$(yq -r '
    .items[] |
    select(.metadata.annotations."kubernetes.io/config.mirror" == null) |
    ([.metadata.ownerReferences[]? | select(.controller == true) | .kind][0] // "") as $kind |
    select($kind != "DaemonSet") |
    .metadata.namespace + "/" + .metadata.name
  ' <<<"$pods_json")"
  [[ -z "$remaining" ]] || {
    printf 'Drainable workloads remain on %s:\n%s\n' "$node" "$remaining" >&2
    return 1
  }
}

verify_workload_replacements() {
  local kubeconfig="$1"
  local node="$2"
  local inventory_file="$3"
  local pod_json namespace pod_name kind owner_uid selector candidates replacement claims claim
  local claim_json pvc pv expected_pvc_uid expected_volume expected_volume_uid job_name job
  [[ -f "$inventory_file" ]] || return 1
  while IFS= read -r pod_json; do
    [[ -n "$pod_json" ]] || continue
    # Static Pods are represented by mirrors owned by the local Node. The kubelet,
    # not a workload controller on a survivor, manages their lifecycle.
    if [[ "$(yq -r '.metadata.annotations."kubernetes.io/config.mirror" != null' <<<"$pod_json")" == true ]]; then
      continue
    fi
    case "$(yq -r '.status.phase // ""' <<<"$pod_json")" in
      Succeeded|Failed) continue ;;
    esac
    kind="$(yq -r '[.metadata.ownerReferences[]? | select(.controller == true) | .kind][0] // ""' <<<"$pod_json")"
    [[ -n "$kind" && "$kind" != 'DaemonSet' ]] || continue
    namespace="$(yq -r '.metadata.namespace' <<<"$pod_json")"
    pod_name="$(yq -r '.metadata.name' <<<"$pod_json")"
    if [[ "$namespace:$kind" == 'longhorn-system:InstanceManager' &&
      "$(yq -r '[.metadata.ownerReferences[]? | select(.controller == true) | .apiVersion][0]' <<<"$pod_json")" == 'longhorn.io/v1beta2' &&
      "$(yq -r '.metadata.labels."longhorn.io/component"' <<<"$pod_json")" == 'instance-manager' &&
      "$(yq -r '.metadata.labels."longhorn.io/node"' <<<"$pod_json")" == "$node" ]]; then
      # Longhorn evacuation and convergence validate this node-local infrastructure.
      continue
    fi
    owner_uid="$(yq -r '[.metadata.ownerReferences[]? | select(.controller == true) | .uid][0] // ""' <<<"$pod_json")"
    [[ -n "$owner_uid" ]] || return 1
    if [[ "$kind" == Job ]]; then
      job_name="$(yq -r '[.metadata.ownerReferences[]? | select(.controller == true) | .name][0] // ""' <<<"$pod_json")"
      [[ -n "$job_name" ]] || return 1
      job="$(drain_kubectl "$kubeconfig" --namespace "$namespace" get job "$job_name" --output json)" || return 1
      [[ "$(yq -r '.metadata.uid' <<<"$job")" == "$owner_uid" ]] || {
        echo "Job identity changed for $namespace/$job_name." >&2
        return 1
      }
      if [[ "$(yq -r '[.status.conditions[]? | select(.type == "Complete") | .status][0] // "False"' <<<"$job")" == True ]]; then
        continue
      fi
    fi
    selector="$(yq -r '.metadata.labels // {} | to_entries | map(.key + "=" + .value) | join(",")' <<<"$pod_json")"
    [[ -n "$selector" ]] || {
      echo "Cannot identify a replacement for $namespace/$pod_name without labels." >&2
      return 1
    }
    candidates="$(drain_kubectl "$kubeconfig" --namespace "$namespace" get pods \
      --selector "$selector" --output json)" || return 1
    replacement="$(TARGET="$node" OWNER_UID="$owner_uid" yq -o=json -I=0 '
      [.items[] |
        select(.spec.nodeName != strenv(TARGET)) |
        select([.metadata.ownerReferences[]? |
          select(.controller == true and .uid == strenv(OWNER_UID))] | length == 1) |
        select(.metadata.deletionTimestamp == null) |
        select(.status.phase == "Running") |
        select([.status.conditions[]? | select(.type == "Ready") | .status][0] == "True")][0] // ""
    ' <<<"$candidates")"
    [[ -n "$replacement" && "$replacement" != '""' ]] || {
      echo "No Ready surviving replacement exists for $namespace/$pod_name." >&2
      return 1
    }
    claims="$(yq -r '.spec.volumes[]? | select(.persistentVolumeClaim != null) | .persistentVolumeClaim.claimName' <<<"$pod_json")"
    while IFS= read -r claim; do
      [[ -n "$claim" ]] || continue
      CLAIM="$claim" yq --exit-status \
        '[.spec.volumes[]? | select(.persistentVolumeClaim.claimName == strenv(CLAIM))] | length > 0' \
        <<<"$replacement" >/dev/null || {
        echo "Replacement for $namespace/$pod_name does not mount PVC $claim." >&2
        return 1
      }
    done <<<"$claims"
  done < <(yq -o=json -I=0 '.items[]' "$inventory_file")

  while IFS= read -r claim_json; do
    [[ -n "$claim_json" ]] || continue
    namespace="$(yq -r '.namespace' <<<"$claim_json")"
    claim="$(yq -r '.name' <<<"$claim_json")"
    expected_pvc_uid="$(yq -r '.uid' <<<"$claim_json")"
    expected_volume="$(yq -r '.volumeName' <<<"$claim_json")"
    expected_volume_uid="$(yq -r '.volumeUid' <<<"$claim_json")"
    pvc="$(drain_kubectl "$kubeconfig" --namespace "$namespace" get pvc "$claim" \
      --output json)" || return 1
    [[ "$(yq -r '.metadata.uid // ""' - <<<"$pvc")" == "$expected_pvc_uid" &&
      "$(yq -r '.spec.volumeName // ""' - <<<"$pvc")" == "$expected_volume" ]] || {
      echo "PVC identity changed for $namespace/$claim." >&2
      return 1
    }
    pv="$(drain_kubectl "$kubeconfig" get pv "$expected_volume" --output json)" || return 1
    [[ "$(yq -r '.metadata.uid // ""' - <<<"$pv")" == "$expected_volume_uid" ]] || {
      echo "PV identity changed for $namespace/$claim." >&2
      return 1
    }
  done < <(yq -o=json -I=0 '.homelabLifecycle.claims[]?' "$inventory_file")
}

wait_for_workload_replacements() {
  local kubeconfig="$1" node="$2" inventory_file="$3"
  local attempt attempts="${NODE_WORKLOAD_REPLACEMENT_ATTEMPTS:-60}"
  [[ "$attempts" =~ ^[1-9][0-9]*$ ]] || return 1
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    verify_workload_replacements "$kubeconfig" "$node" "$inventory_file" && return 0
    if [[ "$attempt" -lt "$attempts" ]]; then
      "${NODE_SLEEP:-sleep}" "${NODE_WORKLOAD_REPLACEMENT_POLL_SECONDS:-5}"
    fi
  done
  echo "Workload recovery did not complete after $attempts observations for $node." >&2
  return 1
}
