#!/usr/bin/env python3
"""Independent selective-disclosure drill verifier.

Reads the hand-written expectations in expected.json and checks them
against the drill's output FILES and fresh CLI runs.  It never imports
any warehouse module.  It contains its own from-spec implementation of
the page credential (docs/disclosure.md): leaf/node hashing, range
proof verification and the completeness checks -- so the exported
pages are verified end-to-end by code that shares nothing with the
exporter.  Digests come from the warehouse `event` CLI (a pre-existing
capability), never from the disclosure exporter under test.

Usage: verify_drill.py [workdir] [expected.json]
"""
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS {msg}")
    else:
        failures.append(msg)
        print(f"  FAIL {msg}")


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------
# from-spec page verification (docs/disclosure.md), independent of
# epcis_warehouse: tags, hashes, tree, range proof, completeness.
# ------------------------------------------------------------------
STATUS_CODE = {"ok": 1, "unsupported": 2, "invalid": 3}


def H(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def row_data_hash(status, first_batch, event_us):
    enc = b"\x00" if event_us is None \
        else b"\x01" + (event_us).to_bytes(8, "big", signed=True)
    return H(b"\x52", bytes([STATUS_CODE[status]]),
             first_batch.to_bytes(4, "big"), enc)


def leaf_hash(eid_b, dig_b, rdh):
    return H(b"\x4c", len(eid_b).to_bytes(4, "big"), eid_b, dig_b, rdh)


def node_hash(l, r):
    return H(b"\x4e", l, r)


def pad_hashes(levels):
    out = [H(b"\x50")]
    for _ in range(levels):
        out.append(node_hash(out[-1], out[-1]))
    return out


def tree_levels(n):
    return 0 if n <= 1 else (n - 1).bit_length()


def merge_leaves(it, size):
    stack = []
    for _ in range(size):
        h = next(it)
        ht = 0
        while stack and stack[-1][0] == ht:
            top_ht, top_h = stack.pop()
            h = node_hash(top_h, h)
            ht = top_ht + 1
        stack.append((ht, h))
    assert len(stack) == 1
    return stack[0][1]


def verify_range_root(levels, a, b, leaf_iter, proof_iter):
    def rec(lo, h):
        size = 1 << h
        if b <= lo or lo + size <= a:
            return next(proof_iter)
        if a <= lo and lo + size <= b:
            return merge_leaves(leaf_iter, size)
        half = size >> 1
        return node_hash(rec(lo, h - 1), rec(lo + half, h - 1))
    return rec(0, levels)


def bound_key(bound, is_lo):
    """Sortable key for a JSON boundary (None = open end)."""
    if bound is None:
        return (0, b"", b"") if is_lo else (2, b"", b"")
    dig = bound.get("digest")
    return (1, bound["event_id"].encode("utf-8"),
            b"" if dig is None else b"\xff" + bytes.fromhex(dig))


def key_of(eid, digest_hex):
    return (1, eid.encode("utf-8"), b"\xff" + bytes.fromhex(digest_hex))


def spec_verify_page(path, checkpoint):
    """Verify one page file against the checkpoint body; return its rows.

    Raises AssertionError on any contract/verification failure.
    """
    n = checkpoint["variants"]
    root = bytes.fromhex(checkpoint["root"])
    levels = tree_levels(n)
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert lines, "empty page"
    hdr = json.loads(lines[0])
    assert hdr["format"] == "epcis-disclosure-page/1"
    assert hdr["checkpoint"] == checkpoint["checkpoint"]
    lo, hi = hdr["lo"], hdr["hi"]
    assert bound_key(lo, True) < bound_key(hi, False), "zero-width page"
    start, row_count = hdr["start"], hdr["row_count"]
    left, right = hdr["left_edge"], hdr["right_edge"]
    rows = [json.loads(l) for l in lines[1:]]
    assert len(rows) == row_count, "row count mismatch / truncation"

    def edge_leaf(e):
        return leaf_hash(e["event_id"].encode("utf-8"),
                         bytes.fromhex(e["digest"]),
                         bytes.fromhex(e["row_hash"]))

    leaves = []
    if left:
        assert key_of(left["event_id"], left["digest"]) < bound_key(lo, True), \
            "left edge not before page range"
        leaves.append(edge_leaf(left))
    prev = None
    for r in rows:
        k = key_of(r["event_id"], r["digest"])
        assert prev is None or prev < k, "rows not increasing"
        assert bound_key(lo, True) <= k < bound_key(hi, False), \
            "row outside page range"
        prev = k
        leaves.append(leaf_hash(r["event_id"].encode("utf-8"),
                                bytes.fromhex(r["digest"]),
                                row_data_hash(r["status"], r["first_batch"],
                                              r["event_us"])))
    if right:
        assert key_of(right["event_id"], right["digest"]) >= bound_key(hi, False), \
            "right edge inside page range"
        leaves.append(edge_leaf(right))

    a = start - (1 if left else 0)
    b = start + row_count + (1 if right else 0)
    if not left:
        assert start == 0
    if not right:
        assert start + row_count == n
    assert 0 <= a <= b <= n
    got = verify_range_root(levels, a, b, iter(leaves),
                            iter(bytes.fromhex(h) for h in hdr["proof"]))
    assert got == root, "range proof does not reach the checkpoint root"
    return rows


def checkpoint_id_of(body):
    return sha256_hex(canon({k: body[k] for k in
                             ("batch", "format", "root", "variants", "warehouse")
                             }).encode("utf-8"))


def us_of(iso):
    dt = datetime.datetime.fromisoformat(iso)
    return int(dt.timestamp() * 1_000_000)


def main():
    work = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "work")
    expected_path = sys.argv[2] if len(sys.argv) > 2 \
        else os.path.join(HERE, "expected.json")
    with open(expected_path, encoding="utf-8") as fh:
        exp = json.load(fh)
    env = dict(os.environ, PYTHONPATH=REPO)
    wh = os.path.join(work, "wh")

    def cli(*argv, expect=0):
        proc = subprocess.run(
            [sys.executable, "-m", "epcis_warehouse.cli", *argv],
            env=env, capture_output=True, text=True)
        if proc.returncode != expect:
            raise RuntimeError(f"CLI {argv} -> {proc.returncode}\n{proc.stderr}")
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def read_jsonl(path):
        with open(path, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]

    # ---------------- oracle: the warehouse's pre-existing `event` CLI
    oracle = {}
    for eid in ("m-07", "m-08", "m-20"):
        out = cli("--root", wh, "event", "--id", eid)
        assert out["status"] == "ok"
        for v in out["versions"]:
            oracle[(eid, v["digest"])] = v

    # ---------------- checkpoints: ids recomputed from committed content
    cps = {}
    for name, fname in (("batch1", "cp1.json"), ("batch2", "cp2.json"),
                        ("empty", "cp0.json")):
        with open(os.path.join(work, "checkpoints", fname), encoding="utf-8") as fh:
            doc = json.load(fh)
        want = exp["checkpoints"][name]
        check(doc["batch"] == want["batch"] and doc["variants"] == want["variants"]
              and doc["format"] == "epcis-disclosure/1",
              f"{name} checkpoint batch/variants as hand-written")
        check(doc["checkpoint"] == checkpoint_id_of(doc),
              f"{name} checkpoint id commits to warehouse/batch/format/count/root")
        cps[name] = doc
    check(cps["batch1"]["warehouse"] == exp["warehouse_id"]
          and cps["batch2"]["warehouse"] == exp["warehouse_id"]
          and cps["empty"]["warehouse"] == exp["empty_warehouse_id"],
          "checkpoint warehouse ids as hand-written")
    check(cps["batch1"]["checkpoint"] != cps["batch2"]["checkpoint"],
          "batch-1 and batch-2 checkpoints differ")

    # ---------------- independent from-spec verification of the pages
    pages = os.path.join(work, "pages")
    page_rows = {}
    for name in ("p1", "p2", "p3"):
        rows = spec_verify_page(os.path.join(pages, f"{name}.jsonl"),
                                cps["batch1"])
        page_rows[name] = rows
        check(len(rows) == exp["pages"][name]["rows"],
              f"{name} row count == {exp['pages'][name]['rows']}")
    check(spec_verify_page(os.path.join(pages, "p0.jsonl"), cps["empty"]) == [],
          "p0 verifies against the empty checkpoint with zero rows")

    # the split: p1/p2 divide m-08's two variants at a digest boundary
    m08_rows = sorted((r for r in page_rows["p1"] + page_rows["p2"]
                       if r["event_id"] == "m-08"),
                      key=lambda r: bytes.fromhex(r["digest"]))
    check(len(m08_rows) == 2
          and page_rows["p1"][-1]["digest"] == m08_rows[0]["digest"]
          and page_rows["p2"][0]["digest"] == m08_rows[1]["digest"],
          "m-08's two variants split across p1/p2 at the digest boundary")

    # ---------------- transcript: the recorded flow matches expectations
    with open(os.path.join(work, "transcript.json"), encoding="utf-8") as fh:
        steps = {s["step"]: s["outcome"] for s in json.load(fh)["steps"]}
    check(steps["receive-p2-out-of-order"]["status"] == "accepted",
          "p2 accepted out of order")
    check(steps["receive-p1"]["status"] == "accepted", "p1 accepted")
    check(steps["receive-p1-again"]["status"] == "already_received",
          "re-received p1 is a no-op")
    check(steps["gaps-before-empty-page"] == exp["gap_after_two_pages"],
          "after two non-empty pages the empty interval is still missing")
    check(steps["gaps-after-rejections"]["rows_received"] == 3
          and steps["gaps-after-rejections"]["missing"] == exp["gap_after_two_pages"],
          "rejections did not pollute progress")
    check(steps["gaps-complete"] is True, "coverage complete after p3")
    check(steps["checkpoint-batch1-rebuilt"] is True,
          "rebuilding the batch-1 checkpoint after batch 2 is identical")

    # ---------------- final inventory vs hand-written rows + oracle
    final = os.path.join(work, "audit", "final")
    inv = read_jsonl(os.path.join(final, "inventory.jsonl"))
    check([r["event_id"] for r in inv] == [r["event_id"] for r in exp["inventory"]],
          "inventory lists exactly the hand-written event ids in order")
    ok_rows = True
    for got, want in zip(inv, exp["inventory"]):
        want_us = us_of(want["event_time"]) if want["event_time"] else None
        if not (got["status"] == want["status"]
                and got["first_batch"] == want["first_batch"]
                and got["event_us"] == want_us):
            ok_rows = False
        oracle_row = oracle.get((got["event_id"], got["digest"]))
        if oracle_row is None or oracle_row["status"] != got["status"] \
                or oracle_row["first_batch"] != got["first_batch"]:
            ok_rows = False
    check(ok_rows, "inventory rows match hand-written status/batch/time "
                   "and the warehouse oracle digests")
    for eid in exp["absent_from_inventory"]:
        check(all(r["event_id"] != eid for r in inv),
              f"{eid} absent from the inventory")
    check(sorted(r["digest"] for r in inv if r["event_id"] == "m-08")
          == sorted(d for (e, d) in oracle if e == "m-08"
                    and oracle[(e, d)]["first_batch"] == 1),
          "m-08 inventory digests == batch-1 oracle digests (batch-2 excluded)")

    bye = read_jsonl(os.path.join(final, "by_event.jsonl"))
    check(bye == exp["by_event"], "by_event summary as hand-written")
    with open(os.path.join(final, "summary.json"), encoding="utf-8") as fh:
        summary = json.load(fh)
    for k, v in exp["summary"].items():
        check(summary.get(k) == v, f"summary.{k} == {v}")
    check(summary["status"] == "complete"
          and summary["request"] == exp["request"]
          and summary["checkpoint"] == cps["batch1"]["checkpoint"],
          "summary binds checkpoint and request")

    # the page rows (independently verified) are exactly the inventory
    check(sorted(canon(r) for r in page_rows["p1"] + page_rows["p2"]
                 + page_rows["p3"])
          == sorted(canon(r) for r in inv),
          "final inventory == union of independently verified page rows")

    # ---------------- negative cases re-run independently, in context
    for stale in os.listdir(work):
        if stale.startswith("disc-verify-"):
            shutil.rmtree(os.path.join(work, stale), ignore_errors=True)
    scratch = tempfile.mkdtemp(prefix="disc-verify-", dir=work)
    ws = os.path.join(scratch, "ws")
    cli("audit", "init", "--workspace", ws,
        "--checkpoint", os.path.join(work, "checkpoints", "cp1.json"),
        "--lo-id", "m-07", "--hi-id", "m-20")
    cli("audit", "receive", "--workspace", ws, "--page",
        os.path.join(pages, "p2.jsonl"))
    cli("audit", "receive", "--workspace", ws, "--page",
        os.path.join(pages, "p1.jsonl"))
    proc = subprocess.run(
        [sys.executable, "-m", "epcis_warehouse.cli", "audit", "finalize",
         "--workspace", ws, "--out", os.path.join(scratch, "never")],
        env=env, capture_output=True, text=True)
    check(proc.returncode == 2
          and not os.path.exists(os.path.join(scratch, "never", "summary.json")),
          "finalize refuses incomplete coverage and writes no result")
    for name in exp["rejected_pages"]:
        proc = subprocess.run(
            [sys.executable, "-m", "epcis_warehouse.cli", "audit", "receive",
             "--workspace", ws, "--page", os.path.join(pages, name)],
            env=env, capture_output=True, text=True)
        check(proc.returncode == 2, f"{name} rejected (exit 2)")
    g = cli("audit", "gaps", "--workspace", ws)
    check(g["rows_received"] == 3 and g["missing"] == exp["gap_after_two_pages"],
          "rejections left received progress untouched")
    # the same workspace continues after the exceptions and completes
    cli("audit", "receive", "--workspace", ws, "--page",
        os.path.join(pages, "p3.jsonl"))
    out3 = os.path.join(scratch, "final3")
    cli("audit", "finalize", "--workspace", ws, "--out", out3)
    for fname in ("inventory.jsonl", "by_event.jsonl", "summary.json"):
        with open(os.path.join(final, fname), "rb") as fa, \
                open(os.path.join(out3, fname), "rb") as fb:
            check(fa.read() == fb.read(),
                  f"workspace continues after exceptions: {fname} identical")

    # ---------------- determinism: different splits + receive order
    ws2 = os.path.join(scratch, "ws2")
    cli("audit", "init", "--workspace", ws2,
        "--checkpoint", os.path.join(work, "checkpoints", "cp1.json"),
        "--lo-id", "m-07", "--hi-id", "m-20")
    d_lo = min(r["digest"] for r in inv if r["event_id"] == "m-08")
    alt = os.path.join(scratch, "alt")
    os.makedirs(alt)
    cli("--root", wh, "page", "export", "--checkpoint",
        cps["batch1"]["checkpoint"], "--lo-id", "m-07", "--hi-id", "m-08",
        "--hi-digest", d_lo, "--out", os.path.join(alt, "a.jsonl"))
    cli("--root", wh, "page", "export", "--checkpoint",
        cps["batch1"]["checkpoint"], "--lo-id", "m-08", "--lo-digest", d_lo,
        "--hi-id", "m-20", "--out", os.path.join(alt, "b.jsonl"))
    cli("audit", "receive", "--workspace", ws2, "--page", os.path.join(alt, "b.jsonl"))
    cli("audit", "receive", "--workspace", ws2, "--page", os.path.join(alt, "a.jsonl"))
    out2 = os.path.join(scratch, "final2")
    cli("audit", "finalize", "--workspace", ws2, "--out", out2)
    for fname in ("inventory.jsonl", "by_event.jsonl", "summary.json"):
        with open(os.path.join(final, fname), "rb") as fa, \
                open(os.path.join(out2, fname), "rb") as fb:
            check(fa.read() == fb.read(),
                  f"re-pagination + receive order: {fname} byte-identical")

    # ---------------- empty-checkpoint audit
    final0 = os.path.join(work, "audit2", "final")
    inv0 = read_jsonl(os.path.join(final0, "inventory.jsonl"))
    with open(os.path.join(final0, "summary.json"), encoding="utf-8") as fh:
        summary0 = json.load(fh)
    check(inv0 == [] and all(summary0[k] == v for k, v in
                             exp["empty_audit"].items())
          and summary0["status"] == "complete",
          "empty checkpoint delivers an empty inventory with coverage proof")
    review = cli("--root", os.path.join(work, "wh2"), "review")
    check(review["event_status_counts"].get("uninterpretable") == 1
          and review["event_status_counts"].get("invalid") == 1,
          "the empty inventory does not mean no records arrived "
          "(unidentified records exist in the warehouse)")

    print()
    if failures:
        print(f"{len(failures)} FAILURES")
        return 1
    print("all disclosure drill checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
