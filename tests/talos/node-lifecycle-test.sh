#!/usr/bin/env bash
# shellcheck disable=SC1091,SC2016,SC2031,SC2218,SC2329
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
export REPO_ROOT
source "$REPO_ROOT/roles/talos_lifecycle/files/lib/node-lifecycle-state.sh"
source "$REPO_ROOT/roles/talos_lifecycle/files/node/common.sh"
source "$REPO_ROOT/roles/talos_lifecycle/files/node/longhorn.sh"
source "$REPO_ROOT/roles/talos_lifecycle/files/node/drain.sh"
source "$REPO_ROOT/roles/talos_lifecycle/files/node/recovery.sh"
source "$REPO_ROOT/roles/talos_lifecycle/files/node/lifecycle.sh"

state_dir="$(mktemp -d "${TMPDIR:-/tmp}/homelab-node-lifecycle-test.XXXXXX")"
trap 'rm -rf -- "$state_dir"' EXIT

context_log="$state_dir/context-calls"
cat >"$state_dir/fake-client" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$CONTEXT_LOG"
printf '%s\n' '{}'
EOF
chmod +x "$state_dir/fake-client"
CONTEXT_LOG="$context_log" NODE_KUBECTL="$state_dir/fake-client" \
  node_kubectl /operator/kubeconfig get nodes >/dev/null
CONTEXT_LOG="$context_log" NODE_TALOSCTL="$state_dir/fake-client" \
  lifecycle_talosctl --talosconfig /operator/talosconfig get hostname >/dev/null
rg -q '^--kubeconfig /operator/kubeconfig --context fixture get nodes$' "$context_log"
rg -q '^--context fixture --talosconfig /operator/talosconfig get hostname$' "$context_log"

fail() {
  echo "$*" >&2
  exit 1
}

assert_fails() {
  local description="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    fail "$description"
  fi
}

assert_fails_with() {
  local description="$1"
  local expected="$2"
  shift 2
  local output
  if output="$("$@" 2>&1)"; then
    fail "$description"
  fi
  rg -Fq -- "$expected" <<<"$output" ||
    fail "$description: missing diagnostic '$expected' in: $output"
}


# Exercise the real entrypoint in a child shell so EXIT traps run after failure.
cat >"$state_dir/cleanup-fixture.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
source "$REPO_ROOT/roles/talos_lifecycle/files/node/lifecycle.sh"
resolve_node_target() { NODE_NAME=node-a; NODE_IP=192.0.2.1; }
require_operator_checkout() { :; }
acquire_test_lease() { :; }
start_test_lease_renewal() { :; }
stop_test_lease_renewal() { echo stopped >> "$FIXTURE_LOG"; }
release_test_lease() {
  echo released >> "$FIXTURE_LOG"
  [[ "${FAIL_RELEASE:-false}" != true ]]
}
assert_cluster_disruption_admissible() { :; }
read_node_lifecycle_record() { echo '{"schemaVersion":1,"kind":"reboot"}'; }
lifecycle_record_kind() { echo reboot; }
require_exact_confirmation() { :; }
run_maintenance_exit_transaction() { return 23; }
node_lifecycle_main "$FIXTURE_PREPARED"
EOF
mkdir -p "$state_dir/clusterconfig"
: >"$state_dir/clusterconfig/node-a.yaml"
: >"$state_dir/cleanup.log"
CONFIG_PATH="$state_dir/cleanup.log" yq -n -o=json \
  '{
    "action":"maintenance-exit", "node":"node-a", "address":"192.0.2.10",
    "holder":"fixture-holder", "talos_kubeconfig":strenv(CONFIG_PATH),
    "talos_talosconfig":strenv(CONFIG_PATH), "talos_kube_context":"fixture",
    "talos_talos_context":"fixture", "talos_confirmation":"accept:node-a:reboot",
    "talos_endpoints":["192.0.2.10","192.0.2.11","192.0.2.12"],
    "tools":{"kubectl":"kubectl","talosctl":"talosctl","python3":"python3"}
  }' >"$state_dir/prepared.json"
cleanup_status=0
REPO_ROOT="$REPO_ROOT" FIXTURE_PREPARED="$state_dir/prepared.json" \
  FIXTURE_ACTION=lifecycle FIXTURE_DIR="$state_dir" FIXTURE_LOG="$state_dir/cleanup.log" \
  bash "$state_dir/cleanup-fixture.sh" >"$state_dir/cleanup-output" 2>&1 || cleanup_status=$?
if [[ "$cleanup_status" != 23 ]]; then
  cat "$state_dir/cleanup-output" >&2
fi
[[ "$cleanup_status" == 23 ]] || fail 'lifecycle cleanup lost the original failure status.'
rg -q '^released$' "$state_dir/cleanup.log" || fail 'lifecycle cleanup did not release its Lease.'
if rg -q 'unbound variable' "$state_dir/cleanup-output"; then
  fail 'lifecycle cleanup used expired local variables.'
fi

: >"$state_dir/cleanup.log"
cleanup_status=0
REPO_ROOT="$REPO_ROOT" FIXTURE_PREPARED="$state_dir/prepared.json" FAIL_RELEASE=true \
  FIXTURE_LOG="$state_dir/cleanup.log" bash "$state_dir/cleanup-fixture.sh" \
  >"$state_dir/cleanup-output" 2>&1 || cleanup_status=$?
[[ "$cleanup_status" == 23 ]] || fail 'cleanup failure replaced the primary status.'
rg -q 'cleanup also failed' "$state_dir/cleanup-output" || \
  fail 'cleanup failure was not reported alongside the primary status.'


reboot_record='{"schemaVersion":1,"kind":"reboot"}'
abrupt_record='{"schemaVersion":1,"kind":"abrupt-loss"}'
maintenance_record='{
  "schemaVersion": 1,
  "kind": "maintenance",
  "longhorn": {
    "allowScheduling": {"before": true, "during": false},
    "evictionRequested": {"before": false, "during": true}
  }
}'

for record in "$reboot_record" "$abrupt_record" "$maintenance_record"; do
  validate_lifecycle_record "$record"
done
[[ "$(lifecycle_record_kind "$maintenance_record")" == 'maintenance' ]]

invalid_records=(
  'not-json'
  '{}'
  '{"schemaVersion":2,"kind":"reboot"}'
  '{"schemaVersion":1,"kind":"unsupported"}'
  '{"schemaVersion":1,"kind":"reboot","step":"drained"}'
  '{"schemaVersion":1,"kind":"reboot","longhorn":{}}'
  '{"schemaVersion":1,"kind":"maintenance"}'
  '{"schemaVersion":1,"kind":"maintenance","extra":true,"longhorn":{"allowScheduling":{"before":true,"during":false},"evictionRequested":{"before":false,"during":true}}}'
  '{"schemaVersion":1,"kind":"maintenance","longhorn":{"allowScheduling":{"before":true,"during":true},"evictionRequested":{"before":false,"during":true}}}'
  '{"schemaVersion":1,"kind":"maintenance","longhorn":{"allowScheduling":{"before":true,"during":false},"evictionRequested":{"before":false,"during":false}}}'
)
for record in "${invalid_records[@]}"; do
  assert_fails "Invalid lifecycle record was accepted: $record" \
    validate_lifecycle_record "$record"
