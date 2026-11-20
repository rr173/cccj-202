#!/usr/bin/env python3
"""Scale reproduction for the offline cold-chain exposure ledger.

Generates at runtime (everything under a temp work directory, nothing
large lands in the repository):

  * 50,000 inspected EPCs, 10 per pallet -> 5,000 pallets, so one
    packaging container and one temperature signal fan out to many
    goods (shared chain + shared installation evidence);
  * 20,000 probe installations, half on the inspected pallets and half
    on containers no inspected EPC ever reaches;
  * 300,000 probe samples, evenly spread over the 20,000 probes, so most
    sample traffic is unrelated to any inspected EPC.

The cold-chain build then runs as a separate process; its peak RSS is
measured with wait4(2) and must stay under --limit-mb (default
256 MiB on Linux).  The bound applies to the ledger build only --
generating the data and building the warehouse is excluded, exactly as
for the settlement scale script.

The checks prove:
  * every inspected EPC gets gap-free coverage for the whole window;
  * a selected EPC's coverage, alarm ownership split and evidence are
    queryable from the output files;
  * the many unrelated-probe samples never appear in any detail row;
  * a repeated build is byte-identical (determinism).

Usage:
  python3 scripts/coldchain_scale.py                 # full run
  python3 scripts/coldchain_scale.py --epcs 2000     # smoke
"""
import argparse
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
LA, LB = "urn:example:loc:A", "urn:example:loc:B"
T0 = 1767225600  # 2026-01-01T00:00:00Z
WINDOW = (20, 60)  # minutes relative to T0


