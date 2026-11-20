#!/usr/bin/env python3
"""Generate the main drill dataset and build its warehouse.

Timeline (business time, all 2026-01-01, UTC):
  09:00  e0  AggregationEvent ADD  A -> box B0        (arrives LATE, batch 2)
  10:00  e1  AggregationEvent ADD  A, O1 -> box B     (batch 1)
  11:00  e2  AggregationEvent DELETE A from B         (batch 1)
  12:00  e3  TransformationEvent T: in A + 5 kg of class X,
             out C (EPC) and 4.8 kg of class D        (batch 1)

Usage: gen_drill.py [workdir]   (default: alongside this script)
"""
import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

CTX = "urn:example:ctx:epcis"
SRC = [{"source": "urn:example:loc:packing"}]
DST = [{"destination": "urn:example:loc:warehouse"}]

A = "urn:example:epc:A"
O1 = "urn:example:epc:O1"
B = "urn:example:epc:B"
B0 = "urn:example:epc:B0"
C = "urn:example:epc:C"
CLS_X = "urn:example:class:X"
CLS_D = "urn:example:class:D"

E0 = "urn:uuid:drill-e0"
E1 = "urn:uuid:drill-e1"
E2 = "urn:uuid:drill-e2"
E3 = "urn:uuid:drill-e3"


def agg(event_id, time, record, action, parent, children):
    return {
        "@context": CTX, "type": "AggregationEvent",
        "eventID": event_id, "eventTime": time, "recordTime": record,
        "action": action, "parentID": parent, "childEPCs": children,
        "sourceList": SRC, "destinationList": DST,
    }


def transformation():
    return {
        "@context": CTX, "type": "TransformationEvent",
        "eventID": E3, "eventTime": "2026-01-01T12:00:00+00:00",
        "recordTime": "2026-01-01T12:00:04+00:00",
        "inputEPCList": [A],
        "inputQuantityList": [{"epcClass": CLS_X, "quantity": 5, "uom": "KGM"}],
        "outputEPCList": [C],
        "outputQuantityList": [{"epcClass": CLS_D, "quantity": 4.8, "uom": "KGM"}],
        "sourceList": SRC, "destinationList": DST,
    }


EVENTS = {
    "e0.json": agg(E0, "2026-01-01T09:00:00+00:00", "2026-01-02T09:00:00+00:00",
                   "ADD", B0, [A]),
    "e1.json": agg(E1, "2026-01-01T10:00:00+00:00", "2026-01-01T10:00:03+00:00",
                   "ADD", B, [A, O1]),
    "e2.json": agg(E2, "2026-01-01T11:00:00+00:00", "2026-01-01T11:00:02+00:00",
                   "DELETE", B, [A]),
    "e3.json": transformation(),
}

UNITS = {"units": {
    "KGM": {"dimension": "mass", "factor": "1"},
    "GRM": {"dimension": "mass", "factor": "1/1000"},
    "EA": {"dimension": "count", "factor": "1"},
}}


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def main():
    work = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "work")
    data = os.path.join(work, "data")
    os.makedirs(data, exist_ok=True)

    write_json(os.path.join(data, "units.json"), UNITS)

    # batch 1: e1, e2, e3 (each its own file; order in manifest is arbitrary)
    b1 = os.path.join(data, "batch1")
    os.makedirs(b1, exist_ok=True)
    for fn in ("e1.json", "e2.json", "e3.json"):
        write_json(os.path.join(b1, fn), EVENTS[fn])
    m1 = {"batch": "drill-batch-1",
          "files": [{"name": fn, "sha256": sha(os.path.join(b1, fn))}
                    for fn in ("e3.json", "e1.json", "e2.json")],
          "lineage_links": []}
    write_json(os.path.join(b1, "manifest.json"), m1)

    # batch 2: the late 09:00 event e0
    b2 = os.path.join(data, "batch2")
    os.makedirs(b2, exist_ok=True)
    write_json(os.path.join(b2, "e0.json"), EVENTS["e0.json"])
    m2 = {"batch": "drill-batch-2",
          "files": [{"name": "e0.json", "sha256": sha(os.path.join(b2, "e0.json"))}],
          "lineage_links": []}
    write_json(os.path.join(b2, "manifest.json"), m2)

    wh = os.path.join(work, "wh")
    if os.path.exists(wh):
        import shutil
        shutil.rmtree(wh)
    env = dict(os.environ, PYTHONPATH=REPO)
    run = lambda *a: subprocess.run(
        [sys.executable, "-m", "epcis_warehouse.cli", "--root", wh, *a],
        check=True, env=env, capture_output=True, text=True)
    run("init", "--units", os.path.join(data, "units.json"))
    out1 = json.loads(run("ingest", os.path.join(b1, "manifest.json")).stdout)
    out2 = json.loads(run("ingest", os.path.join(b2, "manifest.json")).stdout)
    print("batch1:", out1["status"], out1.get("batch_seq"))
    print("batch2:", out2["status"], out2.get("batch_seq"))
    print("warehouse:", wh)


if __name__ == "__main__":
    main()