done

[[ "$(compare_owned_value false true false)" == 'restore' ]]
[[ "$(compare_owned_value true true false)" == 'noop' ]]
assert_fails 'Conflicting lifecycle-owned value was accepted.' \
  compare_owned_value changed true false

node_fixture=''
lifecycle_kubectl() {
  local _kubeconfig="$1"
  shift
  [[ "$*" == 'get nodes --output json' ]] || return 2
  printf '%s\n' "$node_fixture"
}

node_json() {
  local node_one_ready="$1"
  local node_one_unschedulable="$2"
  local node_one_record="$3"
  local node_two_ready="$4"
  local node_two_unschedulable="$5"
  local node_two_record="$6"
  NODE_ONE_READY="$node_one_ready" \
  NODE_ONE_UNSCHEDULABLE="$node_one_unschedulable" \
  NODE_ONE_RECORD="$node_one_record" \
  NODE_TWO_READY="$node_two_ready" \
  NODE_TWO_UNSCHEDULABLE="$node_two_unschedulable" \
  NODE_TWO_RECORD="$node_two_record" \
    yq --null-input --output-format json '
      {
        "items": [
          {
            "metadata": {
              "name": "node-a",
              "annotations": {
                "homelab.supermorphic.com/node-lifecycle": strenv(NODE_ONE_RECORD)
              }
            },
            "spec": {"unschedulable": (strenv(NODE_ONE_UNSCHEDULABLE) == "true")},
            "status": {"conditions": [{"type": "Ready", "status": strenv(NODE_ONE_READY)}]}
          },
          {
            "metadata": {
              "name": "node-b",
              "annotations": {
                "homelab.supermorphic.com/node-lifecycle": strenv(NODE_TWO_RECORD)
              }
            },
            "spec": {"unschedulable": (strenv(NODE_TWO_UNSCHEDULABLE) == "true")},
            "status": {"conditions": [{"type": "Ready", "status": strenv(NODE_TWO_READY)}]}
          }
        ]
      }
    '
}

node_fixture="$(node_json True false '' True false '')"
assert_cluster_disruption_admissible fake-kubeconfig

node_fixture="$(node_json False false '' True false '')"
assert_fails 'An unannotated NotReady node did not block disruption.' \
  assert_cluster_disruption_admissible fake-kubeconfig

node_fixture="$(node_json True true '' True false '')"
assert_fails 'An unexpected cordon did not block disruption.' \
  assert_cluster_disruption_admissible fake-kubeconfig

node_fixture="$(node_json True true "$reboot_record" True false '')"
assert_fails 'An active lifecycle record did not block a new disruption.' \
  assert_cluster_disruption_admissible fake-kubeconfig
assert_cluster_disruption_admissible fake-kubeconfig node-a

node_fixture="$(node_json False true "$abrupt_record" True false '')"
assert_cluster_disruption_admissible fake-kubeconfig node-a

node_fixture="$(node_json True false "$reboot_record" True false '')"
assert_fails 'A lifecycle record without its cordon was accepted for recovery.' \
  assert_cluster_disruption_admissible fake-kubeconfig node-a

node_fixture="$(node_json True true 'malformed' True false '')"
assert_fails 'A malformed lifecycle record was accepted for recovery.' \
  assert_cluster_disruption_admissible fake-kubeconfig node-a

node_fixture="$(node_json True true "$maintenance_record" True true "$reboot_record")"
assert_fails 'Lifecycle state on a second node did not block recovery.' \
  assert_cluster_disruption_admissible fake-kubeconfig node-a

resolve_node_target node-a
[[ "$NODE_NAME" == 'node-a' && "$NODE_IP" == '192.0.2.10' ]]
resolve_node_target node-c
[[ "$NODE_NAME" == 'node-c' && "$NODE_IP" == '192.0.2.12' ]]
assert_fails 'An unknown node target was accepted.' resolve_node_target other

NODE_ALLOW_LINKED_WORKTREE_FOR_TESTS=true require_operator_checkout

run_operator_checkout_fixture() (
  local layout="$1"
  local fake_git_dir='/fixture/repo/.git'
  local fake_git_common_dir='/fixture/repo/.git'
  if [[ "$layout" == 'linked' ]]; then
    fake_git_dir='/fixture/repo/.git/worktrees/task'
  fi
  git() {
    case "$*" in
      'rev-parse --path-format=absolute --git-dir')
        printf '%s\n' "$fake_git_dir"
        ;;
      'rev-parse --path-format=absolute --git-common-dir')
        printf '%s\n' "$fake_git_common_dir"
        ;;
      'rev-parse --show-superproject-working-tree')
        printf '\n'
        ;;
      *) return 64 ;;
    esac
  }
  require_operator_checkout
)

assert_fails 'The linked worktree was accepted as an operator checkout.' \
  run_operator_checkout_fixture linked
run_operator_checkout_fixture standalone

health_nodes_fixture=''
node_kubectl() {
  local _kubeconfig="$1"
  shift
  [[ "$*" == 'get nodes --output json' ]] || return 2
  printf '%s\n' "$health_nodes_fixture"
}

health_nodes_json() {
  local node_a_memory_pressure="$1"
  NUC1_MEMORY_PRESSURE="$node_a_memory_pressure" yq --null-input --output-format json '
    {
      "items": ["node-a", "node-b", "node-c"] | map({
        "metadata": {"name": .},
        "spec": {"unschedulable": false},
        "status": {"conditions": [
          {"type": "Ready", "status": "True"},
          {"type": "MemoryPressure", "status": "False"},
          {"type": "DiskPressure", "status": "False"},
          {"type": "PIDPressure", "status": "False"}
        ]}
      })
    } |
    .items[0].status.conditions[1].status = strenv(NUC1_MEMORY_PRESSURE)
  '
}

health_nodes_fixture="$(health_nodes_json False)"
verify_expected_node_health fake-kubeconfig
health_nodes_fixture="$(health_nodes_json True)"
assert_fails 'Memory pressure did not block node maintenance.' \
  verify_expected_node_health fake-kubeconfig
health_nodes_fixture="$(health_nodes_json False | \
  yq 'del(.items[0].status.conditions[] | select(.type == "PIDPressure"))')"
assert_fails 'A missing pressure condition did not block node maintenance.' \
  verify_expected_node_health fake-kubeconfig

