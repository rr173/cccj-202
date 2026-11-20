"""Offline handover settlement (交接结账).

One settlement run closes handover documents between two parties
against the scans received in one published batch of the warehouse.
The event repository is only read; all indexes live in a scratch
SQLite database that is attached for the run and deleted afterwards.

Inputs (line-delimited JSON, see docs/settlement.md):

  * an opening inventory carrying an as-of instant: named EPC ownership
    lines and class quantity lines (merged per location/class/dimension
    in exact base units);
  * the sender-side and receiver-side document files: one handover per
    line with handover number, origin, destination, agreed effective
    instant and the scan event ids that side used.

A handover posts only when both sides' documents agree (same route,
same UTC effective instant, origin != destination, effective later
than the opening instant) and both sides' scans verify against the
batch: every referenced event must be received in the batch,
conflict-free, a valid ObjectEvent OBSERVE declaring exactly the
document's origin and destination, scanned after the opening instant
and not after the effective instant, with the last sender scan not
after the first receiver scan.  Goods are totalled from the scans
themselves (never from the forms): named EPC sets must be identical
on both sides (an EPC repeated across one side's scans is one good,
not two, and blocks the handover); class quantities are totalled per
class and dimension in exact base units and must match key by key.

Event ids are single-use: an id claimed by two different
(handover, side) pairs sends every involved handover to the exception
list, whatever else is wrong with it; an identical re-sent document
adds no new claim.

Posting happens per effective instant.  Handovers sharing a named EPC
or a quantity key (location, class, dimension) on either the debit or
the credit side form one group (transitively) and settle together:
named goods must be owned by the sender at the start of the instant
and may not change hands twice in the group; per quantity key the
group's total debit must not exceed the balance at the start of the
instant (same-instant credits never cover debits).  Any violation
blocks the whole group -- nothing posts halfway -- while unrelated
groups settle normally.  Successful groups take effect simultaneously
and the next instant sees the new balances.

Outputs (in --out): summary.json, ledger.jsonl, documents.jsonl,
groups.jsonl, balances.jsonl.  Fatal input errors (invalid batch,
broken opening, duplicate opening EPC ownership, document lines
without an identifiable handover number) produce only a failed
summary.json -- no ledger is ever delivered for them.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from array import array
from datetime import datetime, timezone
from fractions import Fraction

from . import parse as P
from .evidence import canonical_json, sha256_bytes
from .units import fmt_fraction, parse_quantity, UnitTableError

SIDES = ("sender", "receiver")
DOC_FIELDS = frozenset({"handover", "from", "to", "effective", "scans"})
RESULT_FILES = ("ledger.jsonl", "documents.jsonl", "groups.jsonl", "balances.jsonl")

SCRATCH_SCHEMA = """
CREATE TABLE settle.docs(
  side TEXT NOT NULL,
  handover TEXT NOT NULL,
  doc_hash TEXT NOT NULL,
  from_loc TEXT,
  to_loc TEXT,
  effective_us INTEGER,
  errors TEXT NOT NULL,          -- JSON array of document-level field errors
  first_line INTEGER NOT NULL,
  PRIMARY KEY(side, handover, doc_hash)
);
CREATE INDEX settle.idx_docs_handover ON docs(handover, side);
CREATE TABLE settle.claims(
  event_id TEXT NOT NULL,
  side TEXT NOT NULL,
  handover TEXT NOT NULL,
  PRIMARY KEY(event_id, side, handover)
);
CREATE INDEX settle.idx_claims_handover ON claims(handover, side, event_id);
CREATE TABLE settle.outcomes(
  handover TEXT PRIMARY KEY,
  status TEXT NOT NULL,          -- failed | eligible | posted | group_blocked
  reasons TEXT NOT NULL,         -- JSON array
  group_id TEXT
);
CREATE TABLE settle.eligible(
  handover TEXT PRIMARY KEY,
  from_loc TEXT NOT NULL,
  to_loc TEXT NOT NULL,
  effective_us INTEGER NOT NULL
);
CREATE INDEX settle.idx_eligible_time ON eligible(effective_us);
CREATE TABLE settle.eligible_epcs(
  handover TEXT NOT NULL,
  epc TEXT NOT NULL,
  PRIMARY KEY(handover, epc)
) WITHOUT ROWID;
CREATE TABLE settle.eligible_qty(
  handover TEXT NOT NULL,
  cls TEXT NOT NULL,
  dim TEXT NOT NULL,
  qty TEXT NOT NULL,             -- exact base-unit quantity string
  PRIMARY KEY(handover, cls, dim)
) WITHOUT ROWID;
"""


class FatalSettlement(ValueError):
    """Inputs too broken to settle: no ledger may be delivered."""

    def __init__(self, code, detail, file=None, line=None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.file = file
        self.line = line


# ---------------------------------------------------------------- small helpers

def _iso(us):
    sec, micro = divmod(int(us), 1_000_000)
    dt = datetime.fromtimestamp(sec, tz=timezone.utc)
    return dt.replace(microsecond=micro).isoformat()


def _parse_instant(text, what):
    dt = P._parse_time(text, what)  # ProfileError on bad shape/missing offset
    return P._epoch_us(P._to_utc(dt))


def _grouped(cursor):
    """Yield (key, [rows]) for a cursor ordered by column 0."""
    key = None
    buf = []
    for row in cursor:
        k = row[0]
        if k != key:
            if buf:
                yield key, buf
            key = k
            buf = [row]
        else:
            buf.append(row)
    if buf:
        yield key, buf


def _need_str(obj, field, errors):
    v = obj.get(field)
    if not isinstance(v, str) or not v:
        errors.append(f"bad_{field}")
        return None
    return v


# ---------------------------------------------------------------- opening inventory

OPENING_KEYS = {
    "opening": frozenset({"type", "as_of"}),
    "epc": frozenset({"type", "epc", "location"}),
    "quantity": frozenset({"type", "location", "class", "quantity", "uom"}),
}


def load_opening(path, units):
    """Parse the opening inventory JSONL.  Raises FatalSettlement."""
    as_of_us = None
    epc_owner = {}
    qty = {}
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as exc:
        raise FatalSettlement("input_unreadable", f"opening: {exc}", path) from exc
    with fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FatalSettlement("opening_invalid_line",
                                      f"not JSON: {exc}", path, lineno) from exc
            if not isinstance(obj, dict):
                raise FatalSettlement("opening_invalid_line",
                                      "line is not a JSON object", path, lineno)
            typ = obj.get("type")
            if typ not in OPENING_KEYS:
                raise FatalSettlement("opening_invalid_line",
                                      f"unknown line type {typ!r}", path, lineno)
            unknown = sorted(set(obj) - OPENING_KEYS[typ])
            if unknown:
                raise FatalSettlement("opening_invalid_line",
                                      f"unknown field(s): {', '.join(unknown)}", path, lineno)
            if typ == "opening":
                if as_of_us is not None:
                    raise FatalSettlement("opening_duplicate_header",
                                          "more than one opening header line", path, lineno)
                try:
                    as_of_us = _parse_instant(obj.get("as_of"), "as_of")
                except P.ProfileError as exc:
                    raise FatalSettlement("opening_invalid_line",
                                          f"as_of: {exc.reason}", path, lineno) from exc
            elif typ == "epc":
                errs = []
                epc = _need_str(obj, "epc", errs)
                loc = _need_str(obj, "location", errs)
                if errs:
                    raise FatalSettlement("opening_invalid_line",
                                          "; ".join(errs), path, lineno)
                if epc in epc_owner:
                    # duplicate named ownership is fatal even for the same location
                    raise FatalSettlement("opening_duplicate_epc",
                                          f"EPC {epc!r} owned more than once "
                                          f"(first: {epc_owner[epc]!r}, again: {loc!r})",
                                          path, lineno)
                epc_owner[epc] = loc
            else:  # quantity
                errs = []
                loc = _need_str(obj, "location", errs)
                cls = _need_str(obj, "class", errs)
                uom = _need_str(obj, "uom", errs)
                if errs:
                    raise FatalSettlement("opening_invalid_line",
                                          "; ".join(errs), path, lineno)
                try:
                    amount = parse_quantity(obj.get("quantity"), what="opening quantity")
                except UnitTableError as exc:
                    raise FatalSettlement("opening_invalid_line",
                                          str(exc), path, lineno) from exc
                if amount < 0:
                    raise FatalSettlement("opening_invalid_line",
                                          "negative quantity", path, lineno)
                if not units.has(uom):
                    raise FatalSettlement("opening_invalid_line",
                                          f"uom {uom!r} not declared in unit table",
                                          path, lineno)
                base, _base_unit, dim = units.to_base(amount, uom)
                key = (loc, cls, dim)
                qty[key] = qty.get(key, Fraction(0)) + base
    if as_of_us is None:
        raise FatalSettlement("opening_missing_header",
                              "no {\"type\": \"opening\", \"as_of\": ...} line", path)
    return as_of_us, epc_owner, qty


# ---------------------------------------------------------------- document files

def load_documents(path, side, opening_us, conn, counters):
    """Stream one document JSONL file into the scratch tables.

    Raises FatalSettlement only when a line is broken beyond
    recognising its handover number; every other defect is recorded as
    a document-level error on the handover.
    """
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as exc:
        raise FatalSettlement("input_unreadable", f"{side}: {exc}", path) from exc
    with fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            counters[side + "_lines"] += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FatalSettlement("document_unidentifiable_handover",
                                      f"not JSON: {exc}", path, lineno) from exc
            if not isinstance(obj, dict):
                raise FatalSettlement("document_unidentifiable_handover",
                                      "line is not a JSON object", path, lineno)
            handover = obj.get("handover")
            if not isinstance(handover, str) or not handover:
                raise FatalSettlement("document_unidentifiable_handover",
                                      "missing or invalid \"handover\"", path, lineno)

            errors = []
            for k in sorted(set(obj) - DOC_FIELDS):
                errors.append(f"unknown_field:{k}")
            from_loc = _need_str(obj, "from", errors)
            to_loc = _need_str(obj, "to", errors)
            if from_loc and to_loc and from_loc == to_loc:
                errors.append("same_origin_destination")
            eff = obj.get("effective")
            effective_us = None
            if not isinstance(eff, str) or not eff:
                errors.append("bad_effective")
            else:
                try:
                    effective_us = _parse_instant(eff, "effective")
                except P.ProfileError:
                    errors.append("bad_effective")
                else:
                    if effective_us <= opening_us:
                        errors.append("effective_not_after_opening")
            scans = obj.get("scans")
            claim_ids = []
            if not isinstance(scans, list) or not scans:
                errors.append("scans_missing_or_empty")
            else:
                seen = set()
                dup = bad = False
                for s in scans:
                    if not isinstance(s, str) or not s:
                        bad = True
                        continue
                    if s in seen:
                        dup = True
                        continue
                    seen.add(s)
                    claim_ids.append(s)
                if dup:
                    errors.append("duplicate_scan_refs")
                if bad:
                    errors.append("non_string_scan_ref")
                if not claim_ids:
                    errors.append("scans_missing_or_empty")

            # canonical identity: scan order and UTC-offset notation are
            # not significant; everything else distinguishes versions
            norm = {
                "from": from_loc,
                "to": to_loc,
                "effective_us": effective_us,
                "effective_raw": None if effective_us is not None
                                 else (eff if isinstance(eff, str) else None),
                "scans": sorted(claim_ids) if claim_ids else None,
                "errors": sorted(errors),
            }
            doc_hash = sha256_bytes(canonical_json(norm).encode("utf-8"))
            cur = conn.execute(
                "INSERT OR IGNORE INTO docs(side,handover,doc_hash,from_loc,to_loc,"
                "effective_us,errors,first_line) VALUES(?,?,?,?,?,?,?,?)",
                (side, handover, doc_hash, from_loc, to_loc, effective_us,
                 json.dumps(sorted(errors)), lineno))
            if cur.rowcount:
                counters[side + "_distinct"] += 1
                for eid in claim_ids:
                    # an identical re-sent document adds no new claim
                    conn.execute(
                        "INSERT OR IGNORE INTO claims(event_id,side,handover)"
                        " VALUES(?,?,?)", (eid, side, handover))


# ---------------------------------------------------------------- scratch db

def _attach_scratch(store):
    fd, path = tempfile.mkstemp(prefix="epcis-settle-", suffix=".sqlite")
    os.close(fd)
    store.conn.execute("ATTACH DATABASE ? AS settle", (path,))
    # the scratch db is a throwaway index: no crash safety needed
    store.conn.execute("PRAGMA settle.journal_mode=MEMORY")
    store.conn.execute("PRAGMA settle.synchronous=OFF")
    store.conn.executescript(SCRATCH_SCHEMA)
    return path


def _detach_scratch(store, path):
    store.conn.commit()
    store.conn.execute("DETACH DATABASE settle")
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


# ---------------------------------------------------------------- verification

class _SideAgg:
    """Goods totalled from one side's scans (never from the form)."""

    __slots__ = ("times", "epc_scans", "qty")

    def __init__(self):
        self.times = []
        self.epc_scans = {}   # epc -> number of this side's scans containing it
        self.qty = {}         # (class, dimension) -> Fraction in base units

    def add(self, payload, event_us):
        self.times.append(event_us)
        for epc in set(payload["epc_list"]):  # one scan = one observation
            self.epc_scans[epc] = self.epc_scans.get(epc, 0) + 1
        for q in payload["quantity_list"]:
            key = (q["class"], q["dimension"])
            self.qty[key] = self.qty.get(key, Fraction(0)) + Fraction(q["base_quantity"])


