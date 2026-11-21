#!/usr/bin/env python3
"""Generate the selective-disclosure drill.

Story (business times 2026-01-01, UTC; locations A/B, class K):

  * batch 1 receives: ``m-07`` twice (a full retransmission -- one
    variant, two provenances), one *valid* ``m-08`` variant, one
    *invalid* ``m-08`` variant (unknown uom, different digest), the
    boundary event ``m-20``, plus one event under an unknown context
    (uninterpretable, no event id -- never enters the inventory).
  * batch 2 receives one more ``m-08`` variant.

The audit requests batch-1's ``[m-07, m-20)``: the inventory must list
``m-07`` once and ``m-08`` twice, and must not contain ``m-20``.  The
two ``m-08`` variants are split into different pages at a digest
boundary; ``[m-10, m-20)`` is left as a zero-hit page.  Pages are
received out of order and one is re-received; the empty interval must
still be reported missing until its page arrives.

Tamper cases (all must be rejected without polluting progress):
t1 a non-empty page with one real row dropped (other rows genuine),
t2 the page's hi bound extended past an undisclosed real variant,
t3 a page exported from the batch-2 checkpoint, t4 a truncated page,
t5 one corrupted digest nibble, t6 a valid page overlapping received
intervals, t7 trailing garbage after the declared rows.

A second mini warehouse holds only records without an event id, so its
checkpoint commits to zero variants: the audit delivers an empty
inventory over open bounds, backed by a valid empty page.

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
LA, LB = "urn:example:loc:A", "urn:example:loc:B"
CK = "urn:example:class:K"
WAREHOUSE_ID = "urn:example:warehouse:drill-1"

UNITS = {"units": {
    "EA": {"dimension": "count", "factor": "1"},
    "KGM": {"dimension": "mass", "factor": "1"},
}}


def obs(eid, qty, minute, uom="EA"):
    return {"@context": CTX, "type": "ObjectEvent", "eventID": eid,
            "eventTime": f"2026-01-01T10:{minute:02d}:00+00:00",
            "recordTime": f"2026-01-01T10:{minute:02d}:00+00:00",
            "action": "OBSERVE",
            "quantityList": [{"epcClass": CK, "quantity": qty, "uom": uom}],
            "sourceList": [{"source": LA}],
            "destinationList": [{"destination": LB}]}


# batch 1 (m-07 appears in two files: a full retransmission)
BATCH1 = [
    ("00-m07.json", obs("m-07", 1, 0)),
    ("01-m07-again.json", obs("m-07", 1, 0)),
    ("02-m08-ok.json", obs("m-08", 2, 1)),
    ("03-m08-invalid.json", obs("m-08", 3, 2, uom="XYZ")),   # unknown uom
    ("04-m20.json", obs("m-20", 5, 3)),
    ("05-noid.json", {"@context": "urn:example:ctx:unknown",
                      "type": "ObjectEvent"}),               # uninterpretable
]
BATCH2 = [("00-m08-late.json", obs("m-08", 4, 4))]

# mini warehouse: only records without an event id
EMPTY_BATCH = [
    ("00-uninterpretable.json", {"@context": "urn:example:ctx:unknown",
                                 "type": "ObjectEvent"}),
    ("01-invalid-noid.json", {"@context": CTX, "type": "ObjectEvent",
                              "eventTime": "2026-01-01T10:00:00+00:00",
                              "recordTime": "2026-01-01T10:00:00+00:00",
                              "action": "OBSERVE",
                              "quantityList": [{"epcClass": CK, "quantity": 1,
                                                "uom": "EA"}],
                              "sourceList": [{"source": LA}],
                              "destinationList": [{"destination": LB}]}),
]


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False)
        fh.write("\n")


def write_batch(data, name, files):
    bdir = os.path.join(data, name)
    os.makedirs(bdir, exist_ok=True)
    entries = []
    for fn, ev in files:
        write_json(os.path.join(bdir, fn), ev)
        entries.append({"name": fn, "sha256": sha(os.path.join(bdir, fn))})
    man = {"batch": name, "files": entries, "lineage_links": []}
    write_json(os.path.join(bdir, "manifest.json"), man)
    return os.path.join(bdir, "manifest.json")


def main():
    work = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "work")
    data = os.path.join(work, "data")
    os.makedirs(data, exist_ok=True)
    write_json(os.path.join(data, "units.json"), UNITS)
    m1 = write_batch(data, "batch1", BATCH1)
    m2 = write_batch(data, "batch2", BATCH2)
    m3 = write_batch(data, "batch3", EMPTY_BATCH)

    # generated artifacts are rebuilt from scratch on every run
    for d in ("wh", "wh2", "checkpoints", "pages", "audit", "audit2"):
        shutil.rmtree(os.path.join(work, d), ignore_errors=True)
    wh = os.path.join(work, "wh")
    wh2 = os.path.join(work, "wh2")
    env = dict(os.environ, PYTHONPATH=REPO)
    transcript = {"steps": []}

    def run(*argv, expect=0):
        proc = subprocess.run(
            [sys.executable, "-m", "epcis_warehouse.cli", *argv],
            env=env, capture_output=True, text=True)
        if proc.returncode != expect:
            raise RuntimeError(f"CLI {argv} -> {proc.returncode}\n{proc.stderr}")
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def note(step, outcome):
        transcript["steps"].append({"step": step, "outcome": outcome})
        print(f"  {step}: {json.dumps(outcome, ensure_ascii=False, sort_keys=True)[:110]}")

    run("--root", wh, "init", "--units", os.path.join(data, "units.json"))
    note("ingest-batch1", run("--root", wh, "ingest", m1)["status"])
    # oracle: batch-1 view of m-08 before batch 2 arrives (warehouse CLI,
    # a pre-existing capability -- not the disclosure exporter)
    m08 = run("--root", wh, "event", "--id", "m-08")
    d_lo, d_hi = sorted(v["digest"] for v in m08["versions"])
    note("ingest-batch2", run("--root", wh, "ingest", m2)["status"])
    run("--root", wh2, "init", "--units", os.path.join(data, "units.json"))
    note("ingest-empty", run("--root", wh2, "ingest", m3)["status"])

    cps = os.path.join(work, "checkpoints")
    pages = os.path.join(work, "pages")
    os.makedirs(cps, exist_ok=True)
    os.makedirs(pages, exist_ok=True)
    cp1 = run("--root", wh, "checkpoint", "build", "--warehouse-id", WAREHOUSE_ID,
              "--batch", "1", "--out", os.path.join(cps, "cp1.json"))
    note("checkpoint-batch1", {"checkpoint": cp1["checkpoint"],
                               "variants": cp1["variants"]})
    cp2 = run("--root", wh, "checkpoint", "build", "--warehouse-id", WAREHOUSE_ID,
              "--batch", "2", "--out", os.path.join(cps, "cp2.json"))
    note("checkpoint-batch2", {"checkpoint": cp2["checkpoint"],
                               "variants": cp2["variants"]})
    # rebuilding the batch-1 checkpoint after batch 2 must be identical
    cp1_again = run("--root", wh, "checkpoint", "build", "--warehouse-id",
                    WAREHOUSE_ID, "--batch", "1")
    note("checkpoint-batch1-rebuilt", cp1_again == cp1)
    cp0 = run("--root", wh2, "checkpoint", "build", "--warehouse-id",
              "urn:example:warehouse:drill-empty", "--batch", "1",
              "--out", os.path.join(cps, "cp0.json"))
    note("checkpoint-empty", {"checkpoint": cp0["checkpoint"],
                              "variants": cp0["variants"]})

    def export(name, cp, *bounds):
        out = run("--root", wh if cp != cp0["checkpoint"] else wh2,
                  "page", "export", "--checkpoint", cp, *bounds,
                  "--out", os.path.join(pages, name))
        note(f"export-{name}", {"rows": out["rows"],
                               "index_reads": out["index_reads"]})
        return out

    export("p1.jsonl", cp1["checkpoint"],
           "--lo-id", "m-07", "--hi-id", "m-08", "--hi-digest", d_hi)
    export("p2.jsonl", cp1["checkpoint"],
           "--lo-id", "m-08", "--lo-digest", d_hi, "--hi-id", "m-10")
    export("p3.jsonl", cp1["checkpoint"], "--lo-id", "m-10", "--hi-id", "m-20")
    export("pbig.jsonl", cp1["checkpoint"], "--lo-id", "m-07", "--hi-id", "m-10")
    export("p2b.jsonl", cp2["checkpoint"],
           "--lo-id", "m-08", "--lo-digest", d_hi, "--hi-id", "m-10")
    export("p0.jsonl", cp0["checkpoint"])   # open bounds over the empty inventory

    # ---- tampered pages -------------------------------------------------
    p1_lines = open(os.path.join(pages, "p1.jsonl"), encoding="utf-8") \
        .read().splitlines()
    hdr = json.loads(p1_lines[0])

    t1 = dict(hdr)
    t1["row_count"] = hdr["row_count"] - 1          # drop the last real row
    with open(os.path.join(pages, "t1.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(t1) + "\n" + "\n".join(p1_lines[1:-1]) + "\n")

    t2 = dict(hdr)
    t2["hi"] = {"event_id": "m-10"}                 # claim past m-08's 2nd variant
    with open(os.path.join(pages, "t2.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(t2) + "\n" + "\n".join(p1_lines[1:]) + "\n")

    raw = open(os.path.join(pages, "p1.jsonl"), "rb").read()
    with open(os.path.join(pages, "t4.jsonl"), "wb") as fh:
        fh.write(raw[: len(raw) * 3 // 4])          # truncated tail

    bad_row = json.loads(p1_lines[1])
    bad_row["digest"] = ("0" if bad_row["digest"][0] != "0" else "1") \
        + bad_row["digest"][1:]
    with open(os.path.join(pages, "t5.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(p1_lines[0] + "\n" + json.dumps(bad_row) + "\n"
                 + "\n".join(p1_lines[2:]) + "\n")

    with open(os.path.join(pages, "t7.jsonl"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(p1_lines) + "\n" + json.dumps({"extra": 1}) + "\n")

    # ---- the audit -------------------------------------------------------
    audit = os.path.join(work, "audit")
    ws = os.path.join(audit, "ws")
    os.makedirs(audit, exist_ok=True)
    # the checkpoint travels over its own trusted channel (here: the
    # checkpoints/ directory); pages arrive over the untrusted one
    note("init", run("audit", "init", "--workspace", ws, "--checkpoint",
                     os.path.join(cps, "cp1.json"),
                     "--lo-id", "m-07", "--hi-id", "m-20")["status"])

    def receive(name, expect=0):
        r = run("audit", "receive", "--workspace", ws,
                "--page", os.path.join(pages, name), expect=expect)
        return {"status": r["status"]} if expect == 0 else r["reason"]

    note("receive-p2-out-of-order", receive("p2.jsonl"))
    note("receive-p1", receive("p1.jsonl"))
    note("receive-p1-again", receive("p1.jsonl"))
    g = run("audit", "gaps", "--workspace", ws)
    note("gaps-before-empty-page", g["missing"])
    # the workspace is closed and reopened between every command; the
    # progress above survives on disk

    for name in ("t1.jsonl", "t2.jsonl", "t4.jsonl", "t5.jsonl", "t7.jsonl",
                 "p2b.jsonl", "pbig.jsonl"):
        note(f"reject-{name}", receive(name, expect=2))
    g = run("audit", "gaps", "--workspace", ws)
    note("gaps-after-rejections", {"missing": g["missing"],
                                   "rows_received": g["rows_received"]})

    note("receive-p3-empty", receive("p3.jsonl"))
    g = run("audit", "gaps", "--workspace", ws)
    note("gaps-complete", g["complete"])
    out = os.path.join(audit, "final")
    note("finalize", run("audit", "finalize", "--workspace", ws, "--out", out))

    # ---- empty-checkpoint audit ------------------------------------------
    audit2 = os.path.join(work, "audit2")
    ws2 = os.path.join(audit2, "ws")
    os.makedirs(audit2, exist_ok=True)
    note("init-empty", run("audit", "init", "--workspace", ws2, "--checkpoint",
                           os.path.join(cps, "cp0.json"))["status"])
    note("receive-p0-empty", run("audit", "receive", "--workspace", ws2,
                                 "--page", os.path.join(pages, "p0.jsonl"))["status"])
    out2 = os.path.join(audit2, "final")
    note("finalize-empty", run("audit", "finalize", "--workspace", ws2,
                               "--out", out2))

    write_json(os.path.join(work, "transcript.json"), transcript)
    print("drill workspace:", work)


if __name__ == "__main__":
    main()