etcd_members_fixture='NODE ID HOSTNAME PEER CLIENT LEARNER
192.0.2.10 member-2 node-b peer-2 client-2 false
192.0.2.10 member-3 node-c peer-3 client-3 false
192.0.2.10 member-1 node-a peer-1 client-1 false
192.0.2.11 member-2 node-b peer-2 client-2 false
192.0.2.11 member-3 node-c peer-3 client-3 false
192.0.2.11 member-1 node-a peer-1 client-1 false
192.0.2.12 member-2 node-b peer-2 client-2 false
192.0.2.12 member-3 node-c peer-3 client-3 false
192.0.2.12 member-1 node-a peer-1 client-1 false'
healthy_etcd_members_fixture="$etcd_members_fixture"
etcd_status_fixture='NODE  MEMBER  DB SIZE  IN USE  LEADER  RAFT INDEX
192.0.2.10  member-1  1 MB  1 MB  member-3  10
192.0.2.11  member-2  1 MB  1 MB  member-3  10
192.0.2.12  member-3  1 MB  1 MB  member-3  10'
healthy_etcd_status_fixture="$etcd_status_fixture"
etcd_alarms_fixture=''
recovery_talosctl() {
  case "$1 $2" in
    'etcd members') printf '%s\n' "$etcd_members_fixture" ;;
    'etcd status') printf '%s\n' "$etcd_status_fixture" ;;
    'etcd alarm')
      [[ "$3" == 'list' ]] || return 2
      printf '%s' "$etcd_alarms_fixture"
      ;;
    *) return 2 ;;
  esac
}

verify_etcd_recovery fake-talosconfig
etcd_members_fixture="$(printf '%s\n' "$etcd_members_fixture" | awk '$3 != "node-c"')"
assert_fails_with 'A missing etcd member did not block node maintenance.' \
  'Expected etcd members node-a, node-b, and node-c' \
  verify_etcd_recovery fake-talosconfig
etcd_members_fixture="$healthy_etcd_members_fixture"
etcd_status_fixture="$(printf '%s\n' "$etcd_status_fixture" | awk '$2 != "member-3"')"
assert_fails_with 'A missing etcd status row did not block node maintenance.' \
  'Expected three etcd status rows with one leader' \
  verify_etcd_recovery fake-talosconfig
etcd_status_fixture="$healthy_etcd_status_fixture"
etcd_alarms_fixture=$'NODE  MEMBER  ALARM\n192.0.2.10  member-1  NOSPACE\n'
assert_fails_with 'An active etcd alarm did not block node maintenance.' \
  'Active etcd alarms block node lifecycle operations' \
  verify_etcd_recovery fake-talosconfig

node_state="$state_dir/node-state.json"
yq --null-input --output-format json '
  {
    "apiVersion": "v1",
    "kind": "Node",
    "metadata": {"name": "node-a", "uid": "node-a-uid", "resourceVersion": "7", "annotations": {}},
    "spec": {"unschedulable": false},
    "status": {"conditions": [{"type": "Ready", "status": "True"}]}
  }
' >"$node_state"
node_conflict=false
node_readback_mismatch=false
node_uid_changed=false
node_kubectl() {
  local _kubeconfig="$1"
  shift
  local operation="$1"
  shift
  case "$operation" in
    get)
      [[ "$1 $2 $3" == 'node node-a --output' && "$4" == 'json' ]] || return 2
      cat "$node_state"
      ;;
    replace)
      [[ "$1 $2" == '--filename -' ]] || return 2
      replacement="$(cat)"
      [[ "$node_conflict" != 'true' ]] || return 1
      current_version="$(yq -r '.metadata.resourceVersion' "$node_state")"
      [[ "$(yq -r '.metadata.resourceVersion' - <<<"$replacement")" == "$current_version" ]] || return 1
      if [[ "$node_readback_mismatch" != 'true' ]]; then
        NEXT_VERSION="$((current_version + 1))" \
          yq '.metadata.resourceVersion = strenv(NEXT_VERSION)' \
          <<<"$replacement" >"$state_dir/node-next.json"
        mv "$state_dir/node-next.json" "$node_state"
        if [[ "$node_uid_changed" == 'true' ]]; then
          yq '.metadata.uid = "replacement-node-uid"' "$node_state" >"$state_dir/node-next.json"
          mv "$state_dir/node-next.json" "$node_state"
        fi
      fi
      ;;
    *) return 2 ;;
  esac
}

persist_node_containment fake-kubeconfig node-a "$reboot_record"
[[ "$(yq -r '.spec.unschedulable' "$node_state")" == 'true' ]]
[[ "$(ANNOTATION="$NODE_LIFECYCLE_ANNOTATION" \
  yq -r '.metadata.annotations[strenv(ANNOTATION)]' "$node_state")" == "$reboot_record" ]]

remove_node_containment_and_uncordon fake-kubeconfig node-a "$reboot_record" recovery-accepted
[[ "$(yq -r '.spec.unschedulable' "$node_state")" == 'false' ]]
[[ "$(ANNOTATION="$NODE_LIFECYCLE_ANNOTATION" \
  yq -r '.metadata.annotations[strenv(ANNOTATION)] // ""' "$node_state")" == '' ]]
assert_fails 'Final Node transition did not require recovery acceptance.' \
  remove_node_containment_and_uncordon fake-kubeconfig node-a "$reboot_record" not-accepted

node_conflict=true
assert_fails 'A resource-version conflict was accepted.' \
  persist_node_containment fake-kubeconfig node-a "$reboot_record"
node_conflict=false
node_readback_mismatch=true
assert_fails 'A containment read-back mismatch was accepted.' \
  persist_node_containment fake-kubeconfig node-a "$reboot_record"
node_readback_mismatch=false

yq '.metadata.uid = "node-a-uid" | .metadata.annotations = {} | .spec.unschedulable = false' \
  "$node_state" >"$state_dir/node-next.json"
mv "$state_dir/node-next.json" "$node_state"
node_uid_changed=true
assert_fails 'A changed Node UID was accepted as owned containment.' \
  persist_node_containment fake-kubeconfig node-a "$reboot_record"
node_uid_changed=false

longhorn_state="$state_dir/longhorn-state.json"
yq --null-input --output-format json '
  {
    "apiVersion": "longhorn.io/v1beta2",
    "kind": "Node",
    "metadata": {"name": "node-a", "namespace": "longhorn-system", "resourceVersion": "20"},
    "spec": {"allowScheduling": true, "evictionRequested": false}
  }
' >"$longhorn_state"
longhorn_replace_count=0
longhorn_write_conflict=false
longhorn_kubectl() {
  local _kubeconfig="$1"
  shift
  local operation="$1"
  shift
  case "$operation" in
    get)
      [[ "$1 $2 $3 $4" == 'nodes.longhorn.io node-a --output json' ]] || return 2
      cat "$longhorn_state"
      ;;
    replace)
      [[ "$1 $2" == '--filename -' ]] || return 2
      replacement="$(cat)"
      [[ "$longhorn_write_conflict" == false ]] || return 1
      current_version="$(yq -r '.metadata.resourceVersion' "$longhorn_state")"
      [[ "$(yq -r '.metadata.resourceVersion' - <<<"$replacement")" == "$current_version" ]] || return 1
      NEXT_VERSION="$((current_version + 1))" \
        yq '.metadata.resourceVersion = strenv(NEXT_VERSION)' \
        <<<"$replacement" >"$state_dir/longhorn-next.json"
      mv "$state_dir/longhorn-next.json" "$longhorn_state"
      longhorn_replace_count=$((longhorn_replace_count + 1))
      ;;
    *) return 2 ;;
  esac
}

