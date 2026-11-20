#!/usr/bin/env python3
"""Generate the cold-chain exposure-ledger drill.

A hand-checkable 20-minute timeline (window 2026-01-01T23:50Z to
2026-01-02T00:10Z, so it crosses UTC midnight) plus the extra cases the
spec asks the drill to re-check:

  E    the headline timeline: inner-box probe with gap/expiry/conflict,
       a posted handover 甲->乙 at minute 5, a move to the pallet at
       minute 13; limit 8 C, alarm threshold 4 min;
  F    stays in the inner box: minute 13 only repairs E, F keeps the box
       probe and its conflict, and after the box probe is removed F
       reaches the pallet probe (no near probe installed, unlike a near
       probe with an unreadable value);
  G    pre-window installation, pre-window reading still valid at the
       window start, and a pre-window posted ownership change;
  H    same-instant remount of one probe with a sample taken exactly at
       the remount instant: the sample belongs to the NEW installation;
  H2   remount without a sample: the old reading must not carry over,
       so the new installation starts not_sampled;
  J    an unposted handover: ownership stays at the origin the whole
       window (no handover evidence ever appears on its segments);
  K    shares the pallet probe: normal 4 C readings with evidence refs
       shared with E's pallet segments (fan-out);
  N    two probes mounted on one box for minutes 3-6: install_conflict;
  Q    a same-instant double-parent packaging event at minute 4 makes
       its chain unknown from then on (chain_unknown);
  R    no probe anywhere on its chain: not_installed for the whole
       window.

The generator also demonstrates two fatal input-contract failures
(overlapping installations of one probe; batch mismatch) and
byte-identical determinism under input line reversal.

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
LA, LB = "urn:example:loc:甲", "urn:example:loc:乙"
LG1, LG2, LJ = "urn:example:loc:G1", "urn:example:loc:G2", "urn:example:loc:J1"
SRC = [{"source": "urn:example:loc:packing"}]
DST = [{"destination": "urn:example:loc:warehouse"}]

P, B, RG, RH, RH2, M = (f"urn:example:pkg:{x}" for x in ("P", "B", "RG", "RH", "RH2", "M"))
QP1, QP2 = "urn:example:pkg:QP1", "urn:example:pkg:QP2"
E, F, G, H, H2, J, K, N, Q, R = (f"urn:example:epc:{x}" for x in
                                 ("E", "F", "G", "H", "H2", "J", "K", "N", "Q", "R"))

W_START = "2026-01-01T23:50:00+00:00"
W_END = "2026-01-02T00:10:00+00:00"
OPENING_T = "2026-01-01T23:40:00+00:00"

UNITS = {"units": {
    "KGM": {"dimension": "mass", "factor": "1"},
    "GRM": {"dimension": "mass", "factor": "1/1000"},
    "EA": {"dimension": "count", "factor": "1"},
}}


def t(minute, second=0):
    """Window-relative minute as an ISO UTC string (minute may be negative)."""
    base = 1767311400  # 2026-01-01T23:50:00Z epoch seconds
    import time as _time
    return _time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                          _time.gmtime(base + minute * 60 + second))


def agg(eid, when, action, parent, children):
    return {"@context": CTX, "type": "AggregationEvent", "eventID": eid,
            "eventTime": when, "recordTime": when, "action": action,
            "parentID": parent, "childEPCs": children,
            "sourceList": SRC, "destinationList": DST}


def obs(eid, when, src, dst, epcs):
    return {"@context": CTX, "type": "ObjectEvent", "eventID": eid,
            "eventTime": when, "recordTime": when, "action": "OBSERVE",
            "epcList": epcs,
            "sourceList": [{"source": src}],
            "destinationList": [{"destination": dst}]}


EVENTS = [
    # ---- pre-window packaging (all settled before the inspection window)
    agg("urn:uuid:pk-eb", t(-8), "ADD", B, [E, F]),
    agg("urn:uuid:pk-bp", t(-7), "ADD", P, [B]),
    agg("urn:uuid:pk-kp", t(-7), "ADD", P, [K]),
    agg("urn:uuid:pk-grg", t(-6), "ADD", RG, [G]),
    agg("urn:uuid:pk-hrh", t(-6), "ADD", RH, [H]),
    agg("urn:uuid:pk-h2rh2", t(-6), "ADD", RH2, [H2]),
    agg("urn:uuid:pk-nm", t(-6), "ADD", M, [N]),
    # ---- settle scans for the two posted handovers
    obs("urn:uuid:sc-gs", t(-4), LG1, LG2, [G]),
    obs("urn:uuid:sc-gr", t(-3), LG1, LG2, [G]),
    obs("urn:uuid:sc-es", t(3), LA, LB, [E]),
    obs("urn:uuid:sc-er", t(3), LA, LB, [E]),
    # HJ references an event the warehouse never received: it stays unposted
    obs("urn:uuid:sc-qs", t(6), LA, LB, [Q]),
    obs("urn:uuid:sc-qr", t(6), LA, LB, [Q]),
    # ---- minute 4: Q joins two different parents at one instant -> unknown
    agg("urn:uuid:pk-q1", t(4), "ADD", QP1, [Q]),
    agg("urn:uuid:pk-q2", t(4), "ADD", QP2, [Q]),
    # ---- minute 13: E leaves the inner box for the pallet (simultaneous group)
    agg("urn:uuid:pk-edel", t(13), "DELETE", B, [E]),
    agg("urn:uuid:pk-eadd", t(13), "ADD", P, [E]),
]

OPENING = [
    {"type": "opening", "as_of": OPENING_T},
    {"type": "epc", "epc": E, "location": LA},
    {"type": "epc", "epc": F, "location": LA},
    {"type": "epc", "epc": G, "location": LG1},
    {"type": "epc", "epc": H, "location": LA},
    {"type": "epc", "epc": H2, "location": LA},
    {"type": "epc", "epc": J, "location": LJ},
    {"type": "epc", "epc": K, "location": LA},
    {"type": "epc", "epc": N, "location": LA},
    {"type": "epc", "epc": Q, "location": LA},
    {"type": "epc", "epc": R, "location": LA},
]


def doc(h, frm, to, eff, scans):
    return {"handover": h, "from": frm, "to": to, "effective": eff, "scans": scans}


SENDER = [
    doc("HG", LG1, LG2, t(-2), ["urn:uuid:sc-gs"]),
    doc("HE", LA, LB, t(5), ["urn:uuid:sc-es"]),
    # HJ never gets a receiver document: it must stay unposted
    doc("HJ", LJ, LB, t(6), ["urn:uuid:sc-qs"]),
]
RECEIVER = [
    doc("HG", LG1, LG2, t(-2), ["urn:uuid:sc-gr"]),
    doc("HE", LA, LB, t(5), ["urn:uuid:sc-er"]),
]

ITEMS = [{"epc": x, "limit_mc": 8000, "alarm_seconds": 240}
         for x in (E, F, G, H, H2, J, K, N, Q, R)]

INSTALLATIONS = [
    # pallet outer probe: mounted pre-window, valid across the whole window
    {"probe": "pP", "container": P, "from": t(-5), "to": t(25),
     "valid_for_seconds": 600},
    # inner-box probe: minute 0 -> 17, 5-minute readings
    {"probe": "pB", "container": B, "from": t(0), "to": t(17),
     "valid_for_seconds": 300},
    # G's probe: mounted pre-window
    {"probe": "pG", "container": RG, "from": t(-5), "to": t(25),
     "valid_for_seconds": 600},
    # H: old installation ends and the new one starts at minute 8
    {"probe": "pRH", "container": RH, "from": t(-2), "to": t(8),
     "valid_for_seconds": 300},
    {"probe": "pRH", "container": RH, "from": t(8), "to": t(25),
     "valid_for_seconds": 300},
    # H2: same remount shape, but no sample at the remount instant
    {"probe": "pRH2", "container": RH2, "from": t(0), "to": t(8),
     "valid_for_seconds": 300},
    {"probe": "pRH2", "container": RH2, "from": t(8), "to": t(25),
     "valid_for_seconds": 300},
    # N's box: two probes mounted together during minutes 3-6
    {"probe": "pM1", "container": M, "from": t(3), "to": t(10),
     "valid_for_seconds": 600},
    {"probe": "pM2", "container": M, "from": t(3), "to": t(6),
     "valid_for_seconds": 300},
]

SAMPLES = [
    # pallet probe reads 4 C the whole time
    {"probe": "pP", "at": t(0), "value_mc": 4000},
    {"probe": "pP", "at": t(10), "value_mc": 4000},
    # inner-box probe: 9 C at minutes 2 and 9; conflict at minute 12;
    # the minute-17 sample is taken exactly at removal -> outside install
    {"probe": "pB", "at": t(2), "value_mc": 9000},
    {"probe": "pB", "at": t(9), "value_mc": 9000},
    {"probe": "pB", "at": t(12), "value_mc": 4000},
    {"probe": "pB", "at": t(12), "value_mc": 9000},
    {"probe": "pB", "at": t(17), "value_mc": 5000},
    # G: a pre-window reading valid into the window (until minute 7)
    {"probe": "pG", "at": t(-3), "value_mc": 5000},
    {"probe": "pG", "at": t(12), "value_mc": 5000},
    # H: old reading at 7; a reading exactly at the minute-8 remount
    {"probe": "pRH", "at": t(7), "value_mc": 5000},
    {"probe": "pRH", "at": t(8), "value_mc": 9000},
    # H2: old reading at 7 (would nominally live to 12), new one at 10
    {"probe": "pRH2", "at": t(7), "value_mc": 5000},
    {"probe": "pRH2", "at": t(10), "value_mc": 5000},
    # N: only pM1 ever reports
    {"probe": "pM1", "at": t(7), "value_mc": 4000},
]


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


def main():
    work = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "work")
    data = os.path.join(work, "data")
    inputs = os.path.join(work, "inputs")
    os.makedirs(data, exist_ok=True)
    os.makedirs(inputs, exist_ok=True)

    write_json(os.path.join(data, "units.json"), UNITS)
    bdir = os.path.join(data, "batch1")
    os.makedirs(bdir, exist_ok=True)
    files = []
    for i, ev in enumerate(EVENTS):
        fn = f"{i:02d}-{ev['eventID'].rsplit(':', 1)[-1]}.json"
        write_json(os.path.join(bdir, fn), ev)
        files.append(fn)
    manifest = {"batch": "cold-drill-b1",
                "files": [{"name": fn, "sha256": sha(os.path.join(bdir, fn))}
                          for fn in files],
                "lineage_links": []}
    write_json(os.path.join(bdir, "manifest.json"), manifest)

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
    out = run("ingest", os.path.join(bdir, "manifest.json"))
    print("batch1:", out["status"], out.get("batch_seq"))

    # a second published batch exists only so a later batch number can be
    # rejected against the batch-1 settle summary (batch mismatch)
    b2 = os.path.join(data, "batch2")
    os.makedirs(b2, exist_ok=True)
    extra = obs("urn:uuid:sc-extra", t(15), LA, LB, [K])
    fn = "00-extra.json"
    write_json(os.path.join(b2, fn), extra)
    manifest2 = {"batch": "cold-drill-b2",
                 "files": [{"name": fn, "sha256": sha(os.path.join(b2, fn))}],
                 "lineage_links": []}
    write_json(os.path.join(b2, "manifest.json"), manifest2)
    out2 = run("ingest", os.path.join(b2, "manifest.json"))
    print("batch2:", out2["status"], out2.get("batch_seq"))

    # ---- settle first: ownership comes from its posted named moves
    opening_p = os.path.join(inputs, "opening.jsonl")
    sender_p = os.path.join(inputs, "sender.jsonl")
    receiver_p = os.path.join(inputs, "receiver.jsonl")
    write_jsonl(opening_p, OPENING)
    write_jsonl(sender_p, SENDER)
    write_jsonl(receiver_p, RECEIVER)
    settle_out = os.path.join(work, "settle")
    settle_summary = run("settle", "--batch", "1", "--opening", opening_p,
                         "--sender", sender_p, "--receiver", receiver_p,
                         "--out", settle_out)
    c = settle_summary["counts"]
    print(f"settle: posted={c['posted']} failed={c['failed']} "
          f"(HJ must be among the failures)")

    items_p = os.path.join(inputs, "items.jsonl")
    inst_p = os.path.join(inputs, "installations.jsonl")
    samp_p = os.path.join(inputs, "samples.jsonl")
    write_jsonl(items_p, ITEMS)
    write_jsonl(inst_p, INSTALLATIONS)
    write_jsonl(samp_p, SAMPLES)
    # reversed-line copies for the determinism run
    write_jsonl(os.path.join(inputs, "items-rev.jsonl"), list(reversed(ITEMS)))
    write_jsonl(os.path.join(inputs, "installations-rev.jsonl"),
                list(reversed(INSTALLATIONS)))
    write_jsonl(os.path.join(inputs, "samples-rev.jsonl"), list(reversed(SAMPLES)))

    common = ["--batch", "1", "--window-start", W_START, "--window-end", W_END,
              "--opening", opening_p,
              "--settle-summary", os.path.join(settle_out, "summary.json"),
              "--ledger", os.path.join(settle_out, "ledger.jsonl")]

    out1 = os.path.join(work, "out", "run1")
    summary = run("coldchain", *common, "--items", items_p,
                  "--installations", inst_p, "--samples", samp_p, "--out", out1)
    print("coldchain run1:", summary["status"], json.dumps(summary["counts"]))

    out2 = os.path.join(work, "out", "run2")
    summary2 = run("coldchain", *common,
                   "--items", os.path.join(inputs, "items-rev.jsonl"),
                   "--installations", os.path.join(inputs, "installations-rev.jsonl"),
                   "--samples", os.path.join(inputs, "samples-rev.jsonl"),
                   "--out", out2)
    print("coldchain run2 (reversed lines):", summary2["status"])

    # ---- fatal case 1: overlapping installations of the same probe
    bad_inst = os.path.join(inputs, "installations-overlap.jsonl")
    write_jsonl(bad_inst, INSTALLATIONS + [
        {"probe": "pB", "container": B, "from": t(10), "to": t(12),
         "valid_for_seconds": 300}])
    bad_out = os.path.join(work, "out", "bad-overlap")
    bad = run("coldchain", *common, "--items", items_p,
              "--installations", bad_inst, "--samples", samp_p,
              "--out", bad_out, expect=2)
    print("overlap input rejected:", bad["error"]["code"])
    assert not os.path.exists(os.path.join(bad_out, "coverage.jsonl"))

    # ---- fatal case 2: batch mismatch against the settle summary
    bad2 = run("coldchain", "--batch", "2", "--window-start", W_START,
               "--window-end", W_END, "--opening", opening_p,
               "--settle-summary", os.path.join(settle_out, "summary.json"),
               "--ledger", os.path.join(settle_out, "ledger.jsonl"),
               "--items", items_p, "--installations", inst_p,
               "--samples", samp_p,
               "--out", os.path.join(work, "out", "bad-batch"), expect=2)
    print("batch mismatch rejected:", bad2["error"]["code"])
    print("warehouse:", wh)


if __name__ == "__main__":
    main()
