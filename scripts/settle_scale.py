#!/usr/bin/env python3
"""Scale reproduction for offline handover settlement.

Generates at runtime (default): a warehouse with 250k scan events, an
opening inventory with 125k quantity lines, and 250k document lines
(125k handovers x two sides).  100k of the document lines sit at one
effective instant and are chained into a single settlement group
through shared quantity keys (L0 -> L1 -> ... -> L50000, one handover
per hop).  The settle phase then runs as a separate process and its
peak RSS is measured with wait4(2); it must stay under --limit-mb
(default 192 MiB, Linux).

The memory limit applies to the settle phase only; building the event
warehouse and generating the inputs is not bounded.  Everything is
written under a temporary work directory (deleted unless --keep);
nothing large lands in the repository.

Usage:
  python3 scripts/settle_scale.py                 # full 250k-line run
  python3 scripts/settle_scale.py --lines 20000 --big 8000   # smoke
  python3 scripts/settle_scale.py --keep --work /tmp/scale
"""
import argparse
import hashlib
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

CTX = "urn:example:ctx:epcis"
CLS = "urn:example:class:K"
Q_LOC = "urn:example:loc:Q"
T0 = 1767225600  # 2026-01-01T00:00:00Z


def ts(sec):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(sec))


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def obs(eid, sec, src, dst):
    return {"@context": CTX, "type": "ObjectEvent", "eventID": eid,
            "eventTime": ts(sec), "recordTime": ts(sec), "action": "OBSERVE",
            "quantityList": [{"epcClass": CLS, "quantity": 1, "uom": "EA"}],
            "sourceList": [{"source": src}],
            "destinationList": [{"destination": dst}]}


def loc(i):
    return f"urn:example:loc:L{i}"


def ploc(i):
    return f"urn:example:loc:P{i}"


