"""Query layer: packaging ancestors/members, transformation trace, evidence.

Every query is evaluated at (business time t, receiving batch b): only
events with eventTime <= t that were completely received in published
batches <= b participate.  The two time dimensions never mix; late
arrivals change newer batches' views of old business moments without
rewriting older batches' views.

Transformation traceability:
  * inputs+outputs of one TransformationEvent form a single node;
  * every output traces to all inputs of the node, but no input share to
    a specific output is ever asserted: allocation = "not_declared";
  * same-dimension input/output totals and their difference are reported
    as raw event statistics; a non-zero difference is not illegal;
  * packaging ADD/DELETE never enters transformation quantity totals;
  * cross-event lineage exists only through explicit manifest
    lineage_links; without a link the participation fact is returned with
    missing_evidence -- never guessed from matching identifiers.
"""
from __future__ import annotations

import json
from collections import defaultdict
from fractions import Fraction

from .state import build_view, walk_ancestors
from .units import fmt_fraction

LINK_INPUT_LISTS = {"inputEPCList", "inputQuantityList"}
LINK_OUTPUT_LISTS = {"outputEPCList", "outputQuantityList"}


def item_key(kind: str, ident: str) -> str:
    return ("class:" + ident) if kind == "class" else ident


def parse_item(text: str):
    if text.startswith("class:"):
        return {"kind": "class", "id": text[len("class:"):]}
    return {"kind": "epc", "id": text}


class EvidenceCollector:
    """Shared evidence: each used event appears once, by content digest."""

    def __init__(self, store, evidence):
        self._store = store
        self._evidence = evidence
        self._by_event = {}

    def add(self, event_id):
        if event_id and event_id not in self._by_event:
            rows = self._store.event_rows_by_id(event_id)
            ok = [r for r in rows if r["status"] == "ok"]
            row = ok[0] if ok else (rows[0] if rows else None)
            self._by_event[event_id] = row["content_digest"] if row else None

    def render(self):
        events = {}
        evidence = {}
        for eid, digest in sorted(self._by_event.items()):
            events[eid] = digest
            if digest and digest not in evidence:
                evidence[digest] = self._evidence.describe(digest)
        return {"events": events, "evidence": evidence}


def _iso(us):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc).isoformat()


def _suspension(view, items, role="subject"):
    """Return conflict detail if any item is touched by a conflicted event.

    role="subject": the item's own state (its parentage, its transformation
        participation) is affected -- used by ancestors/trace/observations.
    role="container": the item is named as parentID by a conflicted event
        -- used by children queries.
    """
    table = view.conflict_subjects if role == "subject" else view.conflict_containers
    hits = []
    for item in items:
        for rec in table.get(item, []):
            hits.append(rec)
    if not hits:
        return None
    return {
        "status": "suspended",
        "reason": "event_id_conflict",
        "detail": "same eventID received with different expanded contents; "
                  "qualified results involving it are suspended in this batch view",
        "conflicts": hits,
    }


# ------------------------------------------------------------ packaging

def query_ancestors(store, evidence, epc, t_us, batch_seq):
    view = build_view(store, batch_seq, t_us)
    coll = EvidenceCollector(store, evidence)
    base = {"query": {"type": "packaging_ancestors", "epc": epc,
                      "event_time": _iso(t_us), "batch": batch_seq}}
    susp = _suspension(view, [epc])
    if susp:
        return {**base, **susp}
    chain, status = walk_ancestors(view, epc)
    # a conflicted event touching any node on the chain also suspends
    nodes = [c["parent"] for c in chain] + [c["child"] for c in chain]
    susp = _suspension(view, nodes)
    if susp:
        return {**base, **susp}
    for c in chain:
        coll.add(c["by_event"])
    if status["state"] != "complete":
        for eid in status.get("events", []) or []:
            coll.add(eid)
        for eid in status.get("cycle_events", []) or []:
            coll.add(eid)
    out_chain = [
        {"child": c["child"], "parent": c["parent"],
         "since": _iso(c["since_us"]), "by_event": c["by_event"]}
        for c in chain
    ]
    detail = {k: v for k, v in status.items() if k != "state"}
    if "since_us" in detail:
        detail["since"] = _iso(detail.pop("since_us"))
    return {**base, "status": status["state"],
            "detail": detail,
            "chain": out_chain,
            **coll.render()}


