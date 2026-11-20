#!/usr/bin/env bash
# Run the full verification: main drill + scenario tests + memory smoke.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== main drill ==="
python3 drills/main/gen_drill.py
python3 drills/main/verify_drill.py

echo "=== scenario tests ==="
python3 tests/run_tests.py

echo "=== memory smoke (5k events) ==="
python3 scripts/memory_check.py --events 5000 >/dev/null && echo "memory check OK"

echo "ALL CHECKS PASSED"
