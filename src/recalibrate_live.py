"""Would a new magnitude line read better than the one in calibration.json? A study, read-only on the logs and the
recordings: it changes nothing that runs. Asked by the maintainer on 2026-10-14 ("recalibrar con los sismos medidos en vivo...
antes de activarlo pruébalo con los días pasados").

    python src/recalibrate_live.py            # prints the comparison, writes calibration_candidate.json

The data: every IGC earthquake paired with a trigger of the station (the fresh trigger nearest to 34 s after the
listed time, 20 to 55 s, one to one, by time alone), from two sources:
- the live detector's log since 2026-10-11 (closed triggers: the whole wave's peak in counts, and the first 15 s
  given back as counts through the line that read them), without the IGC's withdrawn reports;
- the recordings of 2026-10-07 18:00 to 2026-10-11 00:17 UTC (data/, the ones calibration.json was fitted on), read
  here the same way, which hold the sequence's large earthquakes.
Lines compared, for the whole wave and for the first 15 s alike: the one in use; a straight line fitted afresh; and
a line with a bend (a slope for small motions, another above a point chosen by fit). Each is tried day by day: fitted
on the other days, judged on that day. The size to match is the IGC's latest (its revised figure).
"""
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from obspy import UTCDateTime

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import detector_core as core
import recordings

sys.stdout.reconfigure(encoding="utf-8")
LOGS = HERE / "logs"
LINE = json.loads((HERE / "calibration.json").read_text())["line"]
REMOVED = {"20261013T165929", "20261014T033730"}          # withdrawn by the IGC, not in the USGS catalogue (README.md (Results), section 1)
LEVEL = 4.7
BCIP = (9.167, -79.837)
REC = (UTCDateTime("2026-10-07T18:00:00"), UTCDateTime("2026-10-11T00:17:43"))


def when(text):
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def km(lat, lon):
    p1, p2 = math.radians(BCIP[0]), math.radians(lat)
    d = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon - BCIP[1]) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(d))


def pair(events, triggers):
    """One to one, the trigger nearest to 34 s after each listed time, 20 to 55 s after it."""
    cands = sorted((abs(t["t"] - e["t"] - 34), i, j) for i, e in enumerate(events) for j, t in enumerate(triggers) if 20 <= t["t"] - e["t"] <= 55)
    pe, pt, out = set(), set(), []
    for _, i, j in cands:
        if i not in pe and j not in pt:
            pe.add(i); pt.add(j); out.append((events[i], triggers[j]))
    return out, pt


def live_rows():
    det = [json.loads(l) for l in (LOGS / "detections.jsonl").open(encoding="utf-8")]
    closed = {d["trigger"]: d for d in det if d["kind"] == "closed"}
    declared = {d["trigger"] for d in det if d["kind"] == "declared"}
    trig = [{"t": when(d["trigger"]).timestamp(), "id": d["trigger"]} for d in det
            if d["kind"] == "trigger" and d["trigger"] in closed and d["stream_delay_s"] <= 60 and closed[d["trigger"]].get("first_wave")]
    start = min(t["t"] for t in trig)
    igc = {}
    for s in (json.loads(l) for l in (LOGS / "sightings.jsonl").open(encoding="utf-8")):
        if s["source"] == "igc" and s.get("panama") and when(s["origin"]).timestamp() >= start - 60 and s["event"] not in REMOVED:
            e = igc.setdefault(s["event"], {"t": when(s["origin"]).timestamp(), "first": s["mag"], "lat": s["lat"], "lon": s["lon"]})
            e["latest"] = s["mag"]
    events = sorted(igc.values(), key=lambda e: e["t"])
    pairs, used = pair(events, trig)
    rows = []
    for e, t in pairs:
        c = closed[t["id"]]
        early = 10 ** ((c["first_wave"] - LINE["first_offset"]) / LINE["first_slope"])
        rows.append({"src": "live", "t": e["t"], "first": e["first"], "latest": e["latest"], "km": km(e["lat"], e["lon"]),
                     "xw": math.log10(max(c["peak"], 1)), "xf": math.log10(max(early, 1)), "alerted": t["id"] in declared})
    lone = []                                              # triggers with no IGC earthquake: what each line would make of them
    for j, t in enumerate(trig):
        if j not in used:
            c = closed[t["id"]]
            lone.append({"xw": math.log10(max(c["peak"], 1)), "xf": math.log10(max(10 ** ((c["first_wave"] - LINE["first_offset"]) / LINE["first_slope"]), 1))})
    return rows, lone, (start, max(t["t"] for t in trig))


