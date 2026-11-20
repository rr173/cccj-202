#!/usr/bin/env python3
"""Scenario test suite for the offline EPCIS warehouse.

Covers, beyond the main drill:
  class repeated quantity ADD, exact unit conversion, over-quantity
  DELETE (unknown, no clamping, no float tolerance), simultaneous
  double-parent, packaging cycle, transformation lineage links
  (linked / missing_evidence / pending / invalid / cycle), eventID
  conflict suspension, unknown context, unsupported shapes, batch
  rejection, retransmission provenance, recordTime vs eventTime,
  file order/chunking invariance, reopen and copy-reopen.

Run:  python3 tests/run_tests.py
"""
import json
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from epcis_warehouse.warehouse import Warehouse  # noqa: E402

CTX = "urn:example:ctx:epcis"
SRC = [{"source": "urn:example:loc:s"}]
DST = [{"destination": "urn:example:loc:d"}]

UNITS = {"units": {
    "KGM": {"dimension": "mass", "factor": "1"},
    "GRM": {"dimension": "mass", "factor": "1/1000"},
    "LTR": {"dimension": "volume", "factor": "1"},
    "EA": {"dimension": "count", "factor": "1"},
}}

T = "2026-01-01T{:02d}:00:00+00:00"
H = "2026-01-01T{:02d}:30:00+00:00"


def U(x):
    """Short test names become proper absolute IRIs (JSON-LD @id values)."""
    return x if ":" in str(x) else "urn:example:id:" + str(x)


def agg(eid, hour, action, parent, children=None, quantities=None, record=None):
    ev = {"@context": CTX, "type": "AggregationEvent", "eventID": eid,
          "eventTime": T.format(hour), "recordTime": record or T.format(hour),
          "action": action, "parentID": U(parent),
          "sourceList": SRC, "destinationList": DST}
    if children is not None:
        ev["childEPCs"] = [U(c) for c in children]
    if quantities is not None:
        ev["childQuantityList"] = [
            {"epcClass": U(c), "quantity": q, "uom": u} for c, q, u in quantities]
    return ev


def obj(eid, hour, epcs=None, quantities=None):
    ev = {"@context": CTX, "type": "ObjectEvent", "eventID": eid,
          "eventTime": T.format(hour), "recordTime": T.format(hour),
          "action": "OBSERVE", "sourceList": SRC, "destinationList": DST}
    if epcs is not None:
        ev["epcList"] = [U(e) for e in epcs]
    if quantities is not None:
        ev["quantityList"] = [
            {"epcClass": U(c), "quantity": q, "uom": u} for c, q, u in quantities]
    return ev


def trans(eid, hour, in_epcs=None, in_qty=None, out_epcs=None, out_qty=None):
    ev = {"@context": CTX, "type": "TransformationEvent", "eventID": eid,
          "eventTime": T.format(hour), "recordTime": T.format(hour),
          "sourceList": SRC, "destinationList": DST}
    if in_epcs is not None:
        ev["inputEPCList"] = [U(e) for e in in_epcs]
    if in_qty is not None:
        ev["inputQuantityList"] = [
            {"epcClass": U(c), "quantity": q, "uom": u} for c, q, u in in_qty]
    if out_epcs is not None:
        ev["outputEPCList"] = [U(e) for e in out_epcs]
    if out_qty is not None:
        ev["outputQuantityList"] = [
            {"epcClass": U(c), "quantity": q, "uom": u} for c, q, u in out_qty]
    return ev


