#!/usr/bin/env python3
"""Independent settlement drill verifier.

Reads the hand-written reconciliation in expected.json and checks it
against the settlement output FILES only (ledger.jsonl, documents.jsonl,
groups.jsonl, balances.jsonl, summary.json).  It never imports or calls
any warehouse function, so the expectations stay an independent oracle.

Usage: verify_drill.py [workdir] [expected.json]
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def read_jsonl(path):
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main():
    work = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "work")
    expected_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, "expected.json")
    with open(expected_path, "r", encoding="utf-8") as fh:
        spec = json.load(fh)

    failures = []

    def check(cond, msg):
        if cond:
            print(f"  PASS {msg}")
        else:
            failures.append(msg)
            print(f"  FAIL {msg}")

    runs = dict(spec["runs"])
    runs["run4"] = runs[runs["run4"]["$ref"]]

    for name, exp in sorted(runs.items()):
        print(f"{name}:")
        out_dir = os.path.join(work, "out", name)
        with open(os.path.join(out_dir, "summary.json"), encoding="utf-8") as fh:
            summary = json.load(fh)
        check(summary["status"] == "settled", f"{name} settled")
        for k, v in exp["counts"].items():
            check(summary["counts"].get(k) == v, f"{name} counts.{k} == {v} "
                  f"(got {summary['counts'].get(k)})")

        docs = {d["handover"]: d for d in read_jsonl(os.path.join(out_dir, "documents.jsonl"))}
        check(len(docs) == len(exp["documents"]),
              f"{name} one result line per handover ({len(docs)})")
        for h, codes in exp["documents"].items():
            d = docs.get(h)
            if d is None:
                check(False, f"{name} {h} present in documents.jsonl")
                continue
            want_status = "posted" if not codes else "failed"
            got_codes = sorted(r["code"] for r in d["reasons"])
            check(d["status"] == want_status and got_codes == sorted(codes),
                  f"{name} {h}: {want_status} {sorted(codes)} "
                  f"(got {d['status']} {got_codes})")

        groups = {g["group"]: g for g in read_jsonl(os.path.join(out_dir, "groups.jsonl"))}
        check(set(groups) == set(exp["groups"]),
              f"{name} group ids match ({sorted(groups)})")
        for gid, g in exp["groups"].items():
            actual = groups.get(gid)
            if actual is None:
                continue
            ok = actual["status"] == g["status"] and actual["member_count"] == g["member_count"]
            if g["status"] == "blocked":
                blk = actual.get("blocking", {})
                for bq in g.get("blocking_quantities", []):
                    ok = ok and any(
                        all(abq.get(k) == v for k, v in bq.items())
                        for abq in blk.get("quantities", []))
                for be in g.get("blocking_epcs", []):
                    ok = ok and any(
                        all(abe.get(k) == v for k, v in be.items())
                        for abe in blk.get("epcs", []))
            check(ok, f"{name} group {gid} {g['status']} members={g['member_count']} "
                  f"blocking as expected")

        ledger = {l["handover"]: l for l in read_jsonl(os.path.join(out_dir, "ledger.jsonl"))}
        check(set(ledger) == set(exp["ledger"]),
              f"{name} ledger posts exactly {sorted(exp['ledger'])} (got {sorted(ledger)})")
        for h, l in exp["ledger"].items():
            e = ledger.get(h)
            if e is None:
                continue
            got_q = [{"class": m["class"], "quantity": m["quantity"]}
                     for m in e["quantity_moves"]]
            ok = (e["from"] == l["from"] and e["to"] == l["to"]
                  and got_q == l["quantities"]
                  and [m["epc"] for m in e["named_moves"]] == l["named"]
                  and e["evidence"]["sender_scans"] == l["sender_scans"]
                  and e["evidence"]["receiver_scans"] == l["receiver_scans"])
            check(ok, f"{name} ledger {h} moves and both-side scans as expected")

        bal = {"epc": {}, "quantity": {}}
        for row in read_jsonl(os.path.join(out_dir, "balances.jsonl")):
            if row["kind"] == "epc":
                bal["epc"][row["epc"]] = row["location"]
            else:
                key = f"{row['location']}|{row['class']}|{row['dimension']}"
                bal["quantity"][key] = row["quantity"]
        check(bal == exp["balances"],
              f"{name} closing balances equal the hand-written reconciliation"
              + ("" if bal == exp["balances"] else
                 f"\n     want {json.dumps(exp['balances'], sort_keys=True)}"
                 f"\n     got  {json.dumps(bal, sort_keys=True)}"))

    for a, b in spec.get("identical_files", []):
        for name in ("ledger", "documents", "groups", "balances"):
            pa = os.path.join(work, "out", a, f"{name}.jsonl")
            pb = os.path.join(work, "out", b, f"{name}.jsonl")
            with open(pa, "rb") as fa, open(pb, "rb") as fb:
                check(fa.read() == fb.read(), f"{a} == {b}: {name}.jsonl byte-identical")

    print()
    if failures:
        print(f"{len(failures)} FAILURES")
        return 1
    print("all settlement drill checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
