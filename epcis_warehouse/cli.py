"""Command line interface: the stable public surface of the warehouse.

All commands print JSON on stdout.  The independent drill verifier talks
to the warehouse exclusively through this CLI.

Disclosure commands: ``checkpoint build`` and ``page export`` run on the
warehouse side; ``audit ...`` commands run on the auditor's side and
need no warehouse (only the workspace directory and the checkpoint file
received over a trusted channel).
"""
from __future__ import annotations

import argparse
import json
import sys

from . import audit as A
from .disclosure import DisclosureError, is_hex64
from .warehouse import Warehouse


def _emit(obj):
    json.dump(obj, sys.stdout, indent=2, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")


def _add_bound_args(sp):
    sp.add_argument("--lo-id", default=None,
                    help="lower bound event id (inclusive); unset = open")
    sp.add_argument("--lo-digest", default=None,
                    help="lower bound content digest (64 hex); with --lo-id")
    sp.add_argument("--hi-id", default=None,
                    help="upper bound event id (exclusive); unset = open")
    sp.add_argument("--hi-digest", default=None,
                    help="upper bound content digest (64 hex); with --hi-id")


def _bound_from_args(args, side):
    eid = getattr(args, f"{side}_id")
    digest = getattr(args, f"{side}_digest")
    if eid is None:
        if digest is not None:
            raise DisclosureError(f"--{side}-digest requires --{side}-id")
        return A.bound_from_json(None, is_lo=(side == "lo"))
    obj = {"event_id": eid}
    if digest is not None:
        if not is_hex64(digest):
            raise DisclosureError(f"--{side}-digest must be 64 lowercase hex characters")
        obj["digest"] = digest
    return A.bound_from_json(obj, is_lo=(side == "lo"))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="epcis-warehouse")
    ap.add_argument("--root", default=None, help="warehouse directory")
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

    p = sub.add_parser("settle")
    p.add_argument("--batch", required=True,
                   help="published receiving batch number, or 'latest'")
    p.add_argument("--opening", required=True, help="opening inventory JSONL")
    p.add_argument("--sender", required=True, help="sender-side documents JSONL")
    p.add_argument("--receiver", required=True, help="receiver-side documents JSONL")
    p.add_argument("--out", required=True, help="output directory for settlement results")

    p = sub.add_parser("coldchain")
    p.add_argument("--batch", required=True,
                   help="published receiving batch number, or 'latest'")
    p.add_argument("--window-start", required=True,
                   help="inspection window start (inclusive), ISO 8601 with offset")
    p.add_argument("--window-end", required=True,
                   help="inspection window end (exclusive), ISO 8601 with offset")
    p.add_argument("--opening", required=True,
                   help="same opening inventory JSONL used by the settle run")
    p.add_argument("--settle-summary", required=True,
                   help="summary.json of the settle run providing ownership")
    p.add_argument("--ledger", required=True,
                   help="ledger.jsonl of that settle run (posted named moves)")
    p.add_argument("--items", required=True,
                   help="inspected EPCs: epc, limit_mc, alarm_seconds")
    p.add_argument("--installations", required=True,
                   help="probe installations JSONL")
    p.add_argument("--samples", required=True, help="probe samples JSONL")
    p.add_argument("--out", required=True, help="output directory for the ledger")

    cp = sub.add_parser("checkpoint", help="selective-disclosure checkpoints")
    cpsub = cp.add_subparsers(dest="cpcmd", required=True)
    sp = cpsub.add_parser("build", help="build checkpoint + reusable disk index")
    sp.add_argument("--warehouse-id", required=True,
                    help="warehouse identifier committed into the checkpoint")
    sp.add_argument("--batch", required=True,
                    help="published receiving batch number, or 'latest'")
    sp.add_argument("--out", default=None,
                    help="also copy the checkpoint JSON to this path")

    pg = sub.add_parser("page", help="selective-disclosure pages")
    pgsub = pg.add_subparsers(dest="pgcmd", required=True)
    sp = pgsub.add_parser("export", help="export one page of a checkpoint")
    sp.add_argument("--checkpoint", required=True, help="checkpoint id")
    _add_bound_args(sp)
    sp.add_argument("--out", required=True, help="page file to write (JSONL)")

    au = sub.add_parser("audit", help="auditor-side receiving workspace")
    asub = au.add_subparsers(dest="acmd", required=True)
    sp = asub.add_parser("init", help="bind a workspace to checkpoint + request")
    sp.add_argument("--workspace", required=True)
    sp.add_argument("--checkpoint", required=True,
                    help="checkpoint JSON file (received over a trusted channel)")
    _add_bound_args(sp)
    sp = asub.add_parser("receive", help="verify and record one page")
    sp.add_argument("--workspace", required=True)
    sp.add_argument("--page", required=True)
    sp = asub.add_parser("gaps", help="show covered and missing sub-ranges")
    sp.add_argument("--workspace", required=True)
    sp = asub.add_parser("finalize", help="deliver the final inventory")
    sp.add_argument("--workspace", required=True)
    sp.add_argument("--out", required=True)

    args = ap.parse_args(argv)

    try:
        if args.cmd == "audit":
            return _run_audit(args)
        if args.cmd == "init":
            _require_root(args)
            wh = Warehouse.create(args.root, args.units)
            _emit({"status": "created", "root": args.root})
            wh.close()
            return 0

        _require_root(args)
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
            elif args.cmd == "checkpoint":
                _emit(wh.checkpoint_build(args.warehouse_id, args.batch, args.out))
            elif args.cmd == "page":
                lo = _bound_from_args(args, "lo")
                hi = _bound_from_args(args, "hi")
                _emit(wh.page_export(args.checkpoint, lo, hi, args.out))
            elif args.cmd == "settle":
                summary, code = wh.settle(args.batch, args.opening, args.sender,
                                          args.receiver, args.out)
                _emit(summary)
                return code
            elif args.cmd == "coldchain":
                summary, code = wh.coldchain(
                    args.batch, args.window_start, args.window_end, args.opening,
                    args.settle_summary, args.ledger, args.items,
                    args.installations, args.samples, args.out)
                _emit(summary)
                return code
        finally:
            wh.close()
        return 0
    except DisclosureError as exc:
        _emit({"status": "error", "reason": str(exc)})
        return 2


def _require_root(args):
    if not args.root:
        raise DisclosureError(f"command {args.cmd!r} requires --root")


def _run_audit(args):
    if args.acmd == "init":
        lo = _bound_from_args(args, "lo")
        hi = _bound_from_args(args, "hi")
        _emit(A.init_workspace(args.workspace, args.checkpoint, lo, hi))
    elif args.acmd == "receive":
        _emit(A.receive_page(args.workspace, args.page))
    elif args.acmd == "gaps":
        _emit(A.gaps(args.workspace))
    elif args.acmd == "finalize":
        summary, code = A.finalize(args.workspace, args.out)
        _emit(summary)
        return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
