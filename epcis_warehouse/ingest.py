"""Receiving-batch ingest.

A batch is a manifest listing event files with sha256 digests plus
optional lineage_links.  If any listed file is missing or its digest
mismatches, the batch is not published (recorded as rejected) and
previously published batches stay fully queryable.  File order and file
chunking never influence business results: events are keyed by eventID +
canonical expanded content, replay is ordered by eventTime only.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from . import _vendor  # noqa: F401  (ensures vendored deps are importable)
from pyld.jsonld import JsonLdError

from . import parse as P
from .evidence import canonical_json, sha256_bytes
from .jsonld_offline import is_unknown_context_error

LINK_INPUT_LISTS = {"inputEPCList", "inputQuantityList"}
LINK_OUTPUT_LISTS = {"outputEPCList", "outputQuantityList"}


class ManifestError(ValueError):
    pass


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


def _split_event_objects(doc):
    """Split a parsed JSON document into per-event raw objects.

    A file may hold one event, a JSON array of events, or an EPCIS
    document whose eventList elements inherit the document @context.
    """
    if isinstance(doc, list):
        return [e for e in doc]
    if not isinstance(doc, dict):
        raise ManifestError("event file is neither an object nor an array")
    if "eventList" in doc:
        ctx = doc.get("@context")
        events = []
        for el in doc["eventList"]:
            if isinstance(el, dict):
                el = dict(el)
                if ctx is not None and "@context" not in el:
                    el["@context"] = ctx
            events.append(el)
        return events
    return [doc]


def _extract_event_id(expanded):
    try:
        node = expanded[0]
        vals = node.get(P.P_EVENT_ID, [])
        if len(vals) == 1 and isinstance(vals[0].get("@value"), str):
            return vals[0]["@value"]
    except (IndexError, AttributeError, TypeError):
        pass
    return None


def ingest_manifest(wh, manifest_path: str) -> dict:
    store, evidence, expander, units = wh.store, wh.evidence, wh.expander, wh.units
    base_dir = os.path.dirname(os.path.abspath(manifest_path))
    with open(manifest_path, "rb") as fh:
        manifest_bytes = fh.read()
    manifest_sha = sha256_bytes(manifest_bytes)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc

    name = manifest.get("batch") or os.path.basename(manifest_path)
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ManifestError("manifest must list a non-empty 'files' array")
    links = manifest.get("lineage_links", [])
    if not isinstance(links, list):
        raise ManifestError("lineage_links must be an array")

    attempt = store.next_attempt_id()
    summary = {"attempt": attempt, "name": name, "manifest_sha256": manifest_sha}

    # ---- validate lineage link shapes (manifest-level contract)
    link_rows = []
    for i, lk in enumerate(links):
        try:
            down, up = lk["downstream"], lk["upstream"]
            row = (down["event"], down["list"], int(down["index"]),
                   up["event"], up["list"], int(up["index"]))
        except (KeyError, TypeError, ValueError) as exc:
            return _reject(store, attempt, name, manifest_sha,
                           f"lineage_links[{i}] malformed: {exc}", summary)
        if row[1] not in LINK_INPUT_LISTS or row[4] not in LINK_OUTPUT_LISTS:
            return _reject(store, attempt, name, manifest_sha,
                           f"lineage_links[{i}]: downstream list must be an input list "
                           f"and upstream list an output list", summary)
        if row[2] < 0 or row[5] < 0:
            return _reject(store, attempt, name, manifest_sha,
                           f"lineage_links[{i}]: negative index", summary)
        link_rows.append(row)

    # ---- verify all listed files before touching any event
    file_records = []
    problems = []
    for entry in files:
        fname = entry.get("name") if isinstance(entry, dict) else None
        want = entry.get("sha256") if isinstance(entry, dict) else None
        if not fname or not want:
            problems.append((str(entry), "missing", "file entry needs name and sha256"))
            continue
        path = os.path.join(base_dir, fname)
        if not os.path.isfile(path):
            problems.append((fname, "missing", "file not found"))
            file_records.append((fname, want, "missing", "file not found"))
            continue
        with open(path, "rb") as fh:
            got = sha256_bytes(fh.read())
        if got != want:
            file_records.append((fname, want, "digest_mismatch", f"got {got}"))
            problems.append((fname, "digest_mismatch", got))
        else:
            file_records.append((fname, want, "ok", None))
    if problems:
        reason = "; ".join(f"{n}: {s}" for n, s, _ in problems)
        for fname, want, status, detail in file_records:
            store.insert_batch_file(attempt, fname, want or "", status, detail)
        return _reject(store, attempt, name, manifest_sha,
                       f"package incomplete or digest mismatch: {reason}", summary)

    # ---- all files verified: publish atomically
    batch_seq = store.next_batch_seq()
    counts = {"ok": 0, "unsupported": 0, "invalid": 0, "uninterpretable": 0,
              "retransmitted": 0}
    try:
        store.insert_batch(attempt, batch_seq, name, manifest_sha, "published", None, _utcnow())
        for fname, want, status, detail in file_records:
            store.insert_batch_file(attempt, fname, want, status, detail)
        for fname, want, _, _ in file_records:
            path = os.path.join(base_dir, fname)
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            for raw_event in _split_event_objects(doc):
                kind = _ingest_one(store, evidence, expander, units,
                                   raw_event, batch_seq, fname)
                counts[kind] += 1
        for down_e, down_l, down_i, up_e, up_l, up_i in link_rows:
            store.insert_link(batch_seq, down_e, down_l, down_i, up_e, up_l, up_i)
        store.conn.commit()
    except Exception:
        store.conn.rollback()
        raise
    summary.update({"status": "published", "batch_seq": batch_seq,
                    "events": counts, "lineage_links": len(link_rows)})
    return summary


def _reject(store, attempt, name, manifest_sha, reason, summary):
    store.insert_batch(attempt, None, name, manifest_sha, "rejected", reason, _utcnow())
    store.conn.commit()
    summary.update({"status": "rejected", "reason": reason})
    return summary


def _ingest_one(store, evidence, expander, units, raw_event, batch_seq, file_name):
    raw_text = canonical_json(raw_event)
    # 1) expand offline
    try:
        expanded = expander.expand(raw_event)
    except JsonLdError as exc:
        digest = evidence.put_raw(raw_text)
        reason = ("unknown context (offline whitelist)" if is_unknown_context_error(exc)
                  else f"JSON-LD expansion failed: {exc.args[0] if exc.args else exc}")
        store.insert_event(
            event_id=None, content_digest=digest, status=P.UNINTERPRETABLE,
            reason=reason, etype=None, action=None, event_us=None,
            event_time=None, record_time=None, payload=None, first_batch=batch_seq)
        ev_pk = _last_pk(store)
        store.add_provenance(ev_pk, batch_seq, file_name)
        return "uninterpretable"

    digest = sha256_bytes(canonical_json(expanded).encode("utf-8"))
    event_id = _extract_event_id(expanded)

    # 2) dedup: identical expanded content -> full retransmission
    existing = store.find_event(event_id, digest)
    if existing is not None:
        store.add_provenance(existing["event_pk"], batch_seq, file_name)
        return "retransmitted"

    # 3) profile validation
    status, reason, payload = P.OK, None, None
    etype = action = None
    event_us = event_time = record_time = None
    try:
        ev = P.parse_event(expanded, units)
        payload = json.dumps(ev.to_payload(), sort_keys=True)
        etype, action = ev.etype, ev.action
        event_us, event_time, record_time = ev.event_us, ev.event_time, ev.record_time
    except P.UnsupportedError as exc:
        status, reason = P.UNSUPPORTED, exc.reason
    except P.ProfileError as exc:
        status, reason = P.INVALID, exc.reason

    # store original text + expanded form under the content digest (shared evidence)
    _put_evidence(evidence, digest, raw_text, expanded)
    store.insert_event(
        event_id=event_id, content_digest=digest, status=status, reason=reason,
        etype=etype, action=action, event_us=event_us, event_time=event_time,
        record_time=record_time, payload=payload, first_batch=batch_seq)
    ev_pk = _last_pk(store)
    store.add_provenance(ev_pk, batch_seq, file_name)
    return "ok" if status == P.OK else status


def _put_evidence(evidence, digest, raw_text, expanded):
    """Persist original text and expanded form under the content digest."""
    import os
    raw_path = evidence._path(digest, "raw")
    if not os.path.exists(raw_path):
        tmp = raw_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(raw_text)
        os.replace(tmp, raw_path)
    evidence.put_expanded(digest, expanded)


def _last_pk(store):
    return store.conn.execute("SELECT last_insert_rowid() AS pk").fetchone()["pk"]
