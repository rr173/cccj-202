"""Offline selective disclosure: auditor-side receiving workspace.

An audit workspace is a self-contained directory on the auditor's
machine.  It is bound to exactly one checkpoint (delivered over a
separate trusted channel -- never taken from a page) and one requested
key range.  Pages arrive over the untrusted channel, in any order;
each is verified against the checkpoint before anything is recorded,
and a failed page never pollutes received progress.  Only when the
verified pages tile the requested range with no gaps does ``finalize``
deliver the final per-event-id inventory.

The workspace can be closed and reopened at any time: all state lives
in the directory.  Re-opening, receive order and re-pagination never
change the final business inventory.
"""
from __future__ import annotations

import json
import os
import sqlite3
from functools import cmp_to_key

from .disclosure import (
    FORMAT_PAGE, STATUS_CODE, DisclosureError,
    bound_from_json, bound_to_json, cmp_bounds, cmp_key_bound, is_hex64,
    leaf_hash, row_data_hash, tree_levels, validate_checkpoint,
    verify_range_root,
)
from .evidence import canonical_json, sha256_bytes

FORMAT_WORKSPACE = "epcis-audit-workspace/1"
FORMAT_INVENTORY = "epcis-audit-inventory/1"


class AuditError(DisclosureError):
    """Receiver-side contract or verification failure (exit code 2)."""


# ------------------------------------------------------------------
# workspace lifecycle
# ------------------------------------------------------------------
def _ws_json_path(ws):
    return os.path.join(ws, "workspace.json")


def _load_ws(ws):
    try:
        with open(_ws_json_path(ws), "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        raise AuditError(f"not an audit workspace: {ws}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"workspace metadata unreadable: {exc}") from exc
    if doc.get("format") != FORMAT_WORKSPACE:
        raise AuditError("workspace format mismatch")
    return doc


def _connect(ws):
    conn = sqlite3.connect(os.path.join(ws, "workspace.db"))
    conn.isolation_level = None  # explicit BEGIN/COMMIT
    return conn


def init_workspace(ws, checkpoint_path, lo, hi):
    """Create (or idempotently re-open) an audit workspace.

    ``lo``/``hi`` are internal boundary tuples for the requested range;
    the request must be non-zero width.  Re-initialising an existing
    workspace with the same checkpoint and request is a no-op; a
    different checkpoint or request is refused.
    """
    if cmp_bounds(lo, hi) >= 0:
        raise AuditError("request range must be non-zero width (lo < hi)")
    try:
        with open(checkpoint_path, "rb") as fh:
            raw = fh.read()
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditError(f"checkpoint file unreadable: {exc}") from exc
    body, cpid = validate_checkpoint(doc)
    request = {"lo": bound_to_json(lo), "hi": bound_to_json(hi)}

    if os.path.exists(_ws_json_path(ws)):
        existing = _load_ws(ws)
        if existing["checkpoint_id"] != cpid or existing["request"] != request:
            raise AuditError(
                "workspace is bound to a different checkpoint or request")
        return {"status": "already_initialized", "workspace": ws,
                "checkpoint": cpid, "request": request}
    if os.path.exists(ws) and os.listdir(ws):
        raise AuditError(f"directory not empty and not an audit workspace: {ws}")

    os.makedirs(ws)
    conn = _connect(ws)
    conn.execute("BEGIN")
    try:
        conn.execute("CREATE TABLE intervals("
                     "  lo TEXT NOT NULL, hi TEXT NOT NULL,"
                     "  page_sha256 TEXT NOT NULL)")
        conn.execute("CREATE TABLE rows("
                     "  event_id BLOB NOT NULL, digest BLOB NOT NULL,"
                     "  status TEXT NOT NULL, first_batch INTEGER NOT NULL,"
                     "  event_us INTEGER,"
                     "  PRIMARY KEY(event_id, digest)) WITHOUT ROWID")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        raise
    conn.close()
    meta = {"format": FORMAT_WORKSPACE, "checkpoint_id": cpid,
            "checkpoint": body, "request": request}
    tmp = _ws_json_path(ws) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(canonical_json(meta) + "\n")
    os.replace(tmp, _ws_json_path(ws))
    return {"status": "initialized", "workspace": ws,
            "checkpoint": cpid, "request": request}


# ------------------------------------------------------------------
# page parsing & verification
# ------------------------------------------------------------------
def _err(msg):
    raise AuditError(msg)


