#!/usr/bin/env python3
"""Scenario tests for the offline cold-chain exposure ledger.

Covers the rules the hand-checkable drill cannot cover by itself:
half-open installation/sample boundaries, reinstallation that must not
inherit old readings, no fallback from a near bad probe to a far good
one, packaging cycles and conflicted packaging events, unknown
ownership for unregistered EPCs, strictly-greater-than limits, identical
installation de-duplication, install conflicts that resolve on removal,
alarm continuity across probe/owner/midnight changes but not across a
non-over segment, unrelated probes never creating segments, UTC-offset
notation invariance, and the fatal input-contract failures.

Run:  python3 tests/run_coldchain_tests.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from epcis_warehouse.warehouse import Warehouse  # noqa: E402

CTX = "urn:example:ctx:epcis"
LA, LB, LC = (f"urn:example:loc:{p}" for p in "ABC")
P, B, X = "urn:example:pkg:P", "urn:example:pkg:B", "urn:example:pkg:X"
E, F, G, H = (f"urn:example:epc:{x}" for x in "EFGH")
UNITS = {"units": {"EA": {"dimension": "count", "factor": "1"}}}
OPENING_T = "2026-01-01T00:00:00+00:00"


def iso(sec):
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(sec))


T0 = 1767225600  # 2026-01-01T00:00:00Z


def agg(eid, minute, action, parent, children):
    t = iso(T0 + minute * 60)
    return {"@context": CTX, "type": "AggregationEvent", "eventID": eid,
            "eventTime": t, "recordTime": t, "action": action,
            "parentID": parent, "childEPCs": children,
            "sourceList": [{"source": LA}],
            "destinationList": [{"destination": LB}]}


def obs(eid, minute, src, dst, epcs):
    t = iso(T0 + minute * 60)
    return {"@context": CTX, "type": "ObjectEvent", "eventID": eid,
            "eventTime": t, "recordTime": t, "action": "OBSERVE",
            "epcList": epcs, "sourceList": [{"source": src}],
            "destinationList": [{"destination": dst}]}


def at(minute):
    return iso(T0 + minute * 60)


def with_offset(iso_text, offset):
    """Render the same UTC instant with a different offset spelling."""
    from datetime import datetime
    dt = datetime.fromisoformat(iso_text)
    sign = 1 if offset.startswith("+") else -1
    hh, mm = (int(x) for x in offset[1:].split(":"))
    shifted = dt + timedelta(hours=sign * hh, minutes=sign * mm)
    return shifted.strftime("%Y-%m-%dT%H:%M:%S") + offset


class Fixture:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="epcis-cold-test-")
        self.seq = 0
        self.failures = []
        units_path = os.path.join(self.dir, "units.json")
        with open(units_path, "w") as fh:
            json.dump(UNITS, fh)
        self.wh = Warehouse.create(os.path.join(self.dir, "wh"), units_path)

    def ingest(self, events, name=None):
        self.seq += 1
        bdir = os.path.join(self.dir, f"files-{self.seq}")
        os.makedirs(bdir, exist_ok=True)
        fn = "events.json"
        with open(os.path.join(bdir, fn), "w") as fh:
            json.dump(events, fh)
        manifest = {"batch": name or f"b{self.seq}",
                    "files": [{"name": fn, "sha256": hashlib.sha256(
                        open(os.path.join(bdir, fn), "rb").read()).hexdigest()}],
                    "lineage_links": []}
        mpath = os.path.join(bdir, "manifest.json")
        with open(mpath, "w") as fh:
            json.dump(manifest, fh)
        res = self.wh.ingest(mpath)
        assert res["status"] == "published", res
        return res["batch_seq"]

    def write_jsonl(self, name, rows):
        p = os.path.join(self.dir, name)
        with open(p, "w") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return p

    def settle(self, opening, sender, receiver, run="s", batch="1"):
        op = self.write_jsonl(f"opening-{run}.jsonl", opening)
        sp = self.write_jsonl(f"sender-{run}.jsonl", sender)
        rp = self.write_jsonl(f"receiver-{run}.jsonl", receiver)
        out = os.path.join(self.dir, f"settle-{run}")
        summary, code = self.wh.settle(batch, op, sp, rp, out)
        assert code == 0, summary
        return op, os.path.join(out, "summary.json"), os.path.join(out, "ledger.jsonl")

    def coldchain(self, settle_paths, items, installs, samples, run="c",
                  window=(20, 80), batch="1", extra=None):
        op, sump, ledp = settle_paths
        ip = self.write_jsonl(f"items-{run}.jsonl", items)
        xp = self.write_jsonl(f"inst-{run}.jsonl", installs)
        mp = self.write_jsonl(f"samp-{run}.jsonl", samples)
        out = os.path.join(self.dir, f"cold-{run}")
        summary, code = self.wh.coldchain(
            batch, at(window[0]), at(window[1]), op, sump, ledp, ip, xp, mp, out)
        if extra:
            extra["code"] = code
            extra["summary"] = summary
        return out, code, summary

    def read(self, out, name):
        with open(os.path.join(out, f"{name}.jsonl")) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def close(self):
        try:
            self.wh.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


def opening(epcs):
    rows = [{"type": "opening", "as_of": OPENING_T}]
    rows += [{"type": "epc", "epc": e, "location": LA} for e in epcs]
    return rows


def item(epc, limit=8000, alarm=240):
    return {"epc": epc, "limit_mc": limit, "alarm_seconds": alarm}


def inst(probe, container, f, t, valid=300):
    return {"probe": probe, "container": container,
            "from": at(f), "to": at(t), "valid_for_seconds": valid}


def samp(probe, minute, value):
    return {"probe": probe, "at": at(minute), "value_mc": value}


def Fixture_read(out):
    with open(os.path.join(out, "coverage.jsonl")) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main():
    fx = Fixture()
    failures = []

    def check(cond, msg):
        print(f"  {'PASS' if cond else 'FAIL'} {msg}")
        if not cond:
            failures.append(msg)

    # ---- warehouse: E/F in inner box B on pallet P; G and H loose on P
    fx.ingest([
        agg("urn:uuid:a1", 5, "ADD", B, [E, F]),
        agg("urn:uuid:a2", 6, "ADD", P, [B, G, H]),
        obs("urn:uuid:o1", 8, LA, LB, [E]),
        obs("urn:uuid:o2", 8, LA, LB, [E]),
        obs("urn:uuid:o3", 8, LA, LB, [H]),
        obs("urn:uuid:o4", 8, LA, LB, [H]),
    ])
    settle_paths = fx.settle(
        opening([E, F, G, H]),
        [{"handover": "H1", "from": LA, "to": LB, "effective": at(10),
          "scans": ["urn:uuid:o1"]},
         {"handover": "H2", "from": LA, "to": LB, "effective": at(10),
          "scans": ["urn:uuid:o3"]}],
        [{"handover": "H1", "from": LA, "to": LB, "effective": at(10),
          "scans": ["urn:uuid:o2"]},
         {"handover": "H2", "from": LA, "to": LB, "effective": at(10),
          "scans": ["urn:uuid:o4"]}])

    # === 1. half-open boundaries, reinstall, no fallback, strict limit
    items = [item(E), item(F), item(G)]
    installs = [
        inst("pP", P, 0, 90, valid=600),
        # pB removed at minute 40; remounted on the same box at 50
        inst("pB", B, 0, 40, valid=300),
        inst("pB", B, 50, 90, valid=300),
        # identical duplicate installation record counts once
        inst("pB", B, 50, 90, valid=300),
    ]
    samples = [
        # pallet probe: 4 C, refreshed every 5 minutes through the window
        samp("pP", 20, 4000), samp("pP", 25, 4000), samp("pP", 30, 8000),
        samp("pP", 35, 8001), samp("pP", 40, 4000), samp("pP", 45, 4000),
        samp("pP", 50, 4000), samp("pP", 55, 4000),
        # reading at 25 valid to 30 (5-min validity); conflict at 32;
        # a sample taken exactly at removal (40) is outside the install
        samp("pB", 25, 9000),
        samp("pB", 32, 4000), samp("pB", 32, 9000),
        samp("pB", 40, 5000),
        # the remounted probe gets its own reading at 55; the old 25/32
        # readings must never be reused by the new installation
        samp("pB", 55, 9000),
    ]
    out, code, summary = fx.coldchain(settle_paths, items, installs, samples,
                                      run="t1")
    check(code == 0 and summary["status"] == "built", "t1 builds")
    rows = [r for r in Fixture_read(out) if r["epc"] == E]
    timeline = [(r["start"][14:19], r["state"], r["temp_mc"]) for r in rows]
    # 20-25 not_sampled; 25-30 over 9000; 30-32 expired; 32-35 conflict;
    # 35-40 expired (conflict coverage lapsed); 40-50 no near probe -> pallet
    # 4000 normal (not the old pB values); 50-55 new install not_sampled;
    # 55-60 over 9000; then expired until the next pallet-level change only
    got_states = [r["state"] for r in rows]
    check("not_sampled" in got_states and "expired" in got_states
          and "sample_conflict" in got_states,
          "not-sampled / expired / conflict are distinct states")
    # near-probe conflict must never fall back to the good pallet reading
    conflict_rows = [r for r in rows if r["state"] == "sample_conflict"]
    check(conflict_rows and all(
        "installation:inst-" in json.dumps(r["evidence"]) and
        r["evidence"].get("sample") for r in conflict_rows),
        "near-probe conflict is kept (no fallback to the pallet probe)")
    # minute 40 sample belongs to no installation: 40-50 reads the pallet
    seg40 = [r for r in rows if r["start"].endswith("00:40:00+00:00")]
    check(seg40 and seg40[0]["state"] == "normal"
          and seg40[0]["temp_mc"] == 4000,
          "a sample exactly at removal is outside the install; after removal "
          "the pallet probe applies")
    # remount starts without inherited readings
    seg50 = [r for r in rows if r["start"].endswith("00:50:00+00:00")]
    check(seg50 and seg50[0]["state"] == "not_sampled",
          "reinstallation on the same container does not inherit old readings")
    # strict greater-than: G at 8000 normal, 8001 over
    grows = [r for r in Fixture_read(out) if r["epc"] == G]
    check(any(r["state"] == "normal" and r["temp_mc"] == 8000 for r in grows)
          and any(r["state"] == "over_limit" and r["temp_mc"] == 8001
                  for r in grows),
          "only values strictly over the limit alarm (equal limit is normal)")
    check(summary["counts"]["installations"] == 3,
          "identical duplicate installation counted once")

    # === 2. alarm continuity across owner change, probe change, midnight
    # window 23:50 -> 00:30 crosses midnight; H rides the pallet the whole
    # time, with a posted LB->LC handover 30 minutes into the window.
    # A second batch carries the scans for that late handover.
    fx.ingest([
        obs("urn:uuid:o5", 1435, LB, LC, [H]),   # 2026-01-01T23:55
        obs("urn:uuid:o6", 1435, LB, LC, [H]),
    ], name="b-late")
    late_paths = fx.settle(
        opening([E, F, G, H]),
        [{"handover": "H2", "from": LA, "to": LB, "effective": at(10),
          "scans": ["urn:uuid:o3"]},
         {"handover": "H3", "from": LB, "to": LC,
          "effective": "2026-01-02T00:20:00+00:00", "scans": ["urn:uuid:o5"]}],
        [{"handover": "H2", "from": LA, "to": LB, "effective": at(10),
          "scans": ["urn:uuid:o4"]},
         {"handover": "H3", "from": LB, "to": LC,
          "effective": "2026-01-02T00:20:00+00:00", "scans": ["urn:uuid:o6"]}],
        run="late", batch="2")
    w_start, w_end = "2026-01-01T23:50:00+00:00", "2026-01-02T00:30:00+00:00"
    items2 = [item(H, alarm=1200)]
    installs2 = [inst("pP", P, -20, 1500, valid=6000)]
    samples2 = [
        {"probe": "pP", "at": "2026-01-01T23:48:00+00:00", "value_mc": 9000}]
    op, sump, ledp = late_paths
    ip = fx.write_jsonl("items-t2.jsonl", items2)
    xp = fx.write_jsonl("inst-t2.jsonl", installs2)
    mp = fx.write_jsonl("samp-t2.jsonl", samples2)
    out2 = os.path.join(fx.dir, "cold-t2")
    summary2, code2 = fx.wh.coldchain("2", w_start, w_end, op, sump, ledp,
                                      ip, xp, mp, out2)
    check(code2 == 0, "t2 builds")
    alarms2 = fx.read(out2, "alarms")
    check(len(alarms2) == 1 and alarms2[0]["duration_us"] == 40 * 60 * 1_000_000,
          "one continuous 40-minute alarm across midnight and an owner change")
    owners = {o["owner"]: o["duration_us"] // 60_000_000
              for o in alarms2[0]["by_owner"]}
    check(owners == {LB: 30, LC: 10},
          "the alarm splits over-limit time by owner including both locations")
    days = {d["day"] for d in alarms2[0]["by_owner_day"]}
    check(days == {"2026-01-01", "2026-01-02"},
          "midnight alone does not break the alarm")

    # === 3. any positive-duration normal/unknown segment breaks an alarm
    # E's 9 C run is interrupted by a 2-minute expired gap: two runs,
    # each under the 4-minute threshold -> no alarm
    items3 = [item(E, alarm=240)]
    installs3 = [inst("pB", B, 0, 90, valid=120)]
    samples3 = [samp("pB", 20, 9000), samp("pB", 24, 4000),
                samp("pB", 26, 9000)]
    out3, code3, _ = fx.coldchain(settle_paths, items3, installs3, samples3,
                                  run="t3")
    check(code3 == 0 and fx.read(out3, "alarms") == [],
          "a normal gap splits over-limit runs below the threshold")

    # === 4. unknown ownership: an EPC absent from the opening inventory
    items4 = [item("urn:example:epc:ZZ")]
    installs4 = [inst("pP", P, 0, 90, valid=600)]
    samples4 = [samp("pP", 20, 4000)]
    out4, code4, _ = fx.coldchain(settle_paths, items4, installs4, samples4,
                                  run="t4")
    rows4 = Fixture_read(out4)
    check(code4 == 0 and all(r["owner"] is None for r in rows4)
          and all(r["evidence"].get("ownership") is None for r in rows4),
          "unregistered EPC gets null ownership, never a guessed location")

    # === 5. unrelated probe updates never create segments
    items5 = [item(E)]
    installs5 = [inst("pB", B, 0, 90, valid=600),
                 inst("pX", X, 0, 90, valid=60)]   # X is on nobody's chain
    samples5 = [samp("pB", 20, 4000)] + [samp("pX", m, 9000) for m in range(21, 70)]
    out5, code5, summary5 = fx.coldchain(settle_paths, items5, installs5,
                                         samples5, run="t5")
    rows5 = Fixture_read(out5)
    check(code5 == 0 and all("pX" not in json.dumps(r["evidence"])
                             for r in rows5),
          "updates of a probe on no inspected chain produce no detail")
    check(summary5["counts"]["coverage_segments"] <= 6,
          "unrelated samples do not fragment the timeline")

    # === 6. packaging cycle -> chain_unknown for everything through it
    fx2 = Fixture()
    fx2.ingest([
        agg("urn:uuid:c1", 5, "ADD", P, [B]),
        agg("urn:uuid:c2", 6, "ADD", B, [E]),
        agg("urn:uuid:c3", 7, "ADD", B, [P]),   # P -> B closes a cycle
    ])
    sp2 = fx2.settle(opening([E]), [], [])
    out6, code6, _ = fx2.coldchain(
        sp2, [item(E)], [inst("pP", P, 0, 90)], [samp("pP", 20, 4000)],
        run="t6")
    rows6 = Fixture_read(out6)
    check(code6 == 0 and all(r["state"] == "chain_unknown" for r in rows6),
          "a cyclic packaging chain is chain_unknown, never a pallet value")

    # === 7. conflicted packaging event blocks the chain in the new batch
    fx3 = Fixture()
    fx3.ingest([agg("urn:uuid:k1", 5, "ADD", B, [E]),
                agg("urn:uuid:k2", 6, "ADD", P, [B])])
    # same eventID, different expanded content (different parent)
    fx3.ingest([agg("urn:uuid:k1", 5, "ADD", P, [E])], name="b2")
    sp3 = fx3.settle(opening([E]), [], [], batch="2")
    out7, code7, _ = fx3.coldchain(
        sp3, [item(E)], [inst("pP", P, 0, 90)], [samp("pP", 20, 4000)],
        run="t7", batch="2")
    rows7 = Fixture_read(out7)
    check(code7 == 0 and all(r["state"] == "chain_unknown" for r in rows7),
          "a conflicted packaging event blocks temperature attribution")
    ev7 = {json.loads(l)["ref"]: json.loads(l)
           for l in open(os.path.join(out7, "evidence.jsonl"))}
    chain_ev = [v for v in ev7.values() if v["kind"] == "chain"]
    check(chain_ev and any(v.get("block_event") for v in chain_ev),
          "the blocking event id is traceable from the chain evidence")

    # === 8. UTC-offset notation invariance (same instants, different spelling)
    base_rows = Fixture_read(out)
    off_installs = [{**i, "from": with_offset(i["from"], "+08:00"),
                     "to": with_offset(i["to"], "+08:00")} for i in installs]
    off_samples = [dict(s, at=with_offset(s["at"], "-05:00")) for s in samples]
    out8, code8, _ = fx.coldchain(settle_paths, items, off_installs, off_samples,
                                  run="t8")
    check(code8 == 0 and Fixture_read(out8) == base_rows,
          "equivalent offset spellings give byte-identical coverage")

    # === 9. fatal input-contract errors deliver nothing
    bad_installs = installs + [inst("pB", B, 30, 35, valid=300)]  # overlaps
    out9, code9, sum9 = fx.coldchain(settle_paths, items, bad_installs, samples,
                                     run="t9", extra={})
    check(code9 == 2 and sum9["error"]["code"] == "installation_overlap"
          and not os.path.exists(os.path.join(out9, "coverage.jsonl")),
          "overlapping installations fail the whole run with no ledger")
    bad_window = os.path.join(fx.dir, "cold-badwin")
    s9, c9 = fx.wh.coldchain(
        "1", "2025-12-31T00:00:00+00:00", at(80), *settle_paths,
        fx.write_jsonl("items-bw.jsonl", items),
        fx.write_jsonl("inst-bw.jsonl", installs),
        fx.write_jsonl("samp-bw.jsonl", samples), bad_window)
    check(c9 == 2 and s9["error"]["code"] == "window_before_opening",
          "a window starting before the opening is rejected")
    bad_items = [item(E), item(E)]
    out10, code10, sum10 = fx.coldchain(settle_paths, bad_items, installs,
                                        samples, run="t10", extra={})
    check(code10 == 2 and sum10["error"]["code"] == "duplicate_item",
          "duplicate inspected EPC lines are rejected")

    # === 10. daily reconciliation across all built runs
    for name, o in (("t1", out), ("t2", out2), ("t5", out5)):
        daily = fx.read(o, "daily")
        totals = {}
        for r in daily:
            totals[r["epc"]] = totals.get(r["epc"], 0) + r["duration_us"]
        window = 60 * 60 * 1_000_000 if name != "t2" else 40 * 60 * 1_000_000
        check(all(v == window for v in totals.values()),
              f"{name}: daily normal+over+unknown durations equal the window")

    fx.close()
    fx2.close()
    fx3.close()
    print()
    if failures:
        print(f"{len(failures)} FAILURES")
        return 1
    print("all cold-chain scenario tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