def recorded_rows():
    cat = json.loads((recordings.DATA / "igc_catalogue.json").read_text(encoding="utf-8"))
    events = [{"t": UTCDateTime(e["origin"]).timestamp, "first": e["mag"], "latest": e["mag"], "lat": e["lat"], "lon": e["lon"]}
              for e in cat if REC[0] + 300 <= UTCDateTime(e["origin"]) <= REC[1] - 200]
    events.sort(key=lambda e: e["t"])
    trig, peaks = [], {}
    for e in events:                                       # each earthquake's own stretch, read as the live detector reads it
        a = UTCDateTime(e["t"]) - 200
        for tr in recordings.load(core.STATION, a, a + 360):
            if tr.stats.npts < 2 * core.LTA * tr.stats.sampling_rate + 200:
                continue
            prepared = core.prepare(tr)
            for found in core.find_triggers(tr, UTCDateTime(e["t"]) + 20, prepared):
                k = round(found["time"].timestamp, 1)
                if found["time"].timestamp - e["t"] <= 55 and k not in peaks:
                    peaks[k] = (core.peak_since(prepared, found["time"]), core.peak_since(prepared, found["time"], core.FIRST_WAVE))
                    trig.append({"t": k})
    pairs, _ = pair(events, trig)
    return [{"src": "recordings", "t": e["t"], "first": e["first"], "latest": e["latest"], "km": km(e["lat"], e["lon"]),
             "xw": math.log10(max(peaks[t["t"]][0], 1)), "xf": math.log10(max(peaks[t["t"]][1], 1)), "alerted": None} for e, t in pairs]


# ---- the lines ----
def current(train, wave):
    return (LINE["mag_offset"], LINE["mag_slope"], None, 0.0) if wave == "xw" else (LINE["first_offset"], LINE["first_slope"], None, 0.0)


def straight(train, wave):
    x, y = np.array([r[wave] for r in train]), np.array([r["latest"] for r in train])
    b, a = np.polyfit(x, y, 1)
    return a, b, None, 0.0


def bent(train, wave):
    """M = a + b x + c max(0, x - knee), least squares, the knee tried every 0.1 between the 10th and 90th percentile."""
    x, y = np.array([r[wave] for r in train]), np.array([r["latest"] for r in train])
    best = None
    for knee in np.arange(np.percentile(x, 10), np.percentile(x, 90), 0.1):
        A = np.column_stack([np.ones_like(x), x, np.maximum(0, x - knee)])
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        if coef[1] < 0 or coef[1] + coef[2] < 0:
            continue
        sse = float(np.sum((A @ coef - y) ** 2))
        if best is None or sse < best[0]:
            best = (sse, coef[0], coef[1], knee, coef[2])
    return best[1], best[2], best[3], best[4]


def read(model, x):
    a, b, knee, c = model
    return a + b * x + (c * max(0.0, x - knee) if knee is not None else 0.0)


MODELS = {"in use (2026-10-10)": current, "straight, fitted afresh": straight, "with a bend": bent}