class Fixture:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="epcis-test-")
        self.seq = 0
        self.failures = []
        units_path = os.path.join(self.dir, "units.json")
        with open(units_path, "w") as fh:
            json.dump(UNITS, fh)
        self.wh = Warehouse.create(os.path.join(self.dir, "wh"), units_path)

    def ingest(self, events, links=None, chunk=1, shuffle=False, name=None):
        """Write events to files (chunk events per file) and ingest a manifest."""
        self.seq += 1
        name = name or f"batch-{self.seq}"
        bdir = os.path.join(self.dir, f"files-{self.seq}")
        os.makedirs(bdir, exist_ok=True)
        files = []
        order = list(events)
        for i in range(0, len(order), chunk):
            group = order[i:i + chunk]
            fn = f"events-{i // chunk}.json"
            payload = group[0] if len(group) == 1 else group
            with open(os.path.join(bdir, fn), "w") as fh:
                json.dump(payload, fh)
            files.append(fn)
        if shuffle:
            files = list(reversed(files))
        import hashlib
        manifest = {"batch": name,
                    "files": [{"name": fn,
                               "sha256": hashlib.sha256(
                                   open(os.path.join(bdir, fn), "rb").read()).hexdigest()}
                              for fn in files],
                    "lineage_links": links or []}
        mpath = os.path.join(bdir, "manifest.json")
        with open(mpath, "w") as fh:
            json.dump(manifest, fh)
        return self.wh.ingest(mpath)

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


def qty_of(children_result, cls, dim):
    for q in children_result["class_quantities"]:
        if q["class"] == U(cls) and q["dimension"] == dim:
            return q
    return None


# ---------------------------------------------------------------- tests

def t_class_repeated_add(fx):
    print("T1: class repeated quantity ADD accumulates exactly")
    fx.ingest([agg("e1", 10, "ADD", "P", quantities=[("X", 5, "KGM")]),
               agg("e2", 11, "ADD", "P", quantities=[("X", 5, "KGM")])])
    r = fx.wh.children(U("P"), T.format(12))
    q = qty_of(r, "X", "mass")
    check(q and q["quantity"] == "10" and q["status"] == "ok",
          f"two 5 KGM adds give exactly 10 (got {q and q['quantity']})", fx.failures)


def t_unit_conversion(fx):
    print("T2: exact cross-unit arithmetic, no float tolerance")
    fx.ingest([agg("e1", 10, "ADD", "P", quantities=[("X", 1, "KGM")]),
               agg("e2", 11, "DELETE", "P", quantities=[("X", 500, "GRM")]),
               agg("e3", 13, "DELETE", "P", quantities=[("X", 500, "GRM")]),
               agg("e4", 15, "DELETE", "P", quantities=[("X", 1, "GRM")])])
    r = fx.wh.children(U("P"), T.format(12))
    check(qty_of(r, "X", "mass")["quantity"] == "1/2", "1 KGM - 500 GRM = 1/2 KGM", fx.failures)
    r = fx.wh.children(U("P"), T.format(14))
    check(qty_of(r, "X", "mass")["quantity"] == "0", "exactly 0 after second 500 GRM", fx.failures)
    r = fx.wh.children(U("P"), T.format(16))
    q = qty_of(r, "X", "mass")
    check(q["status"] == "unknown" and q["reason"]["reason"] == "over_delete"
          and q["reason"]["declared_total_base"] == "1/1000"
          and q["reason"]["available_base"] == "0"
          and q["reason"]["events"] == ["e4"],
          "1 GRM delete over exact 0 is over_delete (not tolerated, not clamped)", fx.failures)


def t_over_delete_unknown_and_recovery(fx):
    print("T3: over-delete unknown; ordinary adds do not clear; new batch recovers")
    fx.ingest([agg("e1", 10, "ADD", "P", quantities=[("X", 5, "KGM"), ("Y", 3, "EA")]),
               agg("e2", 11, "DELETE", "P", quantities=[("X", 6, "KGM")]),
               agg("e3", 12, "ADD", "P", quantities=[("X", 2, "KGM")])])
    r = fx.wh.children(U("P"), T.format(13), batch=1)
    q = qty_of(r, "X", "mass")
    check(q["status"] == "unknown" and q["reason"]["reason"] == "over_delete"
          and q["reason"]["declared_total_base"] == "6"
          and q["reason"]["available_base"] == "5",
          "delete 6 of 5 -> unknown with exact declared/available", fx.failures)
    check(qty_of(r, "Y", "count")["quantity"] == "3",
          "unaffected quantity key stays queryable", fx.failures)
    r = fx.wh.children(U("P"), T.format(13), batch=1)
    check(qty_of(r, "X", "mass")["status"] == "unknown",
          "later ordinary ADD does not auto-clear unknown", fx.failures)
    # late 09:00 event in batch 2 makes the history conflict-free
    fx.ingest([agg("e0", 9, "ADD", "P", quantities=[("X", 2, "KGM")])])
    r = fx.wh.children(U("P"), T.format(13), batch=2)
    check(qty_of(r, "X", "mass")["quantity"] == "3",
          "new batch recomputes conflict-free history: 2+5-6+2 = 3", fx.failures)
    r = fx.wh.children(U("P"), T.format(13), batch=1)
    check(qty_of(r, "X", "mass")["status"] == "unknown",
          "old batch view is not rewritten", fx.failures)