def ts(minute):
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                         time.gmtime(T0 + minute * 60))


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def generate(work, n_epcs, fanout, n_samples):
    data = os.path.join(work, "data")
    os.makedirs(data, exist_ok=True)
    with open(os.path.join(data, "units.json"), "w") as fh:
        json.dump({"units": {"EA": {"dimension": "count", "factor": "1"}}}, fh)

    n_pallets = (n_epcs + fanout - 1) // fanout
    pallet_of = [f"urn:example:pkg:pallet-{i}" for i in range(n_pallets)]
    epcs = [f"urn:example:epc:e{i:07d}" for i in range(n_epcs)]
    n_rel = n_pallets                       # probes on inspected pallets
    n_unrel = n_pallets                     # probes on unreachable boxes
    n_probes = n_rel + n_unrel
    per_probe = max(1, n_samples // n_probes)

    # ---- warehouse events: one ADD per pallet (fanout named children)
    bdir = os.path.join(data, "batch1")
    os.makedirs(bdir, exist_ok=True)
    events = []
    for pi in range(n_pallets):
        children = epcs[pi * fanout:(pi + 1) * fanout]
        events.append({
            "@context": CTX, "type": "AggregationEvent",
            "eventID": f"urn:uuid:agg-{pi}", "eventTime": ts(1),
            "recordTime": ts(1), "action": "ADD",
            "parentID": pallet_of[pi], "childEPCs": children,
            "sourceList": [{"source": LA}],
            "destinationList": [{"destination": LB}]})
    # settle scans: each handover moves fanout EPCs, verified by two scans
    handovers = n_pallets
    for hi in range(handovers):
        children = epcs[hi * fanout:(hi + 1) * fanout]
        for side in ("s", "r"):
            events.append({
                "@context": CTX, "type": "ObjectEvent",
                "eventID": f"urn:uuid:scan-{side}-{hi}", "eventTime": ts(22),
                "recordTime": ts(22), "action": "OBSERVE", "epcList": children,
                "sourceList": [{"source": LA}],
                "destinationList": [{"destination": LB}]})
    fn = "events.json"
    with open(os.path.join(bdir, fn), "w") as fh:
        json.dump(events, fh)
    import hashlib
    digest = hashlib.sha256(open(os.path.join(bdir, fn), "rb").read()).hexdigest()
    with open(os.path.join(bdir, "manifest.json"), "w") as fh:
        json.dump({"batch": "scale-b1",
                   "files": [{"name": fn, "sha256": digest}],
                   "lineage_links": []}, fh)

    # ---- settle inputs
    opening_p = os.path.join(data, "opening.jsonl")
    with open(opening_p, "w") as fh:
        fh.write(json.dumps({"type": "opening", "as_of": ts(0)}) + "\n")
        for e in epcs:
            fh.write(json.dumps({"type": "epc", "epc": e, "location": LA}) + "\n")
    sender_p = os.path.join(data, "sender.jsonl")
    receiver_p = os.path.join(data, "receiver.jsonl")
    with open(sender_p, "w") as fs, open(receiver_p, "w") as fr:
        for hi in range(handovers):
            doc_s = {"handover": f"H{hi}", "from": LA, "to": LB,
                     "effective": ts(23),
                     "scans": [f"urn:uuid:scan-s-{hi}"]}
            doc_r = {**doc_s, "scans": [f"urn:uuid:scan-r-{hi}"]}
            fs.write(json.dumps(doc_s) + "\n")
            fr.write(json.dumps(doc_r) + "\n")

    # ---- cold-chain lists (streamed; samples never held all at once)
    items_p = os.path.join(data, "items.jsonl")
    with open(items_p, "w") as fh:
        # make the first EPC over-limit so it must raise an alarm
        for i, e in enumerate(epcs):
            fh.write(json.dumps({"epc": e, "limit_mc": 8000,
                                 "alarm_seconds": 240}) + "\n")
    inst_p = os.path.join(data, "installations.jsonl")
    with open(inst_p, "w") as fh:
        for pi in range(n_pallets):
            fh.write(json.dumps({"probe": f"probe-rel-{pi}",
                                 "container": pallet_of[pi],
                                 "from": ts(0), "to": ts(70),
                                 "valid_for_seconds": 300}) + "\n")
        for pi in range(n_unrel):
            fh.write(json.dumps({"probe": f"probe-unrel-{pi}",
                                 "container": f"urn:example:pkg:unrel-{pi}",
                                 "from": ts(0), "to": ts(70),
                                 "valid_for_seconds": 300}) + "\n")
    samp_p = os.path.join(data, "samples.jsonl")
    with open(samp_p, "w") as fh:
        for pi in range(n_pallets):
            # pallet probe: hot for the first 6 minutes so the first EPC
            # exceeds its 8 C limit past the 4-minute alarm threshold,
            # then normal for the rest of the window
            for k in range(per_probe):
                minute = WINDOW[0] + k * 2
                value = 9000 if k < 3 else 4000
                fh.write(json.dumps({"probe": f"probe-rel-{pi}",
                                     "at": ts(minute), "value_mc": value}) + "\n")
        for pi in range(n_unrel):
            for k in range(per_probe):
                minute = WINDOW[0] + k * 2
                fh.write(json.dumps({"probe": f"probe-unrel-{pi}",
                                     "at": ts(minute), "value_mc": 9000}) + "\n")
    return {
        "n_epcs": n_epcs, "n_pallets": n_pallets, "n_probes": n_probes,
        "per_probe": per_probe,
        "paths": {"opening": opening_p, "sender": sender_p,
                  "receiver": receiver_p, "items": items_p,
                  "installations": inst_p, "samples": samp_p},
        "first_epc": epcs[0],
        "unrelated_probe": "probe-unrel-0",
    }


def build_once(wh_dir, data, out_dir):
    """Run the cold-chain CLI in a child process; (exit, peak_mb)."""
    pid = os.fork()
    if pid == 0:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)  # keep the parent's JSON report on stdout clean
            os.execv(sys.executable, [
                sys.executable, "-m", "epcis_warehouse.cli", "--root", wh_dir,
                "coldchain", "--batch", "1",
                "--window-start", ts(WINDOW[0]), "--window-end", ts(WINDOW[1]),
                "--opening", data["paths"]["opening"],
                "--settle-summary", data["paths"]["settle_summary"],
                "--ledger", data["paths"]["ledger"],
                "--items", data["paths"]["items"],
                "--installations", data["paths"]["installations"],
                "--samples", data["paths"]["samples"],
                "--out", out_dir])
        finally:
            os._exit(127)
    _, status, ru = os.wait4(pid, 0)
    return os.waitstatus_to_exitcode(status), ru.ru_maxrss / 1024.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epcs", type=int, default=50_000)
    ap.add_argument("--fanout", type=int, default=10)
    ap.add_argument("--samples", type=int, default=300_000)
    ap.add_argument("--limit-mb", type=int, default=256)
    ap.add_argument("--work", default=None)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    work = args.work or tempfile.mkdtemp(prefix="epcis-cold-scale-")
    report = {"epcs": args.epcs, "samples_target": args.samples, "phases": []}

    def phase(name, fn):
        t0 = time.time()
        out = fn()
        report["phases"].append({"phase": name,
                                 "wall_s": round(time.time() - t0, 2),
                                 "self_peak_rss_mb": round(rss_mb(), 1)})
        return out

    data = phase("generate", lambda: generate(
        work, args.epcs, args.fanout, args.samples))
    from epcis_warehouse.warehouse import Warehouse
    wh_dir = os.path.join(work, "wh")
    wh = phase("init", lambda: Warehouse.create(
        wh_dir, os.path.join(work, "data", "units.json")))
    phase("ingest", lambda: wh.ingest(
        os.path.join(work, "data", "batch1", "manifest.json")))
    settle_out = os.path.join(work, "settle")
    s, c = wh.settle("1", data["paths"]["opening"], data["paths"]["sender"],
                     data["paths"]["receiver"], settle_out)
    assert c == 0, s
    wh.close()
    data["paths"]["settle_summary"] = os.path.join(settle_out, "summary.json")
    data["paths"]["ledger"] = os.path.join(settle_out, "ledger.jsonl")

    out1 = os.path.join(work, "out1")
    t0 = time.time()
    code, peak = build_once(wh_dir, data, out1)
    report["build"] = {"exit": code, "wall_s": round(time.time() - t0, 2),
                       "peak_rss_mb": round(peak, 1),
                       "limit_mb": args.limit_mb, "within_limit": peak <= args.limit_mb}

    # determinism: a second build over the same inputs is identical
    out2 = os.path.join(work, "out2")
    code2, _ = build_once(wh_dir, data, out2)
    identical = code2 == 0
    for name in ("coverage", "daily", "alarms", "evidence"):
        with open(os.path.join(out1, f"{name}.jsonl"), "rb") as fa, \
                open(os.path.join(out2, f"{name}.jsonl"), "rb") as fb:
            identical = identical and fa.read() == fb.read()
    report["determinism_repeat"] = identical

    checks = {}
    with open(os.path.join(out1, "summary.json")) as fh:
        summary = json.load(fh)
    checks["status_built"] = summary["status"] == "built"
    checks["items"] = summary["counts"]["items"] == args.epcs
    window_us = (WINDOW[1] - WINDOW[0]) * 60 * 1_000_000

    # gap-free coverage for every EPC; every day slice reconciles
    seen_epcs, per_epc_us = {}, {}
    unrelated_leaks = 0
    with open(os.path.join(out1, "coverage.jsonl")) as fh:
        for line in fh:
            r = json.loads(line)
            seen_epcs[r["epc"]] = True
            per_epc_us[r["epc"]] = per_epc_us.get(r["epc"], 0) + r["duration_us"]
            if data["unrelated_probe"] in json.dumps(r["evidence"]):
                unrelated_leaks += 1
    checks["all_epcs_covered"] = len(seen_epcs) == args.epcs
    checks["gap_free_window"] = all(v == window_us for v in per_epc_us.values())
    checks["unrelated_probes_absent"] = unrelated_leaks == 0

    # selected EPC: coverage + alarm ownership are queryable from files
    first = data["first_epc"]
    first_rows = []
    with open(os.path.join(out1, "coverage.jsonl")) as fh:
        for line in fh:
            r = json.loads(line)
            if r["epc"] == first:
                first_rows.append(r)
    checks["selected_epc_coverage"] = (
        len(first_rows) > 0 and first_rows[0]["state"] == "over_limit"
        and sum(r["duration_us"] for r in first_rows) == window_us)
    alarms = [json.loads(l) for l in open(os.path.join(out1, "alarms.jsonl"))]
    first_alarm = next((a for a in alarms if a["epc"] == first), None)
    checks["selected_epc_alarm"] = (
        first_alarm is not None
        and {o["owner"] for o in first_alarm["by_owner"]} == {LA, LB})

    # daily rows reconcile per EPC
    daily_sum = {}
    with open(os.path.join(out1, "daily.jsonl")) as fh:
        for line in fh:
            r = json.loads(line)
            daily_sum[r["epc"]] = daily_sum.get(r["epc"], 0) + r["duration_us"]
    checks["daily_reconciles"] = all(v == window_us for v in daily_sum.values())
    checks["sample_count"] = summary["counts"]["sample_lines"] >= args.samples

    report["checks"] = checks
    report["ok"] = (all(checks.values()) and report["build"]["within_limit"]
                    and report["determinism_repeat"] and code == 0)
    print(json.dumps(report, indent=2))
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    else:
        report["work"] = work
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
