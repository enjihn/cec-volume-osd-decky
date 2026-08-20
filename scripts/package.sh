#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
pnpm build
./scripts/check-forbidden-mutations.sh
"${PYTHON:-python3}" scripts/package.py
