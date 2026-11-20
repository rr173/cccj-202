"""Offline JSON-LD expansion.

Expansion uses pyld (a standard JSON-LD processor) with a document
loader that only serves the bundled fixed contexts and the explicit
whitelist IRIs.  No network access is ever attempted: an unknown
context IRI raises :class:`UnknownContextError`, which pyld wraps into a
``JsonLdError`` with code ``loading remote context failed``; the caller
classifies such events as *uninterpretable* and keeps the original text.
"""
from __future__ import annotations

import json
import os

from . import _vendor  # noqa: F401  (ensures vendored deps are importable)
from pyld import jsonld
from pyld.jsonld import JsonLdError

_CONTEXT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contexts")


class UnknownContextError(Exception):
    """Raised by the offline loader for any non-whitelisted IRI."""

    def __init__(self, url):
        super().__init__(f"context IRI not in offline whitelist: {url}")
        self.url = url


class OfflineExpander:
    """Expands JSON-LD documents using only bundled/whitelisted contexts."""

    def __init__(self, whitelist_path: str | None = None):
        whitelist_path = whitelist_path or os.path.join(_CONTEXT_DIR, "whitelist.json")
        with open(whitelist_path, "r", encoding="utf-8") as fh:
            wl = json.load(fh)
        self._contexts = {}
        for iri, rel in wl["contexts"].items():
            path = os.path.join(_CONTEXT_DIR, rel)
            with open(path, "r", encoding="utf-8") as fh:
                self._contexts[iri] = json.load(fh)

    @property
    def known_iris(self):
        return sorted(self._contexts)

    def _loader(self, url, options=None):
        if url in self._contexts:
            return {
                "contentType": "application/ld+json",
                "contextUrl": None,
                "documentUrl": url,
                "document": self._contexts[url],
            }
        raise UnknownContextError(url)

    def expand(self, document):
        """Expand a parsed JSON-LD document. Raises JsonLdError on failure."""
        return jsonld.expand(document, options={"documentLoader": self._loader})


def is_unknown_context_error(exc: BaseException) -> bool:
    """True if the JsonLdError was caused by a non-whitelisted context IRI."""
    if not isinstance(exc, JsonLdError):
        return False
    if exc.code != "loading remote context failed":
        return False
    cause = exc.__cause__
    return isinstance(cause, UnknownContextError) or (
        isinstance(cause, JsonLdError) and cause.code == "loading document failed"
    )
