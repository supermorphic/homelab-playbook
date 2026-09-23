#!/usr/bin/env bash

NODE_LIFECYCLE_ANNOTATION=homelab.supermorphic.com/node-lifecycle
export NODE_LIFECYCLE_ANNOTATION

validate_lifecycle_record() {
  local document="$1" expected_kind="${2:-}"
  python3 - "$document" "$expected_kind" <<'PY'
import json
import sys

try:
    value = json.loads(sys.argv[1])
except (TypeError, json.JSONDecodeError):
    raise SystemExit(1)
kind = value.get("kind") if isinstance(value, dict) else None
if value.get("schemaVersion") != 1 or kind not in {"maintenance", "reboot", "abrupt-loss"}:
    raise SystemExit(1)
if sys.argv[2] and kind != sys.argv[2]:
    raise SystemExit(1)
if kind in {"reboot", "abrupt-loss"}:
    raise SystemExit(0 if set(value) == {"schemaVersion", "kind"} else 1)
if set(value) != {"schemaVersion", "kind", "longhorn"}:
    raise SystemExit(1)
longhorn = value.get("longhorn")
if not isinstance(longhorn, dict) or set(longhorn) != {"allowScheduling", "evictionRequested"}:
    raise SystemExit(1)
for name in ("allowScheduling", "evictionRequested"):
    state = longhorn.get(name)
    if not isinstance(state, dict) or set(state) != {"before", "during"}:
        raise SystemExit(1)
    if any(type(state[key]) is not bool for key in ("before", "during")):
        raise SystemExit(1)
PY
}
