#!/usr/bin/env python3
"""Memory behaviour on a large event set.

Generates a deterministic large dataset (deep packaging chain, wide
containers with class quantities, a long linked transformation chain),
ingests it in several receiving batches, then runs the heaviest queries
while reporting peak RSS and on-disk size.  The point: replay streams
from SQLite and only current state maps live in memory -- never the
full original text nor all ancestor paths.

Usage:
  python3 scripts/memory_check.py [--events N] [--wh DIR] [--limit-mb M]

With --limit-mb, the query phase runs under an address-space cap
(RLIMIT_AS) to prove queries stay within a fixed memory budget.

Suggested external measurement (see README):
  /usr/bin/time -v python3 scripts/memory_check.py --events 30000
"""
import argparse
import hashlib
import json
import os
import resource
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

CTX = "urn:example:ctx:epcis"
SRC = [{"source": "urn:example:loc:s"}]
DST = [{"destination": "urn:example:loc:d"}]
UNITS = {"units": {"KGM": {"dimension": "mass", "factor": "1"},
                   "GRM": {"dimension": "mass", "factor": "1/1000"},
                   "EA": {"dimension": "count", "factor": "1"}}}


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def dir_size_mb(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total / (1024.0 * 1024.0)


def agg(eid, us, action, parent, children=None, quantities=None):
    ev = {"@context": CTX, "type": "AggregationEvent", "eventID": eid,
          "eventTime": f"2026-01-01T00:00:{us % 60:02d}.{us % 1000:03d}+00:00"
          if False else _ts(us),
          "recordTime": _ts(us), "action": action, "parentID": parent,
          "sourceList": SRC, "destinationList": DST}
    if children:
        ev["childEPCs"] = children
    if quantities:
        ev["childQuantityList"] = [
            {"epcClass": c, "quantity": q, "uom": u} for c, q, u in quantities]
    return ev


def _ts(sec):
    base = 1767225600  # 2026-01-01T00:00:00Z
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(base + sec)) + "+00:00"


def trans(eid, sec, in_cls, out_cls):
    return {"@context": CTX, "type": "TransformationEvent", "eventID": eid,
            "eventTime": _ts(sec), "recordTime": _ts(sec),
            "inputQuantityList": [{"epcClass": in_cls, "quantity": 1, "uom": "KGM"}],
            "outputQuantityList": [{"epcClass": out_cls, "quantity": 1, "uom": "KGM"}],
            "sourceList": SRC, "destinationList": DST}


