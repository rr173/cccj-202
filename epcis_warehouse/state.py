"""Packaging state: full replay per (receiving batch, business time).

The state is rebuilt from the stored event stream for every query at a
(new) batch; rows are streamed through a cursor so neither the full
original text nor all ancestor paths are ever held in memory -- only the
current maps (parent edges, quantities, unknowns).

Semantics implemented here:
  * business time is eventTime only; file/arrival order is irrelevant;
  * events sharing one eventTime form a simultaneous group: all declared
    DELETEs first, then all ADDs;
  * a named EPC has at most one direct parent; re-ADD of the same edge is
    a set no-op; a later ADD to another parent moves it;
  * the same named child added to two different parents in one group is
    ambiguous -> the child becomes unknown (no ID-sort tie-break);
  * class members are exact quantities per (parent, class, dimension) in
    base units -- never deduplicated like named members;
  * a DELETE beyond the available exact quantity is a conflict: the
    (parent, class, dimension) key becomes unknown from that moment, it
    is never clamped to zero and no float tolerance applies;
  * unknowns are not auto-cleared by later ordinary ADD/DELETE; only a
    new receiving batch whose recomputed history is conflict-free
    recovers (that falls out of the per-batch full rebuild).
"""
from __future__ import annotations

import json
from collections import defaultdict
from fractions import Fraction

from .units import fmt_fraction


class View:
    def __init__(self, batch_seq, t_us):
        self.batch_seq = batch_seq
        self.t_us = t_us
        # child -> {"parent": p, "since_us": us, "by_event": id}
        self.parent = {}
        # (parent, class, dimension) -> Fraction in base units
        self.qty = {}
        # (parent, class, dimension) -> set of contributing event ids (evidence)
        self.qty_events = {}
        # (parent, class, dimension) -> unknown record
        self.qty_unknown = {}
        # child -> unknown record (double parent)
        self.child_unknown = {}
        # event_id -> [digests] for id-conflicts visible in this view
        self.conflicted = {}
        # item -> [rec] for items whose OWN state is touched by a conflicted
        # event (as child/member/class/transformation item)
        self.conflict_subjects = defaultdict(list)
        # parent -> [rec] for containers named as parentID by a conflicted event
        self.conflict_containers = defaultdict(list)
        # event_id -> content_digest (ok events in this view), filled lazily by callers
        self.event_digests = {}


def _payload(row):
    return json.loads(row["payload"])


def build_view(store, batch_seq: int, t_us: int) -> View:
    view = View(batch_seq, t_us)
    view.conflicted = store.conflicted_event_ids(batch_seq)
    if view.conflicted:
        _collect_conflict_mentions(store, view)

    cur = store.iter_replay(batch_seq, t_us, set(view.conflicted))
    group_us = None
    group = []

    def flush():
        if group:
            _apply_group(view, group_us, group)

    for row in cur:
        us = row["event_us"]
        if group_us is None or us == group_us:
            group_us = us
            group.append(row)
        else:
            flush()
            group_us = us
            group = [row]
    flush()
    return view


def _collect_conflict_mentions(store, view: View):
    for eid in view.conflicted:
        for row in store.event_rows_by_id(eid):
            if row["status"] != "ok" or row["event_us"] is None:
                continue
            if row["event_us"] > view.t_us:
                continue
            p = _payload(row)
            rec = {"event_id": eid, "event_us": row["event_us"],
                   "digests": view.conflicted[eid]}
            subjects = set(p.get("child_epcs", [])) | set(p.get("epc_list", []))
            for q in p.get("child_quantities", []) + p.get("quantity_list", []):
                subjects.add("class:" + q["class"])
            for it in p.get("inputs", []) + p.get("outputs", []):
                subjects.add(("class:" + it["id"]) if it["kind"] == "class" else it["id"])
            for item in subjects:
                view.conflict_subjects[item].append(rec)
            if p.get("parent_id"):
                view.conflict_containers[p["parent_id"]].append(rec)