def _parse_edge(obj, what):
    if obj is None:
        return None
    if not isinstance(obj, dict) or set(obj) != {"event_id", "digest", "row_hash"}:
        _err(f"{what} must name event_id, digest and row_hash")
    eid, digest, row_hash = obj["event_id"], obj["digest"], obj["row_hash"]
    if not isinstance(eid, str) or not eid:
        _err(f"{what} event_id must be a non-empty string")
    if not is_hex64(digest) or not is_hex64(row_hash):
        _err(f"{what} digest and row_hash must be 64 lowercase hex characters")
    try:
        eid_b = eid.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AuditError(f"{what} event_id is not valid UTF-8: {exc}") from exc
    return {"event_id": eid, "event_id_b": eid_b,
            "digest_b": bytes.fromhex(digest), "row_hash_b": bytes.fromhex(row_hash)}


def _parse_row(line, line_no):
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise AuditError(f"row {line_no}: not valid JSON: {exc}") from exc
    if not isinstance(obj, dict) or \
            set(obj) != {"event_id", "digest", "status", "first_batch", "event_us"}:
        _err(f"row {line_no}: must name exactly event_id, digest, status,"
             " first_batch, event_us")
    eid, digest = obj["event_id"], obj["digest"]
    status, first_batch, event_us = obj["status"], obj["first_batch"], obj["event_us"]
    if not isinstance(eid, str) or not eid:
        _err(f"row {line_no}: event_id must be a non-empty string")
    if not is_hex64(digest):
        _err(f"row {line_no}: digest must be 64 lowercase hex characters")
    if status not in STATUS_CODE:
        _err(f"row {line_no}: unknown status {status!r}")
    if not isinstance(first_batch, int) or isinstance(first_batch, bool) \
            or first_batch < 1:
        _err(f"row {line_no}: first_batch must be a positive integer")
    if event_us is not None and (not isinstance(event_us, int)
                                 or isinstance(event_us, bool)):
        _err(f"row {line_no}: event_us must be an integer or null")
    try:
        eid_b = eid.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AuditError(f"row {line_no}: event_id is not valid UTF-8: {exc}") from exc
    return {"event_id": eid, "event_id_b": eid_b, "digest_b": bytes.fromhex(digest),
            "status": status, "first_batch": first_batch, "event_us": event_us}


def _read_header(fh):
    line = fh.readline()
    if line == "":
        _err("page file is empty")
    try:
        header = json.loads(line)
    except json.JSONDecodeError as exc:
        raise AuditError(f"page header is not valid JSON: {exc}") from exc
    if not isinstance(header, dict):
        _err("page header must be a JSON object")
    if header.get("format") != FORMAT_PAGE:
        _err(f"page format must be {FORMAT_PAGE!r}")
    for key in ("checkpoint", "lo", "hi", "start", "row_count",
                    "left_edge", "right_edge", "proof"):
        if key not in header:
            _err(f"page header missing {key!r}")
    start, row_count = header["start"], header["row_count"]
    for name, val in (("start", start), ("row_count", row_count)):
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            _err(f"page {name} must be a non-negative integer")
    proof = header["proof"]
    if not isinstance(proof, list) or not all(is_hex64(h) for h in proof):
        _err("page proof must be a list of 64-char lowercase hex hashes")
    return header


