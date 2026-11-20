"""Warehouse facade: create/open, ingest, query, review, evidence.

A warehouse is a self-contained directory (SQLite db + evidence store +
a copy of the declared unit table).  The directory together with the
bundled contexts can be copied and reopened anywhere offline.
"""
from __future__ import annotations

import json
import os
import shutil

from .evidence import EvidenceStore
from .ingest import ingest_manifest
from .jsonld_offline import OfflineExpander
from .store import Store
from .units import UnitTable
from . import parse as P
from . import query as Q


def _parse_instant(text):
    dt = P._parse_time(text, "t")
    return P._epoch_us(P._to_utc(dt))


class Warehouse:
    def __init__(self, root):
        self.root = root
        self.store = Store(root)
        self.evidence = EvidenceStore(root)
        cfg_path = os.path.join(root, "config.json")
        with open(cfg_path, "r", encoding="utf-8") as fh:
            self._config = json.load(fh)
        self.units = UnitTable.from_file(os.path.join(root, self._config["units_file"]))
        self.expander = OfflineExpander()

    # ---------------------------------------------------------- lifecycle
    @classmethod
    def create(cls, root, unit_table_path):
        if os.path.exists(root) and os.listdir(root):
            raise FileExistsError(f"warehouse directory not empty: {root}")
        os.makedirs(root, exist_ok=True)
        units_dest = "units.json"
        shutil.copyfile(unit_table_path, os.path.join(root, units_dest))
        with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as fh:
            json.dump({"units_file": units_dest, "profile": "epcis-offline/1"}, fh)
        return cls(root)

    @classmethod
    def open(cls, root):
        return cls(root)

    def close(self):
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------- ingest
    def ingest(self, manifest_path):
        return ingest_manifest(self, manifest_path)

    # ---------------------------------------------------------- queries
    def _batch(self, batch):
        if batch in (None, "latest"):
            latest = self.store.latest_batch()
            if latest is None:
                raise ValueError("no published batch yet")
            return latest
        return int(batch)

    def ancestors(self, epc, t, batch="latest"):
        return Q.query_ancestors(self.store, self.evidence, epc,
                                 _parse_instant(t), self._batch(batch))

    def children(self, parent, t, batch="latest"):
        return Q.query_children(self.store, self.evidence, parent,
                                _parse_instant(t), self._batch(batch), self.units)

    def trace(self, item, t, batch="latest"):
        it = Q.parse_item(item)
        return Q.query_trace(self.store, self.evidence, it["kind"], it["id"],
                             _parse_instant(t), self._batch(batch), self.units)

    def transformation(self, event_id, t, batch="latest"):
        return Q.query_transformation(self.store, self.evidence, event_id,
                                      _parse_instant(t), self._batch(batch), self.units)

    def observations(self, item, t, batch="latest"):
        it = Q.parse_item(item)
        return Q.query_observations(self.store, self.evidence, it["kind"], it["id"],
                                    _parse_instant(t), self._batch(batch))

    def event(self, event_id):
        return Q.query_event(self.store, self.evidence, event_id)

    def review(self, batch=None):
        b = None if batch in (None, "latest") else int(batch)
        return Q.review(self.store, self.evidence, b)

    def evidence_doc(self, digest):
        return self.evidence.get(digest)
