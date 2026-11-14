"""Compare the sensor detector with the IGC: who knew first, by how much, and how close the reading was.

Usage:
    python src/compare.py
    python src/compare.py logs/run5      # an earlier run kept in its own folder

Reads detections.jsonl (detect.py) and sightings.jsonl (listen.py) from runs made at the same time.
monitor.py uses earthquakes() for its table. Runs 1 to 5 were written by the three-station detector: their
rows are read as well as they map, and their own figures are the ones in README.md (Results).
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WINDOW = (-80, 110)           # seconds from the IGC's time for an earthquake to the station's trigger: 20 to 55 s by the waves
                              # alone for four entries in five; the IGC's first time can be a minute late (seen: 52 s on 2026-10-11)
EXPECT = 34                   # seconds from an Azuero earthquake to its first wave at the station
FITS = 0.8                    # a trigger is an entry's own only if what it read is within this of the listed magnitude:
                              # in the sequence a window that wide holds some unrelated trigger seven times in ten


def parse(text):
    """Time written by either program (with or without an offset) as an aware datetime."""
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def modern(row):
    """A row as the three-station detector wrote it (runs 1 to 5), in today's shape."""
    if row["kind"] == "station":
        return {"kind": "trigger", "trigger": row["trigger"], "seen": row["seen"], "stream_delay_s": row["stream_delay_s"], "ratio": row["ratio"]}
    if "first_trigger" in row:
        return {"kind": row["kind"], "trigger": row["first_trigger"], "signal": row["seen"], "seen": row["seen"], "reading": row["quick_mag"]}
    return row


def load(folder):
    read = lambda name: [json.loads(line) for line in (folder / name).open(encoding="utf-8")] if (folder / name).exists() else []
    return [modern(row) for row in read("detections.jsonl")], read("sightings.jsonl")


def earthquakes(det, sightings, since=None):
    """One row per earthquake any feed reported in Panama, with what the sensor did, oldest first.

    Returns (rows, lone): `lone` are the sensor's alerts that match no reported earthquake.
    Delays are seconds after the earthquake's own time; a feed that already listed the earthquake
    when the listener started has no measured delay (None)."""
    rows = []
    for s in sightings:
        if not s["panama"] or (since and parse(s["origin"]) < since):
            continue
        # Go by time and place, not by the listener's name for the earthquake: a listener started again gives an
        # entry the IGC has re-timed by a second a new name, and the earthquake would show twice.
        at = parse(s["origin"]).timestamp()
        row = next((r for r in rows if abs(r["at"] - at) <= 12 and abs(r["lat"] - s["lat"]) <= 1 and abs(r["lon"] - s["lon"]) <= 1), None)
        if row is None:
            row = {"event": s["event"], "origin": s["origin"], "at": at, "lat": s["lat"], "lon": s["lon"], "by": {}, "sensor": None}
            rows.append(row)
        mine = row["by"].setdefault(s["source"], {"delay_s": None, "seen": None, "first_mag": None})
        if s["kind"] != "revision" and not s["startup"]:                               # this feed's first report, seen live
            mine.update(delay_s=s["delay_s"], seen=s["seen"], first_mag=s["mag"])
        mine.update(mag=s["mag"], status=s["status"])                                  # the newest values of that feed
        if s["source"] == "igc" or "igc" not in row["by"]:
            row.update(mag=s["mag"], place=s["place"], lat=s["lat"], lon=s["lon"], depth=s["depth"])
    triggers = [d for d in det if d["kind"] == "trigger"]
    declared = [d for d in det if d["kind"] == "declared"]
    final = {d["trigger"]: d["reading"] for d in det if d["kind"] in ("declared", "update", "closed")}      # the last one written wins
    rows.sort(key=lambda r: r["at"])
    listed, onset = [r["at"] for r in rows], [parse(d["trigger"]).timestamp() for d in triggers]

    def fits(k, n):
        """Can trigger n be entry k's? By size if the trigger was read; if not (the detector stopped while following it), only by the waves' own timing."""
        gap, read = onset[n] - listed[k], final.get(triggers[n]["trigger"])
        if read is None:
            return 20 <= gap <= 55
        return WINDOW[0] <= gap <= WINDOW[1] and (rows[k].get("mag") is None or abs(read - rows[k]["mag"]) <= FITS)

    # A trigger belongs to one entry at most. Among the pairs possible, those closest to what the waves allow are made first.
    possible = sorted((abs(onset[n] - listed[k] - EXPECT), k, n) for k in range(len(rows)) for n in range(len(triggers)) if fits(k, n))
    paired, taken, used = set(), set(), set()
    for _, k, n in possible:
        if k in paired or n in taken:
            continue
        paired.add(k)
        taken.add(n)
        row, best = rows[k], triggers[n]
        after = lambda d, field: parse(d[field]).timestamp() - listed[k]
        row["sensor"] = {"trigger_seen_s": after(best, "seen"), "reading": final.get(best["trigger"]), "declared_s": None}
        decl = next((d for d in declared if d["trigger"] == best["trigger"]), None)
        if decl:
            used.add(id(decl))
            row["sensor"].update(declared_s=after(decl, "seen"), declared_seen=decl["seen"], first_reading=decl["reading"])
    lone = [d for d in declared if id(d) not in used]
    return rows, lone


def main():
    det, sightings = load(HERE / sys.argv[1] if len(sys.argv) > 1 else HERE / "logs")
    started = max(parse(min(d["seen"] for d in det)), parse(min(s["seen"] for s in sightings)))
    ended = min(parse(max(d["seen"] for d in det)), parse(max(s["seen"] for s in sightings)))
    print(f"both programs ran from {started:%H:%M:%S} to {ended:%H:%M:%S} UTC")
    rows, lone = earthquakes(det, sightings, started)
    rows = [r for r in rows if r["by"].get("igc", {}).get("delay_s") is not None]          # first seen live at the IGC
    print(f"\nIGC earthquakes in that time: {len(rows)} | sensor triggers: {sum(d['kind'] == 'trigger' for d in det)} | "
          f"sensor alerts: {sum(d['kind'] == 'declared' for d in det)}\n")
    print("Seconds count from the time the IGC gives the earthquake; a trigger before it means that time is late.")
    print("earthquake (UTC)  IGC mag | sensor trigger known | alert written | IGC published | head start | sensor reading")
    for r in rows:
        s, published = r["sensor"], r["by"]["igc"]["delay_s"]
        first = f"{s['trigger_seen_s']:+4.0f} s" if s else " none "
        reading = f"M{s['reading']:.1f}" if s and s["reading"] is not None else "-"
        if s and s["declared_s"] is not None:
            cell = f"{s['declared_s']:+4.0f} s | +{published:3.0f} s | {published - s['declared_s']:+5.0f} s | {reading}"
        else:
            cell = f"  no    | +{published:3.0f} s |    -    | {reading}{', below the announcing level' if s else ''}"
        print(f"{parse(r['origin']):%H:%M:%S}          M{r['by']['igc']['first_mag']:<4} | {first:20s} | {cell}")
    lone = [d for d in lone if parse(d["seen"]) >= started]
    print(f"\nalerts with no IGC earthquake: {len(lone)}", [(d["signal"][11:19], d["reading"]) for d in lone])
    delays = sorted(d["stream_delay_s"] for d in det if d["kind"] == "trigger")
    if delays:
        print(f"signal arrives {delays[len(delays) // 2]:.0f} s after it is recorded (median; from {delays[0]:.0f} to {delays[-1]:.0f} s)")


if __name__ == "__main__":
    main()
