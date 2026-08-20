#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

runtime_paths=(main.py py_modules src)
if [[ -d dist ]]; then
  runtime_paths+=(dist)
fi

forbidden='(cec-ctl|libcec|systemctl|GAMESCOPE_EXTERNAL_OVERLAY|zwlr_layer_shell|SetComposition\(|Wake\(|Standby\(|SetStreamPath|ActiveSource|WriteVolume|VolumeUp\(|VolumeDown\(|Mute\()'
if rg -n --glob '!*.test.*' --glob '!**/__pycache__/**' "$forbidden" "${runtime_paths[@]}"; then
  echo "forbidden host/CEC mutation symbol found in runtime code" >&2
  exit 1
fi

if rg -n --glob '!**/__pycache__/**' "([\"']/dev/cec|open\\([^\\n]*cec)" main.py py_modules src; then
  echo "runtime must not open a kernel CEC device" >&2
  exit 1
fi

"${PYTHON:-python3}" scripts/audit_runtime.py
