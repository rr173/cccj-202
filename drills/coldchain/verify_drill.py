#!/usr/bin/env python3
"""Independent cold-chain drill verifier.

Checks the hand-written expected.json against the cold-chain output
FILES only (coverage.jsonl, daily.jsonl, alarms.jsonl, summary.json,
evidence.jsonl).  It never imports or calls any warehouse function, so
the expectations remain an independent oracle -- including the headline
E timeline, which is derived purely from the spec's narrative rather
than from any production decision function.

Usage: verify_drill.py [workdir] [expected.json]
"""
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))

W_START = datetime.fromisoformat("2026-01-01T23:50:00+00:00")


def read_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def minute(iso_text):
    dt = datetime.fromisoformat(iso_text)
    return int((dt - W_START).total_seconds()) // 60


def owner_label(owner):
    return None if owner is None else owner.rsplit(":", 1)[-1]


def main():
    work = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "work")
    expected_path = sys.argv[2] if len(sys.argv) > 2 \
        else os.path.join(HERE, "expected.json")
    with open(expected_path, encoding="utf-8") as fh:
        spec = json.load(fh)
    failures = []

    def check(cond, msg):
        print(f"  {'PASS' if cond else 'FAIL'} {msg}")
        if not cond:
            failures.append(msg)

    out = os.path.join(work, "out", "run1")
    coverage = read_jsonl(os.path.join(out, "coverage.jsonl"))
    daily = read_jsonl(os.path.join(out, "daily.jsonl"))
    alarms = read_jsonl(os.path.join(out, "alarms.jsonl"))
    with open(os.path.join(out, "summary.json"), encoding="utf-8") as fh:
        summary = json.load(fh)
    evidence = {r["ref"]: r for r in
                read_jsonl(os.path.join(out, "evidence.jsonl"))}

    # ---- coverage: contiguous, gap-free half-open coverage per EPC
    by_epc = {}
    for row in coverage:
        by_epc.setdefault(row["epc"], []).append(row)
    check(set(by_epc) == set(spec["coverage"]),
          "one coverage group per inspected EPC")
    for epc, rows in by_epc.items():
        rows.sort(key=lambda r: r["start"])
        check(rows[0]["start"] == spec["window"]["start"]
              and rows[-1]["end"] == spec["window"]["end"],
              f"{epc[-1]} covers the whole window")
        for a, b in zip(rows, rows[1:]):
            check(a["end"] == b["start"], f"{epc[-1]} no gap between segments")
        check(sum(r["duration_us"] for r in rows)
              == spec["window"]["duration_minutes"] * 60 * 1_000_000,
              f"{epc[-1]} segment durations sum to the window")

        # merge midnight splits back into logical (state/owner/temp) runs
        merged = []
        for r in rows:
            key = (r["state"], r["temp_mc"], owner_label(r["owner"]))
            if merged and merged[-1][0] == key and merged[-1][2] == minute(r["start"]):
                merged[-1][2] = minute(r["end"])
            else:
                merged.append([key, minute(r["start"]), minute(r["end"])])
        # expected rows are [start, end, state, temp_mc, owner]
        got = [[m[1], m[2], m[0][0], m[0][1], m[0][2]] for m in merged]
        want = spec["coverage"][epc]
        check(got == want, f"{epc[-1]} logical timeline equals hand-written "
              f"expectation" + ("" if got == want else
                                f"\n     want {want}\n     got  {got}"))

    # ---- every evidence reference resolves to a shared evidence record
    unresolved = set()
    for row in coverage:
        for ref in row["evidence"].values():
            if isinstance(ref, list):
                unresolved.update(x for x in ref if x not in evidence)
            elif ref not in evidence:
                unresolved.add(ref)
    check(not unresolved, f"all coverage evidence refs resolve ({unresolved})")
    # conflict evidence keeps BOTH conflicting values and both input lines
    conflicts = [r for r in evidence.values() if r["kind"] == "sample"
                 and r["status"] == "conflict"]
    check(len(conflicts) == 1 and conflicts[0]["values_mc"] == [4000, 9000],
          "the minute-12 conflict keeps both conflicting values (no value picked)")

    # ---- alarms: identity, threshold and ownership split
    check(len(alarms) == len(spec["alarms"]),
          f"exactly {len(spec['alarms'])} alarms delivered")
    got_alarms = {}
    for a in alarms:
        epc = a["epc"]
        bo = {owner_label(o["owner"]): o["duration_us"] // 60_000_000
              for o in a["by_owner"]}
        bod = [{"owner": owner_label(x["owner"]), "day": x["day"],
                "minutes": x["duration_us"] // 60_000_000}
               for x in a["by_owner_day"]]
        got_alarms[epc] = {
            "start_minute": minute(a["start"]), "end_minute": minute(a["end"]),
            "duration_minutes": a["duration_us"] // 60_000_000,
            "by_owner": bo, "by_owner_day": bod,
            "meets_threshold": a["duration_us"] >= a["threshold_seconds"] * 1_000_000}
    for want in spec["alarms"]:
        got = got_alarms.get(want["epc"])
        check(got is not None, f"{want['epc'][-1]} alarm present")
        if got:
            for k in ("start_minute", "end_minute", "duration_minutes", "by_owner"):
                check(got[k] == want[k],
                      f"{want['epc'][-1]} alarm {k} == {want[k]} (got {got[k]})")
            # compare owner/day rows as sets so list order is not load-bearing
            check(sorted(tuple(d.items()) for d in got["by_owner_day"])
                  == sorted(tuple(d.items()) for d in want["by_owner_day"]),
                  f"{want['epc'][-1]} alarm owner/day split matches "
                  f"want={want['by_owner_day']} got={got['by_owner_day']}")
            check(got["meets_threshold"],
                  f"{want['epc'][-1]} reaches its 4-minute threshold")
    # the sub-threshold runs must never appear as alarms
    check({a["epc"] for a in alarms} == {a["epc"] for a in spec["alarms"]},
          "the 3-minute runs (E/F after midnight, ...) do not create extra alarms")

    # ---- daily account: the state durations sum to the window per EPC/day
    sums = {}
    for r in daily:
        key = (r["epc"], r["day"])
        sums[key] = sums.get(key, 0) + r["duration_us"]
    for (epc, day), total in sums.items():
        check(total == 10 * 60 * 1_000_000,
              f"{epc[-1]} {day}: all state durations sum to the 10-minute day slice")
    check(len({d for _e, d in sums}) == 2, "the window crosses exactly two UTC dates")
    # hand-written E daily rows
    e_daily = {}
    for r in daily:
        if r["epc"] == "urn:example:epc:E":
            e_daily.setdefault((r["day"], owner_label(r["owner"])), {})[
                r["state"]] = r["duration_us"] // 60_000_000
    for day, owners in spec["daily"]["urn:example:epc:E"].items():
        for owner, states in owners.items():
            check(e_daily.get((day, owner)) == states,
                  f"E daily {day} {owner}: {states}")

    # ---- summary counts and unknown-owner column rule
    for k, v in spec["summary_counts"].items():
        check(summary["counts"].get(k) == v,
              f"summary counts.{k} == {v} (got {summary['counts'].get(k)})")
    check(summary["status"] == "built", "run1 summary status built")
    null_owner_rows = [r for r in coverage if r["owner"] is None]
    check(all(r["evidence"].get("ownership") is None for r in null_owner_rows),
          "unknown-owner segments carry no ownership evidence")

    # ---- determinism: reversed-line run is byte-identical
    for name in ("coverage", "daily", "alarms", "evidence"):
        with open(os.path.join(work, "out", "run1", f"{name}.jsonl"), "rb") as fa, \
                open(os.path.join(work, "out", "run2", f"{name}.jsonl"), "rb") as fb:
            check(fa.read() == fb.read(), f"run1 == run2: {name}.jsonl byte-identical")

    # ---- fatal inputs deliver no result files, only a failed summary
    for sub, code in spec["failures"].items():
        d = os.path.join(work, "out", sub)
        with open(os.path.join(d, "summary.json"), encoding="utf-8") as fh:
            bad = json.load(fh)
        check(bad["status"] == "failed" and bad["error"]["code"] == code,
              f"{sub} fails with {code}")
        check(not os.path.exists(os.path.join(d, "coverage.jsonl"))
              and not os.path.exists(os.path.join(d, "alarms.jsonl")),
              f"{sub} delivers no coverage/alarm files")

    print()
    if failures:
        print(f"{len(failures)} FAILURES")
        return 1
    print("all cold-chain drill checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