def t_double_parent(fx):
    print("T4: simultaneous double-parent is ambiguous, no id tie-break")
    fx.ingest([agg("e1", 10, "ADD", "P1", children=["C"]),
               agg("e2", 10, "ADD", "P2", children=["C"]),
               agg("e3", 10, "ADD", "P1", children=["D"]),
               agg("e4", 12, "ADD", "P1", children=["C"])])
    r = fx.wh.ancestors(U("C"), T.format(13))
    check(r["status"] == "unknown" and r["detail"]["reason"] == "double_parent"
          and r["detail"]["parents"] == [U("P1"), U("P2")]
          and sorted(r["detail"]["events"]) == ["e1", "e2"],
          "child unknown with both parents and both events cited", fx.failures)
    r = fx.wh.children(U("P1"), T.format(13))
    names = [c["epc"] for c in r["named_children"]]
    check(names == [U("D")], "unaffected member D stays in P1; C attributable to neither",
          fx.failures)
    r = fx.wh.ancestors(U("C"), T.format(9))
    check(r["status"] == "complete" and r["chain"] == [],
          "before 10:00 no ambiguity", fx.failures)


def t_cycle(fx):
    print("T5: packaging cycle reported, not infinitely expanded")
    fx.ingest([agg("e1", 10, "ADD", "B", children=["A"]),
               agg("e2", 11, "ADD", "C", children=["B"]),
               agg("e3", 12, "ADD", "A", children=["C"]),
               agg("e4", 12, "ADD", "A", children=["M"])])
    r = fx.wh.ancestors(U("A"), T.format(13))
    check(r["status"] == "cycle"
          and sorted(r["detail"]["cycle_nodes"]) == [U("A"), U("B"), U("C")]
          and sorted(r["detail"]["cycle_events"]) == ["e1", "e2", "e3"],
          "in-cycle ancestors unknown with cycle nodes+events", fx.failures)
    r = fx.wh.ancestors(U("M"), T.format(13))
    check(r["status"] == "cycle" and r["chain"][0]["parent"] == U("A"),
          "ancestor chain through the cycle is unknown too", fx.failures)
    r = fx.wh.ancestors(U("A"), H.format(10))
    check(r["status"] == "complete" and len(r["chain"]) == 1,
          "before the cycle closes, chain is complete", fx.failures)