captured_record="$(build_maintenance_lifecycle_record fake-kubeconfig node-a)"
yq '.metadata.resourceVersion = "21" | .status.conditions = []' "$longhorn_state" >"$state_dir/longhorn-next.json"
mv "$state_dir/longhorn-next.json" "$longhorn_state"
[[ "$(yq -o=json -I=0 '.' - <<<"$captured_record")" == \
  "$(yq -o=json -I=0 '.' - <<<"$maintenance_record")" ]]
apply_longhorn_maintenance_state fake-kubeconfig node-a "$captured_record"
[[ "$(yq -r '[.spec.allowScheduling, .spec.evictionRequested] | join(" ")' "$longhorn_state")" == 'false true' ]]
restore_longhorn_maintenance_state fake-kubeconfig node-a "$captured_record"
[[ "$(yq -r '[.spec.allowScheduling, .spec.evictionRequested] | join(" ")' "$longhorn_state")" == 'true false' ]]
restored_replace_count="$longhorn_replace_count"
restore_longhorn_maintenance_state fake-kubeconfig node-a "$captured_record"
[[ "$longhorn_replace_count" == "$restored_replace_count" ]]
apply_longhorn_maintenance_state fake-kubeconfig node-a "$captured_record"
restore_longhorn_maintenance_state fake-kubeconfig node-a "$captured_record"
longhorn_write_conflict=true
assert_fails 'A Longhorn write conflict did not stop maintenance entry.' \
  apply_longhorn_maintenance_state fake-kubeconfig node-a "$captured_record"
longhorn_write_conflict=false

yq '.spec.allowScheduling = false | .spec.evictionRequested = false' \
  "$longhorn_state" >"$state_dir/longhorn-conflict.json"
mv "$state_dir/longhorn-conflict.json" "$longhorn_state"
assert_fails 'Changed Longhorn owned settings were overwritten during entry.' \
  apply_longhorn_maintenance_state fake-kubeconfig node-a "$captured_record"
restore_longhorn_maintenance_state fake-kubeconfig node-a "$captured_record"
[[ "$(yq -r '[.spec.allowScheduling, .spec.evictionRequested] | join(" ")' "$longhorn_state")" == 'true false' ]]

yq '.spec.allowScheduling = "external"' \
  "$longhorn_state" >"$state_dir/longhorn-conflict.json"
mv "$state_dir/longhorn-conflict.json" "$longhorn_state"
conflict_replace_count="$longhorn_replace_count"
assert_fails 'Conflicting live Longhorn state was overwritten.' \
  restore_longhorn_maintenance_state fake-kubeconfig node-a "$captured_record"
[[ "$longhorn_replace_count" == "$conflict_replace_count" ]]

short_absence_volumes='{"items":[{
  "metadata":{"name":"detached-volume"},
  "spec":{"numberOfReplicas":2},
  "status":{"state":"detached","robustness":"unknown"}
}]}'
short_absence_replicas='{"items":[
  {"metadata":{"name":"replica-node-b"},"spec":{"nodeID":"node-b","volumeName":"detached-volume","failedAt":""}},
  {"metadata":{"name":"replica-node-c"},"spec":{"nodeID":"node-c","volumeName":"detached-volume","failedAt":""}}
]}'
longhorn_kubectl() {
  local _kubeconfig="$1"
  shift
  case "$*" in
    'get settings.longhorn.io node-drain-policy --output jsonpath={.value}')
      printf '%s\n' 'block-if-contains-last-replica'
      ;;
    'get volumes.longhorn.io --output json') printf '%s\n' "$short_absence_volumes" ;;
    'get replicas.longhorn.io --output json') printf '%s\n' "$short_absence_replicas" ;;
    *) return 2 ;;
  esac
}
verify_short_absence_longhorn_safety fake-kubeconfig node-b
recovery_kubectl() {
  case "$*" in
    'fake-kubeconfig --namespace longhorn-system get nodes.longhorn.io --output json')
      printf '%s\n' '{"items":[{"status":{"conditions":[{"type":"Ready","status":"True"}]}},{"status":{"conditions":[{"type":"Ready","status":"True"}]}},{"status":{"conditions":[{"type":"Ready","status":"True"}]}}]}'
      ;;
    'fake-kubeconfig --namespace longhorn-system get volumes.longhorn.io --output json')
      printf '%s\n' "$short_absence_volumes" ;;
    'fake-kubeconfig --namespace longhorn-system get replicas.longhorn.io --output json')
      printf '%s\n' "$short_absence_replicas" ;;
    *) return 2 ;;
  esac
}
verify_longhorn_convergence fake-kubeconfig
short_absence_volumes="$(yq '.items[0].status = {"state":"attached","robustness":"healthy"}' <<<"$short_absence_volumes")"
verify_longhorn_convergence fake-kubeconfig
short_absence_volumes="$(yq '.items[0].status = {"state":"detached","robustness":"unknown"}' <<<"$short_absence_volumes")"
longhorn_evacuation_complete fake-kubeconfig node-a
assert_fails 'Evacuation accepted replicas still on the target.' \
  longhorn_evacuation_complete fake-kubeconfig node-b
short_absence_replicas="$(yq 'del(.items[1])' <<<"$short_absence_replicas")"
assert_fails 'Recovery accepted a detached volume with a missing replica.' \
  verify_longhorn_convergence fake-kubeconfig
assert_fails 'Evacuation accepted a detached volume with a missing replica.' \
  longhorn_evacuation_complete fake-kubeconfig node-a
assert_fails 'A detached Longhorn volume with a missing replica was accepted.' \
  verify_short_absence_longhorn_safety fake-kubeconfig node-b
short_absence_replicas='{"items":[
  {"metadata":{"name":"replica-node-b"},"spec":{"nodeID":"node-b","volumeName":"detached-volume","failedAt":""}},
  {"metadata":{"name":"replica-node-c"},"spec":{"nodeID":"node-c","volumeName":"detached-volume","failedAt":""}}
]}'
short_absence_volumes="$(yq '.items[0].spec.numberOfReplicas = null' <<<"$short_absence_volumes")"
assert_fails 'A detached Longhorn volume without a valid replica target was accepted.' \
  verify_short_absence_longhorn_safety fake-kubeconfig node-b

