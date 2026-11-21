"""Offline selective disclosure: checkpoints, ordered inventory
commitments and page credentials (sender side).

A *checkpoint* commits one warehouse identifier + one published
receiving batch to an ordered inventory of event variants.  Every
variant is one ``(event_id, content_digest)`` pair visible at that
batch (records without an event id never enter the inventory).  The
inventory is sorted by raw UTF-8 bytes of the event id, then by the
raw bytes of the content digest; the checkpoint root is the root of a
Merkle tree over the sorted disclosure rows.  The same logical input
always yields the same checkpoint id -- warehouse row numbers,
insertion order and retransmission provenance never influence it.

Building a checkpoint also materialises a reusable on-disk index
(``disclosure/<checkpoint-id>/index.sqlite``).  Exporting a page later
only reads that index; the event warehouse itself is never touched.

A *page* discloses the rows of one half-open key interval together
with a Merkle range credential.  The format is documented in
``docs/disclosure.md``; the receiver side lives in
``epcis_warehouse/audit.py``.

Trust boundary (also stated in the docs): the checkpoint itself must
reach the verifier over a separate trusted channel.  Pages name the
checkpoint they belong to, but a verifier must never adopt a root
taken from a page.  This module does not endorse real-world events,
does not distribute keys, and draws no conclusions from checkpoints
that were not trusted in the first place.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import struct

from .evidence import canonical_json, sha256_bytes

FORMAT_CHECKPOINT = "epcis-disclosure/1"
FORMAT_PAGE = "epcis-disclosure-page/1"
INDEX_FORMAT = "epcis-disclosure-index/1"

# domain-separation tags for the Merkle tree (see docs/disclosure.md)
TAG_LEAF = b"\x4c"        # 'L' -- leaf over one disclosure row
TAG_NODE = b"\x4e"        # 'N' -- internal node
TAG_ROW = b"\x52"         # 'R' -- row-data hash (non-key row content)
TAG_PAD = b"\x50"         # 'P' -- padding leaf

STATUS_CODE = {"ok": 1, "unsupported": 2, "invalid": 3, "uninterpretable": 4}
CODE_STATUS = {v: k for k, v in STATUS_CODE.items()}

_HEX64 = set("0123456789abcdef")


class DisclosureError(ValueError):
    """Contract or verification failure (CLI maps it to exit code 2)."""


# ------------------------------------------------------------------
# boundaries
#
# A boundary is a position in the ordered key space.  Internally:
#   NEG_INF            -- before everything (unset lower bound)
#   POS_INF            -- after everything (unset upper bound)
#   ("b", eid, None)   -- before all variants of event id ``eid``
#   ("b", eid, digest) -- at the given digest of ``eid`` (inclusive)
# Keys are (event_id bytes, digest bytes) and compare bytewise.
# ------------------------------------------------------------------
NEG_INF = ("inf", -1)
POS_INF = ("inf", 1)


def is_hex64(text):
    return isinstance(text, str) and len(text) == 64 and all(c in _HEX64 for c in text)


def bound_from_json(obj, is_lo):
    """Decode a JSON boundary.  ``None`` means the open end."""
    if obj is None:
        return NEG_INF if is_lo else POS_INF
    if not isinstance(obj, dict) or not set(obj) <= {"event_id", "digest"}:
        raise DisclosureError("boundary must be null or an object with event_id/digest")
    eid = obj.get("event_id")
    if not isinstance(eid, str) or not eid:
        raise DisclosureError("boundary event_id must be a non-empty string")
    try:
        eid_b = eid.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DisclosureError(f"boundary event_id is not valid UTF-8: {exc}") from exc
    digest = obj.get("digest")
    if digest is None:
        return ("b", eid_b, None)
    if not is_hex64(digest):
        raise DisclosureError("boundary digest must be 64 lowercase hex characters")
    return ("b", eid_b, bytes.fromhex(digest))


def bound_to_json(bound):
    if bound is NEG_INF or bound is POS_INF:
        return None
    _, eid_b, dig = bound
    obj = {"event_id": eid_b.decode("utf-8")}
    if dig is not None:
        obj["digest"] = dig.hex()
    return obj


def cmp_bounds(a, b):
    """Total order over boundaries: -inf < (eid,None) < (eid,digest) < +inf."""
    if a is NEG_INF or b is NEG_INF:
        return 0 if a is b else (-1 if a is NEG_INF else 1)
    if a is POS_INF or b is POS_INF:
        return 0 if a is b else (1 if a is POS_INF else -1)
    _, a_eid, a_dig = a
    _, b_eid, b_dig = b
    if a_eid != b_eid:
        return -1 if a_eid < b_eid else 1
    if a_dig is None or b_dig is None:
        return 0 if a_dig is b_dig else (-1 if a_dig is None else 1)
    return (a_dig > b_dig) - (a_dig < b_dig)


def cmp_key_bound(eid_b, dig_b, bound):
    """Compare a concrete key against a boundary (-1/0/+1)."""
    if bound is NEG_INF:
        return 1
    if bound is POS_INF:
        return -1
    _, b_eid, b_dig = bound
    if eid_b != b_eid:
        return -1 if eid_b < b_eid else 1
    if b_dig is None:
        return 1  # every variant of b_eid sits after "before all variants"
    return (dig_b > b_dig) - (dig_b < b_dig)


# ------------------------------------------------------------------
# Merkle tree over the sorted inventory (see docs/disclosure.md)
# ------------------------------------------------------------------
def _sha256(*parts):
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()


def row_data_hash(status, first_batch, event_us):
    """Commitment to the non-key part of a disclosure row."""
    code = STATUS_CODE.get(status)
    if code is None:
        raise DisclosureError(f"unknown event status: {status!r}")
    out = [TAG_ROW, bytes([code]), struct.pack(">I", first_batch)]
    if event_us is None:
        out.append(b"\x00")
    else:
        out.append(b"\x01")
        out.append(struct.pack(">q", event_us))
    return _sha256(*out)


def leaf_hash(eid_b, dig_b, rdh):
    return _sha256(TAG_LEAF, struct.pack(">I", len(eid_b)), eid_b, dig_b, rdh)


def node_hash(left, right):
    return _sha256(TAG_NODE, left, right)


def pad_hashes(levels):
    """PAD[0..levels]: padding leaf hash and padding subtree roots."""
    out = [_sha256(TAG_PAD)]
    for _ in range(levels):
        out.append(node_hash(out[-1], out[-1]))
    return out


def tree_levels(n):
    """L such that the tree over n leaves has 2**L slots (L=0 for n<=1)."""
    if n <= 1:
        return 0
    return (n - 1).bit_length()


def _merge_leaves(leaf_iter, size):
    """Consume ``size`` (a power of two) leaf hashes; return the subtree root."""
    stack = []
    for _ in range(size):
        h = next(leaf_iter)
        height = 0
        while stack and stack[-1][0] == height:
            top_height, top_hash = stack.pop()
            h = node_hash(top_hash, h)
            height = top_height + 1
        stack.append((height, h))
    if len(stack) != 1:
        raise DisclosureError("leaf stream ended mid-subtree")
    return stack[0][1]


def gen_proof(node_at, a, b, levels):
    """Merkle range proof for the leaf segment [a, b).

    ``node_at(h, i)`` must return the hash of the node covering
    [i*2**h, (i+1)*2**h).  The proof lists exactly the hashes a
    verifier needs, in the order the verifier consumes them.
    """
    proof = []

    def rec(lo, h):
        size = 1 << h
        if b <= lo or lo + size <= a:          # disjoint from [a, b)
            proof.append(node_at(h, lo >> h))
        elif a <= lo and lo + size <= b:       # fully inside: verifier computes
            return
        else:
            half = size >> 1
            rec(lo, h - 1)
            rec(lo + half, h - 1)

    rec(0, levels)
    return proof


def verify_range_root(levels, a, b, leaf_iter, proof_iter):
    """Recompute the tree root for segment [a, b).

    Consumes exactly b-a leaf hashes from ``leaf_iter`` and the proof
    hashes from ``proof_iter``.  Callers must check both iterators are
    exhausted afterwards and compare the result with the trusted root.
    """
    def rec(lo, h):
        size = 1 << h
        if b <= lo or lo + size <= a:
            return next(proof_iter)
        if a <= lo and lo + size <= b:
            return _merge_leaves(leaf_iter, size)
        half = size >> 1
        return node_hash(rec(lo, h - 1), rec(lo + half, h - 1))

    return rec(0, levels)


# ------------------------------------------------------------------
# checkpoint build
# ------------------------------------------------------------------
def checkpoint_id_of(body):
    return sha256_bytes(canonical_json(body).encode("utf-8"))


def validate_checkpoint(doc):
    """Validate a checkpoint document; return (body, checkpoint_id)."""
    if not isinstance(doc, dict):
        raise DisclosureError("checkpoint must be a JSON object")
    if doc.get("format") != FORMAT_CHECKPOINT:
        raise DisclosureError(f"checkpoint format must be {FORMAT_CHECKPOINT!r}")
    body = {k: doc.get(k) for k in ("batch", "format", "root", "variants", "warehouse")}
    if not isinstance(body["warehouse"], str) or not body["warehouse"]:
        raise DisclosureError("checkpoint warehouse must be a non-empty string")
    if not isinstance(body["batch"], int) or isinstance(body["batch"], bool) \
            or body["batch"] < 1:
        raise DisclosureError("checkpoint batch must be a positive integer")
    if not isinstance(body["variants"], int) or isinstance(body["variants"], bool) \
            or body["variants"] < 0:
        raise DisclosureError("checkpoint variants must be a non-negative integer")
    if not is_hex64(body["root"]):
        raise DisclosureError("checkpoint root must be 64 lowercase hex characters")
    cpid = checkpoint_id_of(body)
    if doc.get("checkpoint") != cpid:
        raise DisclosureError("checkpoint id does not match its committed content")
    return body, cpid


_INDEX_SCHEMA = """
CREATE TABLE leaves(
  leaf_index INTEGER PRIMARY KEY,
  event_id BLOB NOT NULL,
  digest BLOB NOT NULL,
  status TEXT NOT NULL,
  first_batch INTEGER NOT NULL,
  event_us INTEGER
);
CREATE TABLE nodes(
  level INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  hash BLOB NOT NULL,
  PRIMARY KEY(level, idx)
) WITHOUT ROWID;
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
"""

_BATCH = 4096


def build_checkpoint(store, warehouse_id, batch_seq, out_path=None):
    """Build (or reuse) the checkpoint for one published batch.

    Streams the batch-visible inventory out of the warehouse into a
    reusable on-disk index under ``<root>/disclosure/<checkpoint-id>/``.
    Never writes to the warehouse tables.  Returns the checkpoint
    document (which also names the checkpoint id).
    """
    if not isinstance(warehouse_id, str) or not warehouse_id:
        raise DisclosureError("warehouse id must be a non-empty string")
    row = store.conn.execute(
        "SELECT batch_seq FROM batches WHERE batch_seq=? AND status='published'",
        (batch_seq,)).fetchone()
    if row is None:
        raise DisclosureError(f"batch {batch_seq} is not a published batch")

    disc_dir = os.path.join(store.root, "disclosure")
    os.makedirs(disc_dir, exist_ok=True)
    tmp = os.path.join(disc_dir, f".build-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        conn = sqlite3.connect(os.path.join(tmp, "index.sqlite"))
        conn.executescript(_INDEX_SCHEMA)
        n = _fill_leaves(store, conn, batch_seq)
        levels = tree_levels(n)
        pads = pad_hashes(levels)
        _fill_nodes(conn, n, levels, pads)
        conn.execute("CREATE INDEX idx_leaves_key ON leaves(event_id, digest)")
        root = pads[0].hex() if n == 0 else conn.execute(
            "SELECT hash FROM nodes WHERE level=? AND idx=0", (levels,)).fetchone()[0].hex()
        body = {"batch": batch_seq, "format": FORMAT_CHECKPOINT, "root": root,
                "variants": n, "warehouse": warehouse_id}
        cpid = checkpoint_id_of(body)
        doc = dict(body)
        doc["checkpoint"] = cpid
        conn.execute("INSERT INTO meta VALUES('index_format', ?)", (INDEX_FORMAT,))
        conn.execute("INSERT INTO meta VALUES('checkpoint', ?)",
                     (canonical_json(doc),))
        conn.commit()
        conn.close()
        with open(os.path.join(tmp, "checkpoint.json"), "w", encoding="utf-8") as fh:
            fh.write(canonical_json(doc) + "\n")
        final = os.path.join(disc_dir, cpid)
        if os.path.isdir(final):
            # an identical checkpoint is already published; later batches
            # can never change what an earlier batch committed to
            shutil.rmtree(tmp)
        else:
            os.rename(tmp, final)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    doc = _load_checkpoint_doc(disc_dir, cpid)
    if out_path:
        shutil.copyfile(os.path.join(final, "checkpoint.json"), out_path)
    return doc


def _fill_leaves(store, conn, batch_seq):
    cur = store.conn.execute(
        "SELECT event_id, content_digest, status, first_batch, event_us FROM events"
        " WHERE event_id IS NOT NULL AND first_batch<=?"
        " ORDER BY event_id, content_digest", (batch_seq,))
    n = 0
    leaves, nodes0 = [], []
    for row in cur:
        eid_b = row["event_id"].encode("utf-8")
        dig_b = bytes.fromhex(row["content_digest"])
        rdh = row_data_hash(row["status"], row["first_batch"], row["event_us"])
        leaves.append((n, eid_b, dig_b, row["status"], row["first_batch"],
                       row["event_us"]))
        nodes0.append((0, n, leaf_hash(eid_b, dig_b, rdh)))
        n += 1
        if len(leaves) >= _BATCH:
            conn.executemany("INSERT INTO leaves VALUES(?,?,?,?,?,?)", leaves)
            conn.executemany("INSERT INTO nodes VALUES(?,?,?)", nodes0)
            leaves.clear()
            nodes0.clear()
    if leaves:
        conn.executemany("INSERT INTO leaves VALUES(?,?,?,?,?,?)", leaves)
        conn.executemany("INSERT INTO nodes VALUES(?,?,?)", nodes0)
    return n


def _fill_nodes(conn, n, levels, pads):
    for h in range(1, levels + 1):
        batch = []
        idx_out = 0
        pending = None
        for _, hval in conn.execute(
                "SELECT idx, hash FROM nodes WHERE level=? ORDER BY idx", (h - 1,)):
            if pending is None:
                pending = hval
            else:
                batch.append((h, idx_out, node_hash(pending, hval)))
                idx_out += 1
                pending = None
                if len(batch) >= _BATCH:
                    conn.executemany("INSERT INTO nodes VALUES(?,?,?)", batch)
                    batch.clear()
        if pending is not None:
            batch.append((h, idx_out, node_hash(pending, pads[h - 1])))
        if batch:
            conn.executemany("INSERT INTO nodes VALUES(?,?,?)", batch)


def _load_checkpoint_doc(disc_dir, cpid):
    path = os.path.join(disc_dir, cpid, "checkpoint.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------
# page export
# ------------------------------------------------------------------
def export_page(disclosure_dir, checkpoint_id, lo, hi, out_path):
    """Export one page of the committed inventory.

    Reads only the on-disk index of the named checkpoint -- never the
    event warehouse.  ``lo``/``hi`` are internal boundary tuples with
    lo < hi.  Returns a summary including index access statistics that
    demonstrate the export touched only the page's neighbourhood.
    """
    if cmp_bounds(lo, hi) >= 0:
        raise DisclosureError("page range must be non-zero width (lo < hi)")
    idir = os.path.join(disclosure_dir, checkpoint_id)
    index_path = os.path.join(idir, "index.sqlite")
    if not os.path.isfile(index_path):
        raise DisclosureError(f"unknown checkpoint: {checkpoint_id}")
    conn = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        try:
            meta_row = conn.execute(
                "SELECT value FROM meta WHERE key='checkpoint'").fetchone()
        except sqlite3.DatabaseError as exc:
            raise DisclosureError(
                f"checkpoint index is unreadable: {exc}") from exc
        if meta_row is None:
            raise DisclosureError(
                f"checkpoint index is missing its metadata: {checkpoint_id}")
        doc = json.loads(meta_row[0])
        body, cpid = validate_checkpoint(doc)
        if cpid != checkpoint_id:
            raise DisclosureError("checkpoint id does not match the index directory")
        n = body["variants"]
        levels = tree_levels(n)
        pads = pad_hashes(levels)
        stats = {"leaves_read": 0, "nodes_read": 0}

        def probe(sql, params):
            row = conn.execute(sql, params).fetchone()
            if row is not None:
                stats["leaves_read"] += 1
            return row

        def lower_bound(bound):
            if bound is NEG_INF:
                return 0
            if bound is POS_INF:
                return n
            _, eid_b, dig = bound
            if dig is None:
                row = probe("SELECT leaf_index FROM leaves WHERE event_id>=?"
                            " ORDER BY event_id, digest LIMIT 1", (eid_b,))
                return row[0] if row else n
            r1 = probe("SELECT leaf_index FROM leaves WHERE event_id=? AND digest>=?"
                       " ORDER BY digest LIMIT 1", (eid_b, dig))
            r2 = probe("SELECT leaf_index FROM leaves WHERE event_id>?"
                       " ORDER BY event_id, digest LIMIT 1", (eid_b,))
            cand = [r[0] for r in (r1, r2) if r]
            return min(cand) if cand else n

        i = lower_bound(lo)
        j = lower_bound(hi)

        def leaf_at(idx):
            row = conn.execute(
                "SELECT event_id, digest, status, first_batch, event_us FROM leaves"
                " WHERE leaf_index=?", (idx,)).fetchone()
            stats["leaves_read"] += 1
            return row

        left = leaf_at(i - 1) if i > 0 else None
        right = leaf_at(j) if j < n else None
        a = i - (1 if left else 0)
        b = j + (1 if right else 0)

        def node_at(h, idx):
            if (idx << h) >= n:
                return pads[h]
            row = conn.execute("SELECT hash FROM nodes WHERE level=? AND idx=?",
                               (h, idx)).fetchone()
            stats["nodes_read"] += 1
            return row[0]

        proof = gen_proof(node_at, a, b, levels)

        def edge_json(row):
            eid_b, dig_b, status, first_batch, event_us = row
            return {"event_id": eid_b.decode("utf-8"), "digest": dig_b.hex(),
                    "row_hash": row_data_hash(status, first_batch, event_us).hex()}

        header = {"format": FORMAT_PAGE, "checkpoint": checkpoint_id,
                  "lo": bound_to_json(lo), "hi": bound_to_json(hi),
                  "start": i, "row_count": j - i,
                  "left_edge": edge_json(left) if left else None,
                  "right_edge": edge_json(right) if right else None,
                  "proof": [h.hex() for h in proof]}
        written = 0
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(canonical_json(header) + "\n")
            cur = conn.execute(
                "SELECT event_id, digest, status, first_batch, event_us FROM leaves"
                " WHERE leaf_index>=? AND leaf_index<? ORDER BY leaf_index", (i, j))
            for eid_b, dig_b, status, first_batch, event_us in cur:
                stats["leaves_read"] += 1
                fh.write(canonical_json({
                    "event_id": eid_b.decode("utf-8"), "digest": dig_b.hex(),
                    "status": status, "first_batch": first_batch,
                    "event_us": event_us}) + "\n")
                written += 1
        size = os.path.getsize(out_path)
        return {"checkpoint": checkpoint_id, "lo": bound_to_json(lo),
                "hi": bound_to_json(hi), "rows": written,
                "proof_hashes": len(proof), "page_bytes": size,
                "index_reads": stats, "out": out_path}
    finally:
        conn.close()