def t_lineage(fx):
    print("T6: transformation lineage links: missing/linked/pending/invalid/cycle")
    fx.ingest([
        trans("T0", 10, in_epcs=["Z"], out_epcs=["M"], out_qty=[("X", 2, "KGM")]),
        trans("T1", 12, in_epcs=["M"], in_qty=[("X", 1, "KGM")], out_epcs=["N"]),
        trans("T2", 14, in_epcs=["P"], out_epcs=["Q2"]),
        trans("T3", 14, in_epcs=["Q2"], out_epcs=["P"]),
        trans("T4", 16, in_epcs=["R"], out_epcs=["S"]),
        trans("T5", 17, in_epcs=["S"], out_epcs=["R"]),
        trans("T8", 10, in_epcs=["i8"], out_qty=[("W", 1, "KGM")]),
        trans("T9", 12, in_qty=[("W", 1, "LTR")], out_epcs=["o9"]),
    ])
    r = fx.wh.trace(U("N"), T.format(13), batch=1)
    ins = r["nodes"]["T1"]["inputs"]
    check(all(i["lineage"]["status"] == "missing_evidence" for i in ins)
          and "T0" not in r["nodes"],
          "same identifiers across events do NOT connect without links", fx.failures)

    links = [
        {"downstream": {"event": "T1", "list": "inputEPCList", "index": 0},
         "upstream": {"event": "T0", "list": "outputEPCList", "index": 0}},
        {"downstream": {"event": "T1", "list": "inputQuantityList", "index": 0},
         "upstream": {"event": "T0", "list": "outputQuantityList", "index": 0}},
        {"downstream": {"event": "T2", "list": "inputEPCList", "index": 0},
         "upstream": {"event": "T3", "list": "outputEPCList", "index": 0}},
        {"downstream": {"event": "T3", "list": "inputEPCList", "index": 0},
         "upstream": {"event": "T2", "list": "outputEPCList", "index": 0}},
        {"downstream": {"event": "T4", "list": "inputEPCList", "index": 0},
         "upstream": {"event": "T5", "list": "outputEPCList", "index": 0}},
        {"downstream": {"event": "T9", "list": "inputQuantityList", "index": 0},
         "upstream": {"event": "T8", "list": "outputQuantityList", "index": 0}},
    ]
    fx.ingest([obj("obs1", 18, epcs=["marker"])], links=links)
    r = fx.wh.trace(U("N"), T.format(13), batch=2)
    ins = {i["item"]["id"]: i for i in r["nodes"]["T1"]["inputs"]}
    check(ins[U("M")]["lineage"]["status"] == "linked"
          and ins[U("M")]["lineage"]["upstream"]["event"] == "T0",
          "declared link connects T1 input to T0 output", fx.failures)
    check("T0" in r["nodes"]
          and r["nodes"]["T0"]["inputs"][0]["lineage"]["status"] == "missing_evidence",
          "upstream node included; its own input keeps missing_evidence", fx.failures)
    check(ins[U("X")]["lineage"]["status"] == "linked", "class input linked too", fx.failures)
    r = fx.wh.trace(U("N"), T.format(13), batch=1)
    check("T0" not in r["nodes"], "batch 1 view unchanged by batch 2 links", fx.failures)

    r = fx.wh.trace(U("Q2"), T.format(15), batch=2)
    check(len(r["cycles"]) == 1 and sorted(set(r["cycles"][0]["events"])) == ["T2", "T3"],
          "same-moment link cycle explicitly reported", fx.failures)
    lin = r["nodes"]["T3"]["inputs"][0]["lineage"]
    check(lin["status"] == "cycle", "cyclic link marked, expansion stopped", fx.failures)

    r = fx.wh.trace(U("S"), T.format(18), batch=2)
    lin = r["nodes"]["T4"]["inputs"][0]["lineage"]
    check(lin["status"] == "invalid" and lin["reason"] == "future_source",
          "upstream later than downstream is invalid", fx.failures)

    r = fx.wh.trace(U("o9"), T.format(13), batch=2)
    lin = r["nodes"]["T9"]["inputs"][0]["lineage"]
    check(lin["status"] == "invalid" and lin["reason"] == "dimension_mismatch",
          "same class with incompatible dimensions is invalid", fx.failures)

    # batch 3 adds a link whose upstream endpoint never arrives -> pending
    fx.ingest([obj("obs2", 19, epcs=["marker2"])], links=[
        {"downstream": {"event": "T1", "list": "inputEPCList", "index": 0},
         "upstream": {"event": "NOPE", "list": "outputEPCList", "index": 0}}])
    r = fx.wh.trace(U("N"), T.format(13), batch=3)
    m_lin = r["nodes"]["T1"]["inputs"][0]["lineage"]
    check(isinstance(m_lin, list) and {s["status"] for s in m_lin} == {"linked", "pending"},
          "second link on same coordinate stays pending (missing endpoint)", fx.failures)
    r = fx.wh.trace(U("N"), T.format(13), batch=2)
    check(r["nodes"]["T1"]["inputs"][0]["lineage"]["status"] == "linked",
          "batch 2 view does not see batch 3 links", fx.failures)