controlled_pods='{
  "items": [
    {
      "metadata": {
        "namespace": "media",
        "name": "plex-abc",
        "labels": {"app.kubernetes.io/name": "plex"},
        "ownerReferences": [{"kind": "ReplicaSet", "name": "plex-123", "controller": true}]
      },
      "spec": {
        "volumes": [
          {"name": "config", "persistentVolumeClaim": {"claimName": "plex-config"}},
          {"name": "transcode", "emptyDir": {}}
        ]
      }
    },
    {
      "metadata": {
        "namespace": "kube-system",
        "name": "cilium-abc",
        "ownerReferences": [{"kind": "DaemonSet", "name": "cilium", "controller": true}]
      },
      "spec": {"volumes": []}
    }
  ]
}'
validate_drain_pods "$controlled_pods" >/dev/null
empty_dir_report="$(report_drain_local_data "$controlled_pods")"
rg -q 'media/plex-abc.*transcode' <<<"$empty_dir_report"

unmanaged_pods='{
  "items": [{
    "metadata": {"namespace": "default", "name": "unmanaged", "ownerReferences": []},
    "spec": {"volumes": []}
  }]
}'
assert_fails 'An unmanaged Pod was accepted for drain.' \
  validate_drain_pods "$unmanaged_pods"

mirror_pods='{
  "items": [{
    "metadata": {
      "namespace": "kube-system",
      "name": "static",
      "annotations": {"kubernetes.io/config.mirror": "fixture"}
    },
    "spec": {"volumes": []}
  }]
}'
validate_drain_pods "$mirror_pods" >/dev/null

mirror_inventory="$state_dir/mirror-inventory.json"
yq -o=json '
  .items[0].metadata.ownerReferences = [{"apiVersion":"v1","kind":"Node","name":"node-a","uid":"node-uid","controller":true}] |
  .items[0].metadata.labels = {"component":"kube-apiserver"} |
  .items[0].spec.nodeName = "node-a" |
  .items[0].status.phase = "Running"
' <<<"$mirror_pods" >"$mirror_inventory"
verify_workload_replacements fake-kubeconfig node-a "$mirror_inventory"
yq 'del(.items[0].metadata.annotations."kubernetes.io/config.mirror")' \
  "$mirror_inventory" >"$state_dir/non-mirror-inventory.json"
assert_fails 'A Node owner without a mirror annotation bypassed replacement checks.' \
  verify_workload_replacements fake-kubeconfig node-a "$state_dir/non-mirror-inventory.json"

captured_inventory="$state_dir/captured-inventory.json"
drain_kubectl() {
  local _kubeconfig="$1"
  shift
  case "$*" in
    'get pods --all-namespaces --field-selector spec.nodeName=node-a --output json')
      printf '%s\n' "$controlled_pods"
      ;;
    '--namespace media get pvc plex-config --output json')
      printf '%s\n' '{"metadata":{"uid":"pvc-uid"},"spec":{"volumeName":"pv-plex"}}'
      ;;
    'get pv pv-plex --output json')
      printf '%s\n' '{"metadata":{"uid":"pv-uid"}}'
      ;;
    *) return 2 ;;
  esac
}
capture_drain_inventory fake-kubeconfig node-a "$captured_inventory"
[[ "$(yq -r '.homelabLifecycle.claims[0] | [.namespace, .name, .uid, .volumeName, .volumeUid] | join(" ")' \
  "$captured_inventory")" == 'media plex-config pvc-uid pv-plex pv-uid' ]]

drain_calls="$state_dir/drain-calls"
touch "$drain_calls"
eviction_available=true
drain_kubectl() {
  local _kubeconfig="$1"
  shift
  printf '%s\n' "$*" >>"$drain_calls"
  if [[ "$*" == 'get --raw /api/v1' ]]; then
    if [[ "$eviction_available" == 'true' ]]; then
      printf '%s\n' '{"resources":[{"name":"pods/eviction","kind":"Eviction"}]}'
    else
      printf '%s\n' '{"resources":[]}'
    fi
  fi
}

preflight_kubernetes_drain fake-kubeconfig node-a
preflight_drain_command="$(tail -1 "$drain_calls")"
rg -q '^get --raw /api/v1$' "$drain_calls"
if rg -q '^get --raw /apis/policy/v1$' "$drain_calls"; then
  fail 'Pod eviction discovery used the policy API group instead of core/v1.'
fi
rg -q -- '--dry-run=client' <<<"$preflight_drain_command"
if rg -q -- '--dry-run=server' <<<"$preflight_drain_command"; then
  fail "Scoped maintenance preflight requested mutation authority: $preflight_drain_command"
fi

eviction_available=false
assert_fails_with 'Preflight ran without the core/v1 pods/eviction resource.' \
  'Kubernetes core/v1 does not advertise the pods/eviction resource' \
  preflight_kubernetes_drain fake-kubeconfig node-a
eviction_available=true

perform_kubernetes_drain fake-kubeconfig node-a
drain_command="$(tail -1 "$drain_calls")"
rg -q '^drain node-a ' <<<"$drain_command"
rg -q -- '--ignore-daemonsets' <<<"$drain_command"
rg -q -- '--delete-emptydir-data' <<<"$drain_command"
rg -q -- '--timeout=' <<<"$drain_command"
if rg -q -- '--disable-eviction|--force' <<<"$drain_command"; then
  fail "Unsafe drain flag found: $drain_command"
fi

eviction_available=false
before_refusal_lines="$(wc -l <"$drain_calls" | tr -d ' ')"
assert_fails 'Drain ran without the policy/v1 Eviction resource.' \
  perform_kubernetes_drain fake-kubeconfig node-a
after_refusal_lines="$(wc -l <"$drain_calls" | tr -d ' ')"
[[ "$after_refusal_lines" -eq $((before_refusal_lines + 1)) ]]

replacement_pods='{
  "items": [{
    "metadata": {
      "namespace": "media",
      "name": "plex-new",
      "labels": {"app.kubernetes.io/name": "plex"},
      "ownerReferences": [{"kind": "ReplicaSet", "name": "plex-123", "uid": "owner-uid", "controller": true}]
    },
    "spec": {
      "nodeName": "node-b",
      "volumes": [{"name": "config", "persistentVolumeClaim": {"claimName": "plex-config"}}]
    },
    "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}
  }]
}'
workload_inventory="$state_dir/workload-inventory.json"
OWNER_UID='owner-uid' yq --output-format json '
  .items[0].metadata.ownerReferences[0].uid = strenv(OWNER_UID) |
  .homelabLifecycle.claims = [{
    "namespace": "media",
    "name": "plex-config",
    "uid": "pvc-uid",
    "volumeName": "pv-plex",
    "volumeUid": "pv-uid"
  }]
' <<<"$controlled_pods" >"$workload_inventory"
pvc_uid='pvc-uid'
drain_kubectl() {
  local _kubeconfig="$1"
  shift
  case "$*" in
    '--namespace media get pods --selector '*'-o json'|'--namespace media get pods --selector '*'--output json')
      printf '%s\n' "$replacement_pods"
      ;;
    '--namespace media get pvc plex-config --output json')
      PVC_UID="$pvc_uid" yq --null-input --output-format json \
        '{"metadata":{"uid":strenv(PVC_UID)},"spec":{"volumeName":"pv-plex"}}'
      ;;
    'get pv pv-plex --output json')
      printf '%s\n' '{"metadata":{"uid":"pv-uid"}}'
      ;;
    *) return 2 ;;
  esac
}
verify_workload_replacements fake-kubeconfig node-a "$workload_inventory"
pvc_uid='replacement-pvc-uid'
assert_fails 'A rebound PVC was accepted as the original workload volume.' \
  verify_workload_replacements fake-kubeconfig node-a "$workload_inventory"

