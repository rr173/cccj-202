#!/usr/bin/env python3
"""Scenario tests for offline selective disclosure (checkpoint / page /
audit commands).

Covers: checkpoint determinism under reordering and retransmission,
immutability under later batches, inclusion of invalid/unsupported
variants, exclusion of id-less records, boundary algebra (digest splits,
open ends, non-real bounds), empty pages, the N=0/N=1 trees, receive
ordering and idempotence, overlap/adjacency, truncation and trailing
data, wrong-checkpoint and corrupted pages, completeness attacks
(dropped row, extended bounds), workspace binding, finalize gating and
determinism, and the warehouse's own conflict view staying untouched.

Run:  python3 tests/run_disclosure_tests.py
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
from epcis_warehouse import audit as A  # noqa: E402
from epcis_warehouse import disclosure as D  # noqa: E402

CTX = "urn:example:ctx:epcis"
LA, LB = "urn:example:loc:A", "urn:example:loc:B"
CK = "urn:example:class:K"
UNITS = {"units": {"EA": {"dimension": "count", "factor": "1"}}}
WID = "urn:example:warehouse:test"


def obs(eid, qty, minute=0, uom="EA", epcs=None):
    ev = {"@context": CTX, "type": "ObjectEvent", "eventID": eid,
          "eventTime": f"2026-01-01T10:{minute:02d}:00+00:00",
          "recordTime": f"2026-01-01T10:{minute:02d}:00+00:00",
          "action": "OBSERVE",
          "sourceList": [{"source": LA}],
          "destinationList": [{"destination": LB}]}
    if qty is not None:
        ev["quantityList"] = [{"epcClass": CK, "quantity": qty, "uom": uom}]
    if epcs is not None:
        ev["epcList"] = epcs
    return ev


def agg_delete_all(eid, minute=0):
    """AggregationEvent DELETE with empty members -> unsupported."""
    return {"@context": CTX, "type": "AggregationEvent", "eventID": eid,
            "eventTime": f"2026-01-01T10:{minute:02d}:00+00:00",
            "recordTime": f"2026-01-01T10:{minute:02d}:00+00:00",
            "action": "DELETE", "parentID": "urn:example:epc:p",
            "childEPCs": [],
            "sourceList": [{"source": LA}],
            "destinationList": [{"destination": LB}]}


class Fixture:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="epcis-disc-test-")
        self.seq = 0
        self.failures = []
        units_path = os.path.join(self.dir, "units.json")
        with open(units_path, "w") as fh:
            json.dump(UNITS, fh)
        self.units_path = units_path

    def check(self, cond, msg):
        if cond:
            print(f"  PASS {msg}")
        else:
            self.failures.append(msg)
            print(f"  FAIL {msg}")

    def expect_error(self, fn, needle, msg):
        try:
            fn()
        except D.DisclosureError as exc:
            if needle in str(exc):
                print(f"  PASS {msg}")
            else:
                self.failures.append(f"{msg} (wrong reason: {exc})")
                print(f"  FAIL {msg} (wrong reason: {exc})")
        except Exception as exc:  # noqa: BLE001
            self.failures.append(f"{msg} (wrong exception: {exc!r})")
            print(f"  FAIL {msg} (wrong exception: {exc!r})")
        else:
            self.failures.append(msg)
            print(f"  FAIL {msg} (no error)")

    def warehouse(self, name, batches):
        """Build a warehouse; batches = list of batches, each a list of
        files, each file an event dict or a list of event dicts."""
        wh_dir = os.path.join(self.dir, name)
        wh = Warehouse.create(wh_dir, self.units_path)
        for files_content in batches:
            self.seq += 1
            bdir = os.path.join(self.dir, f"files-{self.seq}")
            os.makedirs(bdir)
            files = []
            for i, content in enumerate(files_content):
                fn = f"{i:02d}.json"
                with open(os.path.join(bdir, fn), "w") as fh:
                    json.dump(content, fh)
                files.append({"name": fn, "sha256": hashlib.sha256(
                    open(os.path.join(bdir, fn), "rb").read()).hexdigest()})
            manifest = {"batch": f"b{self.seq}", "files": files,
                        "lineage_links": []}
            mpath = os.path.join(bdir, "manifest.json")
            with open(mpath, "w") as fh:
                json.dump(manifest, fh)
            out = wh.ingest(mpath)
            assert out["status"] == "published", out
        return wh

    def ws(self, name, checkpoint_path, lo=None, hi=None):
        path = os.path.join(self.dir, name)
        A.init_workspace(path, checkpoint_path,
                         D.bound_from_json(lo, True), D.bound_from_json(hi, False))
        return path


def bound(eid, digest=None):
    obj = {"event_id": eid}
    if digest:
        obj["digest"] = digest
    return obj


def export(wh, cpid, lo, hi, path):
    return D.export_page(os.path.join(wh.root, "disclosure"), cpid,
                         D.bound_from_json(lo, True),
                         D.bound_from_json(hi, False), path)


def page_rows(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh.read().splitlines()[1:]]


def page_hdr(path):
    with open(path, encoding="utf-8") as fh:
        return json.loads(fh.readline())


def rewrite_page(src, dst, mutate_hdr=None, drop_rows=(), extra_tail=None,
                 mutate_row=None):
    with open(src, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    hdr = json.loads(lines[0])
    rows = [json.loads(l) for l in lines[1:]]
    if mutate_row:
        rows = [mutate_row(r) for r in rows]
    rows = [r for i, r in enumerate(rows) if i not in drop_rows]
    if mutate_hdr:
        hdr = mutate_hdr(hdr)
    hdr["row_count"] = len(rows)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(hdr) + "\n")
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        if extra_tail:
            fh.write(extra_tail)
    return dst


def main():
    fx = Fixture()
    P = lambda *a: os.path.join(fx.dir, *a)  # noqa: E731

    # ------------------------------------------------------------------
    print("checkpoint determinism: file order, chunking, retransmission")
    e1, e2, e3 = obs("a-1", 1, 0), obs("a-2", 2, 1), obs("a-3", 3, 2)
    wh1 = fx.warehouse("wh1", [[e1, e2, e3]])
    # same batch-1 view: reordered files, one file holding an array, a
    # full retransmission of e1 in the same batch
    wh2 = fx.warehouse("wh2", [[[e3, e1], e2, e1]])
    cp1 = wh1.checkpoint_build(WID, 1)
    cp2 = wh2.checkpoint_build(WID, 1)
    fx.check(cp1 == cp2 and cp1["variants"] == 3,
             "same logical inventory -> same checkpoint id (order/chunking/"
             "retransmission irrelevant)")
    fx.check(wh1.checkpoint_build(WID, 1) == cp1, "rebuild is idempotent")
    wh1.close(); wh2.close()

    # ------------------------------------------------------------------
    print("inclusion/exclusion of record kinds")
    # an invalid event without eventID: missing eventTime
    noid = obs("k-noid", 9, 9)
    del noid["eventID"]
    del noid["eventTime"]
    wh = fx.warehouse("wh4", [[obs("k-ok", 1, 0), obs("k-bad-uom", 1, 1, uom="XYZ"),
                               agg_delete_all("k-unsupported", 2),
                               {"@context": "urn:example:ctx:unknown",
                                "type": "ObjectEvent"}, noid]])
    cp = wh.checkpoint_build(WID, 1)
    fx.check(cp["variants"] == 3,
             "ok + invalid + unsupported enter; uninterpretable and id-less do not")
    export(wh, cp["checkpoint"], None, None, P("all.jsonl"))
    rows = page_rows(P("all.jsonl"))
    fx.check([r["event_id"] for r in rows]
             == ["k-bad-uom", "k-ok", "k-unsupported"],
             "inventory sorted by raw UTF-8 bytes of event id")
    fx.check({r["status"] for r in rows} == {"ok", "invalid", "unsupported"},
             "all three statuses represented")
    review_before = wh.review(1)

    # checkpoint build does not disturb the warehouse's own conflict view
    wh.checkpoint_build(WID, 1)
    fx.check(wh.review(1) == review_before,
             "checkpoint build leaves warehouse review untouched")

    # ------------------------------------------------------------------
    print("immutability under later batches")
    whb = fx.warehouse("wh5", [[obs("m-1", 1, 0)], [obs("m-1", 2, 1),
                                                   obs("m-2", 3, 2)]])
    cpa = whb.checkpoint_build(WID, 1)
    cpb = whb.checkpoint_build(WID, 2)
    fx.check(cpa["variants"] == 1 and cpb["variants"] == 3,
             "batch views: 1 variant at b1, 3 at b2 (m-1 gains a variant)")
    fx.check(whb.checkpoint_build(WID, 1) == cpa,
             "batch-1 checkpoint identical after batch 2")
    export(whb, cpa["checkpoint"], None, None, P("b1.jsonl"))
    fx.check(len(page_rows(P("b1.jsonl"))) == 1,
             "batch-1 pages still export the batch-1 inventory")
    conflicts_b2 = whb.review(2)["conflicts"]
    fx.check(any(c["event_id"] == "m-1" for c in conflicts_b2),
             "warehouse conflict determination unchanged by disclosure")
    whb.close()

    # ------------------------------------------------------------------
    print("boundary algebra on one id with several variants")
    variants = [obs("x", i, i) for i in range(6)]
    whx = fx.warehouse("whx", [variants])
    cpx = whx.checkpoint_build(WID, 1)
    fx.check(cpx["variants"] == 6, "six variants of one id")
    digests = sorted(r["digest"] for r in
                     json.loads(json.dumps(whx.event("x")))["versions"])
    d = lambda i: digests[i]  # noqa: E731
    # split the id's variants at digest positions: [x@d1, x@d4)
    export(whx, cpx["checkpoint"], bound("x", d(1)), bound("x", d(4)), P("mid.jsonl"))
    fx.check([r["digest"] for r in page_rows(P("mid.jsonl"))]
             == [d(1), d(2), d(3)], "digest-position split selects d1..d3")
    # boundary need not be a real row: nonexistent id and nonexistent digest
    export(whx, cpx["checkpoint"], bound("w"), bound("y"), P("fake.jsonl"))
    fx.check(len(page_rows(P("fake.jsonl"))) == 6,
             "nonexistent-id bounds bracket all variants of x")
    i = 0
    while True:
        fake = hashlib.sha256(f"fake-{i}".encode()).hexdigest()
        i += 1
        if d(0) < fake < d(1):
            break
    export(whx, cpx["checkpoint"], bound("x", fake), bound("x", d(2)), P("fake2.jsonl"))
    fx.check([r["digest"] for r in page_rows(P("fake2.jsonl"))] == [d(1)],
             "nonexistent-digest bound positions inside the id's variant list")
    # open bounds
    export(whx, cpx["checkpoint"], None, bound("x", d(2)), P("open-lo.jsonl"))
    fx.check([r["digest"] for r in page_rows(P("open-lo.jsonl"))] == [d(0), d(1)],
             "open lower bound starts at the first leaf")
    export(whx, cpx["checkpoint"], bound("x", d(4)), None, P("open-hi.jsonl"))
    fx.check([r["digest"] for r in page_rows(P("open-hi.jsonl"))] == [d(4), d(5)],
             "open upper bound ends at the last leaf")
    # zero-width page is refused
    fx.expect_error(
        lambda: export(whx, cpx["checkpoint"], bound("x", d(2)), bound("x", d(2)),
                       P("zw.jsonl")),
        "non-zero width", "zero-width page refused at export")
    whx.close()

    # ------------------------------------------------------------------
    print("receive: ordering, idempotence, overlap, corruption, completeness")
    base = [obs("m-07", 1, 0), obs("m-08", 2, 1), obs("m-08", 3, 2, uom="XYZ"),
            obs("m-20", 5, 3)]
    whm = fx.warehouse("whm", [base, [obs("m-08", 4, 4)]])
    cpm1 = whm.checkpoint_build(WID, 1)
    cpm2 = whm.checkpoint_build(WID, 2)
    with open(P("cpm1.json"), "w") as fh:
        json.dump(cpm1, fh)
    with open(P("cpm2.json"), "w") as fh:
        json.dump(cpm2, fh)
    m08 = sorted(v["digest"] for v in whm.event("m-08")["versions"]
                 if v["first_batch"] == 1)
    dlo, dhi = m08
    export(whm, cpm1["checkpoint"], bound("m-07"), bound("m-08", dhi), P("p1"))
    export(whm, cpm1["checkpoint"], bound("m-08", dhi), bound("m-10"), P("p2"))
    export(whm, cpm1["checkpoint"], bound("m-10"), bound("m-20"), P("p3"))

    ws = fx.ws("ws1", P("cpm1.json"), bound("m-07"), bound("m-20"))
    fx.check(A.receive_page(ws, P("p2"))["status"] == "accepted",
             "out-of-order receive accepted")
    fx.check(A.receive_page(ws, P("p1"))["status"] == "accepted", "p1 accepted")
    fx.check(A.receive_page(ws, P("p1"))["status"] == "already_received",
             "re-receive is a no-op")
    g = A.gaps(ws)
    fx.check(g["rows_received"] == 3 and len(g["missing"]) == 1,
             "coverage tracked by intervals, not row counts")

    # finalize gated on coverage
    summary, code = A.finalize(ws, P("out-early"))
    fx.check(code == 2 and not os.path.exists(P("out-early", "summary.json")),
             "finalize refuses incomplete coverage, writes nothing")

    # overlap: a valid page for [m-07, m-10) overlaps p1+p2
    export(whm, cpm1["checkpoint"], bound("m-07"), bound("m-10"), P("pbig"))
    fx.expect_error(lambda: A.receive_page(ws, P("pbig")),
                    "overlaps", "overlapping valid page rejected")
    # adjacency is allowed: p3 touches p2 at m-10
    fx.check(A.receive_page(ws, P("p3"))["status"] == "accepted",
             "adjacent page accepted")

    # wrong checkpoint (batch-2 page for the same interval)
    export(whm, cpm2["checkpoint"], bound("m-08", dhi), bound("m-10"), P("p2b"))
    fx.expect_error(lambda: A.receive_page(ws, P("p2b")),
                    "different checkpoint", "old/other-batch page rejected")

    # truncated page
    raw = open(P("p1"), "rb").read()
    with open(P("p1trunc"), "wb") as fh:
        fh.write(raw[: len(raw) // 2])
    fx.expect_error(lambda: A.receive_page(ws, P("p1trunc")),
                    "", "truncated page rejected")

    # trailing garbage
    rewrite_page(P("p1"), P("p1tail"), extra_tail='{"x": 1}\n')
    fx.expect_error(lambda: A.receive_page(ws, P("p1tail")),
                    "unconsumed", "page with trailing data rejected")

    # dropped real row (others genuine)
    rewrite_page(P("p1"), P("p1drop"), drop_rows=(1,))
    fx.expect_error(lambda: A.receive_page(ws, P("p1drop")),
                    "checkpoint root", "page missing one real row rejected")

    # hi extended past an undisclosed real variant
    rewrite_page(P("p1"), P("p1ext"),
                 mutate_hdr=lambda h: dict(h, hi=bound("m-10")))
    fx.expect_error(lambda: A.receive_page(ws, P("p1ext")),
                    "right edge", "hi extended over an undisclosed variant rejected")

    # lo reduced before a disclosed neighbour
    rewrite_page(P("p2"), P("p2ext"),
                 mutate_hdr=lambda h: dict(h, lo=bound("m-07")))
    fx.expect_error(lambda: A.receive_page(ws, P("p2ext")),
                    "left edge", "lo reduced over an undisclosed variant rejected")

    # corrupted digest nibble
    def flip(row):
        row = dict(row)
        row["digest"] = ("0" if row["digest"][0] != "0" else "1") + row["digest"][1:]
        return row
    rewrite_page(P("p1"), P("p1flip"), mutate_row=flip)
    fx.expect_error(lambda: A.receive_page(ws, P("p1flip")),
                    "checkpoint root", "corrupted digest rejected")

    # corrupted proof hash
    def bad_proof(h):
        h = dict(h)
        if h["proof"]:
            p = h["proof"][0]
            h["proof"][0] = ("0" if p[0] != "0" else "1") + p[1:]
        else:  # segment covers the whole tree: corrupt a row instead
            h["row_count"] = h["row_count"] + 1
        return h
    rewrite_page(P("p1"), P("p1proof"), mutate_hdr=bad_proof)
    fx.expect_error(lambda: A.receive_page(ws, P("p1proof")),
                    "", "corrupted proof rejected")

    # truncated proof (too few hashes)
    def short_proof(h):
        h = dict(h)
        h["proof"] = h["proof"][:-1]
        return h
    rewrite_page(P("p1"), P("p1short"), mutate_hdr=short_proof)
    fx.expect_error(lambda: A.receive_page(ws, P("p1short")),
                    "", "truncated proof rejected")

    # extra proof hash
    def long_proof(h):
        h = dict(h)
        h["proof"] = h["proof"] + ["0" * 64]
        return h
    rewrite_page(P("p1"), P("p1long"), mutate_hdr=long_proof)
    fx.expect_error(lambda: A.receive_page(ws, P("p1long")),
                    "unconsumed", "proof with extra hash rejected")

    # row outside the page's own range
    def swap_rows(h):
        return h
    rewrite_page(P("p2"), P("p2outside"))
    lines = open(P("p2"), encoding="utf-8").read().splitlines()
    hdr = json.loads(lines[0])
    row = json.loads(lines[1])
    row["event_id"] = "m-20"  # outside [m-08@dhi, m-10)
    with open(P("p2outside"), "w") as fh:
        fh.write(json.dumps(hdr) + "\n" + json.dumps(row) + "\n")
    fx.expect_error(lambda: A.receive_page(ws, P("p2outside")),
                    "outside", "row outside page range rejected")

    # progress untouched by all the rejections
    g = A.gaps(ws)
    fx.check(g["complete"] and g["rows_received"] == 3
             and g["pages_received"] == 3,
             "progress intact after every rejection")

    # finalize: m-07 x1, m-08 x2, no m-20
    summary, code = A.finalize(ws, P("out"))
    fx.check(code == 0 and summary["variants"] == 3
             and summary["event_ids"] == 2 and summary["multi_variant_ids"] == 1,
             "finalize summary counts")
    with open(P("out", "by_event.jsonl")) as fh:
        bye = [json.loads(l) for l in fh]
    fx.check(bye == [{"event_id": "m-07", "multi_variant": False, "variants": 1},
                     {"event_id": "m-08", "multi_variant": True, "variants": 2}],
             "per-id summary with multi-variant flag")

    # determinism: re-paginate + different order + reopened workspace
    ws2 = fx.ws("ws2", P("cpm1.json"), bound("m-07"), bound("m-20"))
    export(whm, cpm1["checkpoint"], bound("m-07"), bound("m-08", dlo), P("q1"))
    export(whm, cpm1["checkpoint"], bound("m-08", dlo), bound("m-20"), P("q2"))
    A.receive_page(ws2, P("q2"))
    A.receive_page(ws2, P("q1"))
    summary2, _ = A.finalize(ws2, P("out2"))
    for fname in ("inventory.jsonl", "by_event.jsonl", "summary.json"):
        with open(P("out", fname), "rb") as fa, open(P("out2", fname), "rb") as fb:
            fx.check(fa.read() == fb.read(),
                     f"re-pagination + order: {fname} byte-identical")

    # workspace binding
    fx.expect_error(lambda: A.init_workspace(ws2, P("cpm2.json"),
                                             D.bound_from_json(bound("m-07"), True),
                                             D.bound_from_json(bound("m-20"), False)),
                    "different checkpoint or request",
                    "rebinding a workspace to another checkpoint refused")
    fx.expect_error(lambda: A.init_workspace(ws2, P("cpm1.json"),
                                             D.bound_from_json(None, True),
                                             D.bound_from_json(bound("m-20"), False)),
                    "different checkpoint or request",
                    "rebinding a workspace to another request refused")
    fx.check(A.init_workspace(ws2, P("cpm1.json"),
                              D.bound_from_json(bound("m-07"), True),
                              D.bound_from_json(bound("m-20"), False))["status"]
             == "already_initialized", "same binding re-init is a no-op")

    # page outside the request
    ws3 = fx.ws("ws3", P("cpm1.json"), bound("m-08"), bound("m-20"))
    fx.expect_error(lambda: A.receive_page(ws3, P("p1")),
                    "outside the audit request", "page outside the request rejected")
    # empty request refused
    fx.expect_error(lambda: fx.ws("ws4", P("cpm1.json"), bound("m-10"), bound("m-10")),
                    "non-zero width", "zero-width request refused")
    whm.close()

    # ------------------------------------------------------------------
    print("degenerate trees: N=0 and N=1")
    # N=1
    whs = fx.warehouse("whs", [[obs("only", 1, 0)]])
    cps = whs.checkpoint_build(WID, 1)
    fx.check(cps["variants"] == 1, "single-variant checkpoint")
    with open(P("cps.json"), "w") as fh:
        json.dump(cps, fh)
    export(whs, cps["checkpoint"], None, None, P("only"))
    export(whs, cps["checkpoint"], None, bound("a"), P("before"))
    export(whs, cps["checkpoint"], bound("z"), None, P("after"))
    fx.check(len(page_rows(P("only"))) == 1 and page_rows(P("before")) == []
             and page_rows(P("after")) == [], "N=1 pages: one full, two empty")
    wss = fx.ws("ws5", P("cps.json"), None, None)
    fx.check(A.receive_page(wss, P("only"))["status"] == "accepted",
             "single full page accepted")
    summary, code = A.finalize(wss, P("outs"))
    fx.check(code == 0 and summary["variants"] == 1, "N=1 finalize")
    wss2 = fx.ws("ws6", P("cps.json"), None, bound("m"))
    export(whs, cps["checkpoint"], None, bound("m"), P("before-m"))
    A.receive_page(wss2, P("before-m"))
    summary, code = A.finalize(wss2, P("outs2"))
    fx.check(code == 0 and summary["variants"] == 0,
             "empty sub-range of N=1 finalizes empty")
    whs.close()

    # N=0
    whe = fx.warehouse("whe", [[{"@context": "urn:example:ctx:unknown",
                                 "type": "ObjectEvent"}]])
    cpe = whe.checkpoint_build(WID, 1)
    fx.check(cpe["variants"] == 0, "empty checkpoint")
    with open(P("cpe.json"), "w") as fh:
        json.dump(cpe, fh)
    export(whe, cpe["checkpoint"], None, None, P("empty"))
    wse = fx.ws("ws7", P("cpe.json"), None, None)
    fx.check(A.receive_page(wse, P("empty"))["status"] == "accepted",
             "empty page over empty checkpoint accepted")
    summary, code = A.finalize(wse, P("oute"))
    fx.check(code == 0 and summary["variants"] == 0,
             "empty inventory delivered with coverage proof")
    whe.close()

    # ------------------------------------------------------------------
    print("checkpoint requires a published batch")
    whr = fx.warehouse("whr", [[obs("r-1", 1, 0)]])
    fx.expect_error(lambda: whr.checkpoint_build(WID, 2),
                    "not a published batch", "unpublished batch refused")
    fx.expect_error(lambda: whr.checkpoint_build("", 1),
                    "non-empty", "empty warehouse id refused")
    whr.close()

    shutil.rmtree(fx.dir, ignore_errors=True)
    print()
    if fx.failures:
        print(f"{len(fx.failures)} FAILURES")
        return 1
    print("all disclosure scenario tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