def t_conflict(fx):
    print("T7: eventID conflict suspends qualified results; old batch intact")
    fx.ingest([agg("X1", 10, "ADD", "P", children=["C"]),
               agg("ok1", 10, "ADD", "P", children=["Q"])])
    r = fx.wh.ancestors(U("C"), T.format(11), batch=1)
    check(r["status"] == "complete" and r["chain"][0]["parent"] == U("P"),
          "batch 1 sees C in P", fx.failures)
    fx.ingest([agg("X1", 10, "ADD", "P2", children=["C"])])  # same id, different content
    r = fx.wh.ancestors(U("C"), T.format(11), batch=2)
    check(r["status"] == "suspended" and r["reason"] == "event_id_conflict",
          "batch 2 suspends ancestors of C", fx.failures)
    r = fx.wh.children(U("P"), T.format(11), batch=2)
    check(r["status"] == "suspended", "children of P suspended too", fx.failures)
    r = fx.wh.ancestors(U("C"), T.format(11), batch=1)
    check(r["status"] == "complete" and r["chain"][0]["parent"] == U("P"),
          "batch 1 view unchanged after the conflict arrives", fx.failures)
    r = fx.wh.ancestors(U("Q"), T.format(11), batch=2)
    check(r["status"] == "complete" and r["chain"][0]["parent"] == U("P"),
          "unrelated members of the same parent remain queryable", fx.failures)
    # retransmission of the identical event keeps provenance, no conflict
    fx.ingest([agg("ok1", 10, "ADD", "P", children=["Q"])])
    ev = fx.wh.event("ok1")
    prov = {(p["batch"], p["file"]) for v in ev["versions"] for p in v["provenance"]}
    check(len(ev["versions"]) == 1 and len(prov) == 2 and ev["conflicted_in_batches"] == [],
          "identical retransmission = multiple provenance, no conflict", fx.failures)


def t_unknown_context_and_unsupported(fx):
    print("T8: unknown context uninterpretable; unsupported shapes explicit")
    bad_ctx = obj("bad1", 10, epcs=["Z"])
    bad_ctx["@context"] = "https://unknown.example.com/epcis.jsonld"
    empty_del = {"@context": CTX, "type": "AggregationEvent", "eventID": "bad2",
                 "eventTime": T.format(10), "recordTime": T.format(10),
                 "action": "DELETE", "parentID": U("P"),
                 "sourceList": SRC, "destinationList": DST}
    obj_add = {"@context": CTX, "type": "ObjectEvent", "eventID": "bad3",
               "eventTime": T.format(10), "recordTime": T.format(10),
               "action": "ADD", "epcList": [U("Z")],
               "sourceList": SRC, "destinationList": DST}
    tx_event = {"@context": CTX, "type": "TransactionEvent", "eventID": "bad4",
                "eventTime": T.format(10), "recordTime": T.format(10),
                "action": "ADD", "epcList": [U("Z")],
                "sourceList": SRC, "destinationList": DST}
    bad_uom = agg("bad5", 10, "ADD", "P", quantities=[("X", 1, "XYZ")])
    no_offset = agg("bad6", 10, "ADD", "P", children=["Z1"])
    no_offset["eventTime"] = "2026-01-01T10:00:00"
    good = agg("good1", 10, "ADD", "P", children=["A"])
    fx.ingest([bad_ctx, empty_del, obj_add, tx_event, bad_uom, no_offset, good])
    rev = fx.wh.review()
    by_id = {p["event_id"]: p for p in rev["non_ok_events"]}
    check(by_id.get("bad2", {}).get("status") == "unsupported"
          and "empty declared members" in by_id["bad2"]["reason"],
          "empty-member DELETE is unsupported, not a no-op", fx.failures)
    check(by_id.get("bad3", {}).get("status") == "unsupported",
          "ObjectEvent ADD unsupported", fx.failures)
    check(by_id.get("bad4", {}).get("status") == "unsupported",
          "TransactionEvent unsupported", fx.failures)
    check(by_id.get("bad5", {}).get("status") == "invalid",
          "undeclared uom invalid", fx.failures)
    check(by_id.get("bad6", {}).get("status") == "invalid",
          "eventTime without offset invalid", fx.failures)
    un = [p for p in rev["non_ok_events"] if p["status"] == "uninterpretable"]
    check(len(un) == 1 and "unknown context" in un[0]["reason"],
          "unknown context uninterpretable", fx.failures)
    doc = fx.wh.evidence_doc(un[0]["digest"])
    check("unknown.example.com" in doc.get("raw", ""),
          "original text of uninterpretable event preserved", fx.failures)
    r = fx.wh.children(U("P"), T.format(11))
    check([c["epc"] for c in r["named_children"]] == [U("A")],
          "good event in the same file unaffected", fx.failures)


