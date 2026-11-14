"""Measure how late each feed was, from what listen.py recorded in logs/sightings.jsonl.

Usage:
    python src/latency.py                # the current log
    python src/latency.py logs/run4      # an earlier run kept in its own folder

Only earthquakes that happened while the listener was running count: an event already listed when the
listener started has no known arrival time.
"""
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(folder):
    rows = [json.loads(line) for line in (folder / "sightings.jsonl").open(encoding="utf-8")]
    started = min(r["seen"] for r in rows)
    first = {}                                          # (event, source) -> first live sighting
    for r in rows:
        if r["startup"] or r["kind"] == "revision" or r["origin"] < started or r["delay_s"] > 6 * 3600 or not r["panama"]:
            continue
        first.setdefault((r["event"], r["source"]), r)
    events = defaultdict(dict)
    for (event, source), r in first.items():
        events[event][source] = r
    print(f"listener ran from {started[11:19]} to {max(r['seen'] for r in rows)[11:19]} UTC; "
          f"{len(events)} Panama earthquakes happened in that time\n")
    delays, wins = defaultdict(list), defaultdict(int)
    for event in sorted(events):
        by = events[event]
        order = sorted(by.values(), key=lambda r: r["seen"])
        wins[order[0]["source"]] += 1
        for r in order:
            delays[r["source"]].append(r["delay_s"])
        mags = {r["source"]: r["mag"] for r in order}
        where = next((r["place"] for r in order if r["place"]), "") or f"{order[0]['lat']}, {order[0]['lon']}"
        print(f"{order[0]['origin'][11:19]}  " + "  ".join(f"{r['source']} +{r['delay_s'] // 60}:{r['delay_s'] % 60:02d} (M{r['mag']})" for r in order) + f"  | {where}")
    print("\nfeed    reported  first  median delay  fastest  slowest")
    for source, d in sorted(delays.items(), key=lambda kv: statistics.median(kv[1])):
        fmt = lambda s: f"{int(s) // 60}:{int(s) % 60:02d}"
        print(f"{source:6s}  {len(d):8d}  {wins[source]:5d}  {fmt(statistics.median(d)):>12s}  {fmt(min(d)):>7s}  {fmt(max(d)):>7s}")


if __name__ == "__main__":
    main(HERE / sys.argv[1] if len(sys.argv) > 1 else HERE / "logs")
