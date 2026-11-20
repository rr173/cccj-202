#!/usr/bin/env python3
"""Independent drill verifier.

Reads the hand-written expectations in expected.json and checks them
against actual warehouse output obtained ONLY through the public CLI.
It never imports or calls the warehouse's rebuild/query functions, so
the expectations stay an independent oracle.

Usage: verify_drill.py [warehouse_dir] [expected.json]
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def run_cli(root, argv):
    env = dict(os.environ, PYTHONPATH=REPO)
    proc = subprocess.run(
        [sys.executable, "-m", "epcis_warehouse.cli", "--root", root, *argv],
        env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"CLI failed: {argv}\n{proc.stderr}")
    return json.loads(proc.stdout)


def subset(expect, actual, path="$"):
    """expect must be a subset of actual (dicts: keywise; lists: elementwise)."""
    if isinstance(expect, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected object, got {type(actual).__name__}"]
        errs = []
        for k, v in expect.items():
            if k not in actual:
                errs.append(f"{path}.{k}: missing in actual")
            else:
                errs += subset(v, actual[k], f"{path}.{k}")
        return errs
    if isinstance(expect, list):
        if not isinstance(actual, list):
            return [f"{path}: expected list, got {type(actual).__name__}"]
        if len(expect) != len(actual):
            return [f"{path}: expected {len(expect)} elements, got {len(actual)}"]
        errs = []
        for i, (e, a) in enumerate(zip(expect, actual)):
            errs += subset(e, a, f"{path}[{i}]")
        return errs
    if expect != actual:
        return [f"{path}: expected {expect!r}, got {actual!r}"]
    return []


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "work", "wh")
    expected_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "expected.json")
    with open(expected_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)

    failures = 0
    for check in spec["checks"]:
        name = check["name"]
        actual = run_cli(root, check["argv"])
        errs = subset(check["expect"], actual)

        if "expect_events" in check:
            want = sorted(check["expect_events"])
            got = sorted((actual.get("events") or {}).keys())
            if want != got:
                errs.append(f"events: expected {want}, got {got}")

        # evidence review: every cited evidence object must resolve, and its
        # raw/expanded payloads must be well-formed
        for digest in (actual.get("evidence") or {}):
            doc = run_cli(root, ["evidence", "--digest", digest])
            if "raw" not in doc:
                errs.append(f"evidence {digest[:12]}: raw text missing")
            else:
                json.loads(doc["raw"])  # must parse
            if "expanded" in doc and not isinstance(doc["expanded"], list):
                errs.append(f"evidence {digest[:12]}: expanded form is not a node array")

        ec = check.get("expect_evidence_contains")
        if ec:
            digest = (actual.get("events") or {}).get(ec["event"])
            if not digest:
                errs.append(f"no evidence digest for {ec['event']}")
            else:
                doc = run_cli(root, ["evidence", "--digest", digest])
                if ec.get("raw_contains") not in doc.get("raw", ""):
                    errs.append(f"raw evidence for {ec['event']} lacks "
                                f"{ec['raw_contains']!r}")
                exp = json.dumps(doc.get("expanded", []))
                if ec.get("expanded_contains") not in exp:
                    errs.append(f"expanded evidence for {ec['event']} lacks "
                                f"{ec['expanded_contains']!r}")

        if errs:
            failures += 1
            print(f"FAIL {name}")
            for e in errs:
                print(f"     {e}")
        else:
            print(f"PASS {name}")

    print(f"\n{len(spec['checks']) - failures}/{len(spec['checks'])} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