def t_observe(fx):
    print("T9: ObjectEvent OBSERVE observations and quantity declarations")
    fx.ingest([obj("o1", 10, epcs=["E"], quantities=[("X", 3, "KGM")])])
    r = fx.wh.observations(U("E"), T.format(11))
    check(len(r["observations"]) == 1
          and r["observations"][0]["event"] == "o1"
          and r["observations"][0]["declared"] == {"epc": U("E")},
          "EPC observation listed", fx.failures)
    r = fx.wh.observations("class:" + U("X"), T.format(11))
    check(len(r["observations"]) == 1
          and r["observations"][0]["declared"]["quantity"] == "3",
          "class quantity observation with exact declared quantity", fx.failures)


def t_batch_rejection(fx):
    print("T10: digest mismatch / incomplete package rejected; old batch queryable")
    fx.ingest([agg("e1", 10, "ADD", "P", children=["A"])])
    bdir = os.path.join(fx.dir, "badbatch")
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, "ev.json"), "w") as fh:
        json.dump(agg("e2", 11, "ADD", "P", children=["B"]), fh)
    manifest = {"batch": "bad", "files": [
        {"name": "ev.json", "sha256": "0" * 64},
        {"name": "missing.json", "sha256": "0" * 64}], "lineage_links": []}
    mpath = os.path.join(bdir, "manifest.json")
    with open(mpath, "w") as fh:
        json.dump(manifest, fh)
    res = fx.wh.ingest(mpath)
    check(res["status"] == "rejected"
          and ("digest mismatch" in res["reason"] or "missing" in res["reason"]),
          "incomplete/mismatched package rejected", fx.failures)
    r = fx.wh.children(U("P"), T.format(12), batch=1)
    check([c["epc"] for c in r["named_children"]] == [U("A")],
          "old batch still queryable, rejected events absent", fx.failures)
    check(fx.wh.store.latest_batch() == 1, "no new batch published", fx.failures)

    # malformed lineage link also rejects the batch
    res = fx.ingest([agg("e3", 12, "ADD", "P", children=["D"])],
                    links=[{"downstream": {"event": "x", "list": "outputEPCList", "index": 0},
                            "upstream": {"event": "y", "list": "outputEPCList", "index": 0}}])
    check(res["status"] == "rejected" and "lineage_links" in res["reason"],
          "link with non-input downstream list rejected", fx.failures)
    check(fx.wh.store.latest_batch() == 1, "still no new batch published", fx.failures)


def t_record_time_not_business_time(fx):
    print("T11: recordTime is only first-reported evidence, not business time")
    late_record = agg("e1", 10, "ADD", "P", children=["A"],
                      record="2026-02-01T00:00:00+00:00")
    early_record = agg("e2", 11, "DELETE", "P", children=["A"],
                       record="2026-01-01T10:30:00+00:00")
    fx.ingest([early_record, late_record])  # file order reversed on purpose
    r = fx.wh.ancestors(U("A"), T.format(12))
    check(r["status"] == "complete" and r["chain"] == [],
          "eventTime governs state (delete at 11 applies after add at 10)", fx.failures)
    ev = fx.wh.event("e1")
    check(ev["versions"][0]["record_time"] == "2026-02-01T00:00:00+00:00",
          "recordTime preserved as reporting evidence", fx.failures)


def _strip(result):
    """Remove warehouse-local paths so results compare across directories."""
    def rec(o):
        if isinstance(o, dict):
            return {k: rec(v) for k, v in o.items()
                    if k not in ("raw_path", "expanded_path")}
        if isinstance(o, list):
            return [rec(v) for v in o]
        return o
    return rec(result)