def query_children(store, evidence, parent, t_us, batch_seq, units):
    view = build_view(store, batch_seq, t_us)
    coll = EvidenceCollector(store, evidence)
    base = {"query": {"type": "packaging_children", "parent": parent,
                      "event_time": _iso(t_us), "batch": batch_seq}}
    susp = _suspension(view, [parent], role="container")
    if susp:
        return {**base, **susp}
    named = []
    for child, edge in sorted(view.parent.items()):
        if edge["parent"] == parent:
            named.append({"epc": child, "since": _iso(edge["since_us"]),
                          "by_event": edge["by_event"]})
            coll.add(edge["by_event"])
    quantities = []
    keys = {k for k in view.qty if k[0] == parent}
    keys |= {k for k in view.qty_unknown if k[0] == parent}
    for (p, cls, dim) in sorted(keys):
        key = (p, cls, dim)
        for eid in view.qty_events.get(key, ()):  # shared evidence
            coll.add(eid)
        if key in view.qty_unknown:
            rec = view.qty_unknown[key]
            for eid in rec.get("events", []):
                coll.add(eid)
            reason = {k: v for k, v in rec.items() if k != "since_us"}
            reason["since"] = _iso(rec["since_us"])
            quantities.append({
                "class": cls, "dimension": dim,
                "base_unit": units.base_unit(dim),
                "status": "unknown",
                "reason": reason,
            })
        else:
            quantities.append({
                "class": cls, "dimension": dim,
                "quantity": fmt_fraction(view.qty[key]),
                "base_unit": units.base_unit(dim),
                "status": "ok",
            })
    # children whose parentage is unknown are not attributable to any parent;
    # they surface in their own ancestor query, not here.
    return {**base, "status": "ok",
            "named_children": named,
            "class_quantities": quantities,
            **coll.render()}


# ------------------------------------------------------------ transformations

def _load_transformations(store, batch_seq, t_us, exclude_ids):
    sql = ("SELECT event_id, payload, event_us, content_digest FROM events"
           " WHERE status='ok' AND etype='TransformationEvent'"
           " AND first_batch<=? AND event_us<=?")
    params = [batch_seq, t_us]
    if exclude_ids:
        marks = ",".join("?" for _ in exclude_ids)
        sql += f" AND event_id NOT IN ({marks})"
        params.extend(sorted(exclude_ids))
    sql += " ORDER BY event_us, event_id"
    nodes = {}
    for row in store.conn.execute(sql, params):
        p = json.loads(row["payload"])
        nodes[row["event_id"]] = {
            "event_id": row["event_id"],
            "event_us": row["event_us"],
            "digest": row["content_digest"],
            "inputs": p["inputs"],
            "outputs": p["outputs"],
        }
    return nodes


def _node_stats(node):
    """Raw same-dimension totals; non-zero difference is not illegal."""
    dims = {}
    for side, items in (("input", node["inputs"]), ("output", node["outputs"])):
        for it in items:
            if it["kind"] != "class":
                continue
            dim = it["dimension"]
            rec = dims.setdefault(dim, {"input_total": Fraction(0),
                                        "output_total": Fraction(0),
                                        "base_unit": it["base_unit"]})
            rec[side + "_total"] += Fraction(it["base_quantity"])
    out = {}
    for dim, rec in dims.items():
        diff = rec["input_total"] - rec["output_total"]
        out[dim] = {
            "input_total": fmt_fraction(rec["input_total"]),
            "output_total": fmt_fraction(rec["output_total"]),
            "difference": fmt_fraction(diff),
            "base_unit": rec["base_unit"],
            "note": "raw event statistics; non-zero difference is not illegal "
                    "(no lossless conversion was promised)",
        }
    return out


def _find_item(items, list_name, index):
    for it in items:
        if it.get("list") == list_name and it.get("index") == index:
            return it
    return None


def _item_matches(it, kind, ident):
    return it["kind"] == kind and it["id"] == ident