def generate(data_dir, n_big, n_small):
    """Write events, manifests, opening and document files (streaming)."""
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "units.json"), "w") as fh:
        json.dump({"units": {"EA": {"dimension": "count", "factor": "1"},
                             "KGM": {"dimension": "mass", "factor": "1"}}}, fh)

    # ---- events: 2 per handover, chunked into files, 5 receiving batches
    n_handovers = n_big + n_small
    manifests = []
    per_file, per_batch = 2000, max(1, (2 * n_handovers) // 5)
    buf, files, batch_idx, count = [], [], 0, 0

    def flush_file():
        fn = f"events-{len(files)}.json"
        path = os.path.join(bdir, fn)
        with open(path, "w") as fh:
            json.dump(buf, fh)
        files.append({"name": fn, "sha256": hashlib.sha256(
            open(path, "rb").read()).hexdigest()})

    def flush_batch():
        nonlocal files
        if not files:
            return
        man = {"batch": f"b{len(manifests) + 1}", "files": files, "lineage_links": []}
        mpath = os.path.join(bdir, "manifest.json")
        with open(mpath, "w") as fh:
            json.dump(man, fh)
        manifests.append(mpath)
        files = []

    bdir = None
    for i in range(n_handovers):
        if i < n_big:
            src, dst, eff = loc(i), loc(i + 1), T0 + 3600
            s_t, r_t = T0 + 1800, T0 + 3000
        else:
            j = i - n_big
            src, dst, eff = ploc(j), Q_LOC, T0 + 3601 + j
            s_t, r_t = eff - 1200, eff - 600
        for eid, t, a, b in ((f"urn:uuid:scale-s-{i}", s_t, src, dst),
                             (f"urn:uuid:scale-r-{i}", r_t, src, dst)):
            if bdir is None or count % per_batch == 0:
                flush_batch()
                bdir = os.path.join(data_dir, f"b{len(manifests) + 1}")
                os.makedirs(bdir, exist_ok=True)
            buf.append(obs(eid, t, a, b))
            count += 1
            if len(buf) >= per_file:
                flush_file()
                buf.clear()
    if buf:
        flush_file()
    flush_batch()

    # ---- opening: one 1 EA line per chain location
    opening_p = os.path.join(data_dir, "opening.jsonl")
    with open(opening_p, "w") as fh:
        fh.write(json.dumps({"type": "opening", "as_of": ts(T0)}) + "\n")
        for i in range(n_big + 1):
            fh.write(json.dumps({"type": "quantity", "location": loc(i),
                                 "class": CLS, "quantity": "1", "uom": "EA"}) + "\n")
        for j in range(n_small):
            fh.write(json.dumps({"type": "quantity", "location": ploc(j),
                                 "class": CLS, "quantity": "1", "uom": "EA"}) + "\n")

    # ---- documents: one line per side per handover
    sender_p = os.path.join(data_dir, "sender.jsonl")
    receiver_p = os.path.join(data_dir, "receiver.jsonl")
    with open(sender_p, "w") as fs, open(receiver_p, "w") as fr:
        for i in range(n_handovers):
            if i < n_big:
                src, dst, eff = loc(i), loc(i + 1), T0 + 3600
            else:
                j = i - n_big
                src, dst, eff = ploc(j), Q_LOC, T0 + 3601 + j
            h = f"HO-{i:07d}"
            fs.write(json.dumps({"handover": h, "from": src, "to": dst,
                                 "effective": ts(eff),
                                 "scans": [f"urn:uuid:scale-s-{i}"]}) + "\n")
            fr.write(json.dumps({"handover": h, "from": src, "to": dst,
                                 "effective": ts(eff),
                                 "scans": [f"urn:uuid:scale-r-{i}"]}) + "\n")
    return manifests, opening_p, sender_p, receiver_p


def settle_once(wh_dir, batch, opening_p, sender_p, receiver_p, out_dir):
    """Run the settle CLI in a child process; return (exit_code, peak_mb)."""
    pid = os.fork()
    if pid == 0:
        try:
            os.execv(sys.executable,
                     [sys.executable, "-m", "epcis_warehouse.cli",
                      "--root", wh_dir, "settle", "--batch", str(batch),
                      "--opening", opening_p, "--sender", sender_p,
                      "--receiver", receiver_p, "--out", out_dir])
        finally:
            os._exit(127)
    _, status, ru = os.wait4(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    return code, ru.ru_maxrss / 1024.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lines", type=int, default=250000,
                    help="total document lines across both sides (default 250k)")
    ap.add_argument("--big", type=int, default=100000,
                    help="document lines in the single big same-instant group")
    ap.add_argument("--limit-mb", type=int, default=192,
                    help="peak RSS budget for the settle process (MiB)")
    ap.add_argument("--work", default=None, help="work directory (default: tempdir)")
    ap.add_argument("--keep", action="store_true", help="keep the work directory")
    args = ap.parse_args()

    n_big = args.big // 2            # handovers in the big group
    n_small = (args.lines - args.big) // 2
    work = args.work or tempfile.mkdtemp(prefix="epcis-settle-scale-")
    data_dir = os.path.join(work, "data")
    wh_dir = os.path.join(work, "wh")
    report = {"lines": args.lines, "big_group_lines": args.big,
              "handovers": n_big + n_small, "phases": []}

    def phase(name, fn):
        t0 = time.time()
        out = fn()
        report["phases"].append({"phase": name,
                                 "wall_s": round(time.time() - t0, 2),
                                 "self_peak_rss_mb": round(rss_mb(), 1)})
        return out

    manifests, opening_p, sender_p, receiver_p = phase(
        "generate", lambda: generate(data_dir, n_big, n_small))

    from epcis_warehouse.warehouse import Warehouse
    wh = phase("init", lambda: Warehouse.create(
        wh_dir, os.path.join(data_dir, "units.json")))

    def do_ingest():
        total = 0
        for m in manifests:
            res = wh.ingest(m)
            assert res["status"] == "published", res
            total += sum(res["events"].values())
        return total
    report["events_ingested"] = phase("ingest", do_ingest)
    latest = wh.store.latest_batch()
    wh.close()

    env = dict(os.environ, PYTHONPATH=REPO)
    out1 = os.path.join(work, "out1")
    t0 = time.time()
    code, peak_mb = settle_once(wh_dir, latest, opening_p, sender_p, receiver_p, out1)
    report["settle"] = {"exit": code, "wall_s": round(time.time() - t0, 2),
                        "peak_rss_mb": round(peak_mb, 1), "limit_mb": args.limit_mb,
                        "within_limit": peak_mb <= args.limit_mb}

    # determinism: a second run over the same inputs must be identical
    out2 = os.path.join(work, "out2")
    code2, _ = settle_once(wh_dir, latest, opening_p, sender_p, receiver_p, out2)
    identical = code2 == 0
    for name in ("ledger", "documents", "groups", "balances"):
        with open(os.path.join(out1, f"{name}.jsonl"), "rb") as fa, \
                open(os.path.join(out2, f"{name}.jsonl"), "rb") as fb:
            identical = identical and fa.read() == fb.read()
    report["determinism_repeat"] = identical

    # ---- correctness checks against hand-computed expectations
    checks = {}
    with open(os.path.join(out1, "summary.json")) as fh:
        summary = json.load(fh)
    checks["summary_counts"] = (
        summary["status"] == "settled"
        and summary["counts"]["handovers"] == n_big + n_small
        and summary["counts"]["posted"] == n_big + n_small
        and summary["counts"]["failed"] == 0
        and summary["counts"]["groups_posted"] == n_small + 1)

    big_gid = f"{ts(T0 + 3600)}#1"
    n_groups = 0
    big_ok = False
    with open(os.path.join(out1, "groups.jsonl")) as fh:
        for line in fh:
            g = json.loads(line)
            n_groups += 1
            if g["group"] == big_gid:
                big_ok = g["status"] == "posted" and g["member_count"] == n_big
    checks["big_group"] = big_ok
    checks["group_count"] = n_groups == n_small + 1

    # closing balances: L0 -> 0, L1..L(n_big-1) stay 1, L(n_big) -> 2,
    # every Pj -> 0, Q -> n_small
    want = {loc(0): "0", loc(n_big): "2", Q_LOC: str(n_small),
            ploc(0): "0", ploc(n_small - 1): "0"}
    got = {}
    with open(os.path.join(out1, "balances.jsonl")) as fh:
        for line in fh:
            row = json.loads(line)
            if row["kind"] == "quantity" and row["location"] in want:
                got[row["location"]] = row["quantity"]
    checks["balances"] = got == want
    report["checks"] = checks
    report["ok"] = (all(checks.values()) and report["settle"]["within_limit"]
                    and report["determinism_repeat"] and code == 0)
    print(json.dumps(report, indent=2))
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