def t_order_chunk_invariance(fx):
    print("T12: file order and chunking do not change results")
    events = [agg("e1", 10, "ADD", "P", children=["A", "B"], quantities=[("X", 5, "KGM")]),
              agg("e2", 11, "DELETE", "P", children=["A"], quantities=[("X", 500, "GRM")]),
              trans("e3", 12, in_epcs=["B"], out_epcs=["N"])]
    fx.ingest(events, chunk=1, shuffle=True)
    fx2 = Fixture()
    fx2.failures = fx.failures
    fx2.ingest(events, chunk=3)  # all in one file
    for t in (T.format(12), T.format(10)):
        r1 = fx.wh.children(U("P"), t, 1)
        r2 = fx2.wh.children(U("P"), t, 1)
        check(_strip(r1) == _strip(r2), f"children identical across chunking at {t}",
              fx.failures)
    r1 = fx.wh.trace(U("N"), T.format(13))
    r2 = fx2.wh.trace(U("N"), T.format(13))
    check(_strip(r1) == _strip(r2), "trace identical across chunking", fx.failures)
    fx2.close()


def t_reopen_and_copy(fx):
    print("T13: close/reopen and copy-reopen give identical results")
    fx.ingest([agg("e1", 10, "ADD", "P", children=["A"], quantities=[("X", 2, "KGM")]),
               trans("e2", 11, in_epcs=["A"], out_epcs=["N"])])
    before_children = _strip(fx.wh.children(U("P"), T.format(12)))
    before_trace = _strip(fx.wh.trace(U("N"), T.format(12)))
    fx.wh.close()
    wh2 = Warehouse.open(os.path.join(fx.dir, "wh"))
    check(_strip(wh2.children(U("P"), T.format(12))) == before_children,
          "reopen identical", fx.failures)
    wh2.close()
    dst = os.path.join(fx.dir, "wh-copy")
    shutil.copytree(os.path.join(fx.dir, "wh"), dst)
    wh3 = Warehouse.open(dst)
    check(_strip(wh3.children(U("P"), T.format(12))) == before_children,
          "copied warehouse identical", fx.failures)
    check(_strip(wh3.trace(U("N"), T.format(12))) == before_trace,
          "copied warehouse trace identical", fx.failures)
    wh3.close()
    fx.wh = Warehouse.open(os.path.join(fx.dir, "wh"))


def t_stats_no_hard_sum(fx):
    print("T14: transformation stats per dimension; packaging never enters totals")
    fx.ingest([trans("T1", 10, in_qty=[("X", 2, "KGM"), ("Y", 3, "EA")],
                     out_qty=[("X", 1, "KGM")]),
               agg("a1", 11, "ADD", "P", quantities=[("X", 100, "KGM")])])
    r = fx.wh.transformation("T1", T.format(12))
    stats = r["node"]["stats"]
    check(stats["mass"]["input_total"] == "2" and stats["mass"]["output_total"] == "1"
          and stats["mass"]["difference"] == "1",
          "mass totals exact, non-zero difference reported raw", fx.failures)
    check(stats["count"]["input_total"] == "3" and stats["count"]["output_total"] == "0",
          "count dimension kept separate (no hard cross-dimension sum)", fx.failures)
    check("mass" in stats and len(stats) == 2,
          "packaging ADD of 100 KGM does not enter transformation totals", fx.failures)


ALL_TESTS = [
    ("T1", t_class_repeated_add),
    ("T2", t_unit_conversion),
    ("T3", t_over_delete_unknown_and_recovery),
    ("T4", t_double_parent),
    ("T5", t_cycle),
    ("T6", t_lineage),
    ("T7", t_conflict),
    ("T8", t_unknown_context_and_unsupported),
    ("T9", t_observe),
    ("T10", t_batch_rejection),
    ("T11", t_record_time_not_business_time),
    ("T12", t_order_chunk_invariance),
    ("T13", t_reopen_and_copy),
    ("T14", t_stats_no_hard_sum),
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
    print("all scenario tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
