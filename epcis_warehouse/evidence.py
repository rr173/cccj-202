"""Content-addressed evidence store.

Every received event keeps its original text and (when interpretable)
its canonical expanded JSON-LD under evidence/<digest>.*.  Query results
cite evidence digests; many results can share the same evidence objects
instead of re-expanding every path per query.  The store is plain files,
so the whole warehouse directory can be copied and reopened.
"""
from __future__ import annotations

import hashlib
import json
import os


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class EvidenceStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(self.dir, exist_ok=True)

    @property
    def dir(self):
        return os.path.join(self.root, "evidence")

    def _path(self, digest: str, kind: str) -> str:
        return os.path.join(self.dir, f"{digest}.{kind}.json")

    def put_raw(self, raw_text: str) -> str:
        digest = sha256_bytes(raw_text.encode("utf-8"))
        path = self._path(digest, "raw")
        if not os.path.exists(path):
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(raw_text)
            os.replace(tmp, path)
        return digest

    def put_expanded(self, digest: str, expanded) -> None:
        path = self._path(digest, "expanded")
        if not os.path.exists(path):
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(canonical_json(expanded))
            os.replace(tmp, path)

    def has(self, digest: str) -> bool:
        return os.path.exists(self._path(digest, "raw"))

    def get(self, digest: str) -> dict:
        out = {"digest": digest}
        raw = self._path(digest, "raw")
        exp = self._path(digest, "expanded")
        if os.path.exists(raw):
            with open(raw, "r", encoding="utf-8") as fh:
                out["raw"] = fh.read()
        if os.path.exists(exp):
            with open(exp, "r", encoding="utf-8") as fh:
                out["expanded"] = json.load(fh)
        return out

    def describe(self, digest: str) -> dict:
        return {
            "digest": digest,
            "raw_path": self._path(digest, "raw") if os.path.exists(self._path(digest, "raw")) else None,
            "expanded_path": self._path(digest, "expanded") if os.path.exists(self._path(digest, "expanded")) else None,
        }