def day_of(t):
    return (datetime.fromtimestamp(t, timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d")


def main():
    live, lone, span = live_rows()
    rec = recorded_rows()
    rows = rec + live
    for r in rows:
        r["day"] = day_of(r["t"])
    days = sorted({r["day"] for r in rows})
    print(f"pairs: {len(rec)} from the recordings ({min(r['day'] for r in rec)} to {max(r['day'] for r in rec)}), {len(live)} live "
          f"({day_of(span[0])} to {day_of(span[1])}); {len(lone)} live triggers with no IGC earthquake")
    print(f"IGC sizes: under 4.0 {sum(r['latest'] < 4 for r in rows)}, 4.0-4.6 {sum(4 <= r['latest'] < 4.7 for r in rows)}, "
          f"4.7-5.4 {sum(4.7 <= r['latest'] < 5.5 for r in rows)}, 5.5 and more {sum(r['latest'] >= 5.5 for r in rows)}")
    results, fitted_all = {}, {}
    for name, fit in MODELS.items():
        preds = {}
        for d in days:                                     # fitted on the other days, judged on this one
            train = [r for r in rows if r["day"] != d]
            mw, mf = fit(train, "xw"), fit(train, "xf")
            for r in rows:
                if r["day"] == d:
                    preds[id(r)] = (read(mw, r["xw"]), read(mf, r["xf"]))
        results[name] = preds
        fitted_all[name] = (fit(rows, "xw"), fit(rows, "xf"))
    print("\nthe reading that decides (the larger of the two) against the IGC's latest size, each day judged by lines fitted on the others")
    print("line | MAE | bias | within 0.5 | bias by IGC size: <4.0, 4.0-4.6, 4.7-5.4, 5.5+ | IGC 4.7+ announced | IGC <4.2 announced | 4.2-4.6 announced | live triggers with no IGC announced")
    for name, preds in results.items():
        e = np.array([max(*preds[id(r)]) - r["latest"] for r in rows])
        by = []
        for lo, hi in ((0, 4.0), (4.0, 4.7), (4.7, 5.5), (5.5, 10)):
            sub = [max(*preds[id(r)]) - r["latest"] for r in rows if lo <= r["latest"] < hi]
            by.append(f"{np.mean(sub):+.2f}" if sub else "-")
        big = [r for r in rows if r["latest"] >= LEVEL]
        small = [r for r in rows if r["latest"] < 4.2]
        mid = [r for r in rows if 4.2 <= r["latest"] < LEVEL]
        mw, mf = fitted_all[name]
        lone_alerts = sum(max(read(mw, t["xw"]), read(mf, t["xf"])) >= LEVEL for t in lone)
        print(f"{name} | {np.abs(e).mean():.2f} | {e.mean():+.2f} | {100 * np.mean(np.abs(e) <= 0.5):.0f} % | {', '.join(by)} | "
              f"{sum(max(*preds[id(r)]) >= LEVEL for r in big)} of {len(big)} | {sum(max(*preds[id(r)]) >= LEVEL for r in small)} of {len(small)} | "
              f"{sum(max(*preds[id(r)]) >= LEVEL for r in mid)} of {len(mid)} | {lone_alerts} of {len(lone)}")
    print("\nthe same lines at other announcing levels (the reading that decides, days judged by lines fitted on the others):")
    print("line, level | IGC 4.7+ announced | IGC 4.2-4.6 announced | IGC under 4.2 announced | live triggers with no IGC announced")
    big = [r for r in rows if r["latest"] >= LEVEL]
    mid = [r for r in rows if 4.2 <= r["latest"] < LEVEL]
    small = [r for r in rows if r["latest"] < 4.2]
    for name, preds in results.items():
        mw, mf = fitted_all[name]
        for level in (4.3, 4.4, 4.5, 4.6, 4.7, 4.8):
            hit = lambda group: sum(max(*preds[id(r)]) >= level for r in group)
            lone_alerts = sum(max(read(mw, t["xw"]), read(mf, t["xf"])) >= level for t in lone)
            print(f"  {name}, {level} | {hit(big)} of {len(big)} | {hit(mid)} of {len(mid)} | {hit(small)} of {len(small)} | {lone_alerts} of {len(lone)}")
    print("\nby day (MAE: in use / straight / bend)")
    for d in days:
        mine = [r for r in rows if r["day"] == d]
        cells = [f"{np.mean([abs(max(*results[n][id(r)]) - r['latest']) for r in mine]):.2f}" for n in MODELS]
        print(f"  {d} ({len(mine)}): " + " / ".join(cells))
    print("\nthe largest earthquakes (IGC latest 5.5 and more): reading in use / straight / bend")
    for r in sorted([r for r in rows if r["latest"] >= 5.5], key=lambda r: r["t"]):
        cells = [f"{max(*results[n][id(r)]):.2f}" for n in MODELS]
        print(f"  {datetime.fromtimestamp(r['t'], timezone.utc):%m-%d %H:%M} UTC | IGC {r['latest']:.1f} | {r['km']:.0f} km | " + " / ".join(cells))
    for name in MODELS:
        (aw, bw, kw, cw), (af, bf, kf, cf) = fitted_all[name]
        bend = lambda a, b, k, c: f"M = {a:.3f} + {b:.3f} x" + (f" + {c:.3f} max(0, x - {k:.2f})" if k is not None else "")
        print(f"\n{name}, fitted on all {len(rows)}: whole wave {bend(aw, bw, kw, cw)}; first 15 s {bend(af, bf, kf, cf)}  (x = log10 of the peak in counts)")
    (aw, bw, kw, cw), (af, bf, kf, cf) = fitted_all["with a bend"]
    (HERE / "calibration_candidate.json").write_text(json.dumps({
        "made_utc": str(UTCDateTime())[:19], "station": core.STATION, "not_in_use": "a study's result: calibration.json is what the detector reads",
        "pairs": {"recordings": len(rec), "live": len(live)},
        "line": {"mag_offset": aw, "mag_slope": bw, "mag_knee": kw, "mag_bend": cw, "first_offset": af, "first_slope": bf, "first_knee": kf, "first_bend": cf}}, indent=1))
    print("\nwritten calibration_candidate.json (the line with a bend); calibration.json untouched")


if __name__ == "__main__":
    main()