def _verify_handover(handover, doc, claim_rows, conflicted, opening_us, units, reasons):
    """Evidence verification for a structurally sound handover.

    Appends reason objects; returns (epcs, qty) totalled from the
    sender side when everything verifies, else None.
    """
    per_side = {}
    missing = {s: [] for s in SIDES}
    unusable = {s: [] for s in SIDES}
    for side in SIDES:
        agg = _SideAgg()
        seen = set()
        for row in claim_rows:
            if row["side"] != side:
                continue
            eid = row["event_id"]
            if eid in seen:
                continue  # extra content versions of a conflicted id
            seen.add(eid)
            if row["status"] is None:
                missing[side].append(eid)
                continue
            if eid in conflicted:
                unusable[side].append({"event_id": eid, "why": "conflicted",
                                       "digests": conflicted[eid]})
                continue
            if row["status"] != "ok":
                unusable[side].append({"event_id": eid, "why": "event_not_ok",
                                       "status": row["status"], "detail": row["reason"]})
                continue
            if row["etype"] != "ObjectEvent" or row["action"] != "OBSERVE":
                unusable[side].append({"event_id": eid, "why": "not_observe",
                                       "etype": row["etype"], "action": row["action"]})
                continue
            payload = json.loads(row["payload"])
            if set(payload["sources"]) != {doc["from"]} or \
                    set(payload["destinations"]) != {doc["to"]}:
                unusable[side].append({"event_id": eid, "why": "location_mismatch",
                                       "sources": payload["sources"],
                                       "destinations": payload["destinations"]})
                continue
            if row["event_us"] <= opening_us or row["event_us"] > doc["effective_us"]:
                unusable[side].append({
                    "event_id": eid, "why": "time_out_of_window",
                    "event_time": _iso(row["event_us"]),
                    "window": {"after_opening": _iso(opening_us),
                               "not_after_effective": _iso(doc["effective_us"])}})
                continue
            agg.add(payload, row["event_us"])
        per_side[side] = agg
    for side in SIDES:
        if missing[side]:
            reasons.append({"code": "evidence_missing", "side": side,
                            "event_ids": sorted(missing[side])})
        if unusable[side]:
            reasons.append({"code": "evidence_unusable", "side": side,
                            "items": unusable[side]})
    if reasons:
        return None

    s_agg, r_agg = per_side["sender"], per_side["receiver"]
    for side, agg in per_side.items():
        dups = sorted(e for e, n in agg.epc_scans.items() if n > 1)
        if dups:
            # repeated across one side's scans: one good, not two -- block
            reasons.append({"code": "epc_scanned_twice", "side": side, "epcs": dups})
    if max(s_agg.times) > min(r_agg.times):
        reasons.append({"code": "scan_time_order",
                        "last_sender_scan": _iso(max(s_agg.times)),
                        "first_receiver_scan": _iso(min(r_agg.times))})
    s_epcs, r_epcs = set(s_agg.epc_scans), set(r_agg.epc_scans)
    if s_epcs != r_epcs:
        reasons.append({"code": "goods_mismatch",
                        "named": {"only_sender": sorted(s_epcs - r_epcs),
                                  "only_receiver": sorted(r_epcs - s_epcs)}})
    qty_diffs = []
    for key in sorted(set(s_agg.qty) | set(r_agg.qty)):
        sv = s_agg.qty.get(key, Fraction(0))
        rv = r_agg.qty.get(key, Fraction(0))
        if sv != rv:
            qty_diffs.append({"class": key[0], "dimension": key[1],
                              "sender": fmt_fraction(sv), "receiver": fmt_fraction(rv),
                              "base_unit": units.base_unit(key[1])})
    if qty_diffs:
        reasons.append({"code": "goods_mismatch", "quantities": qty_diffs})
    if reasons:
        return None
    return sorted(s_epcs), s_agg.qty