def query_trace(store, evidence, kind, ident, t_us, batch_seq, units):
    view = build_view(store, batch_seq, t_us)
    coll = EvidenceCollector(store, evidence)
    base = {"query": {"type": "transformation_trace",
                      "item": {"kind": kind, "id": ident},
                      "event_time": _iso(t_us), "batch": batch_seq}}
    susp = _suspension(view, [item_key(kind, ident)])
    if susp:
        return {**base, **susp}

    nodes = _load_transformations(store, batch_seq, t_us, set(view.conflicted))
    links = store.links_for_view(batch_seq)

    # index links by downstream endpoint
    by_downstream = defaultdict(list)
    for lk in links:
        by_downstream[lk["downstream_event"]].append(lk)

    def link_status(lk, down_node):
        up_id = lk["upstream_event"]
        if up_id in view.conflicted:
            return {"status": "invalid", "reason": "endpoint_conflict",
                    "detail": "upstream event has conflicting retransmissions",
                    "link": _link_json(lk)}
        up = nodes.get(up_id)
        if up is None:
            return {"status": "pending",
                    "reason": "upstream endpoint not received in this batch view",
                    "link": _link_json(lk)}
        down_item = _find_item(down_node["inputs"], lk["down_list"], lk["down_index"])
        up_item = _find_item(up["outputs"], lk["up_list"], lk["up_index"])
        if down_item is None or up_item is None:
            return {"status": "invalid", "reason": "bad_coordinate",
                    "link": _link_json(lk)}
        if down_item["kind"] != up_item["kind"] or down_item["id"] != up_item["id"]:
            return {"status": "invalid", "reason": "identity_mismatch",
                    "down_item": down_item, "up_item": up_item,
                    "link": _link_json(lk)}
        if down_item["kind"] == "class" and down_item["dimension"] != up_item["dimension"]:
            return {"status": "invalid", "reason": "dimension_mismatch",
                    "down_item": down_item, "up_item": up_item,
                    "link": _link_json(lk)}
        if up["event_us"] > down_node["event_us"]:
            return {"status": "invalid", "reason": "future_source",
                    "detail": "upstream eventTime later than downstream eventTime",
                    "link": _link_json(lk)}
        return {"status": "linked",
                "upstream": {"event": up_id, "list": lk["up_list"],
                             "index": lk["up_index"]},
                "link": _link_json(lk)}

    # roots: nodes where the item appears among outputs
    roots = []
    for eid, node in nodes.items():
        for it in node["outputs"]:
            if _item_matches(it, kind, ident):
                roots.append({"event": eid, "list": it["list"], "index": it["index"]})
    # downstream participation: nodes where the item appears among inputs
    consumed_by = []
    for eid, node in nodes.items():
        for it in node["inputs"]:
            if _item_matches(it, kind, ident):
                consumed_by.append({"event": eid, "list": it["list"], "index": it["index"]})

    # ---- expand reachable nodes with an explicit worklist (no recursion,
    # ---- so arbitrarily deep link chains cannot overflow the stack)
    built = {}
    valid_edges = []          # (down_eid, up_eid, status_dict)
    queued = set()
    queue = []

    def ensure(eid):
        if eid not in built and eid not in queued:
            queued.add(eid)
            queue.append(eid)

    for r in roots + consumed_by:
        ensure(r["event"])

    while queue:
        eid = queue.pop()
        if eid in built:
            continue
        node = nodes[eid]
        inputs = []
        for it in node["inputs"]:
            entry = {"item": {k: v for k, v in it.items()},
                     "allocation": "not_declared"}
            coord = (it.get("list"), it.get("index"))
            lks = [lk for lk in by_downstream.get(eid, [])
                   if lk["down_list"] == coord[0] and lk["down_index"] == coord[1]]
            if not lks:
                entry["lineage"] = {"status": "missing_evidence",
                                    "detail": "no lineage link declares this input's "
                                              "production source; not guessed across events"}
            else:
                # profile manifests declare at most one link per input coordinate;
                # if several appear, each is validated and reported
                statuses = []
                for lk in lks:
                    st = link_status(lk, node)
                    if st["status"] == "linked":
                        valid_edges.append((eid, lk["upstream_event"], st))
                        ensure(lk["upstream_event"])
                    statuses.append(st)
                entry["lineage"] = statuses[0] if len(statuses) == 1 else statuses
            inputs.append(entry)
        built[eid] = {
            "event_id": eid,
            "event_time": _iso(node["event_us"]),
            "inputs": inputs,
            "outputs": node["outputs"],
            "stats": _node_stats(node),
        }
        coll.add(eid)

    # ---- explicit cycle report over the valid link graph (valid links never
    # ---- go forward in time, so any cycle is a same-moment cycle)
    cyclic_sccs, comp = _cyclic_sccs(valid_edges)
    cycles = []
    cyclic_of = {}
    for scc in cyclic_sccs:
        members = sorted(scc)
        cycles.append({"events": members,
                       "detail": "lineage links form a same-moment cycle; "
                                 "expansion stopped, no certain path fabricated"})
        for n in scc:
            cyclic_of[n] = members
    for d, u, st in valid_edges:
        if d in cyclic_of and u in cyclic_of and comp.get(d) == comp.get(u):
            st["status"] = "cycle"
            st["cycle_events"] = cyclic_of[d]

    consumed_out = []
    for c in consumed_by:
        eid = c["event"]
        lks = [lk for lk in by_downstream.get(eid, [])
               if lk["down_list"] == c["list"] and lk["down_index"] == c["index"]]
        linked_from = []
        for lk in lks:
            st = link_status(lk, nodes[eid])
            if st["status"] in ("linked", "cycle"):
                linked_from.append({"event": lk["upstream_event"],
                                    "list": lk["up_list"], "index": lk["up_index"]})
        consumed_out.append({
            **c,
            "allocation": "not_declared",
            "declared_upstream_link": linked_from or None,
            "note": "participation fact only; matching identifiers alone do "
                    "not establish production lineage",
        })

    return {**base, "status": "ok",
            "produced_by": roots,
            "consumed_by": consumed_out,
            "nodes": built,
            "cycles": cycles,
            **coll.render()}


