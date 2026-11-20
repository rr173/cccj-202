"""Make the vendored pure-python dependencies importable.

The warehouse ships its runtime dependencies (pyld, cachetools,
frozendict, lxml) under <repo>/vendor so the whole directory can be
copied and reopened without any network access or installation step.
"""
import os
import sys

_VENDOR_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")


def ensure_vendor_path():
    if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
        sys.path.insert(0, _VENDOR_DIR)


ensure_vendor_path()