# ---------------------------------------------------------------- union-find

def _find(parent, x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


# ---------------------------------------------------------------- main run

def _resolve_batch(store, batch):
    if batch == "latest":
        seq = store.latest_batch()
        if seq is None:
            raise FatalSettlement("invalid_batch", "no published batch yet")
        return seq
    try:
        seq = int(batch)
    except (TypeError, ValueError) as exc:
        raise FatalSettlement("invalid_batch", f"not a batch number: {batch!r}") from exc
    row = store.conn.execute(
        "SELECT 1 FROM batches WHERE batch_seq=? AND status='published'", (seq,)).fetchone()
    if row is None:
        raise FatalSettlement("invalid_batch", f"batch {seq} is not a published batch")
    return seq


def run_settlement(store, units, batch, opening_path, sender_path, receiver_path, out_dir):
    """Run one settlement.  Returns (summary_dict, exit_code)."""
    os.makedirs(out_dir, exist_ok=True)
    for name in RESULT_FILES + ("summary.json",):
        try:
            os.remove(os.path.join(out_dir, name))
        except OSError:
            pass
    inputs = {"batch": batch, "opening": opening_path,
              "sender": sender_path, "receiver": receiver_path}
    try:
        batch_seq = _resolve_batch(store, batch)
        inputs["batch"] = batch_seq
        opening_us, epc_owner, qty_balance = load_opening(opening_path, units)
        inputs["opening_as_of"] = _iso(opening_us)
        scratch_path = _attach_scratch(store)
        try:
            counters = {"sender_lines": 0, "receiver_lines": 0,
                        "sender_distinct": 0, "receiver_distinct": 0}
            load_documents(sender_path, "sender", opening_us, store.conn, counters)
            load_documents(receiver_path, "receiver", opening_us, store.conn, counters)
            store.conn.commit()
            counts = _verify_and_post(store, units, batch_seq, opening_us,
                                      epc_owner, qty_balance, out_dir)
            counters.update(counts)
        finally:
            _detach_scratch(store, scratch_path)
        summary = {"status": "settled", "inputs": inputs, "counts": counters,
                   "outputs": {name.split(".")[0]: name for name in RESULT_FILES}}
        code = 0
    except FatalSettlement as exc:
        summary = {"status": "failed",
                   "error": {"code": exc.code, "detail": exc.detail,
                             "file": exc.file, "line": exc.line},
                   "inputs": inputs}
        code = 2
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    return summary, code


def _verify_and_post(store, units, batch_seq, opening_us, epc_owner, qty_balance, out_dir):
    conn = store.conn

    # ---- conflicted event ids visible in this batch view (streamed)
    conflicted = {}
    for row in conn.execute(
            "SELECT event_id, GROUP_CONCAT(content_digest) AS ds FROM events"
            " WHERE event_id IS NOT NULL AND status != 'uninterpretable'"
            " AND first_batch<=?"
            " GROUP BY event_id HAVING COUNT(DISTINCT content_digest) > 1",
            (batch_seq,)):
        conflicted[row["event_id"]] = sorted(row["ds"].split(","))

    # ---- event ids claimed by more than one (handover, side)
    reused = {}
    cur = conn.execute(
        "SELECT c.event_id, c.handover, c.side FROM settle.claims c"
        " JOIN (SELECT event_id FROM settle.claims GROUP BY event_id"
        "       HAVING COUNT(*) > 1) r ON r.event_id = c.event_id"
        " ORDER BY c.event_id, c.handover, c.side")
    for eid, rows in _grouped(cur):
        claimants = [(r["handover"], r["side"]) for r in rows]
        for h, s in claimants:
            reused.setdefault(h, []).append({
                "event_id": eid,
                "also_claimed_by": [{"handover": h2, "side": s2}
                                    for h2, s2 in claimants if (h2, s2) != (h, s)]})

    # ---- verification pass over all handovers (both streams ordered)
    docs_cur = conn.execute(
        "SELECT handover, side, doc_hash, from_loc, to_loc, effective_us, errors,"
        " first_line FROM settle.docs ORDER BY handover, side")
    claims_cur = conn.execute(
        "SELECT c.handover, c.side, c.event_id, e.status, e.reason, e.etype,"
        " e.action, e.event_us, e.payload"
        " FROM settle.claims c LEFT JOIN events e"
        "   ON e.event_id = c.event_id AND e.first_batch<=?"
        " ORDER BY c.handover, c.side, c.event_id", (batch_seq,))
    claims_it = _grouped(claims_cur)
    claims_next = next(claims_it, None)

    out_buf, eli_buf, epc_buf, qty_buf = [], [], [], []

    def flush():
        if out_buf:
            conn.executemany(
                "INSERT OR REPLACE INTO settle.outcomes(handover,status,reasons,group_id)"
                " VALUES(?,?,?,?)", out_buf)
            out_buf.clear()
        if eli_buf:
            conn.executemany(
                "INSERT INTO settle.eligible(handover,from_loc,to_loc,effective_us)"
                " VALUES(?,?,?,?)", eli_buf)
            eli_buf.clear()
        if epc_buf:
            conn.executemany(
                "INSERT INTO settle.eligible_epcs(handover,epc) VALUES(?,?)", epc_buf)
            epc_buf.clear()
        if qty_buf:
            conn.executemany(
                "INSERT INTO settle.eligible_qty(handover,cls,dim,qty) VALUES(?,?,?,?)",
                qty_buf)
            qty_buf.clear()

    n_handover = 0
    for handover, doc_rows in _grouped(docs_cur):
        n_handover += 1
        claim_rows = []
        while claims_next is not None and claims_next[0] < handover:
            claims_next = next(claims_it, None)  # cannot happen: claims subset of docs
        if claims_next is not None and claims_next[0] == handover:
            claim_rows = claims_next[1]
            claims_next = next(claims_it, None)

        versions = {s: [r for r in doc_rows if r["side"] == s] for s in SIDES}
        reasons = []
        for side in SIDES:
            vs = versions[side]
            if not vs:
                reasons.append({"code": f"missing_{side}"})
            elif len(vs) > 1:
                reasons.append({"code": "conflicting_documents", "side": side,
                                "versions": len(vs),
                                "doc_hashes": sorted(v["doc_hash"] for v in vs),
                                "lines": sorted(v["first_line"] for v in vs)})
        if len(versions["sender"]) == 1 and len(versions["receiver"]) == 1:
            s, r = versions["sender"][0], versions["receiver"][0]
            s_err = json.loads(s["errors"])
            r_err = json.loads(r["errors"])
            for side, v, errs in (("sender", s, s_err), ("receiver", r, r_err)):
                if errs:
                    reasons.append({"code": "invalid_document", "side": side,
                                    "line": v["first_line"], "errors": errs})
            if not s_err and not r_err:
                if (s["from_loc"], s["to_loc"], s["effective_us"]) != \
                        (r["from_loc"], r["to_loc"], r["effective_us"]):
                    reasons.append({
                        "code": "mismatch",
                        "sender": {"from": s["from_loc"], "to": s["to_loc"],
                                   "effective": _iso(s["effective_us"]),
                                   "line": s["first_line"]},
                        "receiver": {"from": r["from_loc"], "to": r["to_loc"],
                                     "effective": _iso(r["effective_us"]),
                                     "line": r["first_line"]}})
        if handover in reused:
            reasons.append({"code": "scan_reused",
                            "claims": sorted(reused[handover],
                                             key=lambda c: c["event_id"])})

        if not reasons:
            doc = {"from": versions["sender"][0]["from_loc"],
                   "to": versions["sender"][0]["to_loc"],
                   "effective_us": versions["sender"][0]["effective_us"]}
            goods = _verify_handover(handover, doc, claim_rows, conflicted,
                                     opening_us, units, reasons)
            if goods is not None:
                epcs, qty = goods
                eli_buf.append((handover, doc["from"], doc["to"], doc["effective_us"]))
                epc_buf.extend((handover, e) for e in epcs)
                qty_buf.extend((handover, cls, dim, fmt_fraction(amount))
                               for (cls, dim), amount in sorted(qty.items()))
                out_buf.append((handover, "eligible", "[]", None))
        if reasons:
            out_buf.append((handover, "failed", json.dumps(reasons, sort_keys=True), None))
        if len(out_buf) >= 8192:
            flush()
    flush()
    conn.commit()

    # ---- posting pass: one effective instant at a time
    ledger_fh = open(os.path.join(out_dir, "ledger.jsonl"), "w", encoding="utf-8")
    groups_fh = open(os.path.join(out_dir, "groups.jsonl"), "w", encoding="utf-8")
    counts = {"handovers": n_handover, "posted": 0, "failed": 0,
              "groups_posted": 0, "groups_blocked": 0}
    touched_qty = set(qty_balance)
    try:
        times = [r[0] for r in conn.execute(
            "SELECT DISTINCT effective_us FROM settle.eligible ORDER BY effective_us")]
        for t_us in times:
            handovers, from_locs, to_locs = [], [], []
            for r in conn.execute(
                    "SELECT handover, from_loc, to_loc FROM settle.eligible"
                    " WHERE effective_us=? ORDER BY handover", (t_us,)):
                handovers.append(sys.intern(r[0]))
                from_locs.append(sys.intern(r[1]))
                to_locs.append(sys.intern(r[2]))
            n = len(handovers)
            idx_of = {h: i for i, h in enumerate(handovers)}
            doc_epcs = [[] for _ in range(n)]
            for h, epc in conn.execute(
                    "SELECT handover, epc FROM settle.eligible_epcs WHERE handover IN"
                    " (SELECT handover FROM settle.eligible WHERE effective_us=?)"
                    " ORDER BY handover, epc", (t_us,)):
                doc_epcs[idx_of[h]].append(sys.intern(epc))
            doc_qtys = [[] for _ in range(n)]
            for h, cls, dim, q in conn.execute(
                    "SELECT handover, cls, dim, qty FROM settle.eligible_qty"
                    " WHERE handover IN"
                    " (SELECT handover FROM settle.eligible WHERE effective_us=?)"
                    " ORDER BY handover", (t_us,)):
                doc_qtys[idx_of[h]].append((sys.intern(cls), sys.intern(dim), Fraction(q)))

            # connect handovers sharing a named EPC or a quantity key on
            # either the debit or the credit side (transitively)
            parent = array("i", range(n))
            first_epc, first_key = {}, {}
            for i in range(n):
                for epc in doc_epcs[i]:
                    j = first_epc.setdefault(epc, i)
                    if j != i:
                        ra, rb = _find(parent, i), _find(parent, j)
                        if ra != rb:
                            parent[max(ra, rb)] = min(ra, rb)
                for cls, dim, _amt in doc_qtys[i]:
                    for loc in (from_locs[i], to_locs[i]):
                        j = first_key.setdefault((loc, cls, dim), i)
                        if j != i:
                            ra, rb = _find(parent, i), _find(parent, j)
                            if ra != rb:
                                parent[max(ra, rb)] = min(ra, rb)
            comps = {}
            for i in range(n):
                comps.setdefault(_find(parent, i), []).append(i)
            comp_list = sorted(comps.values(), key=lambda members: members[0])

            for gno, members in enumerate(comp_list, 1):
                gid = f"{_iso(t_us)}#{gno}"
                # named checks: owned by the sender at the start of the
                # instant, never transferred twice inside the group
                epc_docs = {}
                for i in members:
                    for epc in doc_epcs[i]:
                        epc_docs.setdefault(epc, []).append(i)
                blocking_epcs = []
                for epc in sorted(epc_docs):
                    owners = epc_docs[epc]
                    if len(owners) > 1:
                        blocking_epcs.append({
                            "epc": epc, "reason": "transferred_twice_in_group",
                            "documents": sorted(handovers[i] for i in owners)})
                    else:
                        i = owners[0]
                        owner = epc_owner.get(epc)
                        if owner != from_locs[i]:
                            blocking_epcs.append({
                                "epc": epc, "reason": "not_owned_by_sender",
                                "owner": owner, "required": from_locs[i],
                                "document": handovers[i]})
                # quantity checks: the group's total debit per key against
                # the balance at the start of the instant (unlisted = zero);
                # same-instant credits never cover debits
                debit = {}
                for i in members:
                    for cls, dim, amt in doc_qtys[i]:
                        key = (from_locs[i], cls, dim)
                        debit[key] = debit.get(key, Fraction(0)) + amt
                blocking_qty = []
                for (loc, cls, dim) in sorted(debit):
                    need = debit[(loc, cls, dim)]
                    avail = qty_balance.get((loc, cls, dim), Fraction(0))
                    if need > avail:
                        blocking_qty.append({
                            "location": loc, "class": cls, "dimension": dim,
                            "required": fmt_fraction(need),
                            "available": fmt_fraction(avail),
                            "base_unit": units.base_unit(dim)})
                if blocking_epcs or blocking_qty:
                    counts["groups_blocked"] += 1
                    groups_fh.write(canonical_json({
                        "group": gid, "effective": _iso(t_us), "status": "blocked",
                        "member_count": len(members),
                        "blocking": {"epcs": blocking_epcs,
                                     "quantities": blocking_qty}}) + "\n")
                    conn.executemany(
                        "UPDATE settle.outcomes SET status='group_blocked',"
                        " reasons=?, group_id=? WHERE handover=?",
                        [(json.dumps([{"code": "group_blocked", "group": gid}]),
                          gid, handovers[i]) for i in members])
                else:
                    counts["groups_posted"] += 1
                    groups_fh.write(canonical_json({
                        "group": gid, "effective": _iso(t_us), "status": "posted",
                        "member_count": len(members)}) + "\n")
                    for i in members:
                        h = handovers[i]
                        for epc in doc_epcs[i]:
                            epc_owner[epc] = to_locs[i]
                        moves = []
                        for cls, dim, amt in doc_qtys[i]:
                            kf = (from_locs[i], cls, dim)
                            kt = (to_locs[i], cls, dim)
                            qty_balance[kf] = qty_balance.get(kf, Fraction(0)) - amt
                            qty_balance[kt] = qty_balance.get(kt, Fraction(0)) + amt
                            touched_qty.add(kf)
                            touched_qty.add(kt)
                            moves.append({"class": cls, "dimension": dim,
                                          "quantity": fmt_fraction(amt),
                                          "base_unit": units.base_unit(dim),
                                          "from": from_locs[i], "to": to_locs[i]})
                        s_scans, r_scans = [], []
                        for side, eid in conn.execute(
                                "SELECT side, event_id FROM settle.claims"
                                " WHERE handover=? ORDER BY side, event_id", (h,)):
                            (s_scans if side == "sender" else r_scans).append(eid)
                        ledger_fh.write(canonical_json({
                            "handover": h, "effective": _iso(t_us),
                            "from": from_locs[i], "to": to_locs[i], "group": gid,
                            "named_moves": [{"epc": e, "from": from_locs[i],
                                             "to": to_locs[i]}
                                            for e in doc_epcs[i]],
                            "quantity_moves": moves,
                            "evidence": {"sender_scans": s_scans,
                                         "receiver_scans": r_scans}}) + "\n")
                    conn.executemany(
                        "UPDATE settle.outcomes SET status='posted', group_id=?"
                        " WHERE handover=?",
                        [(gid, handovers[i]) for i in members])
    finally:
        ledger_fh.close()
        groups_fh.close()

    # ---- per-document results
    with open(os.path.join(out_dir, "documents.jsonl"), "w", encoding="utf-8") as fh:
        for row in conn.execute(
                "SELECT handover, status, reasons, group_id FROM settle.outcomes"
                " ORDER BY handover"):
            status = "posted" if row["status"] == "posted" else "failed"
            if status == "posted":
                counts["posted"] += 1
            else:
                counts["failed"] += 1
            fh.write(canonical_json({
                "handover": row["handover"], "status": status,
                "reasons": json.loads(row["reasons"]),
                "group": row["group_id"]}) + "\n")

    # ---- closing balances (opening keys plus everything posted movements touched)
    with open(os.path.join(out_dir, "balances.jsonl"), "w", encoding="utf-8") as fh:
        for epc in sorted(epc_owner):
            fh.write(canonical_json({"kind": "epc", "epc": epc,
                                     "location": epc_owner[epc]}) + "\n")
        for loc, cls, dim in sorted(touched_qty):
            fh.write(canonical_json({
                "kind": "quantity", "location": loc, "class": cls, "dimension": dim,
                "quantity": fmt_fraction(qty_balance.get((loc, cls, dim), Fraction(0))),
                "base_unit": units.base_unit(dim)}) + "\n")
    return counts