def _cyclic_sccs(edges):
    """Strongly connected components that form cycles (iterative Kosaraju).

    edges: list of (downstream, upstream, payload).  Returns (cyclic_sccs,
    comp) where comp maps node -> component id.
    """
    adj = defaultdict(list)
    radj = defaultdict(list)
    endpoints = set()
    self_loops = set()
    for d, u, _ in edges:
        adj[d].append(u)
        radj[u].append(d)
        endpoints.add(d)
        endpoints.add(u)
        if d == u:
            self_loops.add(d)
    visited = set()
    order = []
    for s in endpoints:
        if s in visited:
            continue
        visited.add(s)
        stack = [(s, iter(adj[s]))]
        while stack:
            node, it = stack[-1]
            for nxt in it:
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, iter(adj[nxt])))
                    break
            else:
                order.append(node)
                stack.pop()
    comp = {}
    for s in reversed(order):
        if s in comp:
            continue
        comp[s] = s
        stack = [s]
        while stack:
            node = stack.pop()
            for nxt in radj[node]:
                if nxt not in comp:
                    comp[nxt] = s
                    stack.append(nxt)
    sccs = defaultdict(set)
    for n, cid in comp.items():
        sccs[cid].add(n)
    cyclic = [members for members in sccs.values()
              if len(members) > 1 or next(iter(members)) in self_loops]
    return cyclic, comp


def _link_json(lk):
    return {"downstream": {"event": lk["downstream_event"],
                           "list": lk["down_list"], "index": lk["down_index"]},
            "upstream": {"event": lk["upstream_event"],
                         "list": lk["up_list"], "index": lk["up_index"]},
            "declared_in_batch": lk["batch_seq"]}


def query_transformation(store, evidence, event_id, t_us, batch_seq, units):
    view = build_view(store, batch_seq, t_us)
    base = {"query": {"type": "transformation", "event": event_id,
                      "event_time": _iso(t_us), "batch": batch_seq}}
    if event_id in view.conflicted:
        return {**base, "status": "suspended", "reason": "event_id_conflict",
                "digests": view.conflicted[event_id]}
    nodes = _load_transformations(store, batch_seq, t_us, set(view.conflicted))
    node = nodes.get(event_id)
    coll = EvidenceCollector(store, evidence)
    if node is None:
        return {**base, "status": "not_found_in_view"}
    coll.add(event_id)
    return {**base, "status": "ok",
            "node": {"event_id": event_id,
                     "event_time": _iso(node["event_us"]),
                     "inputs": node["inputs"],
                     "outputs": node["outputs"],
                     "stats": _node_stats(node)},
            **coll.render()}


# ------------------------------------------------------------ observations / events