def _apply_group(view: View, us: int, rows):
    payloads = [(row["event_id"], _payload(row)) for row in rows]
    aggs = [(eid, p) for eid, p in payloads
            if p["etype"] == "AggregationEvent"]
    # ---- phase 1: all declared DELETEs of the simultaneous group
    # named members: set removals (order-free)
    for eid, p in aggs:
        if p["action"] != "DELETE":
            continue
        parent = p["parent_id"]
        for child in p["child_epcs"]:
            if child in view.child_unknown:
                continue  # unknowns are not auto-cleared
            cur = view.parent.get(child)
            if cur and cur["parent"] == parent:
                del view.parent[child]
    # class members: sum declared deletes per key, compare exactly
    del_sum = defaultdict(Fraction)
    del_events = defaultdict(list)
    del_declared = defaultdict(list)
    for eid, p in aggs:
        if p["action"] != "DELETE":
            continue
        for q in p["child_quantities"]:
            key = (p["parent_id"], q["class"], q["dimension"])
            del_sum[key] += Fraction(q["base_quantity"])
            if eid not in del_events[key]:
                del_events[key].append(eid)
            del_declared[key].append({"event": eid, "declared": q["base_quantity"],
                                      "base_unit": q["base_unit"]})
    for key, total in del_sum.items():
        view.qty_events.setdefault(key, set()).update(del_events[key])
        if key in view.qty_unknown:
            continue  # stays unknown; ordinary deletes do not clear it
        available = view.qty.get(key, Fraction(0))
        if total > available:
            # exact conflict: never clamp to zero, never tolerate by float
            view.qty.pop(key, None)
            view.qty_unknown[key] = {
                "since_us": us,
                "reason": "over_delete",
                "events": sorted(del_events[key]),
                "declared_total_base": fmt_fraction(total),
                "available_base": fmt_fraction(available),
                "declarations": del_declared[key],
            }
        else:
            view.qty[key] = available - total

    # ---- phase 2: all ADDs of the simultaneous group
    add_parents = defaultdict(set)          # child -> {parent}
    add_events = defaultdict(set)           # child -> {event_id}
    for eid, p in aggs:
        if p["action"] != "ADD":
            continue
        for child in p["child_epcs"]:
            add_parents[child].add(p["parent_id"])
            add_events[child].add(eid)
    for child, parents in add_parents.items():
        if len(parents) > 1:
            # ambiguous simultaneous double parent: no tie-break by id
            view.parent.pop(child, None)
            view.child_unknown[child] = {
                "since_us": us,
                "reason": "double_parent",
                "parents": sorted(parents),
                "events": sorted(add_events[child]),
            }
            continue
        if child in view.child_unknown:
            continue  # unknowns are not auto-cleared
        parent = next(iter(parents))
        cur = view.parent.get(child)
        if cur and cur["parent"] == parent:
            continue  # set semantics: repeated identical ADD is a no-op
        by_event = sorted(add_events[child])[0]
        view.parent[child] = {"parent": parent, "since_us": us, "by_event": by_event}
    # class member adds: exact accumulation, never set-deduplicated
    for eid, p in aggs:
        if p["action"] != "ADD":
            continue
        for q in p["child_quantities"]:
            key = (p["parent_id"], q["class"], q["dimension"])
            view.qty_events.setdefault(key, set()).add(eid)
            if key in view.qty_unknown:
                continue  # stays unknown until a conflict-free rebuild
            view.qty[key] = view.qty.get(key, Fraction(0)) + Fraction(q["base_quantity"])


# ------------------------------------------------------------ chain walking

def walk_ancestors(view: View, item: str):
    """Walk the parent chain. Returns (chain, status).

    chain: [{"child","parent","since_us","by_event"}...] bottom-up.
    status: {"state":"complete"} | {"state":"unknown",...} | {"state":"cycle",...}
    """
    chain = []
    seen = {}
    node = item
    while True:
        if node in view.child_unknown:
            rec = view.child_unknown[node]
            return chain, {"state": "unknown", "at": node, **rec}
        edge = view.parent.get(node)
        if edge is None:
            return chain, {"state": "complete"}
        if node in seen:
            cycle_nodes = [c["child"] for c in chain[seen[node]:]]
            cycle_events = sorted({c["by_event"] for c in chain[seen[node]:]})
            return chain, {"state": "cycle", "cycle_nodes": cycle_nodes,
                           "cycle_events": cycle_events}
        seen[node] = len(chain)
        chain.append({"child": node, "parent": edge["parent"],
                      "since_us": edge["since_us"], "by_event": edge["by_event"]})
        node = edge["parent"]