def generate(data_dir, n_chain, n_wide_parents, wide_size, n_trans, n_filler):
    """Stream the dataset to files; return (manifest_paths, query anchors)."""
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "units.json"), "w") as fh:
        json.dump(UNITS, fh)

    manifests = []
    links = []

    def write_batch(idx, events_iter, batch_links):
        bdir = os.path.join(data_dir, f"b{idx}")
        os.makedirs(bdir, exist_ok=True)
        files = []
        buf = []
        for ev in events_iter:
            buf.append(ev)
            if len(buf) >= 500:
                files.append(buf)
                buf = []
        if buf:
            files.append(buf)
        entries = []
        for i, group in enumerate(files):
            fn = f"events-{i}.json"
            path = os.path.join(bdir, fn)
            with open(path, "w") as fh:
                json.dump(group, fh)
            entries.append({"name": fn,
                            "sha256": hashlib.sha256(open(path, "rb").read()).hexdigest()})
        m = {"batch": f"b{idx}", "files": entries, "lineage_links": batch_links}
        mpath = os.path.join(bdir, "manifest.json")
        with open(mpath, "w") as fh:
            json.dump(m, fh)
        manifests.append(mpath)

    # batch 1: deep nesting chain C0 <- C1 <- ... <- C_{n_chain}
    write_batch(1, (agg(f"chain-{i}", 10 + i, "ADD",
                        f"urn:ex:C{i + 1}", children=[f"urn:ex:C{i}"])
                    for i in range(n_chain)), [])

    # batch 2: wide parents with named children + class quantities
    def wide():
        sec = 100000
        for p in range(n_wide_parents):
            yield agg(f"wide-{p}", sec + p, "ADD", f"urn:ex:W{p}",
                      children=[f"urn:ex:w{p}-{k}" for k in range(wide_size)],
                      quantities=[("urn:ex:cls:Q", wide_size, "EA"),
                                  ("urn:ex:cls:M", p + 1, "KGM")])
    write_batch(2, wide(), [])

    # batch 3: transformation chain m0 -> m1 -> ... -> m_{n_trans}
    for i in range(n_trans):
        links.append({"downstream": {"event": f"T-{i + 1}", "list": "inputQuantityList",
                                     "index": 0},
                      "upstream": {"event": f"T-{i}", "list": "outputQuantityList",
                                   "index": 0}})
    write_batch(3, (trans(f"T-{i}", 200000 + i, f"urn:ex:cls:m{i}", f"urn:ex:cls:m{i + 1}")
                    for i in range(n_trans + 1)), links)

    # batch 4: filler add/delete churn
    def filler():
        sec = 300000
        for i in range(n_filler):
            p = f"urn:ex:F{i % 500}"
            yield agg(f"fill-a-{i}", sec + 2 * i, "ADD", p,
                      children=[f"urn:ex:f{i}"],
                      quantities=[("urn:ex:cls:Q", 1, "EA")])
            yield agg(f"fill-d-{i}", sec + 2 * i + 1, "DELETE", p,
                      children=[f"urn:ex:f{i}"])
    write_batch(4, filler(), [])

    anchors = {"deep_epc": "urn:ex:C0", "chain_depth": n_chain,
               "wide_parent": "urn:ex:W0", "wide_size": wide_size,
               "trace_item": f"class:urn:ex:cls:m{n_trans}", "trace_len": n_trans,
               "end_t": _ts(400000)}
    return manifests, anchors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=30000, help="approximate event count")
    ap.add_argument("--wh", default=None, help="warehouse dir (default: tempdir)")
    ap.add_argument("--limit-mb", type=int, default=0,
                    help="apply RLIMIT_AS of this many MB before the query phase")
    ap.add_argument("--keep", action="store_true", help="keep the warehouse dir")
    args = ap.parse_args()

    n = args.events
    n_chain = max(1000, n // 10)
    n_trans = max(500, n // 20)
    n_filler = max(1000, (n - n_chain - n_trans - 200) // 2)
    n_wide, wide_size = 100, 100

    work = args.wh or tempfile.mkdtemp(prefix="epcis-mem-")
    data_dir = os.path.join(work, "data")
    wh_dir = os.path.join(work, "wh")

    report = {"events_target": n, "phases": []}

    def phase(name, fn):
        t0 = time.time()
        out = fn()
        report["phases"].append({"phase": name, "wall_s": round(time.time() - t0, 2),
                                 "peak_rss_mb": round(rss_mb(), 1)})
        return out

    manifests, anchors = phase("generate", lambda: generate(
        data_dir, n_chain, n_wide, wide_size, n_trans, n_filler))

    from epcis_warehouse.warehouse import Warehouse
    wh = phase("init", lambda: Warehouse.create(wh_dir, os.path.join(data_dir, "units.json")))

    def do_ingest():
        counts = 0
        for m in manifests:
            res = wh.ingest(m)
            assert res["status"] == "published", res
            counts += sum(res["events"].values())
        return counts
    total = phase("ingest", do_ingest)
    report["events_ingested"] = total

    if args.limit_mb:
        limit = args.limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        report["rlimit_as_mb"] = args.limit_mb

    def do_queries():
        out = {}
        r = wh.ancestors(anchors["deep_epc"], anchors["end_t"])
        out["deep_chain_len"] = len(r["chain"])
        r = wh.children(anchors["wide_parent"], anchors["end_t"])
        out["wide_children"] = len(r["named_children"])
        r = wh.trace(anchors["trace_item"], anchors["end_t"])
        out["trace_nodes"] = len(r["nodes"])
        out["trace_cycles"] = len(r["cycles"])
        rev = wh.review()
        out["review_status_counts"] = rev["event_status_counts"]
        # evidence spot check on a shared event
        r = wh.ancestors(anchors["deep_epc"], anchors["end_t"])
        digest = next(iter(r["evidence"]))
        doc = wh.evidence_doc(digest)
        out["evidence_ok"] = "raw" in doc and "expanded" in doc
        return out
    report["query_results"] = phase("queries", do_queries)

    phase("close", wh.close)
    wh2 = Warehouse.open(wh_dir)
    r = wh2.ancestors(anchors["deep_epc"], anchors["end_t"])
    report["reopen_chain_len"] = len(r["chain"])
    wh2.close()

    report["on_disk_mb"] = round(dir_size_mb(wh_dir), 1)
    report["warehouse_dir"] = wh_dir if args.keep else None
    expected = {"deep_chain_len": anchors["chain_depth"],
                "wide_children": wide_size,
                "trace_nodes": anchors["trace_len"] + 1}
    got = {k: report["query_results"][k] for k in expected}
    report["correct"] = got == expected and report["reopen_chain_len"] == anchors["chain_depth"]
    print(json.dumps(report, indent=2))
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if report["correct"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
