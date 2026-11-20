"""Command line interface: the stable public surface of the warehouse.

All commands print JSON on stdout.  The independent drill verifier talks
to the warehouse exclusively through this CLI.
"""
from __future__ import annotations

import argparse
import json
import sys

from .warehouse import Warehouse


def _emit(obj):
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="epcis-warehouse")
    ap.add_argument("--root", required=True, help="warehouse directory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init")
    p.add_argument("--units", required=True, help="declared unit table JSON")

    p = sub.add_parser("ingest")
    p.add_argument("manifest")

    q = sub.add_parser("query")
    qsub = q.add_subparsers(dest="qcmd", required=True)
    for name in ("ancestors", "children", "trace", "transformation", "observations"):
        sp = qsub.add_parser(name)
        sp.add_argument("--t", required=True, help="business time ISO 8601 with offset")
        sp.add_argument("--batch", default="latest")
    qsub.choices["ancestors"].add_argument("--epc", required=True)
    qsub.choices["children"].add_argument("--parent", required=True)
    qsub.choices["trace"].add_argument("--item", required=True,
                                       help="EPC IRI, or class:<class IRI>")
    qsub.choices["transformation"].add_argument("--event", required=True)
    qsub.choices["observations"].add_argument("--item", required=True)

    p = sub.add_parser("event")
    p.add_argument("--id", required=True)

    p = sub.add_parser("review")
    p.add_argument("--batch", default=None)

    p = sub.add_parser("evidence")
    p.add_argument("--digest", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "init":
        wh = Warehouse.create(args.root, args.units)
        _emit({"status": "created", "root": args.root})
        wh.close()
        return 0

    wh = Warehouse.open(args.root)
    try:
        if args.cmd == "ingest":
            _emit(wh.ingest(args.manifest))
        elif args.cmd == "query":
            if args.qcmd == "ancestors":
                _emit(wh.ancestors(args.epc, args.t, args.batch))
            elif args.qcmd == "children":
                _emit(wh.children(args.parent, args.t, args.batch))
            elif args.qcmd == "trace":
                _emit(wh.trace(args.item, args.t, args.batch))
            elif args.qcmd == "transformation":
                _emit(wh.transformation(args.event, args.t, args.batch))
            elif args.qcmd == "observations":
                _emit(wh.observations(args.item, args.t, args.batch))
        elif args.cmd == "event":
            _emit(wh.event(args.id))
        elif args.cmd == "review":
            _emit(wh.review(args.batch))
        elif args.cmd == "evidence":
            _emit(wh.evidence_doc(args.digest))
    finally:
        wh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