# Completed Pods are historical inventory, not failover demand.
completed_inventory="$state_dir/completed-inventory.json"
printf '%s\n' '{"items":[{"metadata":{"namespace":"automation-data","name":"bootstrap-old","ownerReferences":[{"controller":true,"kind":"Job","uid":"job-uid","name":"bootstrap"}]},"status":{"phase":"Succeeded"}}]}' >"$completed_inventory"
verify_workload_replacements fake-kubeconfig node-a "$completed_inventory"
# A node-local Longhorn instance manager has no same-owner survivor replacement.
printf '%s\n' '{"items":[{"metadata":{"namespace":"longhorn-system","name":"im","labels":{"longhorn.io/component":"instance-manager","longhorn.io/node":"node-a"},"ownerReferences":[{"controller":true,"apiVersion":"longhorn.io/v1beta2","kind":"InstanceManager","uid":"im-uid"}]},"spec":{"nodeName":"node-a"},"status":{"phase":"Running"}}]}' >"$completed_inventory"
verify_workload_replacements fake-kubeconfig node-a "$completed_inventory"
yq '.items[0].metadata.namespace = "default"' "$completed_inventory" >"$state_dir/not-longhorn.json"
assert_fails 'An unrelated InstanceManager owner bypassed replacement checks.' \
  verify_workload_replacements fake-kubeconfig node-a "$state_dir/not-longhorn.json"

# A Job may complete after inventory capture; use its UID and Complete condition.
printf '%s\n' '{"items":[{"metadata":{"namespace":"automation-data","name":"bootstrap-running","labels":{"job-name":"bootstrap"},"ownerReferences":[{"controller":true,"kind":"Job","uid":"job-uid","name":"bootstrap"}]},"status":{"phase":"Running"}}]}' >"$completed_inventory"
job_uid='job-uid'
job_complete=True
job_candidates='{"items":[]}'
drain_kubectl() {
  case "$*" in
    'fake-kubeconfig --namespace automation-data get job bootstrap --output json')
      JOB_UID="$job_uid" COMPLETE="$job_complete" yq -n -o=json '{"metadata":{"uid":strenv(JOB_UID)},"status":{"conditions":[{"type":"Complete","status":strenv(COMPLETE)}]}}' ;;
    'fake-kubeconfig --namespace automation-data get pods --selector job-name=bootstrap --output json')
      printf '%s\n' "$job_candidates" ;;
    *) return 2 ;;
  esac
}
verify_workload_replacements fake-kubeconfig node-a "$completed_inventory"
job_uid='another-job'
assert_fails 'A different Job UID satisfied the original workload.' \
  verify_workload_replacements fake-kubeconfig node-a "$completed_inventory"
job_uid='job-uid'
job_complete=False
assert_fails 'An incomplete Job without a Ready replacement was accepted.' \
  verify_workload_replacements fake-kubeconfig node-a "$completed_inventory"
complete_job_after_poll() { job_complete=True; }
NODE_WORKLOAD_REPLACEMENT_ATTEMPTS=2 NODE_SLEEP=complete_job_after_poll \
  wait_for_workload_replacements fake-kubeconfig node-a "$completed_inventory"
job_complete=False
NODE_WORKLOAD_REPLACEMENT_ATTEMPTS=1 \
  assert_fails 'Replacement wait accepted an unfinished Job after its deadline.' \
    wait_for_workload_replacements fake-kubeconfig node-a "$completed_inventory"

recovery_calls=''
record_recovery_call() {
  recovery_calls+="${recovery_calls:+ }$1"
}
verify_returned_node_contained() { record_recovery_call contained; }
verify_talos_recovery() { record_recovery_call talos; }
restore_longhorn_maintenance_state() { record_recovery_call longhorn-restore; }
verify_longhorn_convergence() { record_recovery_call longhorn-converged; }
verify_etcd_recovery() { record_recovery_call etcd; }
verify_platform_recovery() { record_recovery_call platform; }
verify_workload_replacements() { record_recovery_call workloads; }

# shellcheck disable=SC2218  # The later definitions are deliberate transaction fakes.
perform_recovery_acceptance fake-kubeconfig fake-talosconfig node-a 192.0.2.10 \
  "$maintenance_record" fake-inventory
[[ "$recovery_calls" == \
  'contained talos contained longhorn-restore longhorn-converged etcd workloads platform' ]]

recovery_calls=''
# shellcheck disable=SC2218  # The later definitions are deliberate transaction fakes.
perform_recovery_acceptance fake-kubeconfig fake-talosconfig node-a 192.0.2.10 \
  "$reboot_record"
[[ "$recovery_calls" == 'contained talos longhorn-converged etcd platform' ]]

