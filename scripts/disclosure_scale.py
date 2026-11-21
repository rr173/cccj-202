#!/usr/bin/env python3
"""Scale reproduction for offline selective disclosure.

Generates at runtime (default): a warehouse whose events table holds
1,000,000 variants -- one event id (``...:big``) with 200,000 variants,
plus 800,000 dense single-variant ids with an internal empty segment
(no ids in ``d:0300000..d:0399999``).  Data generation writes the
warehouse's events table directly (documented: generating data and
building the raw warehouse are NOT the measured phases -- the measured
phases are checkpoint build, single page export, page receive and
audit finalisation, each run as a separate process and capped with
wait4(2) at --limit-mb (default 192 MiB, Linux)).

Proof obligations checked here:

  * every measured phase stays within the RSS budget;
  * exporting a narrow page (3 hits out of 1M) reads O(hits + log N)
    index rows -- the command's own ``index_reads`` statistics are
    asserted, so a full-inventory scan would fail the check;
  * a page credential carries O(log N) hashes and at most two adjacent
    keys -- never the whole inventory or unrelated variants' digests
    (asserted on proof length and page size);
  * a 200k-variant id is summarised across pages without the page or
    the summary residing in memory (a 50k-row page is exported and
    received inside the same budget);
  * re-pagination and receive order never change the final inventory
    (byte-identical outputs across two audit runs).

Everything is written under a temporary work directory (deleted unless
--keep); nothing large lands in the repository.

Usage:
  python3 scripts/disclosure_scale.py                  # full 1M-variant run
  python3 scripts/disclosure_scale.py --variants 40000 --big 8000   # smoke
  python3 scripts/disclosure_scale.py --keep --work /tmp/disc-scale
"""
import argparse
import hashlib
import json
import os
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

WID = "urn:example:warehouse:scale"
BIG_ID = "urn:example:scale:big"
DENSE = "urn:example:scale:d:%07d"
T0 = 1767225600_000_000  # 2026-01-01T00:00:00Z in microseconds


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def generate(wh_dir, n_big, n_dense_ids, gap):
    """Create the warehouse and fill its events table directly."""
    from epcis_warehouse.warehouse import Warehouse
    os.makedirs(wh_dir, exist_ok=False)
    units_path = os.path.join(wh_dir + "-units.json")
    with open(units_path, "w") as fh:
        json.dump({"units": {"EA": {"dimension": "count", "factor": "1"}}}, fh)
    wh = Warehouse.create(wh_dir, units_path)
    conn = wh.store.conn
    conn.execute(
        "INSERT INTO batches(attempt_id,batch_seq,name,manifest_sha256,status,"
        "reject_reason,created_utc) VALUES(1,1,'scale','','published',NULL,"
        "'2026-01-01T00:00:00+00:00')")

    def flush(batch):
        conn.executemany(
            "INSERT INTO events(event_id,content_digest,status,first_batch,"
            "event_us) VALUES(?,?,?,?,?)", batch)

    batch = []
    for i in range(n_big):
        batch.append((BIG_ID, sha(f"big:{i}"), "ok", 1, T0 + i))
        if len(batch) >= 10000:
            flush(batch)
            batch.clear()
    dense_present = 0
    for i in range(n_dense_ids):
        if gap[0] <= i < gap[1]:
            continue  # the internal empty segment
        dense_present += 1
        status = "invalid" if i % 1000 == 999 else "ok"
        event_us = None if status == "invalid" else T0 + i
        batch.append((DENSE % i, sha(f"d:{i}"), status, 1, event_us))
        if len(batch) >= 10000:
            flush(batch)
            batch.clear()
    if batch:
        flush(batch)
    conn.commit()
    wh.close()
    os.unlink(units_path)
    return n_big + dense_present


