#!/usr/bin/env bash
# Run the full verification: main drill + settlement drill + scenario tests
# + memory smoke.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== main drill ==="
python3 drills/main/gen_drill.py
python3 drills/main/verify_drill.py

echo "=== settlement drill ==="
python3 drills/settle/gen_drill.py
python3 drills/settle/verify_drill.py

echo "=== scenario tests ==="
python3 tests/run_tests.py

echo "=== settlement scenario tests ==="
python3 tests/run_settle_tests.py

echo "=== memory smoke (5k events) ==="
python3 scripts/memory_check.py --events 5000 >/dev/null && echo "memory check OK"

echo "=== settle scale smoke (20k lines) ==="
python3 scripts/settle_scale.py --lines 20000 --big 8000 >/dev/null && echo "settle scale OK"

echo "ALL CHECKS PASSED"