def receive_page(ws, page_path):
    """Verify one page against the workspace checkpoint and record it.

    Out-of-order delivery is fine.  Re-delivering an already received
    interval is a no-op (a valid page for a given interval is unique,
    so a re-delivery that verifies is byte-identical in content).
    A page whose interval overlaps a *different* received interval is
    rejected, as is any format, credential or completeness failure;
    failures leave received progress untouched.
    """
    wsj = _load_ws(ws)
    body = wsj["checkpoint"]
    n = body["variants"]
    root = bytes.fromhex(body["root"])
    req_lo = bound_from_json(wsj["request"]["lo"], is_lo=True)
    req_hi = bound_from_json(wsj["request"]["hi"], is_lo=False)
    levels = tree_levels(n)

    try:
        fh = open(page_path, "r", encoding="utf-8")
    except OSError as exc:
        raise AuditError(f"page file unreadable: {exc}") from exc
    with fh:
        try:
            header = _read_header(fh)
        except UnicodeDecodeError as exc:
            raise AuditError(f"page is not valid UTF-8: {exc}") from exc
        if header["checkpoint"] != wsj["checkpoint_id"]:
            _err("page belongs to a different checkpoint")
        page_lo = bound_from_json(header["lo"], is_lo=True)
        page_hi = bound_from_json(header["hi"], is_lo=False)
        if cmp_bounds(page_lo, page_hi) >= 0:
            _err("page range must be non-zero width")
        if cmp_bounds(page_lo, req_lo) < 0 or cmp_bounds(page_hi, req_hi) > 0:
            _err("page range lies outside the audit request")
        left_edge = _parse_edge(header["left_edge"], "left_edge")
        right_edge = _parse_edge(header["right_edge"], "right_edge")
        start, row_count = header["start"], header["row_count"]
        proof = [bytes.fromhex(h) for h in header["proof"]]

        # completeness boundary checks: the neighbours of the proven
        # segment must sit strictly outside the claimed range
        if left_edge and cmp_key_bound(left_edge["event_id_b"],
                                       left_edge["digest_b"], page_lo) >= 0:
            _err("left edge key is not strictly before the page range")
        if right_edge and cmp_key_bound(right_edge["event_id_b"],
                                        right_edge["digest_b"], page_hi) < 0:
            _err("right edge key lies inside the page range")

        a = start - (1 if left_edge else 0)
        if a < 0:
            _err("page claims a left edge before the first leaf")
        if left_edge is None and start != 0:
            _err("page without a left edge must start at the first leaf")
        rows_end = start + row_count
        if right_edge is None and rows_end != n:
            _err("page without a right edge must end at the last leaf")
        b = rows_end + (1 if right_edge else 0)
        if b > n:
            _err("page segment extends beyond the committed inventory")

        conn = _connect(ws)
        try:
            intervals = [(bound_from_json(json.loads(lo), True),
                          bound_from_json(json.loads(hi), False))
                         for lo, hi in conn.execute("SELECT lo, hi FROM intervals")]
            duplicate = False
            for iv_lo, iv_hi in intervals:
                if iv_lo == page_lo and iv_hi == page_hi:
                    duplicate = True
                elif cmp_bounds(page_lo, iv_hi) < 0 and cmp_bounds(iv_lo, page_hi) < 0:
                    _err("page range overlaps an already received range")

            insert = not duplicate
            prev_key = [None]

            def leaf_stream():
                if left_edge:
                    yield leaf_hash(left_edge["event_id_b"], left_edge["digest_b"],
                                    left_edge["row_hash_b"])
                for k in range(row_count):
                    try:
                        line = fh.readline()
                    except UnicodeDecodeError as exc:
                        raise AuditError(f"row {k + 2}: not valid UTF-8: {exc}") \
                            from exc
                    if line == "":
                        _err(f"page truncated: expected {row_count} rows, got {k}")
                    row = _parse_row(line, k + 2)
                    key = (row["event_id_b"], row["digest_b"])
                    if prev_key[0] is not None and key <= prev_key[0]:
                        _err("page rows are not in strictly increasing key order")
                    prev_key[0] = key
                    if cmp_key_bound(*key, page_lo) < 0 \
                            or cmp_key_bound(*key, page_hi) >= 0:
                        _err("page row lies outside the page range")
                    if insert:
                        conn.execute(
                            "INSERT INTO rows VALUES(?,?,?,?,?)",
                            (row["event_id_b"], row["digest_b"], row["status"],
                             row["first_batch"], row["event_us"]))
                    yield leaf_hash(row["event_id_b"], row["digest_b"],
                                    row_data_hash(row["status"], row["first_batch"],
                                                  row["event_us"]))
                if right_edge:
                    yield leaf_hash(right_edge["event_id_b"],
                                    right_edge["digest_b"], right_edge["row_hash_b"])
                if fh.readline() != "":
                    _err("page carries unconsumed data after the declared rows")

            leaves = leaf_stream()
            proofs = iter(proof)
            conn.execute("BEGIN")
            try:
                try:
                    got = verify_range_root(levels, a, b, leaves, proofs)
                except StopIteration:
                    _err("page proof is too short for the declared segment")
                if next(leaves, None) is not None:
                    _err("page leaves do not match the declared segment")
                if next(proofs, None) is not None:
                    _err("page proof carries unconsumed hashes")
                if got != root:
                    _err("page credential does not match the checkpoint root")
                if insert:
                    with open(page_path, "rb") as raw:
                        page_sha = sha256_bytes(raw.read())
                    conn.execute(
                        "INSERT INTO intervals VALUES(?,?,?)",
                        (canonical_json(header["lo"]),
                         canonical_json(header["hi"]), page_sha))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
    return {"status": "already_received" if duplicate else "accepted",
            "checkpoint": wsj["checkpoint_id"],
            "lo": header["lo"], "hi": header["hi"], "rows": row_count}