def cli_out(args, env, expect=0):
    """CLI in a child process with stdout captured via a temp file."""
    fd, tmp = tempfile.mkstemp(prefix="disc-scale-out-")
    pid = os.fork()
    if pid == 0:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 2)
            os.dup2(fd, 1)
            os.execve(sys.executable,
                      [sys.executable, "-m", "epcis_warehouse.cli", *args], env)
        finally:
            os._exit(127)
    os.close(fd)
    _, status, ru = os.wait4(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    with open(tmp, "r", encoding="utf-8") as fh:
        out = fh.read()
    os.unlink(tmp)
    if code != expect:
        raise RuntimeError(f"CLI {args} -> {code}\n{out[:2000]}")
    return (json.loads(out) if out.strip() else None), ru.ru_maxrss / 1024.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", type=int, default=1000000,
                    help="total variants (default 1,000,000)")
    ap.add_argument("--big", type=int, default=200000,
                    help="variants of the single big event id (default 200,000)")
    ap.add_argument("--limit-mb", type=int, default=192,
                    help="peak RSS budget per measured phase (MiB)")
    ap.add_argument("--work", default=None, help="work directory (default: tempdir)")
    ap.add_argument("--keep", action="store_true", help="keep the work directory")
    args = ap.parse_args()

    n_big = args.big
    n_dense = args.variants - args.big          # dense records present
    assert n_dense >= 1000, "dense id space too small"
    gap_size = max(1, n_dense // 10)
    n_dense_ids = n_dense + gap_size            # id space (gap included)
    gap = (n_dense // 2, n_dense // 2 + gap_size)
    work = args.work or tempfile.mkdtemp(prefix="epcis-disc-scale-")
    wh_dir = os.path.join(work, "wh")
    pages = os.path.join(work, "pages")
    os.makedirs(pages, exist_ok=True)
    env = dict(os.environ, PYTHONPATH=REPO)
    report = {"variants": args.variants, "big_id_variants": n_big,
              "limit_mb": args.limit_mb, "phases": {}, "checks": {}}

    def phase(name, fn):
        t0 = time.time()
        out = fn()
        report["phases"][name] = {"wall_s": round(time.time() - t0, 2)}
        return out

    total = phase("generate", lambda: generate(wh_dir, n_big, n_dense_ids, gap))
    report["generated_variants"] = total

    def measure(name, cli_args, expect=0):
        t0 = time.time()
        out, peak = cli_out(cli_args, env, expect)
        report["phases"][name] = {"wall_s": round(time.time() - t0, 2),
                                  "peak_rss_mb": round(peak, 1),
                                  "within_limit": peak <= args.limit_mb}
        return out

    # ---- checkpoint build (measured)
    cp = measure("checkpoint_build",
                 ["--root", wh_dir, "checkpoint", "build",
                  "--warehouse-id", WID, "--batch", "1",
                  "--out", os.path.join(work, "checkpoint.json")])
    cpid = cp["checkpoint"]
    report["checks"]["checkpoint_variants"] = cp["variants"] == total

    # ---- sender-side pagination planning (reads the index, as the
    # exporter's tooling legitimately does, to choose split digests)
    conn = sqlite3.connect(os.path.join(
        wh_dir, "disclosure", cpid, "index.sqlite"))
    splits = []
    n_chunks = max(1, min(4, n_big // 1000))
    chunk = n_big // n_chunks
    for k in range(1, n_chunks):
        row = conn.execute(
            "SELECT digest FROM leaves WHERE event_id=? ORDER BY digest"
            " LIMIT 1 OFFSET ?", (BIG_ID.encode("utf-8"), k * chunk)).fetchone()
        splits.append(row[0].hex())
    conn.close()

    def dense_id(i):
        return DENSE % i

    # ---- page exports (each measured)
    def export(name, *bounds):
        return measure(f"export:{name}",
                       ["--root", wh_dir, "page", "export", "--checkpoint", cpid,
                        *bounds, "--out", os.path.join(pages, name)])

    # narrow page: 3 hits out of the whole inventory
    narrow = export("narrow.jsonl", "--lo-id", dense_id(100),
                    "--hi-id", dense_id(103))
    # empty page over the internal empty segment
    empty = export("empty.jsonl", "--lo-id", dense_id(gap[0]),
                   "--hi-id", dense_id(gap[1]))
    # big id in n_chunks pages at digest boundaries
    big_bounds = [("--lo-id", BIG_ID)]
    for s in splits:
        big_bounds.append(("--lo-id", BIG_ID, "--lo-digest", s))
    big_pages = []
    for i in range(n_chunks):
        lo = big_bounds[i]
        hi = ("--hi-id", BIG_ID, "--hi-digest", splits[i]) if i < n_chunks - 1 \
            else ("--hi-id", dense_id(0))
        big_pages.append(export(f"big-{i}.jsonl", *lo, *hi))
    # dense segment pages of 100k ids
    dense_pages = []
    step = 100000
    for lo_i in range(0, gap[0], step):
        hi_i = min(lo_i + step, gap[0])
        dense_pages.append(export(f"dense-{lo_i}.jsonl",
                                  "--lo-id", dense_id(lo_i),
                                  "--hi-id", dense_id(hi_i)))

    # ---- index access statistics: the proof obligations
    stats = narrow["index_reads"]
    report["narrow_page"] = {"rows": narrow["rows"], "index_reads": stats,
                             "proof_hashes": narrow["proof_hashes"],
                             "page_bytes": narrow["page_bytes"]}
    report["checks"]["narrow_no_full_scan"] = (
        narrow["rows"] == 3 and stats["leaves_read"] <= 16
        and stats["nodes_read"] <= 128)
    report["checks"]["narrow_credential_small"] = (
        narrow["proof_hashes"] <= 64 and narrow["page_bytes"] <= 65536)
    estats = empty["index_reads"]
    report["empty_page"] = {"rows": empty["rows"], "index_reads": estats}
    report["checks"]["empty_no_full_scan"] = (
        empty["rows"] == 0 and estats["leaves_read"] <= 8)

    # ---- the audit: request [big, d:0400000)
    req = ["--lo-id", BIG_ID, "--hi-id", dense_id(gap[1])]

    def run_audit(tag, page_plan):
        ws = os.path.join(work, f"ws-{tag}")
        measure(f"audit_init:{tag}",
                ["audit", "init", "--workspace", ws, "--checkpoint",
                 os.path.join(work, "checkpoint.json"), *req])
        for name in page_plan:
            measure(f"receive:{tag}:{name}",
                    ["audit", "receive", "--workspace", ws,
                     "--page", os.path.join(pages, name)])
        out_dir = os.path.join(work, f"final-{tag}")
        summary = measure(f"finalize:{tag}",
                          ["audit", "finalize", "--workspace", ws, "--out", out_dir])
        return summary, out_dir

    plan1 = ([f"big-{i}.jsonl" for i in reversed(range(n_chunks))]
             + [os.path.basename(p["out"]) for p in dense_pages]
             + ["empty.jsonl"])
    summary1, out1 = run_audit("run1", plan1)

    # second run: different pagination (big id in 2 pages, dense in
    # 150k-id pages) and a different receive order
    mid_splits = [splits[len(splits) // 2]] if splits else []
    big2_pages = []
    for i in range(len(mid_splits) + 1):
        lo = ("--lo-id", BIG_ID) if i == 0 \
            else ("--lo-id", BIG_ID, "--lo-digest", mid_splits[i - 1])
        hi = ("--hi-id", BIG_ID, "--hi-digest", mid_splits[i]) \
            if i < len(mid_splits) else ("--hi-id", dense_id(0))
        big2_pages.append(export(f"big2-{i}.jsonl", *lo, *hi))
    dense2_pages = []
    step2 = 150000
    for lo_i in range(0, gap[0], step2):
        hi_i = min(lo_i + step2, gap[0])
        dense2_pages.append(export(f"dense2-{lo_i}.jsonl",
                                   "--lo-id", dense_id(lo_i),
                                   "--hi-id", dense_id(hi_i)))
    plan2 = ([os.path.basename(p["out"]) for p in reversed(dense2_pages)]
             + ["empty.jsonl"]
             + [f"big2-{i}.jsonl" for i in range(len(mid_splits) + 1)])
    summary2, out2 = run_audit("run2", plan2)

    # ---- checks on the delivered inventory
    expected_variants = n_big + gap[0]
    expected_ids = 1 + gap[0]
    report["checks"]["summary_counts"] = (
        summary1["variants"] == expected_variants
        and summary1["event_ids"] == expected_ids
        and summary1["multi_variant_ids"] == 1
        and summary1["status"] == "complete")
    n_lines = 0
    big_summary = None
    with open(os.path.join(out1, "inventory.jsonl"), encoding="utf-8") as fh:
        for _ in fh:
            n_lines += 1
    with open(os.path.join(out1, "by_event.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row["event_id"] == BIG_ID:
                big_summary = row
    report["checks"]["inventory_lines"] = n_lines == expected_variants
    report["checks"]["big_id_cross_page_summary"] = (
        big_summary == {"event_id": BIG_ID, "multi_variant": True,
                        "variants": n_big})
    identical = True
    for fname in ("inventory.jsonl", "by_event.jsonl", "summary.json"):
        with open(os.path.join(out1, fname), "rb") as fa, \
                open(os.path.join(out2, fname), "rb") as fb:
            identical = identical and fa.read() == fb.read()
    report["checks"]["repagination_determinism"] = identical
    report["checks"]["run2_counts"] = summary2["variants"] == expected_variants

    rss_ok = all(p.get("within_limit", True)
                 for p in report["phases"].values() if "peak_rss_mb" in p)
    report["checks"]["rss_within_limit"] = rss_ok
    report["ok"] = all(report["checks"].values())
    print(json.dumps(report, indent=2, sort_keys=True))
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
