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

echo "=== cold-chain drill ==="
python3 drills/coldchain/gen_drill.py
python3 drills/coldchain/verify_drill.py

echo "=== cold-chain scenario tests ==="
python3 tests/run_coldchain_tests.py

echo "=== disclosure drill ==="
python3 drills/disclosure/gen_drill.py
python3 drills/disclosure/verify_drill.py

echo "=== disclosure scenario tests ==="
python3 tests/run_disclosure_tests.py

echo "=== memory smoke (5k events) ==="
python3 scripts/memory_check.py --events 5000 >/dev/null && echo "memory check OK"

echo "=== settle scale smoke (20k lines) ==="
python3 scripts/settle_scale.py --lines 20000 --big 8000 >/dev/null && echo "settle scale OK"

echo "=== cold-chain scale smoke (2k EPCs, 30k samples) ==="
python3 scripts/coldchain_scale.py --epcs 2000 --samples 30000 >/dev/null && echo "cold-chain scale OK"

echo "=== disclosure scale smoke (40k variants, 8k single-id) ==="
python3 scripts/disclosure_scale.py --variants 40000 --big 8000 >/dev/null && echo "disclosure scale OK"

echo "ALL CHECKS PASSED"
