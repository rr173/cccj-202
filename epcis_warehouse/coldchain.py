"""Offline cold-chain exposure ledger (冷链暴露台账).

One run takes a published receiving batch, a half-open inspection window
[start, end), the three artefacts of one settle run (opening inventory,
settlement summary, ledger.jsonl) and three input lists (inspected
EPCs, probe installations, probe samples) and builds, fully offline:

  * per-EPC temperature coverage detail: the whole window cut into
    non-overlapping half-open segments without gaps, each segment
    carrying ownership, a temperature or an explicit unknown reason and
    traceable evidence;
  * a per-EPC / UTC-date / ownership daily account of normal, over-limit
    and each-unknown-reason durations;
  * over-limit alarms: maximal runs of consecutive over-limit segments
    whose in-window duration reaches the EPC's own alarm threshold, with
    the over-limit time split by ownership (unknown ownership included).

Nothing unmeasured is ever reported as acceptable.  Unknown reasons are
kept distinct:

  not_installed      no probe installed on any chain container
  not_sampled        a probe is mounted but no valid sample exists yet
  expired            the latest reading's validity has run out
  sample_conflict    same-instant samples of the same probe disagree
  install_conflict   more than one probe mounted on the chosen container
  chain_unknown      the packaging chain is ambiguous, cyclic or blocked
                     by a conflicted packaging event

The event warehouse is read only.  Streaming inputs (samples in
particular) are indexed in a scratch SQLite database attached for the
run and deleted afterwards; the full sample batch is never held in
memory.  Only time changes that can affect an inspected EPC are
expanded: the packaging replay tracks just the inspected EPCs and their
ancestor containers, and per-EPC sweeps only touch probes installed on
that EPC's chain containers.

Fatal input-contract errors produce only a failed summary.json (exit
code 2); no coverage/daily/alarm files are delivered.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

from . import parse as P
from .evidence import canonical_json, sha256_bytes

RESULT_FILES = ("coverage.jsonl", "daily.jsonl", "alarms.jsonl", "evidence.jsonl")

# coverage segment states
ST_NORMAL = "normal"
ST_OVER = "over_limit"
UNKNOWN_REASONS = (
    "not_installed",
    "not_sampled",
    "expired",
    "sample_conflict",
    "install_conflict",
    "chain_unknown",
)

SCRATCH_SCHEMA = """
CREATE TABLE cold.items(
  epc TEXT PRIMARY KEY,
  limit_mc INTEGER NOT NULL,
  alarm_us INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE cold.installations(
  inst_id TEXT PRIMARY KEY,
  probe TEXT NOT NULL,
  container TEXT NOT NULL,
  from_us INTEGER NOT NULL,
  to_us INTEGER NOT NULL,
  valid_for_us INTEGER NOT NULL,
  line INTEGER NOT NULL,
  raw TEXT NOT NULL
);
CREATE INDEX cold.idx_inst_container ON installations(container, from_us, to_us);
CREATE INDEX cold.idx_inst_probe ON installations(probe, from_us, to_us);
CREATE TABLE cold.samples_raw(
  probe TEXT NOT NULL,
  sample_us INTEGER NOT NULL,
  value_mc INTEGER NOT NULL,
  line INTEGER NOT NULL
);
CREATE INDEX cold.idx_raw_probe_time ON samples_raw(probe, sample_us);
CREATE TABLE cold.samples(
  probe TEXT NOT NULL,
  sample_us INTEGER NOT NULL,
  status TEXT NOT NULL,           -- conflict | ok
  values_json TEXT NOT NULL,     -- sorted distinct milli-Celsius values
  lines_json TEXT NOT NULL
);
CREATE INDEX cold.idx_samples_probe ON samples(probe, sample_us);
CREATE TABLE cold.sample_scan(
  probe TEXT NOT NULL,
  sample_us INTEGER NOT NULL,
  value_mc INTEGER,              -- NULL for a conflict row
  inst_id TEXT,                  -- NULL when outside every installation
  status TEXT NOT NULL,          -- ok | conflict | outside_install
  line INTEGER NOT NULL
);
CREATE INDEX cold.idx_scan_probe_time ON sample_scan(probe, sample_us);
CREATE TABLE cold.chains(
  chain_id TEXT PRIMARY KEY,       -- content-derived hash, input-order free
  status TEXT NOT NULL,          -- complete | unknown | cycle
  nodes_json TEXT NOT NULL,      -- bottom-up node ids (partial on unknown/cycle)
  block_event TEXT,
  detail_json TEXT NOT NULL
);
CREATE TABLE cold.chain_intervals(
  epc TEXT NOT NULL,
  from_us INTEGER NOT NULL,
  to_us INTEGER NOT NULL,
  chain_id INTEGER NOT NULL,
  block_event TEXT
);
CREATE INDEX cold.idx_chain_epc ON chain_intervals(epc, from_us);
CREATE TABLE cold.daily_acc(
  epc TEXT NOT NULL,
  day TEXT NOT NULL,
  owner_key TEXT NOT NULL,      -- internal sentinel for NULL ownership
  state TEXT NOT NULL,
  duration_us INTEGER NOT NULL,
  PRIMARY KEY(epc, day, owner_key, state)
) WITHOUT ROWID;
CREATE TABLE cold.evidence_refs(
  ref TEXT PRIMARY KEY,
  json TEXT NOT NULL
);
"""

# bounds on in-memory caches during the per-EPC sweep
PROBE_CACHE_MAX = 2048
CONTAINER_CACHE_MAX = 4096

# internal sentinel for unknown ownership inside NOT NULL scratch columns;
# always rendered back to JSON null on output
UNKNOWN_OWNER_KEY = "\0unknown-owner"


class FatalColdChain(ValueError):
    """Inputs violate the contract: no ledger may be delivered."""

    def __init__(self, code, detail, file=None, line=None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.file = file
        self.line = line


# ---------------------------------------------------------------- small helpers

def iso(us):
    sec, micro = divmod(int(us), 1_000_000)
    dt = datetime.fromtimestamp(sec, tz=timezone.utc)
    return dt.replace(microsecond=micro).isoformat()


def day_of(us):
    return iso(us)[:10]


def next_midnight_after(us):
    """First UTC midnight strictly after the given instant (microseconds)."""
    sec, _micro = divmod(int(us), 1_000_000)
    return ((sec // 86400) + 1) * 86400 * 1_000_000


def parse_instant(text, what):
    dt = P._parse_time(text, what)  # ProfileError on bad shape/missing offset
    return P._epoch_us(P._to_utc(dt))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_jsonl(path, label):
    """Yield (lineno, obj) for a JSONL file. Fatal on unreadable/bad JSON."""
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as exc:
        raise FatalColdChain("input_unreadable", f"{label}: {exc}", path) from exc
    with fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FatalColdChain("invalid_jsonl", f"{label} line {lineno}: {exc}",
                                     path, lineno) from exc
            if not isinstance(obj, dict):
                raise FatalColdChain("invalid_jsonl",
                                     f"{label} line {lineno}: not a JSON object",
                                     path, lineno)
            yield lineno, obj


def _need_str(obj, field, errors):
    v = obj.get(field)
    if not isinstance(v, str) or not v:
        errors.append(f"bad_{field}")
        return None
    return v


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------- input loading

def load_items(path):
    """Inspected EPC list: epc -> (limit_mc, alarm_us)."""
    items = {}
    for lineno, obj in read_jsonl(path, "items"):
        errs = []
        epc = _need_str(obj, "epc", errs)
        limit = obj.get("limit_mc")
        alarm_s = obj.get("alarm_seconds")
        if not _is_int(limit):
            errs.append("bad_limit_mc")
        if not _is_int(alarm_s) or alarm_s <= 0:
            errs.append("bad_alarm_seconds")
        unknown = sorted(set(obj) - {"epc", "limit_mc", "alarm_seconds"})
        if unknown:
            errs.append("unknown_field:" + ",".join(unknown))
        if errs:
            raise FatalColdChain("invalid_item", "; ".join(errs), path, lineno)
        if epc in items:
            raise FatalColdChain("duplicate_item", f"EPC {epc!r} listed twice",
                                 path, lineno)
        items[epc] = (limit, alarm_s * 1_000_000)
    if not items:
        raise FatalColdChain("empty_items", "no inspected EPC listed", path)
    return items


def load_installations(path, conn):
    """Stream installations into scratch.

    Exactly identical installation records of one probe count once.
    Different installation segments of one probe may abut but must not
    overlap (even on the same container): otherwise it cannot be told
    which validity window applies, and the whole input fails.
    """
    seen_identical = set()
    per_probe = {}
    n = 0
    for lineno, obj in read_jsonl(path, "installations"):
        errs = []
        probe = _need_str(obj, "probe", errs)
        container = _need_str(obj, "container", errs)
        valid_s = obj.get("valid_for_seconds")
        if not _is_int(valid_s) or valid_s <= 0:
            errs.append("bad_valid_for_seconds")
        from_us = to_us = None
        frm = obj.get("from")
        if not isinstance(frm, str) or not frm:
            errs.append("bad_from")
        else:
            try:
                from_us = parse_instant(frm, "from")
            except P.ProfileError:
                errs.append("bad_from")
        to = obj.get("to")
        if not isinstance(to, str) or not to:
            errs.append("bad_to")
        else:
            try:
                to_us = parse_instant(to, "to")
            except P.ProfileError:
                errs.append("bad_to")
        unknown = sorted(set(obj) - {"probe", "container", "from", "to",
                                     "valid_for_seconds"})
        if unknown:
            errs.append("unknown_field:" + ",".join(unknown))
        if errs:
            raise FatalColdChain("invalid_installation", "; ".join(errs),
                                 path, lineno)
        if to_us <= from_us:
            raise FatalColdChain("invalid_installation_range",
                                 "installation to must be after from", path, lineno)
        identity = (probe, container, from_us, to_us, valid_s)
        if identity in seen_identical:
            continue
        seen_identical.add(identity)
        for a, b in per_probe.get(probe, ()):
            if from_us < b and a < to_us:
                raise FatalColdChain(
                    "installation_overlap",
                    f"probe {probe!r}: overlapping installation segments "
                    f"[{iso(a)}, {iso(b)}) and [{iso(from_us)}, {iso(to_us)})",
                    path, lineno)
        per_probe.setdefault(probe, []).append((from_us, to_us))
        n += 1
        # content-derived id: stable under input reordering and identical
        # re-sends (which were already de-duplicated above)
        inst_id = "inst-" + sha256_bytes(canonical_json(
            {"probe": probe, "container": container, "from_us": from_us,
             "to_us": to_us, "valid_for_us": valid_s * 1_000_000})
            .encode("utf-8"))[:16]
        conn.execute(
            "INSERT INTO cold.installations(inst_id,probe,container,from_us,to_us,"
            "valid_for_us,line,raw) VALUES(?,?,?,?,?,?,?,?)",
            (inst_id, probe, container, from_us, to_us,
             valid_s * 1_000_000, lineno, canonical_json(obj)))
    if n == 0:
        raise FatalColdChain("empty_installations", "no installation listed", path)
    conn.commit()
    return n


def load_samples(path, conn):
    """Stream samples into scratch (the full batch is never held in memory).

    Same probe + instant + value repeated is one measurement; different
    values at one instant are kept as an explicit conflict, never picked
    between.
    """
    n_lines = 0
    for lineno, obj in read_jsonl(path, "samples"):
        errs = []
        probe = _need_str(obj, "probe", errs)
        value = obj.get("value_mc")
        if not _is_int(value):
            errs.append("bad_value_mc")
        at_us = None
        at = obj.get("at")
        if not isinstance(at, str) or not at:
            errs.append("bad_at")
        else:
            try:
                at_us = parse_instant(at, "at")
            except P.ProfileError:
                errs.append("bad_at")
        unknown = sorted(set(obj) - {"probe", "at", "value_mc"})
        if unknown:
            errs.append("unknown_field:" + ",".join(unknown))
        if errs:
            raise FatalColdChain("invalid_sample", "; ".join(errs), path, lineno)
        n_lines += 1
        conn.execute(
            "INSERT INTO cold.samples_raw(probe,sample_us,value_mc,line)"
            " VALUES(?,?,?,?)", (probe, at_us, value, lineno))
    if n_lines == 0:
        raise FatalColdChain("empty_samples", "no sample listed", path)
    conn.execute(
        "INSERT INTO cold.samples(probe,sample_us,status,values_json,lines_json)"
        " SELECT probe, sample_us,"
        "   CASE WHEN COUNT(DISTINCT value_mc)=1 THEN 'ok' ELSE 'conflict' END,"
        "   json_group_array(DISTINCT value_mc),"
        "   json_group_array(DISTINCT line)"
        " FROM (SELECT probe, sample_us, value_mc, line FROM cold.samples_raw"
        "       ORDER BY probe, sample_us, value_mc, line)"
        " GROUP BY probe, sample_us")
    conn.commit()
    return n_lines


def annotate_samples(conn):
    """Attach every folded sample to at most one installation of its probe.

    Fully set-based SQL: a sample is usable only inside one installation
    (half-open: a sample taken exactly at removal belongs to no
    installation).  Overlapping installations are already fatal, so the
    correlated lookup matches at most one row.  Samples with no active
    installation stay outside_install forever and can never become valid
    when a probe is mounted later.
    """
    conn.execute(
        "INSERT INTO cold.sample_scan(probe,sample_us,value_mc,inst_id,status,line)"
        " SELECT s.probe, s.sample_us,"
        "   CASE WHEN s.status='ok'"
        "       THEN json_extract(s.values_json,'$[0]') ELSE NULL END,"
        "   (SELECT i.inst_id FROM cold.installations i"
        "    WHERE i.probe=s.probe AND i.from_us<=s.sample_us"
        "      AND s.sample_us<i.to_us LIMIT 1),"
        "   CASE WHEN (SELECT i.inst_id FROM cold.installations i"
        "               WHERE i.probe=s.probe AND i.from_us<=s.sample_us"
        "                 AND s.sample_us<i.to_us LIMIT 1) IS NULL"
        "       THEN 'outside_install' ELSE s.status END,"
        "   json_extract(s.lines_json,'$[0]')"
        " FROM cold.samples s")
    n_outside = conn.execute(
        "SELECT COUNT(*) AS n FROM cold.sample_scan"
        " WHERE status='outside_install'").fetchone()["n"]
    conn.commit()
    return n_outside


# ---------------------------------------------------------------- settle inputs

def load_settle_inputs(units, opening_path, summary_path, ledger_path,
                       batch_seq, window_start_us):
    """Validate the three settle artefacts and build ownership timelines.

    Ownership comes only from named_moves of successfully posted ledger
    lines (the settle output only contains posted lines), switching at
    the posting effective instant.  Unposted documents, closing
    balances, packaging holders and scan instants are never used.
    EPCs absent from the opening inventory may still be inspected; their
    ownership stays unknown unless a consistent ledger flow exists.
    """
    from .settle import load_opening
    opening_us, epc_owner, _qty = load_opening(opening_path, units)
    if opening_us > window_start_us:
        raise FatalColdChain(
            "window_before_opening",
            f"window starts {iso(window_start_us)} before opening {iso(opening_us)}",
            opening_path)

    try:
        with open(summary_path, "r", encoding="utf-8") as fh:
            summary = json.load(fh)
    except OSError as exc:
        raise FatalColdChain("input_unreadable", f"summary: {exc}",
                             summary_path) from exc
    except json.JSONDecodeError as exc:
        raise FatalColdChain("invalid_summary", f"not JSON: {exc}",
                             summary_path) from exc
    if not isinstance(summary, dict):
        raise FatalColdChain("invalid_summary", "not an object", summary_path)
    if summary.get("status") != "settled":
        raise FatalColdChain("summary_not_settled",
                             f"settle summary status is {summary.get('status')!r}",
                             summary_path)
    inputs = summary.get("inputs")
    if not isinstance(inputs, dict):
        raise FatalColdChain("invalid_summary", "missing inputs", summary_path)
    if inputs.get("batch") != batch_seq:
        raise FatalColdChain(
            "batch_mismatch",
            f"summary batch {inputs.get('batch')!r} != requested {batch_seq}",
            summary_path)
    if inputs.get("opening_as_of") != iso(opening_us):
        raise FatalColdChain(
            "opening_mismatch",
            f"summary opening {inputs.get('opening_as_of')!r} != opening file "
            f"{iso(opening_us)}", summary_path)

    # epc -> [(effective_us, from, to, handover, group, line)]
    moves = {}
    for lineno, obj in read_jsonl(ledger_path, "ledger"):
        for field in ("effective", "handover", "group", "from", "to", "named_moves"):
            if field not in obj:
                raise FatalColdChain("invalid_ledger_line",
                                     f"missing field {field!r}", ledger_path, lineno)
        try:
            eff_us = parse_instant(obj["effective"], "effective")
        except P.ProfileError as exc:
            raise FatalColdChain("invalid_ledger_line", f"effective: {exc.reason}",
                                 ledger_path, lineno) from exc
        if eff_us <= opening_us:
            raise FatalColdChain("invalid_ledger_line",
                                 "ledger effective not after opening",
                                 ledger_path, lineno)
        named = obj.get("named_moves")
        if not isinstance(named, list):
            raise FatalColdChain("invalid_ledger_line", "named_moves not a list",
                                 ledger_path, lineno)
        for mv in named:
            if not isinstance(mv, dict) or set(mv) != {"epc", "from", "to"}:
                raise FatalColdChain("invalid_ledger_line", "bad named_move",
                                     ledger_path, lineno)
            epc, frm, to = mv["epc"], mv["from"], mv["to"]
            if not all(isinstance(x, str) and x for x in (epc, frm, to)) or frm == to:
                raise FatalColdChain("invalid_ledger_line",
                                     "bad named_move endpoints", ledger_path, lineno)
            moves.setdefault(epc, []).append(
                (eff_us, frm, to, obj["handover"], obj["group"], lineno))

    owner_at = dict(epc_owner)
    for epc, rows in moves.items():
        rows.sort(key=lambda r: (r[0], r[5], r[4]))
        for eff_us, frm, to, handover, _group, lineno in rows:
            current = owner_at.get(epc)
            if current is None:
                raise FatalColdChain(
                    "ownership_flow_conflict",
                    f"ledger moves {epc!r} ({handover!r}) but it is not in the "
                    "opening inventory", ledger_path, lineno)
            if current != frm:
                raise FatalColdChain(
                    "ownership_flow_conflict",
                    f"{epc!r}: handover {handover!r} posts from {frm!r} but the "
                    f"flow currently owns it at {current!r}", ledger_path, lineno)
            owner_at[epc] = to
    return opening_us, epc_owner, moves


# ---------------------------------------------------------------- scratch db

def attach_scratch(store):
    fd, path = tempfile.mkstemp(prefix="epcis-coldchain-", suffix=".sqlite")
    os.close(fd)
    store.conn.execute("ATTACH DATABASE ? AS cold", (path,))
    store.conn.execute("PRAGMA cold.journal_mode=MEMORY")
    store.conn.execute("PRAGMA cold.synchronous=OFF")
    store.conn.executescript(SCRATCH_SCHEMA)
    return path


def detach_scratch(store, path):
    store.conn.commit()
    store.conn.execute("DETACH DATABASE cold")
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


# ---------------------------------------------------------------- packaging replay

def _conflicted_aggregations(store, batch_seq, t_max_us):
    """event_id -> record for conflicted AggregationEvents visible in the view.

    Only packaging events can block a temperature chain; a conflicted
    ObjectEvent scan never changes packaging and cannot block one.
    """
    conflicted = store.conflicted_event_ids(batch_seq)
    if not conflicted:
        return {}
    marks = ",".join("?" for _ in conflicted)
    out = {}
    rows = store.conn.execute(
        f"SELECT event_id, payload, event_us FROM events"
        f" WHERE status='ok' AND etype='AggregationEvent' AND event_us<=?"
        f"  AND event_id IN ({marks})"
        f" ORDER BY event_us, event_id",
        [t_max_us, *sorted(conflicted)])
    for row in rows:
        p = json.loads(row["payload"])
        subjects = set(p.get("child_epcs", ()))
        if p.get("parent_id"):
            subjects.add(p["parent_id"])
        out[row["event_id"]] = {"event_us": row["event_us"],
                                "digests": conflicted[row["event_id"]],
                                "subjects": subjects}
    return out


def _descendants(seeds, children):
    out, stack, seen = set(), list(seeds), set()
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        out.add(node)
        stack.extend(children.get(node, ()))
    return out


def build_chain_intervals(store, conn, batch_seq, window_start_us, window_end_us,
                          inspected):
    """Replay packaging once; materialise per-inspected-EPC chain intervals.

    Warehouse semantics are reused exactly (all changes at one eventTime
    take effect together, DELETEs before ADDs, double-parent ambiguity,
    idempotent same-edge ADD).  Chains are snapshotted only for inspected
    EPCs touched by a packaging instant or a conflicted packaging event;
    identical chain contents share one chains row (fan-out: one pallet
    signal serving many goods costs one row).
    """
    conflicted_ids = set(store.conflicted_event_ids(batch_seq))
    blocks = _conflicted_aggregations(store, batch_seq, window_end_us)

    parent = {}          # child -> (parent_node, since_us, by_event)
    children = {}        # node -> set(direct children)
    child_unknown = {}   # child -> record (double parent)
    blocked = {}         # node -> earliest block record
    for eid, rec in sorted(blocks.items(), key=lambda kv: (kv[1]["event_us"], kv[0])):
        for node in rec["subjects"]:
            blocked.setdefault(node, {"event": eid, **rec})

    chain_cache = {}     # (status, tuple(nodes), block_event, detail key) -> cid
    raw_intervals = []   # (epc, from_us, to_us, cid, block_event)
    heads = {}           # epc -> [from_us, chain_id, block_event]
    initial = {}         # epc -> cid of the state before its first in-replay touch

    def evaluate(epc):
        """Chain state for epc under the current in-memory packaging state."""
        nodes, seen = [], {}
        node = epc
        while True:
            if node in blocked:
                rec = blocked[node]
                return ("unknown", tuple(nodes), rec["event"],
                        ("block", rec["event"], rec["digests"]))
            if node in child_unknown:
                rec = child_unknown[node]
                return ("unknown", tuple(nodes), None,
                        ("double_parent", rec["reason"], rec["parents"],
                         rec["events"]))
            edge = parent.get(node)
            if edge is None:
                return ("complete", tuple(nodes + [node]) if nodes else (epc,),
                        None, None)
            if node in seen:
                cyc = [c for c in nodes[seen[node]:]]
                events = sorted({parent[c][2] for c in cyc})
                return ("cycle", tuple(nodes), None,
                        ("cycle", cyc, events))
            seen[node] = len(nodes)
            nodes.append(node)
            node = edge[0]

    def chain_id(epc):
        status, nodes, block_event, detail = evaluate(epc)
        key = (status, nodes, block_event, canonical_json(detail))
        cid = chain_cache.get(key)
        if cid is None:
            cid = "chain-" + sha256_bytes(canonical_json({
                "status": status, "nodes": list(nodes),
                "block_event": block_event,
                "detail": detail}).encode("utf-8"))[:16]
            conn.execute(
                "INSERT OR IGNORE INTO cold.chains(chain_id,status,nodes_json,"
                "block_event,detail_json) VALUES(?,?,?,?,?)",
                (cid, status, canonical_json(list(nodes)), block_event,
                 canonical_json(detail)))
            chain_cache[key] = cid
        return cid, block_event

    def open_head(epc, us):
        cid, be = chain_id(epc)
        heads[epc] = [us, cid, be]

    def close_head(epc, us):
        rec = heads.pop(epc, None)
        if rec is not None and rec[0] < us:
            raw_intervals.append((epc, rec[0], us, rec[1], rec[2]))

    def apply_group(us, rows):
        payloads = [(r["event_id"], json.loads(r["payload"])) for r in rows]
        aggs = [(eid, p) for eid, p in payloads
                if p["etype"] == "AggregationEvent"]
        # which inspected EPCs may change? named children + inspected
        # descendants of every involved container (pre-group edges)
        seeds = set()
        for _eid, p in aggs:
            if p.get("parent_id"):
                seeds.add(p["parent_id"])
            seeds.update(p["child_epcs"])
        affected = {e for e in _descendants(seeds, children) if e in inspected}

        # pre-change snapshot (state before anything at this instant)
        for epc in affected:
            if epc not in initial:
                cid, _be = chain_id(epc)
                initial[epc] = cid
            close_head(epc, us)

        # conflicted packaging events at this instant block first; the
        # block is not cleared by the simultaneous ordinary changes
        block_hits = set()
        for row in rows:
            rec = blocks.get(row["event_id"])
            if rec is None:
                continue
            for node in rec["subjects"]:
                blocked.setdefault(node, {"event": row["event_id"], **rec})
            block_hits |= {e for e in _descendants(rec["subjects"], children)
                           if e in inspected}
        for epc in sorted(block_hits):
            open_head(epc, us)

        # ordinary simultaneous group: DELETEs first, then ADDs
        for _eid, p in aggs:
            if p["action"] != "DELETE":
                continue
            top = p["parent_id"]
            for child in p["child_epcs"]:
                if child in child_unknown:
                    continue
                edge = parent.get(child)
                if edge and edge[0] == top:
                    del parent[child]
                    kids = children.get(top)
                    if kids is not None:
                        kids.discard(child)
        add_parents, add_events = {}, {}
        for _eid, p in aggs:
            if p["action"] != "ADD":
                continue
            top = p["parent_id"]
            for child in p["child_epcs"]:
                add_parents.setdefault(child, set()).add(top)
                add_events.setdefault(child, set()).add(_eid)
        for child, tops in add_parents.items():
            if len(tops) > 1:
                old = parent.pop(child, None)
                if old is not None:
                    kids = children.get(old[0])
                    if kids is not None:
                        kids.discard(child)
                child_unknown[child] = {
                    "since_us": us, "reason": "double_parent",
                    "parents": sorted(tops), "events": sorted(add_events[child])}
                continue
            if child in child_unknown:
                continue
            top = next(iter(tops))
            edge = parent.get(child)
            if edge and edge[0] == top:
                continue
            if edge is not None:
                kids = children.get(edge[0])
                if kids is not None:
                    kids.discard(child)
            parent[child] = (top, us, sorted(add_events[child])[0])
            children.setdefault(top, set()).add(child)

        # post-change snapshot for every affected EPC (a blocked chain
        # evaluates the same unknown state, so the head simply carries on)
        for epc in sorted(affected):
            cid, be = chain_id(epc)
            cur = heads.get(epc)
            if cur is None:
                heads[epc] = [us, cid, be]
            elif cur[1] != cid or cur[2] != be:
                if cur[0] < us:
                    raw_intervals.append((epc, cur[0], us, cur[1], cur[2]))
                heads[epc] = [us, cid, be]

    cur = store.iter_replay(batch_seq, window_end_us, conflicted_ids)
    group_us, group = None, []
    for row in cur:
        us = row["event_us"]
        if group_us is None or us == group_us:
            group_us = us
            group.append(row)
        else:
            apply_group(group_us, group)
            group_us, group = us, [row]
    if group:
        apply_group(group_us, group)
    for epc, rec in heads.items():
        if rec[0] < window_end_us:
            raw_intervals.append((epc, rec[0], window_end_us, rec[1], rec[2]))

    # clip to the window, merge adjacent identical chains, guarantee coverage
    by_epc = {}
    for epc, f, t, cid, be in raw_intervals:
        f, t = max(f, window_start_us), min(t, window_end_us)
        if f < t:
            by_epc.setdefault(epc, []).append((f, t, cid, be))
    for epc in sorted(inspected):
        ivs = by_epc.get(epc)
        if not ivs:
            cid, _be = chain_id(epc)  # state constant across the window
            ivs = [(window_start_us, window_end_us, cid, None)]
        else:
            ivs.sort()
            if ivs[0][0] > window_start_us:
                # the state before the EPC's first in-window touch was
                # snapshotted as its initial chain; prepend it so the
                # pre-change history is not rewritten with the new state
                pre_cid = initial.get(epc)
                if pre_cid is not None:
                    ivs.insert(0, (window_start_us, ivs[0][0], pre_cid, None))
                else:
                    f0, t0, cid0, be0 = ivs[0]
                    ivs[0] = (window_start_us, t0, cid0, be0)
            merged = []
            for f, t, cid, be in ivs:
                if merged and merged[-1][2] == cid and merged[-1][3] == be \
                        and merged[-1][1] == f:
                    pf, pt, pcid, pbe = merged[-1]
                    merged[-1] = (pf, t, pcid, pbe)
                else:
                    merged.append((f, t, cid, be))
            ivs = merged
        for f, t, cid, be in ivs:
            conn.execute(
                "INSERT INTO cold.chain_intervals(epc,from_us,to_us,chain_id,"
                "block_event) VALUES(?,?,?,?,?)", (epc, f, t, cid, be))
    conn.commit()
    return blocks


# ---------------------------------------------------------------- per-EPC sweep

class _LRU:
    def __init__(self, maxsize):
        self.maxsize = maxsize
        self.data = {}

    def get(self, key, producer):
        if key in self.data:
            value = self.data.pop(key)
            self.data[key] = value
            return value
        value = producer()
        self.data[key] = value
        if len(self.data) > self.maxsize:
            oldest = next(iter(self.data))
            del self.data[oldest]
        return value


class Sweeper:
    """Builds coverage segments for one inspected EPC at a time.

    All lookups go through indexed scratch SQL; hot probes/containers use
    bounded LRU caches, so unrelated probes never occupy memory and the
    EPC x global-sample cartesian product is never built.
    """

    def __init__(self, conn, items, opening_owner, moves):
        self.conn = conn
        self.items = items
        self.opening_owner = opening_owner
        self.moves = moves
        self.inst_cache = _LRU(CONTAINER_CACHE_MAX)
        self.sample_cache = _LRU(PROBE_CACHE_MAX)
        self.evidence = set()

    # ---- evidence refs (deduplicated into the scratch evidence table)
    def ev(self, ref, payload):
        if ref not in self.evidence:
            self.evidence.add(ref)
            self.conn.execute(
                "INSERT OR IGNORE INTO cold.evidence_refs(ref,json) VALUES(?,?)",
                (ref, canonical_json({"ref": ref, **payload})))
        return ref

    def opening_ref(self, epc, owner):
        ref = f"opening:{epc}"
        return self.ev(ref, {"kind": "opening", "epc": epc, "location": owner,
                             "note": "opening inventory named-EPC ownership"})

    def handover_ref(self, handover, effective_us, ledger_line):
        ref = f"handover:{handover}"
        return self.ev(ref, {"kind": "handover", "handover": handover,
                             "effective": iso(effective_us),
                             "ledger_line": ledger_line})

    def chain_ref(self, cid):
        ref = f"chain:{cid}"
        row = self.conn.execute(
            "SELECT status,nodes_json,block_event,detail_json FROM cold.chains"
            " WHERE chain_id=?", (cid,)).fetchone()
        return self.ev(ref, {"kind": "chain", "chain_id": cid,
                             "status": row["status"],
                             "nodes": json.loads(row["nodes_json"]),
                             "block_event": row["block_event"],
                             "detail": json.loads(row["detail_json"])})

    def installation_ref(self, inst_id):
        ref = f"installation:{inst_id}"
        row = self.conn.execute(
            "SELECT probe,container,from_us,to_us,valid_for_us,line,raw"
            " FROM cold.installations WHERE inst_id=?", (inst_id,)).fetchone()
        return self.ev(ref, {"kind": "installation", "inst_id": inst_id,
                             "probe": row["probe"], "container": row["container"],
                             "from": iso(row["from_us"]), "to": iso(row["to_us"]),
                             "valid_for_seconds": row["valid_for_us"] // 1_000_000,
                             "raw": json.loads(row["raw"])})

    def sample_ref(self, probe, sample_us):
        row = self.conn.execute(
            "SELECT status,values_json,lines_json FROM cold.samples"
            " WHERE probe=? AND sample_us=?", (probe, sample_us)).fetchone()
        installs = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT inst_id FROM cold.sample_scan"
            " WHERE probe=? AND sample_us=? AND inst_id IS NOT NULL"
            " ORDER BY inst_id", (probe, sample_us))]
        ref = f"sample:{probe}:{sample_us}"
        return self.ev(ref, {
            "kind": "sample", "probe": probe, "at": iso(sample_us),
            "status": row["status"], "values_mc": json.loads(row["values_json"]),
            "installations": installs})

    # ---- bounded caches over scratch SQL
    def installs_on(self, container):
        def load():
            return self.conn.execute(
                "SELECT inst_id,probe,from_us,to_us,valid_for_us FROM"
                " cold.installations WHERE container=? ORDER BY from_us, inst_id",
                (container,)).fetchall()
        return self.inst_cache.get(container, load)

    def samples_of(self, probe):
        def load():
            return self.conn.execute(
                "SELECT sample_us,status,values_json FROM cold.samples"
                " WHERE probe=? ORDER BY sample_us", (probe,)).fetchall()
        return self.sample_cache.get(probe, load)

    # ---- ownership timeline (only posted named moves)
    def owner_intervals(self, epc, start_us, end_us):
        """Half-open [(from, to, owner_or_None, ref_or_None)] covering window."""
        pieces = []
        owner = self.opening_owner.get(epc)
        ref = self.opening_ref(epc, owner) if owner is not None else None
        cur = start_us
        for eff_us, frm, to, handover, _group, line in self.moves.get(epc, ()):
            if eff_us <= start_us:
                owner, ref = to, self.handover_ref(handover, eff_us, line)
                continue
            if eff_us >= end_us:
                break
            pieces.append((cur, eff_us, owner, ref))
            cur = eff_us
            owner, ref = to, self.handover_ref(handover, eff_us, line)
        pieces.append((cur, end_us, owner, ref))
        return pieces

    # ---- temperature classification at one instant
    def classify(self, epc, t_us, chain_row):
        cid = chain_row["chain_id"]
        chain = self.conn.execute(
            "SELECT status,nodes_json,block_event FROM cold.chains"
            " WHERE chain_id=?", (cid,)).fetchone()
        chain_ref = self.chain_ref(cid)
        if chain["status"] != "complete":
            return {"state": "chain_unknown", "reason": "chain_unknown",
                    "temp_mc": None,
                    "evidence": {"chain": chain_ref}}
        nodes = json.loads(chain["nodes_json"])
        # nearest mounted probe: the EPC itself first, then ancestors outward
        for container in nodes:
            active = [r for r in self.installs_on(container)
                      if r["from_us"] <= t_us < r["to_us"]]
            if not active:
                continue
            if len(active) > 1:
                return {"state": "install_conflict", "reason": "install_conflict",
                        "temp_mc": None,
                        "evidence": {"chain": chain_ref,
                                     "installations": [self.installation_ref(r["inst_id"])
                                                       for r in active]}}
            inst = active[0]
            inst_ref = self.installation_ref(inst["inst_id"])
            ev = {"chain": chain_ref, "installation": inst_ref}
            # a reading is used only inside THIS installation: reinstalling
            # the same probe (even on the same container) never inherits a
            # previous installation's samples, and a sample at removal is
            # excluded by the half-open upper bound
            mine = self.conn.execute(
                "SELECT s.sample_us, s.status, s.values_json"
                " FROM cold.samples s"
                " WHERE s.probe=? AND EXISTS ("
                "   SELECT 1 FROM cold.sample_scan sc"
                "   WHERE sc.probe=s.probe AND sc.sample_us=s.sample_us"
                "     AND sc.inst_id=?) ORDER BY s.sample_us",
                (inst["probe"], inst["inst_id"])).fetchall()
            prior = [r for r in mine if r["sample_us"] <= t_us]
            if not prior:
                return {"state": "not_sampled", "reason": "not_sampled",
                        "temp_mc": None, "evidence": ev}
            latest = prior[-1]
            later = [r for r in mine if r["sample_us"] > t_us]
            next_us = later[0]["sample_us"] if later else None
            ev["sample"] = self.sample_ref(inst["probe"], latest["sample_us"])
            ends = [inst["to_us"], latest["sample_us"] + inst["valid_for_us"]]
            if next_us is not None:
                ends.append(next_us)
            coverage_end = min(ends)
            if latest["status"] == "conflict":
                if t_us < coverage_end:
                    return {"state": "sample_conflict", "reason": "sample_conflict",
                            "temp_mc": None, "evidence": ev}
                return {"state": "expired", "reason": "expired",
                        "temp_mc": None, "evidence": ev}
            if t_us >= coverage_end:
                return {"state": "expired", "reason": "expired",
                        "temp_mc": None, "evidence": ev}
            value = json.loads(latest["values_json"])[0]
            return {"state": "measured", "reason": None, "temp_mc": value,
                    "evidence": ev}
        return {"state": "not_installed", "reason": "not_installed",
                "temp_mc": None, "evidence": {"chain": chain_ref}}

    # ---- full segment build for one EPC
    def build(self, epc, start_us, end_us):
        limit_mc, alarm_us = self.items[epc]
        chain_ivs = self.conn.execute(
            "SELECT from_us,to_us,chain_id,block_event FROM cold.chain_intervals"
            " WHERE epc=? ORDER BY from_us", (epc,)).fetchall()
        owners = self.owner_intervals(epc, start_us, end_us)

        boundaries = {start_us, end_us}
        m = next_midnight_after(start_us)
        while m < end_us:
            boundaries.add(m)
            m += 86_400_000_000
        for r in chain_ivs:
            if start_us < r["from_us"] < end_us:
                boundaries.add(r["from_us"])
        for f, t, _o, _r in owners:
            if start_us < t < end_us:
                boundaries.add(t)
        # installations and samples only for containers on this EPC's chains
        containers = set()
        for r in chain_ivs:
            row = self.conn.execute(
                "SELECT nodes_json,status FROM cold.chains WHERE chain_id=?",
                (r["chain_id"],)).fetchone()
            if row["status"] == "complete":
                containers.update(json.loads(row["nodes_json"]))
        probes = set()
        for container in containers:
            for ins in self.installs_on(container):
                probes.add(ins["probe"])
                if start_us < ins["from_us"] < end_us:
                    boundaries.add(ins["from_us"])
                if start_us < ins["to_us"] < end_us:
                    boundaries.add(ins["to_us"])
        for probe in probes:
            for r in self.samples_of(probe):
                s = r["sample_us"]
                if start_us < s < end_us:
                    boundaries.add(s)
                # validity expiry is installation-specific
                for ins in self.conn.execute(
                        "SELECT inst_id,from_us,to_us,valid_for_us FROM"
                        " cold.installations WHERE probe=? ORDER BY inst_id",
                        (probe,)):
                    if ins["from_us"] <= s < ins["to_us"]:
                        exp = s + ins["valid_for_us"]
                        if start_us < exp < end_us:
                            boundaries.add(exp)
        timeline = sorted(boundaries)

        def chain_at(t_us):
            for r in chain_ivs:
                if r["from_us"] <= t_us < r["to_us"]:
                    return r
            return chain_ivs[-1]

        def owner_at(t_us):
            for f, t, owner, ref in owners:
                if f <= t_us < t:
                    return owner, ref
            return owners[-1][2], owners[-1][3]

        logical = []  # (a, b, owner, owner_ref, class_dict) pre-midnight segments
        for a, b in zip(timeline, timeline[1:]):
            owner, owner_ref = owner_at(a)
            c = self.classify(epc, a, chain_at(a))
            measured = c["state"] == "measured"
            if measured:
                state = ST_OVER if c["temp_mc"] > limit_mc else ST_NORMAL
            else:
                state = c["reason"]
            key = (owner, state, c["temp_mc"],
                   canonical_json(c["evidence"]), owner_ref)
            if logical and logical[-1][0] == a and logical[-1][5] == key:
                pf, _pt, po, por, pc, _pk = logical[-1]
                logical[-1] = (pf, b, po, por, pc, key)
            else:
                logical.append((a, b, owner, owner_ref, c, key))

        # split at UTC midnight for coverage output and daily accounting
        coverage, daily = [], []
        for a, b, owner, owner_ref, c, _key in logical:
            cur = a
            while cur < b:
                m = next_midnight_after(cur)
                cut = min(m, b)
                coverage.append((cur, cut, owner, owner_ref, c, limit_mc))
                day = day_of(cur)
                state = c["state"] if c["state"] == "measured" else None
                state = (ST_OVER if c["state"] == "measured"
                         and c["temp_mc"] > limit_mc
                         else ST_NORMAL if c["state"] == "measured"
                         else c["reason"])
                daily.append((epc, day, owner, state, cut - cur))
                cur = cut
        for _e, day, owner, state, dur in daily:
            self.conn.execute(
                "INSERT INTO cold.daily_acc(epc,day,owner_key,state,duration_us)"
                " VALUES(?,?,?,?,?) ON CONFLICT(epc,day,owner_key,state)"
                " DO UPDATE SET duration_us=duration_us+excluded.duration_us",
                (epc, day,
                 UNKNOWN_OWNER_KEY if owner is None else owner, state, dur))

        # alarms: maximal consecutive over_limit runs across midnight;
        # any positive-duration normal or unknown segment breaks a run
        alarms = []
        run_pieces = []
        run_start = None
        for a, b, owner, _oref, c, _key in logical:
            is_over = c["state"] == "measured" and c["temp_mc"] > limit_mc
            if is_over:
                if run_start is None:
                    run_start = a
                run_pieces.append((a, b, owner))
            elif run_pieces:
                self._close_alarm(alarms, epc, run_start, a, run_pieces, alarm_us)
                run_pieces, run_start = [], None
        if run_pieces:
            self._close_alarm(alarms, epc, run_start, logical[-1][1],
                              run_pieces, alarm_us)
        return coverage, alarms

    def _close_alarm(self, alarms, epc, start_us, end_us, pieces, alarm_us):
        total = end_us - start_us
        if total < alarm_us:
            return
        by_owner, by_owner_day = {}, {}
        for a, b, owner in pieces:
            by_owner[owner] = by_owner.get(owner, 0) + (b - a)
            day = day_of(a)
            key = (owner, day)
            by_owner_day[key] = by_owner_day.get(key, 0) + (b - a)
        null_first = lambda owner: (owner is not None, owner or "")
        alarms.append({
            "epc": epc, "start": iso(start_us), "end": iso(end_us),
            "duration_us": total,
            "threshold_seconds": alarm_us // 1_000_000,
            "by_owner": [
                {"owner": owner, "duration_us": dur}
                for owner, dur in sorted(by_owner.items(),
                                         key=lambda kv: null_first(kv[0]))],
            "by_owner_day": [
                {"owner": owner, "day": day, "duration_us": dur}
                for (owner, day), dur in sorted(
                    by_owner_day.items(),
                    key=lambda kv: (null_first(kv[0][0]), kv[0][1]))],
            "segments": [iso(a) for a, _b, _o in pieces],
        })


# ---------------------------------------------------------------- main run

def _resolve_batch(store, batch):
    if batch == "latest":
        seq = store.latest_batch()
        if seq is None:
            raise FatalColdChain("invalid_batch", "no published batch yet")
        return seq
    try:
        seq = int(batch)
    except (TypeError, ValueError) as exc:
        raise FatalColdChain("invalid_batch", f"not a batch number: {batch!r}") from exc
    row = store.conn.execute(
        "SELECT 1 FROM batches WHERE batch_seq=? AND status='published'",
        (seq,)).fetchone()
    if row is None:
        raise FatalColdChain("invalid_batch",
                             f"batch {seq} is not a published batch")
    return seq


def run_coldchain(store, units, batch, window_start, window_end, opening_path,
                  summary_path, ledger_path, items_path, installations_path,
                  samples_path, out_dir):
    """Build one cold-chain exposure ledger. Returns (summary_dict, exit_code)."""
    os.makedirs(out_dir, exist_ok=True)
    for name in RESULT_FILES + ("summary.json",):
        try:
            os.remove(os.path.join(out_dir, name))
        except OSError:
            pass
    inputs = {
        "batch": batch, "window_start_raw": window_start,
        "window_end_raw": window_end,
        "files": {"opening": opening_path, "summary": summary_path,
                  "ledger": ledger_path, "items": items_path,
                  "installations": installations_path, "samples": samples_path},
    }
    try:
        batch_seq = _resolve_batch(store, batch)
        inputs["batch"] = batch_seq
        try:
            start_us = parse_instant(window_start, "window_start")
            end_us = parse_instant(window_end, "window_end")
        except P.ProfileError as exc:
            raise FatalColdChain("invalid_window", exc.reason) from exc
        if end_us <= start_us:
            raise FatalColdChain("invalid_window",
                                 "window end must be after window start")
        inputs["window_start"] = iso(start_us)
        inputs["window_end"] = iso(end_us)
        inputs["file_digests"] = {
            k: sha256_file(v) for k, v in inputs["files"].items()}

        opening_us, opening_owner, moves = load_settle_inputs(
            units, opening_path, summary_path, ledger_path, batch_seq, start_us)
        inputs["opening_as_of"] = iso(opening_us)

        scratch = attach_scratch(store)
        try:
            items = load_items(items_path)
            for epc, (limit, alarm_us) in items.items():
                store.conn.execute(
                    "INSERT INTO cold.items(epc,limit_mc,alarm_us) VALUES(?,?,?)",
                    (epc, limit, alarm_us))
            store.conn.commit()
            n_install = load_installations(installations_path, store.conn)
            n_sample_lines = load_samples(samples_path, store.conn)
            n_outside = annotate_samples(store.conn)
            n_conflicts = store.conn.execute(
                "SELECT COUNT(*) AS n FROM cold.samples WHERE status='conflict'"
            ).fetchone()["n"]

            build_chain_intervals(store, store.conn, batch_seq, start_us, end_us,
                                  set(items))

            sweeper = Sweeper(store.conn, items, opening_owner, moves)
            coverage_path = os.path.join(out_dir, "coverage.jsonl")
            alarms_path = os.path.join(out_dir, "alarms.jsonl")
            n_segments = 0
            n_alarms = 0
            unknown_durations = {r: 0 for r in UNKNOWN_REASONS}
            normal_us = over_us = 0
            with open(coverage_path, "w", encoding="utf-8") as cfh, \
                    open(alarms_path, "w", encoding="utf-8") as afh:
                for epc in sorted(items):
                    coverage, alarms = sweeper.build(epc, start_us, end_us)
                    for ordinal, (a, b, owner, owner_ref, c, limit_mc) in \
                            enumerate(coverage, 1):
                        n_segments += 1
                        if c["state"] == "measured":
                            state = ST_OVER if c["temp_mc"] > limit_mc else ST_NORMAL
                            reason = None
                        else:
                            state, reason = c["reason"], c["reason"]
                            unknown_durations[reason] += b - a
                        if state == ST_NORMAL:
                            normal_us += b - a
                        elif state == ST_OVER:
                            over_us += b - a
                        evidence = dict(c["evidence"])
                        if owner_ref is not None:
                            evidence["ownership"] = owner_ref
                        cfh.write(canonical_json({
                            "epc": epc, "seg": ordinal,
                            "start": iso(a), "end": iso(b),
                            "duration_us": b - a,
                            "owner": owner, "state": state, "reason": reason,
                            "temp_mc": c["temp_mc"], "limit_mc": limit_mc,
                            "evidence": evidence}) + "\n")
                    for ordinal, alarm in enumerate(alarms, 1):
                        n_alarms += 1
                        alarm = {"alarm_id": f"{epc}#{ordinal}", **alarm}
                        afh.write(canonical_json(alarm) + "\n")

            with open(os.path.join(out_dir, "daily.jsonl"), "w",
                      encoding="utf-8") as fh:
                for row in store.conn.execute(
                        "SELECT epc,day,owner_key,state,duration_us FROM cold.daily_acc"
                        " ORDER BY epc,day,owner_key,state"):
                    fh.write(canonical_json({
                        "epc": row["epc"], "day": row["day"],
                        "owner": None if row["owner_key"] == UNKNOWN_OWNER_KEY
                        else row["owner_key"],
                        "state": row["state"],
                        "duration_us": row["duration_us"]}) + "\n")
            with open(os.path.join(out_dir, "evidence.jsonl"), "w",
                      encoding="utf-8") as fh:
                for row in store.conn.execute(
                        "SELECT json FROM cold.evidence_refs ORDER BY ref"):
                    fh.write(row["json"] + "\n")

            # accounting cross-check: every EPC's daily totals cover the window
            for epc in sorted(items):
                got = store.conn.execute(
                    "SELECT COALESCE(SUM(duration_us),0) AS n FROM cold.daily_acc"
                    " WHERE epc=?", (epc,)).fetchone()["n"]
                if got != end_us - start_us:
                    raise FatalColdChain(
                        "internal_accounting_gap",
                        f"{epc}: daily totals {got}us != window {end_us - start_us}us")

            summary = {
                "status": "built",
                "inputs": inputs,
                "counts": {
                    "items": len(items),
                    "installations": n_install,
                    "sample_lines": n_sample_lines,
                    "sample_conflict_instants": n_conflicts,
                    "samples_outside_installation": n_outside,
                    "coverage_segments": n_segments,
                    "alarms": n_alarms,
                },
                "duration_us": {
                    "normal": normal_us, "over_limit": over_us,
                    **unknown_durations,
                    "window_total_per_item": end_us - start_us,
                },
                "outputs": {name.split(".")[0]: name for name in RESULT_FILES},
            }
            code = 0
        finally:
            detach_scratch(store, scratch)
    except FatalColdChain as exc:
        # a failed run delivers only the failed summary: never leave
        # partial coverage/daily/alarm/evidence files behind
        for name in RESULT_FILES:
            try:
                os.remove(os.path.join(out_dir, name))
            except OSError:
                pass
        summary = {"status": "failed",
                   "error": {"code": exc.code, "detail": exc.detail,
                             "file": exc.file, "line": exc.line},
                   "inputs": inputs}
        code = 2
    with open(os.path.join(out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    return summary, code