capacity_nodes="$state_dir/capacity-nodes.json"
capacity_pods="$state_dir/capacity-pods.json"
cat >"$capacity_nodes" <<'EOF'
{"items":[
  {"metadata":{"name":"node-a","labels":{"kubernetes.io/hostname":"node-a","fixture/placement":"node-a"}},"status":{"allocatable":{"cpu":"4000m","memory":"8Gi","pods":"110","gpu.intel.com/i915":"1"}}},
  {"metadata":{"name":"node-b","labels":{"kubernetes.io/hostname":"node-b","fixture/placement":"node-b"}},"status":{"allocatable":{"cpu":"4000m","memory":"8Gi","pods":"110","gpu.intel.com/i915":"1"}}},
  {"metadata":{"name":"node-c","labels":{"kubernetes.io/hostname":"node-c","fixture/placement":"node-c"}},"status":{"allocatable":{"cpu":"4000m","memory":"8Gi","pods":"110","gpu.intel.com/i915":"1"}}}
]}
EOF
cat >"$capacity_pods" <<'EOF'
{"items":[
  {"metadata":{"namespace":"media","name":"plex","ownerReferences":[{"kind":"ReplicaSet","controller":true}]},"spec":{"nodeName":"node-a","containers":[{"resources":{"requests":{"cpu":"100m","memory":"512Mi","gpu.intel.com/i915":"1"}}}]},"status":{"phase":"Running"}},
  {"metadata":{"namespace":"tailscale","name":"router-0","labels":{"app":"router"},"ownerReferences":[{"kind":"StatefulSet","controller":true}]},"spec":{"nodeName":"node-b","containers":[{"resources":{"requests":{"cpu":"1m","memory":"1Mi"}}}],"topologySpreadConstraints":[{"maxSkew":1,"topologyKey":"kubernetes.io/hostname","whenUnsatisfiable":"DoNotSchedule","labelSelector":{"matchLabels":{"app":"router"}}}]},"status":{"phase":"Running"}},
  {"metadata":{"namespace":"tailscale","name":"router-1","labels":{"app":"router"},"ownerReferences":[{"kind":"StatefulSet","controller":true}]},"spec":{"nodeName":"node-a","containers":[{"resources":{"requests":{"cpu":"1m","memory":"1Mi"}}}],"topologySpreadConstraints":[{"maxSkew":1,"topologyKey":"kubernetes.io/hostname","whenUnsatisfiable":"DoNotSchedule","labelSelector":{"matchLabels":{"app":"router"}}}]},"status":{"phase":"Running"}},
  {"metadata":{"namespace":"kube-system","name":"cilium-node-b","labels":{"k8s-app":"cilium"},"ownerReferences":[{"kind":"DaemonSet","controller":true}]},"spec":{"nodeName":"node-b","containers":[{"resources":{"requests":{"cpu":"100m","memory":"100Mi"}}}]},"status":{"phase":"Running"}},
  {"metadata":{"namespace":"kube-system","name":"cilium-node-c","labels":{"k8s-app":"cilium"},"ownerReferences":[{"kind":"DaemonSet","controller":true}]},"spec":{"nodeName":"node-c","containers":[{"resources":{"requests":{"cpu":"100m","memory":"100Mi"}}}]},"status":{"phase":"Running"}},
  {"metadata":{"namespace":"kube-system","name":"operator-0","labels":{"app":"operator"},"ownerReferences":[{"kind":"ReplicaSet","controller":true}]},"spec":{"nodeName":"node-b","containers":[{"resources":{"requests":{"cpu":"10m","memory":"10Mi"}}}]},"status":{"phase":"Running"}},
  {"metadata":{"namespace":"kube-system","name":"operator-1","labels":{"app":"operator"},"ownerReferences":[{"kind":"ReplicaSet","controller":true}]},"spec":{"nodeName":"node-a","containers":[{"resources":{"requests":{"cpu":"10m","memory":"10Mi"}}}],"affinity":{"podAntiAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":[{"labelSelector":{"matchLabels":{"app":"operator"}},"topologyKey":"kubernetes.io/hostname"}]}}},"status":{"phase":"Running"}},
  {"metadata":{"namespace":"kube-system","name":"relay","labels":{"app":"relay"},"ownerReferences":[{"kind":"ReplicaSet","controller":true}]},"spec":{"nodeName":"node-a","containers":[{"resources":{"requests":{"cpu":"10m","memory":"10Mi"}}}],"affinity":{"podAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":[{"labelSelector":{"matchLabels":{"k8s-app":"cilium"}},"topologyKey":"kubernetes.io/hostname"}]}}},"status":{"phase":"Running"}},
  {"metadata":{"namespace":"default","name":"existing","ownerReferences":[{"kind":"ReplicaSet","controller":true}]},"spec":{"nodeName":"node-b","containers":[{"resources":{"requests":{"cpu":"500m","memory":"1Gi"}}}]},"status":{"phase":"Running"}}
]}
EOF
mise exec -- python "$REPO_ROOT/roles/talos_lifecycle/files/node/capacity.py" node-a "$capacity_nodes" "$capacity_pods" >/dev/null
yq '.items += [
  {"metadata":{"namespace":"tailscale","name":"router-extra-1","labels":{"app":"router"},"ownerReferences":[{"kind":"StatefulSet","controller":true}]},"spec":{"nodeName":"node-b","containers":[{"resources":{"requests":{"cpu":"1m","memory":"1Mi"}}}]},"status":{"phase":"Running"}},
  {"metadata":{"namespace":"tailscale","name":"router-extra-2","labels":{"app":"router"},"ownerReferences":[{"kind":"StatefulSet","controller":true}]},"spec":{"nodeName":"node-b","containers":[{"resources":{"requests":{"cpu":"1m","memory":"1Mi"}}}]},"status":{"phase":"Running"}}
]' "$capacity_pods" >"$state_dir/capacity-existing-skew.json"
mise exec -- python "$REPO_ROOT/roles/talos_lifecycle/files/node/capacity.py" node-a \
  "$capacity_nodes" "$state_dir/capacity-existing-skew.json" >/dev/null
yq 'del(.items[] | select(.metadata.labels."k8s-app" == "cilium"))' \
  "$capacity_pods" >"$state_dir/capacity-affinity-blocked.json"
assert_fails 'Unsatisfied required pod affinity was accepted.' \
  mise exec -- python "$REPO_ROOT/roles/talos_lifecycle/files/node/capacity.py" node-a \
    "$capacity_nodes" "$state_dir/capacity-affinity-blocked.json"
yq '.items += [{"metadata":{"namespace":"kube-system","name":"operator-extra","labels":{"app":"operator"},"ownerReferences":[{"kind":"ReplicaSet","controller":true}]},"spec":{"nodeName":"node-c","containers":[{"resources":{"requests":{"cpu":"10m","memory":"10Mi"}}}]},"status":{"phase":"Running"}}]' \
  "$capacity_pods" >"$state_dir/capacity-anti-affinity-blocked.json"
assert_fails 'Unsatisfied required pod anti-affinity was accepted.' \
  mise exec -- python "$REPO_ROOT/roles/talos_lifecycle/files/node/capacity.py" node-a \
    "$capacity_nodes" "$state_dir/capacity-anti-affinity-blocked.json"
yq '(.items[] | select(.metadata.name == "router-1") | .spec.nodeSelector) = {"fixture/placement":"node-b"} |
  (.items[] | select(.metadata.name == "router-1") | .spec.topologySpreadConstraints[0].nodeAffinityPolicy) = "Ignore"' \
  "$capacity_pods" >"$state_dir/capacity-spread-blocked.json"
assert_fails 'Unsatisfied hard topology spread was accepted.' \
  mise exec -- python "$REPO_ROOT/roles/talos_lifecycle/files/node/capacity.py" node-a \
    "$capacity_nodes" "$state_dir/capacity-spread-blocked.json"
yq '(.items[] | select(.metadata.name == "node-b") | .status.allocatable."gpu.intel.com/i915") = "0"' \
  "$capacity_nodes" >"$state_dir/capacity-existing-anti-affinity-nodes.json"
yq '.items += [{
  "metadata":{"namespace":"media","name":"existing-guard","labels":{"app":"guard"},"ownerReferences":[{"kind":"ReplicaSet","controller":true}]},
  "spec":{"nodeName":"node-c","containers":[{"resources":{"requests":{"cpu":"1m","memory":"1Mi"}}}],"affinity":{"podAntiAffinity":{"requiredDuringSchedulingIgnoredDuringExecution":[{"labelSelector":{"matchLabels":{}},"namespaces":["media"],"topologyKey":"kubernetes.io/hostname"}]}}},
  "status":{"phase":"Running"}
}]' "$capacity_pods" >"$state_dir/capacity-existing-anti-affinity-pods.json"
assert_fails 'Required anti-affinity declared by an existing Pod was accepted.' \
  mise exec -- python "$REPO_ROOT/roles/talos_lifecycle/files/node/capacity.py" node-a \
    "$state_dir/capacity-existing-anti-affinity-nodes.json" \
    "$state_dir/capacity-existing-anti-affinity-pods.json"