# ------------------------------------------------------------------
# gaps & finalisation
# ------------------------------------------------------------------
def _sorted_intervals(conn):
    out = []
    for lo, hi in conn.execute("SELECT lo, hi FROM intervals"):
        out.append((bound_from_json(json.loads(lo), True),
                    bound_from_json(json.loads(hi), False)))
    out.sort(key=cmp_to_key(lambda x, y: cmp_bounds(x[0], y[0])))
    return out


def gaps(ws):
    """Report received coverage and the still-missing sub-ranges."""
    wsj = _load_ws(ws)
    req_lo = bound_from_json(wsj["request"]["lo"], is_lo=True)
    req_hi = bound_from_json(wsj["request"]["hi"], is_lo=False)
    conn = _connect(ws)
    try:
        intervals = _sorted_intervals(conn)
        rows_received = conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
    finally:
        conn.close()
    covered, missing = [], []
    cursor = req_lo
    for iv_lo, iv_hi in intervals:
        if cmp_bounds(cursor, iv_lo) < 0:
            missing.append((cursor, iv_lo))
        if cmp_bounds(cursor, iv_hi) < 0:
            cursor = iv_hi
    if cmp_bounds(cursor, req_hi) < 0:
        missing.append((cursor, req_hi))
    return {"checkpoint": wsj["checkpoint_id"], "request": wsj["request"],
            "covered": [{"lo": bound_to_json(lo), "hi": bound_to_json(hi)}
                        for lo, hi in intervals],
            "missing": [{"lo": bound_to_json(lo), "hi": bound_to_json(hi)}
                        for lo, hi in missing],
            "complete": not missing,
            "pages_received": len(intervals),
            "rows_received": rows_received}


def finalize(ws, out_dir):
    """Deliver the final inventory once coverage is complete.

    Returns (summary, exit_code).  Nothing is written while any
    sub-range of the request is still uncovered -- a half-finished
    audit is never marked as a final result.
    """
    state = gaps(ws)
    if not state["complete"]:
        return {"status": "incomplete", "missing": state["missing"]}, 2
    wsj = _load_ws(ws)
    conn = _connect(ws)
    os.makedirs(out_dir, exist_ok=True)
    inv_tmp = os.path.join(out_dir, ".inventory.jsonl.tmp")
    by_tmp = os.path.join(out_dir, ".by_event.jsonl.tmp")
    variants = 0
    event_ids = 0
    multi_ids = 0
    try:
        cur = conn.execute(
            "SELECT event_id, digest, status, first_batch, event_us FROM rows"
            " ORDER BY event_id, digest")
        with open(inv_tmp, "w", encoding="utf-8") as inv, \
                open(by_tmp, "w", encoding="utf-8") as bye:
            cur_id_b = None
            cur_id = None
            cur_n = 0
            for eid_b, dig_b, status, first_batch, event_us in cur:
                eid = eid_b.decode("utf-8")
                if eid_b != cur_id_b:
                    if cur_id_b is not None:
                        bye.write(canonical_json({
                            "event_id": cur_id, "variants": cur_n,
                            "multi_variant": cur_n > 1}) + "\n")
                        event_ids += 1
                        multi_ids += 1 if cur_n > 1 else 0
                    cur_id_b, cur_id, cur_n = eid_b, eid, 0
                cur_n += 1
                variants += 1
                inv.write(canonical_json({
                    "event_id": eid, "digest": dig_b.hex(), "status": status,
                    "first_batch": first_batch, "event_us": event_us}) + "\n")
            if cur_id_b is not None:
                bye.write(canonical_json({
                    "event_id": cur_id, "variants": cur_n,
                    "multi_variant": cur_n > 1}) + "\n")
                event_ids += 1
                multi_ids += 1 if cur_n > 1 else 0
    finally:
        conn.close()
    summary = {"status": "complete", "format": FORMAT_INVENTORY,
               "checkpoint": wsj["checkpoint_id"],
               "warehouse": wsj["checkpoint"]["warehouse"],
               "batch": wsj["checkpoint"]["batch"],
               "request": wsj["request"],
               "variants": variants, "event_ids": event_ids,
               "multi_variant_ids": multi_ids}
    sum_tmp = os.path.join(out_dir, ".summary.json.tmp")
    with open(sum_tmp, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.replace(inv_tmp, os.path.join(out_dir, "inventory.jsonl"))
    os.replace(by_tmp, os.path.join(out_dir, "by_event.jsonl"))
    # the summary lands last: its presence marks a completed audit
    os.replace(sum_tmp, os.path.join(out_dir, "summary.json"))
    return summary, 0