def query_observations(store, evidence, kind, ident, t_us, batch_seq):
    view = build_view(store, batch_seq, t_us)
    base = {"query": {"type": "observations",
                      "item": {"kind": kind, "id": ident},
                      "event_time": _iso(t_us), "batch": batch_seq}}
    susp = _suspension(view, [item_key(kind, ident)])
    if susp:
        return {**base, **susp}
    coll = EvidenceCollector(store, evidence)
    sql = ("SELECT event_id, payload, event_us FROM events"
           " WHERE status='ok' AND etype='ObjectEvent' AND first_batch<=? AND event_us<=?")
    params = [batch_seq, t_us]
    if view.conflicted:
        marks = ",".join("?" for _ in view.conflicted)
        sql += f" AND event_id NOT IN ({marks})"
        params.extend(sorted(view.conflicted))
    sql += " ORDER BY event_us, event_id"
    found = []
    for row in store.conn.execute(sql, params):
        p = json.loads(row["payload"])
        declared = None
        if kind == "epc" and ident in p["epc_list"]:
            declared = {"epc": ident}
        elif kind == "class":
            for q in p["quantity_list"]:
                if q["class"] == ident:
                    declared = dict(q)
                    break
        if declared:
            found.append({"event": row["event_id"],
                          "event_time": _iso(row["event_us"]),
                          "record_time": p["record_time"],
                          "declared": declared,
                          "sources": p["sources"],
                          "destinations": p["destinations"]})
            coll.add(row["event_id"])
    return {**base, "status": "ok", "observations": found, **coll.render()}


def query_event(store, evidence, event_id):
    rows = store.event_rows_by_id(event_id)
    out = {"query": {"type": "event", "event": event_id}}
    if not rows:
        return {**out, "status": "unknown_event"}
    versions = []
    coll = EvidenceCollector(store, evidence)
    for row in rows:
        prov = [{"batch": p["batch_seq"], "file": p["file_name"]}
                for p in store.provenance_of(row["event_pk"])]
        v = {"status": row["status"], "reason": row["reason"],
             "digest": row["content_digest"],
             "event_time": row["event_time"], "record_time": row["record_time"],
             "first_batch": row["first_batch"], "provenance": prov}
        if row["payload"]:
            p = json.loads(row["payload"])
            v["declared"] = {
                "etype": p["etype"], "action": p["action"],
                "parent_id": p["parent_id"],
                "child_epcs": p["child_epcs"],
                "child_quantities": p["child_quantities"],
                "epc_list": p["epc_list"],
                "quantity_list": p["quantity_list"],
                "inputs": p["inputs"], "outputs": p["outputs"],
                "sources": p["sources"], "destinations": p["destinations"],
            }
        versions.append(v)
        coll.add(event_id)
    # conflict visibility per published batch
    conflicted_in = []
    latest = store.latest_batch() or 0
    for b in range(1, latest + 1):
        if event_id in store.conflicted_event_ids(b):
            conflicted_in.append(b)
    return {**out, "status": "ok", "versions": versions,
            "conflicted_in_batches": conflicted_in,
            **coll.render()}


# ------------------------------------------------------------ review

def review(store, evidence, batch_seq=None):
    out = {"query": {"type": "review", "batch": batch_seq}}
    batches = []
    for b in store.batches_summary():
        files = [{"name": f["name"], "sha256": f["sha256"], "status": f["status"],
                  "detail": f["detail"]} for f in store.batch_files(b["attempt_id"])]
        batches.append({"attempt": b["attempt_id"], "batch_seq": b["batch_seq"],
                        "name": b["name"], "manifest_sha256": b["manifest_sha256"],
                        "status": b["status"], "reject_reason": b["reject_reason"],
                        "files": files})
    out["batches"] = batches
    out["latest_published_batch"] = store.latest_batch()

    scope = batch_seq if batch_seq is not None else store.latest_batch()
    if scope is not None:
        out["event_status_counts"] = {
            r["status"]: r["n"] for r in store.events_summary_by_status(scope)}
        problems = []
        for row in store.conn.execute(
                "SELECT event_id, status, reason, content_digest, first_batch FROM events"
                " WHERE status != 'ok' AND first_batch<=? ORDER BY first_batch, event_id",
                (scope,)):
            problems.append({"event_id": row["event_id"], "status": row["status"],
                             "reason": row["reason"], "digest": row["content_digest"],
                             "first_batch": row["first_batch"]})
        out["non_ok_events"] = problems
        conflicts = store.conflicted_event_ids(scope)
        out["conflicts"] = [{"event_id": k, "digests": v} for k, v in sorted(conflicts.items())]
    return out
