#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=/dev/null
source "$repo_root/roles/talos_lifecycle/files/lib/node-lifecycle-state.sh"

maintenance='{"schemaVersion":1,"kind":"maintenance","longhorn":{"allowScheduling":{"before":true,"during":false},"evictionRequested":{"before":false,"during":true}}}'
reboot='{"schemaVersion":1,"kind":"reboot"}'
abrupt='{"schemaVersion":1,"kind":"abrupt-loss"}'
validate_lifecycle_record "$maintenance" maintenance
validate_lifecycle_record "$reboot" reboot
validate_lifecycle_record "$abrupt" abrupt-loss
for invalid in \
  '{"schemaVersion":2,"kind":"reboot"}' \
  '{"schemaVersion":1,"kind":"unknown"}' \
  '{"schemaVersion":1,"kind":"reboot","extra":true}' \
  '{"schemaVersion":1,"kind":"maintenance","longhorn":{"allowScheduling":{"before":"true","during":false},"evictionRequested":{"before":false,"during":true}}}'; do
  if validate_lifecycle_record "$invalid" >/dev/null 2>&1; then
    printf '%s\n' 'invalid lifecycle record accepted' >&2
    exit 1
  fi
done
printf '%s\n' 'Talos lifecycle schema regressions passed.'
