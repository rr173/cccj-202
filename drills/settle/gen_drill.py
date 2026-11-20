#!/usr/bin/env python3
"""Generate the settlement drill: events, warehouse, inputs, four settle runs.

Story (business times are 2026-01-01, UTC; locations A/B/C, classes X/Y/Z/W):

  * 08:00  opening: A has 10 kg X, 8 kg Y, 10 kg Z, 1 kg W; C has 10 kg W;
           named items e1 and e2 sit at A.
  * 10:00  H1 A->B 6 kg X (sender scans 2 kg + 4 kg, receiver scan 6000 g),
           H2 A->C 5 kg X + 3 kg Y, H3 A->C 2 kg Y.  The three share A's
           X/Y balances into one group: X needs 11 of 10, so the whole
           group is blocked even though Y suffices.  H4 C->B 6 kg W
           (2 kg + 4 kg vs 6000 g) is unrelated and posts -- once.
  * 11:00  H5 A->B 6 kg X; run1 also places H6 B->C 6 kg X at 11:00, so B
           would spend what it receives in the same instant: the pair is
           blocked as one group.  run2/run3 move H6 to 12:00: both post.
  * 13:00  H7 and H8 both cite scan s7 (reuse), H9 has no receiver
           document, H10's receiver cites a scan never received, H11
           A->C 1 kg W posts at batch 1 but fails at batch 2 (a
           conflicting r11 arrives later), H12 moves named e1 A->C,
           H13/H14 both move named e2 at the same instant (blocked).

Run 4 replays run 2 with shuffled input line order; results must be
byte-identical (except summary.json, which names the input paths).

Usage: gen_drill.py [workdir]   (default: alongside this script)
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

CTX = "urn:example:ctx:epcis"
LA, LB, LC = (f"urn:example:loc:{p}" for p in "ABC")
CX, CY, CZ, CW = (f"urn:example:class:{c}" for c in "XYZW")
E1, E2 = "urn:example:epc:e1", "urn:example:epc:e2"

UNITS = {"units": {
    "KGM": {"dimension": "mass", "factor": "1"},
    "GRM": {"dimension": "mass", "factor": "1/1000"},
    "EA": {"dimension": "count", "factor": "1"},
}}


def obs(eid, minute, src, dst, qty=None, epcs=None):
    t = f"2026-01-01T09:{minute:02d}:00+00:00"
    ev = {"@context": CTX, "type": "ObjectEvent", "eventID": f"urn:uuid:drill-{eid}",
          "eventTime": t, "recordTime": t, "action": "OBSERVE",
          "sourceList": [{"source": src}], "destinationList": [{"destination": dst}]}
    if qty:
        ev["quantityList"] = [{"epcClass": c, "quantity": q, "uom": u} for c, q, u in qty]
    if epcs:
        ev["epcList"] = epcs
    return ev


def E(eid):
    return f"urn:uuid:drill-{eid}"


BATCH1 = [
    obs("s1a", 0, LA, LB, qty=[(CX, 2, "KGM")]),
    obs("s1b", 5, LA, LB, qty=[(CX, 4, "KGM")]),
    obs("r1", 30, LA, LB, qty=[(CX, 6000, "GRM")]),
    obs("s2", 10, LA, LC, qty=[(CX, 5, "KGM"), (CY, 3, "KGM")]),
    obs("r2", 35, LA, LC, qty=[(CX, 5, "KGM"), (CY, 3, "KGM")]),
    obs("s3", 12, LA, LC, qty=[(CY, 2, "KGM")]),
    obs("r3", 36, LA, LC, qty=[(CY, 2, "KGM")]),
    obs("s4a", 15, LC, LB, qty=[(CW, 2, "KGM")]),
    obs("s4b", 16, LC, LB, qty=[(CW, 4, "KGM")]),
    obs("r4", 40, LC, LB, qty=[(CW, 6000, "GRM")]),
    obs("s5", 20, LA, LB, qty=[(CX, 6, "KGM")]),
    obs("r5", 41, LA, LB, qty=[(CX, 6, "KGM")]),
    obs("s6", 25, LB, LC, qty=[(CX, 6, "KGM")]),
    obs("r6", 45, LB, LC, qty=[(CX, 6, "KGM")]),
    obs("s7", 26, LA, LC, qty=[(CZ, 1, "KGM")]),
    obs("r7", 46, LA, LC, qty=[(CZ, 1, "KGM")]),
    obs("r8", 47, LA, LC, qty=[(CZ, 1, "KGM")]),
    obs("s9", 27, LA, LC, qty=[(CZ, 1, "KGM")]),
    obs("s10", 28, LA, LC, qty=[(CZ, 2, "KGM")]),
    obs("s11", 29, LA, LC, qty=[(CW, 1, "KGM")]),
    obs("r11", 48, LA, LC, qty=[(CW, 1, "KGM")]),
    obs("s12", 31, LA, LC, epcs=[E1]),
    obs("r12", 49, LA, LC, epcs=[E1]),
    obs("s13", 32, LA, LC, epcs=[E2]),
    obs("r13", 50, LA, LC, epcs=[E2]),
    obs("s14", 33, LA, LB, epcs=[E2]),
    obs("r14", 51, LA, LB, epcs=[E2]),
]

# batch 2: a conflicting retransmission of r11 (1 kg -> 2 kg)
BATCH2 = [obs("r11", 48, LA, LC, qty=[(CW, 2, "KGM")])]

OPENING = [
    {"type": "opening", "as_of": "2026-01-01T08:00:00+00:00"},
    {"type": "quantity", "location": LA, "class": CX, "quantity": "10", "uom": "KGM"},
    {"type": "quantity", "location": LA, "class": CY, "quantity": "8", "uom": "KGM"},
    {"type": "quantity", "location": LA, "class": CZ, "quantity": "10", "uom": "KGM"},
    {"type": "quantity", "location": LA, "class": CW, "quantity": "1", "uom": "KGM"},
    {"type": "quantity", "location": LC, "class": CW, "quantity": "10", "uom": "KGM"},
    {"type": "epc", "epc": E1, "location": LA},
    {"type": "epc", "epc": E2, "location": LA},
]

T1, T2, T3, T4 = (f"2026-01-01T{h:02d}:00:00+00:00" for h in (10, 11, 12, 13))


def doc(h, frm, to, eff, scans):
    return {"handover": h, "from": frm, "to": to, "effective": eff, "scans": scans}


def common_docs():
    """(sender lines, receiver lines) shared by every run."""
    s = [doc("H1", LA, LB, T1, [E("s1a"), E("s1b")]),
         doc("H2", LA, LC, T1, [E("s2")]),
         doc("H3", LA, LC, T1, [E("s3")]),
         doc("H4", LC, LB, T1, [E("s4a"), E("s4b")]),
         doc("H5", LA, LB, T2, [E("s5")]),
         doc("H7", LA, LC, T4, [E("s7")]),
         doc("H8", LA, LC, T4, [E("s7")]),   # same scan as H7: reuse
         doc("H9", LA, LC, T4, [E("s9")]),   # never confirmed by the receiver
         doc("H10", LA, LC, T4, [E("s10")]),
         doc("H11", LA, LC, T4, [E("s11")]),
         doc("H12", LA, LC, T4, [E("s12")]),
         doc("H13", LA, LC, T4, [E("s13")]),
         doc("H14", LA, LB, T4, [E("s14")])]
    r = [doc("H1", LA, LB, T1, [E("r1")]),
         doc("H2", LA, LC, T1, [E("r2")]),
         doc("H3", LA, LC, T1, [E("r3")]),
         doc("H4", LC, LB, T1, [E("r4")]),
         doc("H5", LA, LB, T2, [E("r5")]),
         doc("H7", LA, LC, T4, [E("r7")]),
         doc("H8", LA, LC, T4, [E("r8")]),
         doc("H10", LA, LC, T4, ["urn:uuid:drill-r10"]),  # never received
         doc("H11", LA, LC, T4, [E("r11")]),
         doc("H12", LA, LC, T4, [E("r12")]),
         doc("H13", LA, LC, T4, [E("r13")]),
         doc("H14", LA, LB, T4, [E("r14")])]
    return s, r


def run1_docs():
    s, r = common_docs()
    s.append(doc("H6", LB, LC, T2, [E("s6")]))   # same instant as H5: chained
    r.append(doc("H6", LB, LC, T2, [E("r6")]))
    return s, r


def run2_docs():
    s, r = common_docs()
    s.append(doc("H6", LB, LC, T3, [E("s6")]))   # next instant: both post
    r.append(doc("H6", LB, LC, T3, [E("r6")]))
    return s, r


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_batch(data, name, events):
    bdir = os.path.join(data, name)
    os.makedirs(bdir, exist_ok=True)
    files = []
    for i, ev in enumerate(events):
        fn = f"{i:02d}-{ev['eventID'].rsplit('-', 1)[-1]}.json"
        write_json(os.path.join(bdir, fn), ev)
        files.append(fn)
    man = {"batch": name,
           "files": [{"name": fn, "sha256": sha(os.path.join(bdir, fn))}
                     for fn in files],
           "lineage_links": []}
    write_json(os.path.join(bdir, "manifest.json"), man)
    return os.path.join(bdir, "manifest.json")


def main():
    work = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "work")
    data = os.path.join(work, "data")
    inputs = os.path.join(work, "inputs")
    os.makedirs(data, exist_ok=True)
    os.makedirs(inputs, exist_ok=True)

    write_json(os.path.join(data, "units.json"), UNITS)
    m1 = write_batch(data, "batch1", BATCH1)
    m2 = write_batch(data, "batch2", BATCH2)

    wh = os.path.join(work, "wh")
    if os.path.exists(wh):
        shutil.rmtree(wh)
    env = dict(os.environ, PYTHONPATH=REPO)

    def run(*argv, expect=0):
        proc = subprocess.run(
            [sys.executable, "-m", "epcis_warehouse.cli", "--root", wh, *argv],
            env=env, capture_output=True, text=True)
        if proc.returncode != expect:
            raise RuntimeError(f"CLI {argv} -> {proc.returncode}\n{proc.stderr}")
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    run("init", "--units", os.path.join(data, "units.json"))
    out1 = run("ingest", m1)
    out2 = run("ingest", m2)
    print("batch1:", out1["status"], out1.get("batch_seq"))
    print("batch2:", out2["status"], out2.get("batch_seq"))

    write_jsonl(os.path.join(inputs, "opening.jsonl"), OPENING)
    s1, r1 = run1_docs()
    s2, r2 = run2_docs()
    write_jsonl(os.path.join(inputs, "sender1.jsonl"), s1)
    write_jsonl(os.path.join(inputs, "receiver1.jsonl"), r1)
    write_jsonl(os.path.join(inputs, "sender2.jsonl"), s2)
    write_jsonl(os.path.join(inputs, "receiver2.jsonl"), r2)
    # run 4: run 2 inputs with reversed line order -- same business results
    write_jsonl(os.path.join(inputs, "sender2-rev.jsonl"), list(reversed(s2)))
    write_jsonl(os.path.join(inputs, "receiver2-rev.jsonl"), list(reversed(r2)))

    opening = os.path.join(inputs, "opening.jsonl")
    runs = [
        ("run1", "1", "sender1.jsonl", "receiver1.jsonl"),
        ("run2", "1", "sender2.jsonl", "receiver2.jsonl"),
        ("run3", "2", "sender2.jsonl", "receiver2.jsonl"),
        ("run4", "1", "sender2-rev.jsonl", "receiver2-rev.jsonl"),
    ]
    for name, batch, sf, rf in runs:
        out_dir = os.path.join(work, "out", name)
        summary = run("settle", "--batch", batch, "--opening", opening,
                      "--sender", os.path.join(inputs, sf),
                      "--receiver", os.path.join(inputs, rf),
                      "--out", out_dir)
        c = summary["counts"]
        print(f"{name}: {summary['status']} posted={c['posted']} failed={c['failed']} "
              f"groups_posted={c['groups_posted']} groups_blocked={c['groups_blocked']}")
    print("warehouse:", wh)


if __name__ == "__main__":
    main()
