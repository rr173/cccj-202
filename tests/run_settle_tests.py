#!/usr/bin/env python3
"""Scenario tests for offline handover settlement (settle command).

Covers: basic posting and ledger traceability, exact unit conversion,
one-sided/mismatched/conflicting documents, identical re-sends, scan
reuse across handovers and across sides, evidence missing vs unusable
(invalid/unsupported/conflicted/wrong-location/wrong-time events),
scan time ordering, same-side duplicate EPCs, named-EPC ownership and
double transfer, transitive quantity-key grouping with whole-group
blocking, credit-side grouping, zero-default balances, fatal input
errors, boundary instants, and determinism under line reordering.

Run:  python3 tests/run_settle_tests.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from epcis_warehouse.warehouse import Warehouse  # noqa: E402

CTX = "urn:example:ctx:epcis"
LA, LB, LC = (f"urn:example:loc:{p}" for p in "ABC")
CX, CY, CW, CV = (f"urn:example:class:{c}" for c in "XYWV")
E1, E2, E3 = (f"urn:example:epc:e{i}" for i in (1, 2, 3))

UNITS = {"units": {
    "KGM": {"dimension": "mass", "factor": "1"},
    "GRM": {"dimension": "mass", "factor": "1/1000"},
    "EA": {"dimension": "count", "factor": "1"},
}}

OPENING_T = "2026-01-01T08:00:00+00:00"
T1, T2, T3 = (f"2026-01-01T{h:02d}:00:00+00:00" for h in (10, 11, 12))


def scan_time(minute):
    return f"2026-01-01T09:{minute:02d}:00+00:00"


def obj(eid, minute, src, dst, epcs=None, qty=None):
    ev = {"@context": CTX, "type": "ObjectEvent", "eventID": eid,
          "eventTime": scan_time(minute), "recordTime": scan_time(minute),
          "action": "OBSERVE",
          "sourceList": [{"source": src}], "destinationList": [{"destination": dst}]}
    if epcs is not None:
        ev["epcList"] = epcs
    if qty is not None:
        ev["quantityList"] = [{"epcClass": c, "quantity": q, "uom": u} for c, q, u in qty]
    return ev


def doc(h, frm, to, eff, scans):
    return {"handover": h, "from": frm, "to": to, "effective": eff, "scans": scans}


class Fixture:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="epcis-settle-test-")
        self.seq = 0
        self.failures = []
        units_path = os.path.join(self.dir, "units.json")
        with open(units_path, "w") as fh:
            json.dump(UNITS, fh)
        self.wh = Warehouse.create(os.path.join(self.dir, "wh"), units_path)

    def ingest(self, events, name=None):
        self.seq += 1
        name = name or f"batch-{self.seq}"
        bdir = os.path.join(self.dir, f"files-{self.seq}")
        os.makedirs(bdir, exist_ok=True)
        fn = "events.json"
        with open(os.path.join(bdir, fn), "w") as fh:
            json.dump(events, fh)
        manifest = {"batch": name,
                    "files": [{"name": fn, "sha256": hashlib.sha256(
                        open(os.path.join(bdir, fn), "rb").read()).hexdigest()}],
                    "lineage_links": []}
        mpath = os.path.join(bdir, "manifest.json")
        with open(mpath, "w") as fh:
            json.dump(manifest, fh)
        res = self.wh.ingest(mpath)
        assert res["status"] == "published", res
        return res["batch_seq"]

    def settle(self, opening, sender, receiver, batch="1", run="run"):
        opening_p = os.path.join(self.dir, f"opening-{run}.jsonl")
        sender_p = os.path.join(self.dir, f"sender-{run}.jsonl")
        receiver_p = os.path.join(self.dir, f"receiver-{run}.jsonl")
        for path, rows in ((opening_p, opening), (sender_p, sender),
                           (receiver_p, receiver)):
            with open(path, "w") as fh:
                for row in rows:
                    fh.write(row if isinstance(row, str) else json.dumps(row))
                    fh.write("\n")
        out = os.path.join(self.dir, f"out-{run}")
        summary, code = self.wh.settle(batch, opening_p, sender_p, receiver_p, out)
        return summary, code, out

    def results(self, out):
        docs = {}
        with open(os.path.join(out, "documents.jsonl")) as fh:
            for line in fh:
                d = json.loads(line)
                docs[d["handover"]] = d
        return docs

    def read(self, out, name):
        with open(os.path.join(out, f"{name}.jsonl")) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def balances(self, out):
        bal = {"epc": {}, "quantity": {}}
        for row in self.read(out, "balances"):
            if row["kind"] == "epc":
                bal["epc"][row["epc"]] = row["location"]
            else:
                bal["quantity"][(row["location"], row["class"], row["dimension"])] = \
                    row["quantity"]
        return bal

    def close(self):
        try:
            self.wh.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


def check(cond, msg, failures):
    if cond:
        print(f"  PASS {msg}")
    else:
        failures.append(msg)
        print(f"  FAIL {msg}")


def opening(qty=None, epcs=None, as_of=OPENING_T):
    rows = [{"type": "opening", "as_of": as_of}]
    for loc, cls, q, u in (qty or []):
        rows.append({"type": "quantity", "location": loc, "class": cls,
                     "quantity": q, "uom": u})
    for epc, loc in (epcs or []):
        rows.append({"type": "epc", "epc": epc, "location": loc})
    return rows


# ---------------------------------------------------------------- tests

def t_basic_posting(fx):
    print("S1: basic posting, ledger traceable to document and both-side scans")
    fx.ingest([obj("s1", 0, LA, LB, qty=[(CX, 2, "KGM")]),
               obj("r1", 30, LA, LB, qty=[(CX, 2000, "GRM")])])
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [doc("H1", LA, LB, T1, ["s1"])],
        [doc("H1", LA, LB, T1, ["r1"])])
    check(code == 0 and summary["status"] == "settled", "settled exit 0", fx.failures)
    docs = fx.results(out)
    check(docs["H1"]["status"] == "posted", "H1 posted", fx.failures)
    ledger = fx.read(out, "ledger")
    check(len(ledger) == 1 and ledger[0]["handover"] == "H1"
          and ledger[0]["evidence"] == {"sender_scans": ["s1"], "receiver_scans": ["r1"]}
          and ledger[0]["quantity_moves"] == [
              {"class": CX, "dimension": "mass", "quantity": "2", "base_unit": "KGM",
               "from": LA, "to": LB}],
          "ledger entry traces to document and both scans", fx.failures)
    bal = fx.balances(out)
    check(bal["quantity"][(LA, CX, "mass")] == "8"
          and bal["quantity"][(LB, CX, "mass")] == "2",
          "balances moved 2 kg A->B", fx.failures)


def t_unit_conversion_and_mismatch(fx):
    print("S2: exact cross-unit matching; 6001 g vs 6 kg is a goods mismatch")
    fx.ingest([obj("s1", 0, LA, LB, qty=[(CX, 2, "KGM")]),
               obj("s2", 5, LA, LB, qty=[(CX, 4, "KGM")]),
               obj("r1", 30, LA, LB, qty=[(CX, 6000, "GRM")]),
               obj("s3", 10, LA, LB, qty=[(CX, 6, "KGM")]),
               obj("r3", 35, LA, LB, qty=[(CX, 6001, "GRM")])])
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "20", "KGM")]),
        [doc("H1", LA, LB, T1, ["s1", "s2"]), doc("H2", LA, LB, T2, ["s3"])],
        [doc("H1", LA, LB, T1, ["r1"]), doc("H2", LA, LB, T2, ["r3"])])
    docs = fx.results(out)
    check(docs["H1"]["status"] == "posted", "2 kg + 4 kg == 6000 g, posted once",
          fx.failures)
    ledger = {l["handover"]: l for l in fx.read(out, "ledger")}
    check(len(ledger) == 1 and ledger["H1"]["quantity_moves"][0]["quantity"] == "6",
          "H1 posted as a single 6 kg move (not 2+4+6)", fx.failures)
    check(docs["H2"]["status"] == "failed"
          and docs["H2"]["reasons"][0]["code"] == "goods_mismatch"
          and docs["H2"]["reasons"][0]["quantities"][0]["sender"] == "6"
          and docs["H2"]["reasons"][0]["quantities"][0]["receiver"] == "6001/1000",
          "6001 g vs 6 kg reported as exact goods_mismatch", fx.failures)


def t_one_sided_and_mismatch(fx):
    print("S3: missing side and two-sided mismatch are distinct reasons")
    fx.ingest([obj("s1", 0, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("r1", 30, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("s2", 5, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("r2", 35, LA, LC, qty=[(CX, 1, "KGM")])])
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [doc("H1", LA, LB, T1, ["s1"]), doc("H2", LA, LB, T1, ["s2"])],
        [doc("H2", LA, LC, T1, ["r2"]), doc("H3", LA, LB, T1, ["r1"])])
    docs = fx.results(out)
    check(docs["H1"]["reasons"][0]["code"] == "missing_receiver",
          "sender-only handover: missing_receiver", fx.failures)
    check(docs["H3"]["reasons"][0]["code"] == "missing_sender",
          "receiver-only handover: missing_sender", fx.failures)
    r = docs["H2"]["reasons"][0]
    check(r["code"] == "mismatch" and r["sender"]["to"] == LB
          and r["receiver"]["to"] == LC,
          "route mismatch reported with both sides", fx.failures)
    # same instant written with different UTC offsets still matches
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [doc("H1", LA, LB, "2026-01-01T10:00:00+00:00", ["s1"])],
        [doc("H1", LA, LB, "2026-01-01T18:00:00+08:00", ["r1"])], run="tz")
    check(fx.results(out)["H1"]["status"] == "posted",
          "offset notation is insignificant for matching", fx.failures)
    # different effective instants do not match
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [doc("H1", LA, LB, T1, ["s1"])],
        [doc("H1", LA, LB, T2, ["r1"])], run="time")
    check(fx.results(out)["H1"]["reasons"][0]["code"] == "mismatch",
          "different effective instants: mismatch", fx.failures)


def t_identical_resend_and_conflicts(fx):
    print("S4: identical re-sends count once; conflicting versions never picked")
    fx.ingest([obj("s1", 0, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("r1", 30, LA, LB, qty=[(CX, 1, "KGM")])])
    d_s = doc("H1", LA, LB, T1, ["s1"])
    d_r = doc("H1", LA, LB, T1, ["r1"])
    # re-sent identically, and once more with a different scan order
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [d_s, d_s, doc("H1", LA, LB, "2026-01-01T12:00:00+02:00", ["s1"])],
        [d_r, d_r])
    counts = summary["counts"]
    check(counts["sender_distinct"] == 1 and counts["receiver_distinct"] == 1,
          "re-sends collapse to one document per side", fx.failures)
    check(len(fx.read(out, "ledger")) == 1, "posted exactly once", fx.failures)
    # same number, different content on one side: never pick one
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [doc("H1", LA, LB, T1, ["s1"]), doc("H1", LA, LC, T1, ["s1"])],
        [d_r], run="conflict")
    docs = fx.results(out)
    r = docs["H1"]["reasons"][0]
    check(docs["H1"]["status"] == "failed" and r["code"] == "conflicting_documents"
          and r["side"] == "sender" and r["versions"] == 2,
          "conflicting same-number documents reported, not chosen between", fx.failures)
    check(fx.read(out, "ledger") == [], "nothing posted for the conflicted number",
          fx.failures)


def t_scan_reuse(fx):
    print("S5: scan reuse across handovers and across sides; re-send is not reuse")
    fx.ingest([obj("s1", 0, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("r1", 30, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("s2", 5, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("r2", 35, LA, LB, qty=[(CX, 1, "KGM")])])
    # same scan cited by two handovers -> both to the exception list
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [doc("H1", LA, LB, T1, ["s1"]), doc("H2", LA, LB, T1, ["s1"])],
        [doc("H1", LA, LB, T1, ["r1"]), doc("H2", LA, LB, T1, ["r2"])])
    docs = fx.results(out)
    check(docs["H1"]["reasons"][0]["code"] == "scan_reused"
          and docs["H2"]["reasons"][0]["code"] == "scan_reused",
          "cross-handover reuse flags both handovers", fx.failures)
    claims = docs["H1"]["reasons"][0]["claims"][0]
    check(claims["event_id"] == "s1"
          and claims["also_claimed_by"] == [{"handover": "H2", "side": "sender"}],
          "reuse exception names the event and the other claimant", fx.failures)
    # same event as evidence for both sides of one handover
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [doc("H1", LA, LB, T1, ["s1"])],
        [doc("H1", LA, LB, T1, ["s1"])], run="both")
    check(fx.results(out)["H1"]["reasons"][0]["code"] == "scan_reused",
          "one event cannot evidence both sides", fx.failures)
    # identical re-send of the same document is not a reuse
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [doc("H1", LA, LB, T1, ["s1"]), doc("H1", LA, LB, T1, ["s1"])],
        [doc("H1", LA, LB, T1, ["r1"])], run="resend")
    check(fx.results(out)["H1"]["status"] == "posted",
          "identical re-send adds no claim and posts", fx.failures)
    # a one-sided document is still pulled into the exception list by reuse
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [doc("H1", LA, LB, T1, ["s1"]), doc("H2", LA, LB, T1, ["s1"])],
        [doc("H1", LA, LB, T1, ["r1"])], run="onesided")
    docs = fx.results(out)
    codes1 = sorted(r["code"] for r in docs["H1"]["reasons"])
    codes2 = sorted(r["code"] for r in docs["H2"]["reasons"])
    check(codes1 == ["scan_reused"] and codes2 == ["missing_receiver", "scan_reused"],
          "reuse flags even the one-sided handover, with its other error kept",
          fx.failures)


def t_retransmission_not_double_counted(fx):
    print("S5b: original event retransmitted in a later batch does not amplify scans")
    fx.ingest([obj("s1", 0, LA, LB, qty=[(CX, 2, "KGM")]),
               obj("r1", 30, LA, LB, qty=[(CX, 2, "KGM")])])
    fx.ingest([obj("s1", 0, LA, LB, qty=[(CX, 2, "KGM")])])  # identical retransmission
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [doc("H1", LA, LB, T1, ["s1"])],
        [doc("H1", LA, LB, T1, ["r1"])], batch="2")
    ledger = fx.read(out, "ledger")
    check(len(ledger) == 1 and ledger[0]["quantity_moves"][0]["quantity"] == "2",
          "retransmitted event evidences 2 kg once, not 4 kg", fx.failures)


def t_evidence_missing_and_unusable(fx):
    print("S6: evidence missing vs unusable (invalid/unsupported/conflicted/"
          "wrong location/wrong time/not OBSERVE)")
    bad_time = obj("bad-time", 10, LA, LB, qty=[(CX, 1, "KGM")])
    bad_time["eventTime"] = "2026-01-01T09:10:00"  # no offset -> invalid
    unsupported = obj("unsup", 10, LA, LB, qty=[(CX, 1, "KGM")])
    unsupported["action"] = "ADD"  # ObjectEvent ADD -> unsupported
    agg = {"@context": CTX, "type": "AggregationEvent", "eventID": "agg1",
           "eventTime": scan_time(10), "recordTime": scan_time(10), "action": "ADD",
           "parentID": "urn:example:epc:box", "childEPCs": ["urn:example:epc:c1"],
           "sourceList": [{"source": LA}], "destinationList": [{"destination": LB}]}
    fx.ingest([obj("ok1", 10, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("ok2", 10, LA, LC, qty=[(CX, 1, "KGM")]),
               obj("early", 0, LA, LB, qty=[(CX, 1, "KGM")]),
               bad_time, unsupported, agg,
               obj("conf", 10, LA, LB, qty=[(CX, 1, "KGM")])])
    late = obj("late", 59, LA, LB, qty=[(CX, 1, "KGM")])
    late["eventTime"] = late["recordTime"] = "2026-01-01T10:30:00+00:00"
    fx.ingest([obj("conf", 10, LA, LB, qty=[(CX, 2, "KGM")]), late])  # conf: new content

    def one(scan, run, eff=T1):
        s, c, o = fx.settle(opening(qty=[(LA, CX, "50", "KGM")]),
                            [doc("H", LA, LB, eff, [scan])],
                            [doc("H", LA, LB, eff, ["ok1"])], batch="2", run=run)
        return fx.results(o)["H"]

    check(one("never-seen", "m")["reasons"][0]["code"] == "evidence_missing",
          "unknown event id: evidence_missing", fx.failures)
    r = one("bad-time", "inv")["reasons"][0]
    check(r["code"] == "evidence_unusable" and r["items"][0]["why"] == "event_not_ok"
          and r["items"][0]["status"] == "invalid",
          "invalid event: unusable/event_not_ok", fx.failures)
    r = one("unsup", "unsup")["reasons"][0]
    check(r["items"][0]["why"] == "event_not_ok" and r["items"][0]["status"] == "unsupported",
          "unsupported event: unusable", fx.failures)
    r = one("agg1", "agg")["reasons"][0]
    check(r["items"][0]["why"] == "not_observe",
          "AggregationEvent is not an OBSERVE scan", fx.failures)
    r = one("conf", "conf")["reasons"][0]
    check(r["items"][0]["why"] == "conflicted" and len(r["items"][0]["digests"]) == 2,
          "conflicted event: unusable with both digests", fx.failures)
    r = one("ok2", "loc")["reasons"][0]
    check(r["items"][0]["why"] == "location_mismatch"
          and r["items"][0]["destinations"] == [LC],
          "scan declaring another route: location_mismatch", fx.failures)
    # 'early' is at 09:00 > 08:00 opening, fine; use opening at 09:30 instead
    s, c, o = fx.settle(opening(qty=[(LA, CX, "50", "KGM")], as_of="2026-01-01T09:30:00+00:00"),
                        [doc("H", LA, LB, T1, ["early"])],
                        [doc("H", LA, LB, T1, ["ok1"])], batch="2", run="early")
    r = fx.results(o)["H"]["reasons"][0]
    check(r["items"][0]["why"] == "time_out_of_window",
          "scan not after the opening instant: time_out_of_window", fx.failures)
    r = one("late", "late")["reasons"][0]
    check(r["items"][0]["why"] == "time_out_of_window",
          "scan after the effective instant: time_out_of_window", fx.failures)
    # conflict arriving in a later batch: old batch view still settles
    s, c, o = fx.settle(opening(qty=[(LA, CX, "50", "KGM")]),
                        [doc("H", LA, LB, T1, ["conf"])],
                        [doc("H", LA, LB, T1, ["ok1"])], batch="1", run="oldview")
    check(fx.results(o)["H"]["status"] == "posted",
          "batch 1 view is not rewritten by the later conflict", fx.failures)


def t_scan_time_order(fx):
    print("S7: last sender scan must not be after the first receiver scan")
    fx.ingest([obj("s1", 40, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("r1", 30, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("s2", 30, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("r2", 30, LA, LB, qty=[(CX, 1, "KGM")])])
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [doc("H1", LA, LB, T1, ["s1"]), doc("H2", LA, LB, T1, ["s2"])],
        [doc("H1", LA, LB, T1, ["r1"]), doc("H2", LA, LB, T1, ["r2"])])
    docs = fx.results(out)
    r = docs["H1"]["reasons"][0]
    check(r["code"] == "scan_time_order"
          and r["last_sender_scan"] == scan_time(40)
          and r["first_receiver_scan"] == scan_time(30),
          "sender scan after receiver scan blocks", fx.failures)
    check(docs["H2"]["status"] == "posted",
          "equal instants are allowed (not after)", fx.failures)


def t_epc_rules(fx):
    print("S8: named EPCs: set equality, same-side duplicates, ownership, "
          "double transfer in one group")
    fx.ingest([obj("s1", 0, LA, LB, epcs=[E1]),
               obj("s1b", 5, LA, LB, epcs=[E1]),   # same EPC scanned twice (sender)
               obj("r1", 30, LA, LB, epcs=[E1]),
               obj("s2", 10, LA, LB, epcs=[E2]),
               obj("r2", 35, LA, LB, epcs=[E3]),   # different EPC on the receiver side
               obj("s3", 12, LA, LB, epcs=[E3]),
               obj("r3", 40, LA, LB, epcs=[E3]),
               obj("s4", 14, LB, LC, epcs=[E3]),
               obj("r4", 45, LB, LC, epcs=[E3])])
    op = opening(epcs=[(E1, LA), (E2, LA), (E3, LA)])
    summary, code, out = fx.settle(
        op,
        [doc("H1", LA, LB, T1, ["s1", "s1b"]), doc("H2", LA, LB, T1, ["s2"]),
         doc("H3", LA, LB, T2, ["s3"]), doc("H4", LB, LC, T2, ["s4"])],
        [doc("H1", LA, LB, T1, ["r1"]), doc("H2", LA, LB, T1, ["r2"]),
         doc("H3", LA, LB, T2, ["r3"]), doc("H4", LB, LC, T2, ["r4"])])
    docs = fx.results(out)
    check(docs["H1"]["reasons"][0]["code"] == "epc_scanned_twice"
          and docs["H1"]["reasons"][0]["epcs"] == [E1],
          "same-side cross-scan duplicate EPC blocks (one good, not two)", fx.failures)
    r = docs["H2"]["reasons"][0]
    check(r["code"] == "goods_mismatch" and r["named"] == {"only_sender": [E2],
                                                           "only_receiver": [E3]},
          "named EPC sets must be identical", fx.failures)
    # H3 (A->B e3) and H4 (B->C e3) share e3 at the same instant: one group,
    # e3 transferred twice -> whole group blocked
    check(docs["H3"]["status"] == "failed" and docs["H4"]["status"] == "failed"
          and docs["H3"]["group"] == docs["H4"]["group"],
          "same-instant EPC chain forms one blocked group", fx.failures)
    groups = {g["group"]: g for g in fx.read(out, "groups")}
    blk = groups[docs["H3"]["group"]]["blocking"]["epcs"][0]
    check(blk["epc"] == E3 and blk["reason"] == "transferred_twice_in_group"
          and blk["documents"] == ["H3", "H4"],
          "blocking EPC named with both documents", fx.failures)
    # moved to the next instant, both post
    summary, code, out = fx.settle(
        op, [doc("H3", LA, LB, T2, ["s3"]), doc("H4", LB, LC, T3, ["s4"])],
        [doc("H3", LA, LB, T2, ["r3"]), doc("H4", LB, LC, T3, ["r4"])], run="next")
    docs = fx.results(out)
    bal = fx.balances(out)
    check(docs["H3"]["status"] == "posted" and docs["H4"]["status"] == "posted"
          and bal["epc"][E3] == LC,
          "next instant the chain posts and ownership lands at C", fx.failures)
    # sender does not own the EPC at the start of the instant
    summary, code, out = fx.settle(
        op, [doc("H5", LB, LC, T1, ["s4"])], [doc("H5", LB, LC, T1, ["r4"])], run="owner")
    docs = fx.results(out)
    groups = {g["group"]: g for g in fx.read(out, "groups")}
    blk = groups[docs["H5"]["group"]]["blocking"]["epcs"][0]
    check(blk["reason"] == "not_owned_by_sender" and blk["owner"] == LA,
          "EPC not owned by the sender at the instant blocks the group", fx.failures)


def t_quantity_groups(fx):
    print("S9: transitive quantity-key grouping; whole group blocked; "
          "unrelated group posts; zero-default balance")
    # A has 10 X; H1 A->B 6 X, H2 A->C 5 X + 1 Y, H3 A->C 1 Y (shares Y with H2)
    fx.ingest([obj("s1", 0, LA, LB, qty=[(CX, 6, "KGM")]),
               obj("r1", 30, LA, LB, qty=[(CX, 6, "KGM")]),
               obj("s2", 5, LA, LC, qty=[(CX, 5, "KGM"), (CY, 1, "KGM")]),
               obj("r2", 35, LA, LC, qty=[(CX, 5, "KGM"), (CY, 1, "KGM")]),
               obj("s3", 10, LA, LC, qty=[(CY, 1, "KGM")]),
               obj("r3", 40, LA, LC, qty=[(CY, 1, "KGM")]),
               obj("s4", 15, LB, LC, qty=[(CW, 2, "KGM")]),
               obj("r4", 45, LB, LC, qty=[(CW, 2, "KGM")]),
               obj("s5", 20, LC, LA, qty=[(CV, 1, "KGM")]),
               obj("r5", 50, LC, LA, qty=[(CV, 1, "KGM")])])
    op = opening(qty=[(LA, CX, "10", "KGM"), (LA, CY, "5", "KGM"),
                      (LB, CW, "2", "KGM")])
    summary, code, out = fx.settle(
        op,
        [doc("H1", LA, LB, T1, ["s1"]), doc("H2", LA, LC, T1, ["s2"]),
         doc("H3", LA, LC, T1, ["s3"]), doc("H4", LB, LC, T1, ["s4"]),
         doc("H5", LC, LA, T1, ["s5"])],
        [doc("H1", LA, LB, T1, ["r1"]), doc("H2", LA, LC, T1, ["r2"]),
         doc("H3", LA, LC, T1, ["r3"]), doc("H4", LB, LC, T1, ["r4"]),
         doc("H5", LC, LA, T1, ["r5"])])
    docs = fx.results(out)
    # H1-H2 share (A,X); H2-H3 share (A,Y): one group of three, X debit 11 > 10
    check(docs["H1"]["group"] == docs["H2"]["group"] == docs["H3"]["group"]
          and docs["H1"]["status"] == "failed",
          "transitively shared keys make one blocked group", fx.failures)
    groups = {g["group"]: g for g in fx.read(out, "groups")}
    g = groups[docs["H1"]["group"]]
    check(g["member_count"] == 3
          and g["blocking"]["quantities"] == [
              {"location": LA, "class": CX, "dimension": "mass",
               "required": "11", "available": "10", "base_unit": "KGM"}],
          "blocking key reported exactly (Y not listed: it suffices)", fx.failures)
    check(docs["H4"]["status"] == "posted", "unrelated same-instant group posts",
          fx.failures)
    # H5 debits (C,W) which has no opening balance: zero default blocks it
    check(docs["H5"]["status"] == "failed", "unlisted balance counts as zero",
          fx.failures)
    g5 = groups[docs["H5"]["group"]]
    check(g5["blocking"]["quantities"][0]["available"] == "0"
          and g5["blocking"]["quantities"][0]["required"] == "1",
          "zero-default blocking key reported", fx.failures)
    bal = fx.balances(out)
    check(bal["quantity"][(LA, CX, "mass")] == "10"
          and bal["quantity"][(LB, CW, "mass")] == "0"
          and bal["quantity"][(LC, CW, "mass")] == "2",
          "blocked groups leave balances untouched; posted group applied", fx.failures)


def t_credit_side_grouping(fx):
    print("S10: sharing a credit-side key also groups (both post together)")
    fx.ingest([obj("s1", 0, LA, LC, qty=[(CX, 1, "KGM")]),
               obj("r1", 30, LA, LC, qty=[(CX, 1, "KGM")]),
               obj("s2", 5, LB, LC, qty=[(CX, 2, "KGM")]),
               obj("r2", 35, LB, LC, qty=[(CX, 2, "KGM")])])
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "1", "KGM"), (LB, CX, "2", "KGM")]),
        [doc("H1", LA, LC, T1, ["s1"]), doc("H2", LB, LC, T1, ["s2"])],
        [doc("H1", LA, LC, T1, ["r1"]), doc("H2", LB, LC, T1, ["r2"])])
    docs = fx.results(out)
    check(docs["H1"]["group"] == docs["H2"]["group"]
          and docs["H1"]["status"] == "posted" and docs["H2"]["status"] == "posted",
          "two deliveries to the same key form one posted group", fx.failures)
    bal = fx.balances(out)
    check(bal["quantity"][(LC, CX, "mass")] == "3", "credits accumulate", fx.failures)


def t_fatal_inputs(fx):
    print("S11: fatal inputs deliver no ledger")
    fx.ingest([obj("s1", 0, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("r1", 30, LA, LB, qty=[(CX, 1, "KGM")])])
    s = [doc("H1", LA, LB, T1, ["s1"])]
    r = [doc("H1", LA, LB, T1, ["r1"])]

    def expect_failed(opening_rows, sender_rows, code_part, run, batch="1"):
        summary, code, out = fx.settle(opening_rows, sender_rows, r,
                                       batch=batch, run=run)
        ok = (code == 2 and summary["status"] == "failed"
              and summary["error"]["code"] == code_part
              and not os.path.exists(os.path.join(out, "ledger.jsonl")))
        check(ok, f"{code_part}: failed summary, exit 2, no ledger file", fx.failures)

    # duplicate named ownership, even at the same location
    expect_failed(opening(epcs=[(E1, LA), (E1, LA)]), s, "opening_duplicate_epc", "dup1")
    expect_failed(opening(epcs=[(E1, LA), (E1, LB)]), s, "opening_duplicate_epc", "dup2")
    # invalid opening lines
    expect_failed(["not json"], s, "opening_invalid_line", "badjson")
    expect_failed(opening(qty=[(LA, CX, "1", "XYZ")]), s, "opening_invalid_line", "baduom")
    expect_failed([{"type": "quantity", "location": LA, "class": CX,
                    "quantity": "1", "uom": "KGM"}], s, "opening_missing_header", "nohdr")
    # invalid batch
    summary, code, out = fx.settle(opening(), s, r, batch="99", run="badbatch")
    check(code == 2 and summary["error"]["code"] == "invalid_batch"
          and not os.path.exists(os.path.join(out, "ledger.jsonl")),
          "unknown batch: failed, no ledger", fx.failures)
    # document line without an identifiable handover number
    expect_failed(opening(), ['{"from": "x"}'], "document_unidentifiable_handover", "noh")
    expect_failed(opening(), ["not json"], "document_unidentifiable_handover", "noh2")
    # a broken field with the handover intact is NOT fatal
    summary, code, out = fx.settle(opening(), [doc("H1", LA, LA, T1, ["s1"])], r,
                                   run="field")
    check(code == 0 and fx.results(out)["H1"]["reasons"][0]["code"] == "invalid_document"
          and "same_origin_destination" in fx.results(out)["H1"]["reasons"][0]["errors"],
          "same origin/destination is a document error, not fatal", fx.failures)


def t_document_field_errors(fx):
    print("S12: empty/duplicate scan references; effective not after opening")
    fx.ingest([obj("s1", 0, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("r1", 30, LA, LB, qty=[(CX, 1, "KGM")])])
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")]),
        [doc("H1", LA, LB, T1, []), doc("H2", LA, LB, T1, ["s1", "s1"]),
         doc("H3", LA, LB, OPENING_T, ["s1"])],
        [doc("H1", LA, LB, T1, ["r1"]), doc("H2", LA, LB, T1, ["r1"]),
         doc("H3", LA, LB, OPENING_T, ["r1"])])
    docs = fx.results(out)
    check("scans_missing_or_empty" in docs["H1"]["reasons"][0]["errors"],
          "empty scan list is a document error", fx.failures)
    check("duplicate_scan_refs" in docs["H2"]["reasons"][0]["errors"],
          "duplicate scan reference in one list is a document error", fx.failures)
    check("effective_not_after_opening" in docs["H3"]["reasons"][0]["errors"],
          "effective must be later than the opening instant", fx.failures)
    # scan exactly at the effective instant is fine
    fx.ingest([obj("s9", 0, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("r9", 0, LA, LB, qty=[(CX, 1, "KGM")])])
    summary, code, out = fx.settle(
        opening(qty=[(LA, CX, "10", "KGM")], as_of="2026-01-01T08:30:00+00:00"),
        [doc("H9", LA, LB, "2026-01-01T09:00:00+00:00", ["s9"])],
        [doc("H9", LA, LB, "2026-01-01T09:00:00+00:00", ["r9"])], batch="2", run="edge")
    check(fx.results(out)["H9"]["status"] == "posted",
          "scan at the effective instant posts", fx.failures)


def t_determinism(fx):
    print("S13: line order and re-runs do not change business results")
    fx.ingest([obj("s1", 0, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("r1", 30, LA, LB, qty=[(CX, 1, "KGM")]),
               obj("s2", 5, LB, LC, qty=[(CX, 1, "KGM")]),
               obj("r2", 35, LB, LC, qty=[(CX, 1, "KGM")]),
               obj("s3", 10, LA, LC, qty=[(CX, 5, "KGM")]),
               obj("r3", 40, LA, LC, qty=[(CX, 5, "KGM")])])
    op = opening(qty=[(LA, CX, "3", "KGM")])
    s = [doc("H1", LA, LB, T1, ["s1"]), doc("H2", LB, LC, T1, ["s2"]),
         doc("H3", LA, LC, T1, ["s3"])]
    r = [doc("H1", LA, LB, T1, ["r1"]), doc("H2", LB, LC, T1, ["r2"]),
         doc("H3", LA, LC, T1, ["r3"])]
    summary1, _, out1 = fx.settle(op, s, r, run="d1")
    summary2, _, out2 = fx.settle(op, list(reversed(s)), list(reversed(r)), run="d2")
    summary3, _, out3 = fx.settle(op, s, r, run="d3")
    same = True
    for name in ("ledger", "documents", "groups", "balances"):
        for a, b in ((out1, out2), (out1, out3)):
            with open(os.path.join(a, f"{name}.jsonl"), "rb") as fa, \
                    open(os.path.join(b, f"{name}.jsonl"), "rb") as fb:
                same = same and fa.read() == fb.read()
    check(same, "reordered and repeated runs are byte-identical", fx.failures)
    docs = fx.results(out1)
    # H1 (A->B 1 X) and H2 (B->C 1 X) chain at T1; H3 (A->C 5 X) alone.
    # Group {H1,H2}: B has nothing at the start of T1 -> blocked.  H3: 5 > 3 -> blocked.
    check(docs["H1"]["status"] == "failed" and docs["H2"]["status"] == "failed"
          and docs["H3"]["status"] == "failed",
          "chained debits cannot be covered by same-instant credits", fx.failures)


ALL_TESTS = [
    ("S1", t_basic_posting),
    ("S2", t_unit_conversion_and_mismatch),
    ("S3", t_one_sided_and_mismatch),
    ("S4", t_identical_resend_and_conflicts),
    ("S5", t_scan_reuse),
    ("S5b", t_retransmission_not_double_counted),
    ("S6", t_evidence_missing_and_unusable),
    ("S7", t_scan_time_order),
    ("S8", t_epc_rules),
    ("S9", t_quantity_groups),
    ("S10", t_credit_side_grouping),
    ("S11", t_fatal_inputs),
    ("S12", t_document_field_errors),
    ("S13", t_determinism),
]


def main():
    only = sys.argv[1:] or None
    total_failures = []
    for name, fn in ALL_TESTS:
        if only and name not in only:
            continue
        fx = Fixture()
        fx.failures = total_failures
        try:
            fn(fx)
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            total_failures.append(f"{name} crashed: {exc}")
        finally:
            fx.close()
    print()
    if total_failures:
        print(f"{len(total_failures)} FAILURES")
        return 1
    print("all settlement scenario tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