yq '(.items[] | select(.metadata.name != "node-a") | .status.allocatable."gpu.intel.com/i915") = "0"' \
  "$capacity_nodes" >"$state_dir/capacity-blocked.json"
assert_fails 'Insufficient extended-resource headroom was accepted.' \
  mise exec -- python "$REPO_ROOT/roles/talos_lifecycle/files/node/capacity.py" node-a \
    "$state_dir/capacity-blocked.json" "$capacity_pods"

NODE_MAINTENANCE_CONFIRM='enter:node-a:192.0.2.10' \
  require_exact_confirmation NODE_MAINTENANCE_CONFIRM 'enter:node-a:192.0.2.10'
NODE_REBOOT_CONFIRM='reboot:node-a:192.0.2.10' \
  require_exact_confirmation NODE_REBOOT_CONFIRM 'reboot:node-a:192.0.2.10'
NODE_LIFECYCLE_CONFIRM='accept:node-a:maintenance' \
  require_exact_confirmation NODE_LIFECYCLE_CONFIRM 'accept:node-a:maintenance'
assert_fails 'A stale maintenance confirmation was accepted.' \
  env NODE_MAINTENANCE_CONFIRM='enter:node-b:192.0.2.11' bash -c \
    'source "$REPO_ROOT/roles/talos_lifecycle/files/node/common.sh"; require_exact_confirmation NODE_MAINTENANCE_CONFIRM enter:node-a:192.0.2.10'

transaction_calls=''
record_transaction_call() {
  transaction_calls+="${transaction_calls:+ }$1"
}
persist_node_containment() { record_transaction_call contain; }
apply_longhorn_maintenance_state() { record_transaction_call longhorn-during; }
capture_drain_inventory() { record_transaction_call inventory; }
perform_kubernetes_drain() { record_transaction_call drain; }
verify_workload_replacements() { record_transaction_call replacements; }
verify_no_drainable_workloads() { record_transaction_call drain-empty; }
evacuate_longhorn_replicas() { record_transaction_call longhorn-evacuated; }
verify_short_absence_longhorn_safety() { record_transaction_call longhorn-safe; }
repeat_disruption_safety() { record_transaction_call safety-repeat; }
repeat_pre_containment_safety() { record_transaction_call pre-containment-safety; }
require_current_lease() { record_transaction_call lease-recheck; }
assert_cluster_disruption_admissible() { record_transaction_call recovery-admission; }
send_talos_shutdown() { record_transaction_call shutdown; }
send_talos_reboot() { record_transaction_call reboot; }
verify_node_offline() { record_transaction_call offline; }
observe_node_reboot() { record_transaction_call reboot-observed; }
perform_recovery_acceptance() { record_transaction_call accepted; }
remove_node_containment_and_uncordon() { record_transaction_call uncordon; }

run_maintenance_enter_transaction fake-kubeconfig fake-talosconfig node-a \
  192.0.2.10 holder "$maintenance_record" fake-inventory
[[ "$transaction_calls" == \
  'pre-containment-safety lease-recheck contain lease-recheck longhorn-during inventory lease-recheck drain replacements drain-empty longhorn-evacuated safety-repeat lease-recheck shutdown offline' ]]

transaction_calls=''
run_reboot_transaction fake-kubeconfig fake-talosconfig node-a \
  192.0.2.10 holder "$reboot_record" fake-inventory
[[ "$transaction_calls" == \
  'pre-containment-safety lease-recheck contain inventory lease-recheck drain replacements drain-empty longhorn-safe safety-repeat lease-recheck reboot reboot-observed accepted safety-repeat lease-recheck uncordon' ]]

transaction_calls=''
run_maintenance_exit_transaction fake-kubeconfig fake-talosconfig node-a \
  192.0.2.10 holder "$maintenance_record" fake-inventory
[[ "$transaction_calls" == \
  'lease-recheck recovery-admission accepted safety-repeat lease-recheck uncordon' ]]

# shellcheck disable=SC2329  # Invoked indirectly by run_reboot_transaction.
perform_kubernetes_drain() { record_transaction_call drain; return 1; }
transaction_calls=''
assert_fails 'A blocked drain did not stop reboot.' \
  run_reboot_transaction fake-kubeconfig fake-talosconfig node-a \
    192.0.2.10 holder "$reboot_record" fake-inventory
[[ "$transaction_calls" == 'pre-containment-safety lease-recheck contain inventory lease-recheck drain' ]]

perform_kubernetes_drain() { record_transaction_call drain; }
perform_recovery_acceptance() { record_transaction_call accepted; return 1; }
transaction_calls=''
assert_fails 'Rejected recovery was reported as a successful reboot.' \
  run_reboot_transaction fake-kubeconfig fake-talosconfig node-a \
    192.0.2.10 holder "$reboot_record" fake-inventory
[[ "$transaction_calls" != *uncordon* ]]

# The abrupt-loss adapter joins the root holder and cannot acquire, renew, or release it.
source "$REPO_ROOT/roles/talos_lifecycle/files/node/abrupt-loss-bridge.sh"
cat >"$state_dir/abrupt-prepared.json" <<EOF
{"talos_kubeconfig":"/operator/kube","talos_talosconfig":"/operator/talos","talos_kube_context":"fixture","talos_talos_context":"fixture","node":"node-a","address":"192.0.2.10","holder":"root-holder","talos_endpoints":["192.0.2.10","192.0.2.11","192.0.2.12"],"tools":{"kubectl":"/tools/kubectl","talosctl":"/tools/talosctl","python3":"/tools/python"}}
EOF
bridge_calls=''
bridge_call() { bridge_calls+="${bridge_calls:+ }$1"; }
join_test_lease() { bridge_call join; }
require_current_lease() { bridge_call current; }
abrupt_survivors_safe() { bridge_call survivors; }
persist_node_containment() { bridge_call contain; }
assert_cluster_disruption_admissible() { bridge_call recovery-admission; }
read_node_lifecycle_record() { printf '%s\n' '{"schemaVersion":1,"kind":"abrupt-loss"}'; }
perform_recovery_acceptance() { bridge_call accepted; }
remove_node_containment_and_uncordon() { bridge_call uncordon; }
acquire_test_lease() { fail 'abrupt bridge tried to acquire a second Lease'; }
start_test_lease_renewal() { fail 'abrupt bridge tried to renew the root Lease'; }
release_test_lease() { fail 'abrupt bridge tried to release the root Lease'; }
abrupt_loss_bridge_main contain "$state_dir/abrupt-prepared.json"
[[ "$bridge_calls" == 'join current survivors current contain current' ]]
bridge_calls=''
abrupt_loss_bridge_main recover "$state_dir/abrupt-prepared.json"
[[ "$bridge_calls" == 'join current recovery-admission accepted current uncordon current' ]]

echo 'Node lifecycle state tests passed.'
