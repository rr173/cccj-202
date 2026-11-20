"""SQLite persistence for the warehouse.

Events, provenance, batches and lineage links live in a single SQLite
database inside the warehouse directory.  Replay streams rows through a
cursor; the full original text never has to be held in memory (it lives
in the evidence store and is read on demand).
"""
from __future__ import annotations

import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS batches(
  attempt_id INTEGER PRIMARY KEY,
  batch_seq INTEGER,            -- dense over published batches, NULL when rejected
  name TEXT,
  manifest_sha256 TEXT,
  status TEXT,                  -- published | rejected
  reject_reason TEXT,
  created_utc TEXT
);
CREATE TABLE IF NOT EXISTS batch_files(
  attempt_id INTEGER,
  name TEXT,
  sha256 TEXT,
  status TEXT,                  -- ok | missing | digest_mismatch
  detail TEXT,
  PRIMARY KEY(attempt_id, name)
);
CREATE TABLE IF NOT EXISTS events(
  event_pk INTEGER PRIMARY KEY,
  event_id TEXT,                -- NULL when not extractable
  content_digest TEXT,          -- sha256 of canonical expanded form (raw digest when uninterpretable)
  status TEXT,                  -- ok | unsupported | invalid | uninterpretable
  reason TEXT,
  etype TEXT,
  action TEXT,
  event_us INTEGER,
  event_time TEXT,
  record_time TEXT,
  payload TEXT,                 -- normalised JSON for ok events
  first_batch INTEGER,          -- published batch_seq of first reception
  UNIQUE(event_id, content_digest)
);
CREATE INDEX IF NOT EXISTS idx_events_replay ON events(status, first_batch, event_us);
CREATE INDEX IF NOT EXISTS idx_events_id ON events(event_id);
CREATE TABLE IF NOT EXISTS provenance(
  event_pk INTEGER,
  batch_seq INTEGER,
  file_name TEXT,
  PRIMARY KEY(event_pk, batch_seq, file_name)
);
CREATE TABLE IF NOT EXISTS lineage_links(
  link_pk INTEGER PRIMARY KEY,
  batch_seq INTEGER,
  downstream_event TEXT, down_list TEXT, down_index INTEGER,
  upstream_event TEXT, up_list TEXT, up_index INTEGER
);
"""


class Store:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.db_path = os.path.join(root, "warehouse.db")
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self):
        if self._conn is not None:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def conn(self):
        return self._conn

    # ------------------------------------------------------------ batches
    def next_attempt_id(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(attempt_id),0)+1 AS n FROM batches").fetchone()
        return row["n"]

    def next_batch_seq(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(batch_seq),0)+1 AS n FROM batches WHERE status='published'"
        ).fetchone()
        return row["n"]

    def latest_batch(self) -> int | None:
        row = self._conn.execute(
            "SELECT MAX(batch_seq) AS b FROM batches WHERE status='published'"
        ).fetchone()
        return row["b"]

    def insert_batch(self, attempt_id, batch_seq, name, manifest_sha256, status, reject_reason, created_utc):
        self._conn.execute(
            "INSERT INTO batches(attempt_id,batch_seq,name,manifest_sha256,status,reject_reason,created_utc)"
            " VALUES(?,?,?,?,?,?,?)",
            (attempt_id, batch_seq, name, manifest_sha256, status, reject_reason, created_utc))

    def insert_batch_file(self, attempt_id, name, sha256, status, detail=None):
        self._conn.execute(
            "INSERT INTO batch_files(attempt_id,name,sha256,status,detail) VALUES(?,?,?,?,?)",
            (attempt_id, name, sha256, status, detail))

    # ------------------------------------------------------------ events
    def find_event(self, event_id, content_digest):
        return self._conn.execute(
            "SELECT * FROM events WHERE event_id IS ? AND content_digest=?",
            (event_id, content_digest)).fetchone()

    def insert_event(self, *, event_id, content_digest, status, reason, etype, action,
                     event_us, event_time, record_time, payload, first_batch) -> int:
        cur = self._conn.execute(
            "INSERT INTO events(event_id,content_digest,status,reason,etype,action,event_us,"
            "event_time,record_time,payload,first_batch) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, content_digest, status, reason, etype, action,
             event_us, event_time, record_time, payload, first_batch))
        return cur.lastrowid

    def add_provenance(self, event_pk, batch_seq, file_name):
        self._conn.execute(
            "INSERT OR IGNORE INTO provenance(event_pk,batch_seq,file_name) VALUES(?,?,?)",
            (event_pk, batch_seq, file_name))

    def insert_link(self, batch_seq, downstream_event, down_list, down_index,
                    upstream_event, up_list, up_index):
        self._conn.execute(
            "INSERT INTO lineage_links(batch_seq,downstream_event,down_list,down_index,"
            "upstream_event,up_list,up_index) VALUES(?,?,?,?,?,?,?)",
            (batch_seq, downstream_event, down_list, down_index,
             upstream_event, up_list, up_index))

    # ------------------------------------------------------------ views
    def conflicted_event_ids(self, batch_seq: int) -> dict:
        """event_id -> [content_digest,...] for ids with >1 distinct contents.

        Computed over everything received up to and including batch_seq, so
        earlier batch views never learn about later conflicts.
        """
        out = {}
        cur = self._conn.execute(
            "SELECT event_id, content_digest FROM events"
            " WHERE event_id IS NOT NULL AND status != 'uninterpretable' AND first_batch<=?"
            " ORDER BY event_id", (batch_seq,))
        for row in cur:
            out.setdefault(row["event_id"], set()).add(row["content_digest"])
        return {eid: sorted(ds) for eid, ds in out.items() if len(ds) > 1}

    def iter_replay(self, batch_seq: int, event_us_max: int, exclude_ids):
        """Stream ok events for replay, ordered by business time."""
        sql = ("SELECT event_id, payload, event_us FROM events"
               " WHERE status='ok' AND first_batch<=? AND event_us<=?")
        params = [batch_seq, event_us_max]
        if exclude_ids:
            marks = ",".join("?" for _ in exclude_ids)
            sql += f" AND event_id NOT IN ({marks})"
            params.extend(sorted(exclude_ids))
        sql += " ORDER BY event_us, event_id"
        return self._conn.execute(sql, params)

    def iter_events_of_batch_view(self, batch_seq: int):
        return self._conn.execute(
            "SELECT * FROM events WHERE first_batch<=? ORDER BY event_us, event_id",
            (batch_seq,))

    def event_rows_by_id(self, event_id):
        return self._conn.execute(
            "SELECT * FROM events WHERE event_id IS ? ORDER BY first_batch, content_digest",
            (event_id,)).fetchall()

    def provenance_of(self, event_pk):
        return self._conn.execute(
            "SELECT batch_seq, file_name FROM provenance WHERE event_pk=? ORDER BY batch_seq, file_name",
            (event_pk,)).fetchall()

    def links_for_view(self, batch_seq: int):
        return self._conn.execute(
            "SELECT * FROM lineage_links WHERE batch_seq<=? ORDER BY link_pk",
            (batch_seq,)).fetchall()

    def batches_summary(self):
        return self._conn.execute(
            "SELECT * FROM batches ORDER BY attempt_id").fetchall()

    def batch_files(self, attempt_id):
        return self._conn.execute(
            "SELECT * FROM batch_files WHERE attempt_id=? ORDER BY name",
            (attempt_id,)).fetchall()

    def events_summary_by_status(self, batch_seq=None):
        if batch_seq is None:
            return self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM events GROUP BY status").fetchall()
        return self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM events WHERE first_batch<=? GROUP BY status",
            (batch_seq,)).fetchall()
