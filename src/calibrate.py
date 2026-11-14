"""Measure the detector's station against the IGC list and fit the two lines that turn motion into magnitude.

Usage:
    python src/calibrate.py             # fetch what is missing (recordings, IGC list), then analyse
    python src/calibrate.py --offline   # only what is already in data/
    python src/calibrate.py --compare   # the same for the three candidate stations, side by side

The IGC list is the reference. It reports how many IGC earthquakes the station saw (by magnitude, with the
share expected by chance), how often it triggers on nothing the IGC lists, on ordinary days and during the
aftershock sequence, and how well the size of the motion tells the magnitude: the lines (one for the whole
wave, one for the first wave alone) are fitted on the first hours of the sequence and judged on the hours
after them, which the fit never saw. Then, for each announcing magnitude, what would have been announced
rightly and wrongly. calibration.json gets the lines (refitted on every earthquake matched) and these measurements.
--compare is the study behind the choice of station: it needs the recordings of all three (about 110 MB).
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from obspy import UTCDateTime
from obspy.signal.trigger import trigger_onset
from obspy.taup import TauPyModel

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detector_core as core
import recordings

HERE = Path(__file__).resolve().parent
CATALOGUE, SCAN = recordings.DATA / "igc_catalogue.json", recordings.DATA / "scan.json"
IGC = "https://sismosgeociencias.up.ac.pa/"
ARRIVAL = (-5.0, 12.0)             # seconds around the predicted first wave in which a trigger is that earthquake's first wave
GROWTH = (-5.0, 60.0)              # seconds around it in which the motion can reach an announcing size (the strongest waves come later)
SPLIT = UTCDateTime("2026-10-10T06:40:00")      # the sequence before this fits the magnitude line, after it tests it
WARM = 120.0                       # seconds at the start of every unbroken stretch in which nothing may trigger (as live)
LEVELS = (3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0)  # trigger levels tried
CLASSES = ((0, 4), (4, 4.5), (4.5, 5), (5, 5.5), (5.5, 10))
SHIFTS = (-1500, -900, -420, 420, 900, 1500)    # seconds the list is moved by to see what matches by chance
ANNOUNCE = (4.5, 5.0, 5.5)         # announcing magnitudes compared


def igc_catalogue(offline=False):
    """Every earthquake the IGC has listed, oldest first.

    The page shows only its newest two hundred or so, so what it showed before is kept in data/ and the
    page as it is now replaces the entries it still carries (the IGC revises times by seconds and sizes by tenths)."""
    saved = json.loads(CATALOGUE.read_text(encoding="utf-8")) if CATALOGUE.exists() else []
    if not offline:
        raw = urllib.request.urlopen(urllib.request.Request(IGC, headers={"User-Agent": "Mozilla/5.0"}), timeout=40).read()
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:                  # the IGC page is not UTF-8
            html = raw.decode("latin-1")
        page = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S):
            c = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)]
            if len(c) >= 8 and re.fullmatch(r"[\d.]+", c[6]):
                origin = datetime.strptime(f"{c[1]} {c[2]}", "%Y-%m-%d %I:%M:%S%p").replace(tzinfo=timezone.utc) + timedelta(hours=5)
                page.append({"origin": origin.strftime("%Y-%m-%dT%H:%M:%S"), "lat": float(c[3]), "lon": float(c[4]),
                             "depth": float(c[5].replace("km", "") or 10), "mag": float(c[6]), "status": c[0], "place": c[7]})
        same = lambda a, b: abs(UTCDateTime(a["origin"]) - UTCDateTime(b["origin"])) <= 20 and core.km(a["lat"], a["lon"], b["lat"], b["lon"]) <= 150
        saved = sorted([s for s in saved if not any(same(s, p) for p in page)] + page, key=lambda e: e["origin"])
        recordings.DATA.mkdir(exist_ok=True)
        CATALOGUE.write_text(json.dumps(saved, ensure_ascii=False, indent=0), encoding="utf-8")
    events = [{**e, "origin": UTCDateTime(e["origin"])} for e in saved]
    # the list sometimes carries one earthquake twice (preliminary and revised): keep the revised one
    out = []
    for e in events:
        if out and e["origin"] - out[-1]["origin"] <= 10:
            if e["status"] == "REVISADO" and out[-1]["status"] != "REVISADO":
                out[-1] = e
        else:
            out.append(e)
    return out


def scan(sid, start, end):
    """Every trigger of one station between start and end, at each trigger level tried.

    Returns (hours in which it could trigger, {level: [trigger, ...]}). A trigger carries its time, its top
    ratio and `sec`: the largest filtered motion in each second of the PEAK_WINDOW after it."""
    hours, out = 0.0, {level: [] for level in LEVELS}
    for seg in recordings.load(sid, start, end):
        rate = seg.stats.sampling_rate
        if seg.stats.npts < (WARM + 60) * rate:
            continue
        tr, ratio = core.prepare(seg)
        ready, n = seg.stats.starttime + WARM, int(rate)
        hours += (seg.stats.endtime - ready) / 3600
        motion = np.abs(tr.data)
        for level in LEVELS:
            for on, off in trigger_onset(ratio, level, core.OFF):
                t = seg.stats.starttime + on / rate
                if t < ready:
                    continue
                window = motion[on:on + int(core.PEAK_WINDOW) * n]
                whole = len(window) // n
                out[level].append({"time": round(t.timestamp, 2), "ratio": round(float(ratio[on:off + 1].max()), 1),
                                   "sec": [round(float(x), 1) for x in window[:whole * n].reshape(whole, n).max(axis=1)]})
    return hours, out


def scans(end, wanted):
    """scan() of both stretches for each station wanted, kept in data/scan.json until the recordings or the trigger change."""
    stamp = json.loads(json.dumps([str(end)[:19], core.BAND, core.STA, core.LTA, core.OFF, core.PEAK_WINDOW, WARM, LEVELS]))      # what a scan depends on
    kept = json.loads(SCAN.read_text()) if SCAN.exists() else {}
    stations = kept["stations"] if kept.get("stamp") == stamp else {}
    for sid in wanted:
        if sid not in stations:
            if not any((recordings.DATA / sid).glob("*.mseed")):
                sys.exit(f"no recordings of {sid} in data/: run without --offline to fetch them")
            stations[sid] = {}
            for name, (a, b) in (("quiet", recordings.QUIET), ("sequence", (recordings.SEQUENCE, end))):
                hours, found = scan(sid, a, b)
                stations[sid][name] = {"hours": hours, "triggers": {str(level): rows for level, rows in found.items()}}
            SCAN.write_text(json.dumps({"stamp": stamp, "stations": stations}))
    return stations


def first_waves(cat, wanted):
    """Predicted arrival of the first wave of every earthquake at each station: {stream id: array of epoch seconds}."""
    model, known, out = TauPyModel("iasp91"), {}, {sid: [] for sid in wanted}
    for e in cat:
        for sid in wanted:
            key = (round(core.km(e["lat"], e["lon"], *core.CANDIDATES[sid]) / 2), round(max(e["depth"], 1) / 2))       # 2 km steps
            if key not in known:
                arrivals = model.get_travel_times(key[1] * 2 or 1, key[0] * 2 / 111.19, phase_list=["p", "P", "Pn", "Pg"])
                known[key] = min(a.time for a in arrivals) if arrivals else key[0] * 2 / 7.0
            out[sid].append(e["origin"].timestamp + known[key])
    return {sid: np.array(w) for sid, w in out.items()}


def hits(triggers, waves, shift=0.0):
    """For each earthquake, the index of the trigger that is its first wave at this station, or None. Nearest wins."""
    times = np.array([t["time"] for t in triggers])
    out, used = [], set()
    for w in waves:
        k = None
        if len(times):
            gap = times - (w + shift)
            k = int(np.argmin(np.where((gap >= ARRIVAL[0]) & (gap <= ARRIVAL[1]), np.abs(gap), np.inf)))
            k = k if ARRIVAL[0] <= gap[k] <= ARRIVAL[1] and k not in used else None
        used.add(k)
        out.append(k)
    return out


def fit(amplitudes, mags):
    """Straight line from log10(motion) to magnitude, refitted once without the points more than 2.5 deviations off."""
    x, y = np.log10(np.maximum(amplitudes, 1e-9)), np.asarray(mags)
    line = np.polyfit(x, y, 1)
    keep = np.abs(np.polyval(line, x) - y) <= 2.5 * np.std(np.polyval(line, x) - y)
    return np.polyfit(x[keep], y[keep], 1) if keep.sum() >= 8 else line


def announce(sid, events, limit, whole, first=None):
    """The station events whose reading reaches `limit`: when it did, and what the whole wave read in the end.

    As live: the largest motion so far on the whole-wave line and, with `first`, the largest motion of the
    first FIRST_WAVE seconds on the first-wave line; whichever reaches the limit first announces."""
    out = []
    for t in events:
        if t["sec"]:
            run = np.log10(np.maximum(np.maximum.accumulate(np.array(t["sec"])), 1e-9))
            reads = whole[0] * run + whole[1]
            if first is not None:
                n = int(core.FIRST_WAVE)
                early = first[0] * run[:n] + first[1]
                reads = np.maximum(reads, np.concatenate([early, np.full(len(run) - len(early), early[-1])]))
            k = int(np.argmax(reads >= limit))
            if reads[k] >= limit:
                out.append({"sid": sid, "onset": t["time"], "time": t["time"] + k + 1, "reading": float(whole[0] * run[-1] + whole[1]), "limit": limit})
    return out


def judge(alerts, waves, mags, shift=0.0, window=GROWTH):
    """Which earthquakes the alerts announce (set of indices), and the largest earthquake behind each alert (None: none)."""
    caught, behind = set(), []
    for a in alerts:
        gap = a["time"] - (waves[a["sid"]] + shift)
        near = np.flatnonzero((gap >= window[0]) & (gap <= window[1]))
        caught.update(near.tolist())
        behind.append(float(mags[near].max()) if len(near) else None)
    return caught, behind


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--compare", action="store_true")
    args = ap.parse_args()
    wanted = list(core.CANDIDATES) if args.compare else [core.STATION]
    if not args.offline:
        recordings.fetch(wanted)
    end = recordings.newest()
    cat = [e for e in igc_catalogue(args.offline) if recordings.QUIET[0] + WARM <= e["origin"] <= end - 150]
    mags = np.array([e["mag"] for e in cat])
    origins = np.array([e["origin"].timestamp for e in cat])
    sequence_from = recordings.SEQUENCE.timestamp
    seq = np.flatnonzero(origins >= sequence_from)
    train, test = seq[origins[seq] < SPLIT.timestamp], seq[origins[seq] >= SPLIT.timestamp]
    quiet = np.flatnonzero(origins < sequence_from)
    print(f"recordings: {str(recordings.QUIET[0])[:16]} to {str(end)[:16]} UTC")
    print(f"IGC list in that time: {len(cat)} earthquakes ({len(quiet)} on the two ordinary days, {len(train)} in the hours used to fit, {len(test)} in the hours used to test)")
    stations, waves = scans(end, wanted), first_waves(cat, wanted)
    raw = lambda sid, level=core.ON: stations[sid]["quiet"]["triggers"][str(float(level))] + stations[sid]["sequence"]["triggers"][str(float(level))]
    hours = lambda sid: (stations[sid]["sequence"]["hours"], stations[sid]["quiet"]["hours"])
    name = lambda sid: sid.split(".")[1]

    # ---------- what each station sees, and how well its motion tells the magnitude ----------
    events, lines, measured = {}, {}, {}
    for sid in wanted:
        seq_h, quiet_h = hours(sid)
        saw = np.array([k is not None for k in hits(raw(sid), waves[sid])])                   # the ratio crossed the level when the first wave came
        chance = np.mean([[k is not None for k in hits(raw(sid), waves[sid], s)] for s in SHIFTS], axis=0)
        events[sid] = core.station_events(raw(sid))                                           # as live: one trigger per 75 s
        found = hits(events[sid], waves[sid])
        mine = {k for k in found if k is not None}
        lone = [t for k, t in enumerate(events[sid]) if k not in mine]
        lone_seq, lone_quiet = [t for t in lone if t["time"] >= sequence_from], [t for t in lone if t["time"] < sequence_from]
        print(f"\n{sid}  ({core.km(*core.CANDIDATES[sid], 7.587, -80.769):.0f} km from the mainshock; recorded {seq_h:.1f} h of the sequence and {quiet_h:.1f} h of the ordinary days)")
        print("  IGC earthquakes whose first wave crossed the trigger level: whole sequence | test hours only | by chance")
        for lo, hi in CLASSES:
            a, b = seq[(mags[seq] >= lo) & (mags[seq] < hi)], test[(mags[test] >= lo) & (mags[test] < hi)]
            print(f"    M{lo}-{hi if hi < 10 else ''}: {saw[a].sum():3d} of {len(a):3d} ({100 * saw[a].mean():3.0f} %) | {saw[b].sum():3d} of {len(b):3d} | {100 * chance[a].mean():2.0f} %")
        delay = np.array([events[sid][k]["time"] - origins[i] for i, k in enumerate(found) if k is not None and origins[i] >= sequence_from])
        print(f"  the trigger comes {np.median(delay):.0f} s after the earthquake (middle half {np.percentile(delay, 25):.0f} to {np.percentile(delay, 75):.0f} s)")
        print(f"  triggers with no IGC earthquake behind them: {len(lone_seq) / seq_h:.1f} per hour in the sequence, {len(lone_quiet) / quiet_h * 24:.0f} per day on ordinary days")
        full, early = int(core.PEAK_WINDOW), int(core.FIRST_WAVE)
        pairs = [(i, events[sid][k]) for i, k in enumerate(found) if k is not None and len(events[sid][k]["sec"]) >= full]
        fit_p, test_p = [(i, t) for i, t in pairs if origins[i] < SPLIT.timestamp], [(i, t) for i, t in pairs if origins[i] >= SPLIT.timestamp]
        top = lambda t, seconds: max(t["sec"][:seconds])
        line_of = lambda chosen, seconds: fit([top(t, seconds) for i, t in chosen], [mags[i] for i, t in chosen])
        print(f"  magnitude read from the largest motion so far against the IGC magnitude, in the test hours (line fitted on {len(fit_p)} earthquakes, judged on {len(test_p)}):")
        errors = {}
        for seconds in sorted({5, 10, early, 20, 30, 45, full}):
            line = line_of(fit_p, seconds)
            errors[seconds] = np.array([np.polyval(line, np.log10(max(top(t, seconds), 1e-9))) - mags[i] for i, t in test_p])
            print(f"    after {seconds:2d} s: typical error {errors[seconds].std():.2f}, bias {errors[seconds].mean():+.2f}, worst {np.abs(errors[seconds]).max():.2f}")
        lines[sid] = (line_of(fit_p, full), line_of(fit_p, early))                                # what is announced below uses the lines fitted on the first hours
        whole, first = line_of(pairs, full), line_of(pairs, early)
        big, big_test = seq[mags[seq] >= 4.5], test[mags[test] >= 4.5]
        measured[sid] = {"line": {"mag_slope": float(whole[0]), "mag_offset": float(whole[1]), "mag_error": round(float(errors[full].std()), 2),
                                  "first_slope": float(first[0]), "first_offset": float(first[1]), "first_error": round(float(errors[early].std()), 2),
                                  "fitted_on": len(pairs), "followed_s": full, "first_wave_s": early},
                         "saw_m45": f"{saw[big].sum()}/{len(big)}", "saw_m45_test_hours": f"{saw[big_test].sum()}/{len(big_test)}",
                         "trigger_after_s": round(float(np.median(delay))), "triggers_with_no_igc_per_hour_sequence": round(len(lone_seq) / seq_h, 1),
                         "triggers_with_no_igc_per_day_ordinary": round(len(lone_quiet) / quiet_h * 24), "announcing": {}}

    # ---------- what would have been announced ----------
    def report(label, alerts, clock):
        """One row per announcing magnitude; `clock` is the station whose recorded hours the rates are per."""
        seq_h, quiet_h = hours(clock)
        rows = {}
        for limit in ANNOUNCE:
            mine = [a for a in alerts if a["limit"] == limit]
            caught, behind = judge(mine, waves, mags)
            by_chance = np.mean([sum(mags[i] >= limit and origins[i] >= sequence_from for i in judge(mine, waves, mags, s)[0]) for s in SHIFTS])
            want, want_test = seq[mags[seq] >= limit], test[mags[test] >= limit]
            in_seq = [(a, b) for a, b in zip(mine, behind) if a["time"] >= sequence_from]
            in_test = [(a, b) for a, b in in_seq if a["time"] >= SPLIT.timestamp]
            in_quiet = [(a, b) for a, b in zip(mine, behind) if a["time"] < sequence_from]
            none, small = sum(b is None for a, b in in_seq), sum(b is not None and b < limit - 0.5 for a, b in in_seq)
            when = [min(a["time"] for a in mine if GROWTH[0] <= a["time"] - waves[a["sid"]][i] <= GROWTH[1]) - origins[i] for i in want if i in caught]
            rows[str(limit)] = {"igc_that_size_announced": f"{sum(i in caught for i in want)}/{len(want)}",
                                "in_test_hours": f"{sum(i in caught for i in want_test)}/{len(want_test)}", "by_chance": round(float(by_chance), 1),
                                "seconds_after_earthquake": round(float(np.median(when))) if when else None,
                                "alerts_in_sequence": len(in_seq), "with_no_igc_earthquake": none, "far_too_high": small,
                                "alerts_in_test_hours": len(in_test), "with_no_igc_in_test_hours": sum(b is None for a, b in in_test),
                                "alerts_on_ordinary_days": len(in_quiet)}
            r = rows[str(limit)]
            print(f"  {label:30s} M{limit}: {r['igc_that_size_announced']:>5s}  test {r['in_test_hours']:>5s}  chance {by_chance:4.1f} | {r['seconds_after_earthquake'] or 0:3d} s | "
                  f"{len(in_seq):3d} alerts, {none:3d} with no IGC earthquake, {small:2d} far too high | test hours {len(in_test):3d}, {r['with_no_igc_in_test_hours']:2d} | "
                  f"ordinary days {len(in_quiet):2d} ({len(in_quiet) / quiet_h * 24:4.1f} per day)")
        return rows

    print(f"\nwhat would have been announced, at trigger level {core.ON:.0f}")
    print("  station, announcing magnitude: IGC earthquakes that size announced (whole sequence, test hours, by chance) | seconds after the earthquake | "
          "alerts in the sequence | in the test hours, with no IGC earthquake | on the ordinary days")
    single = {sid: [a for limit in ANNOUNCE for a in announce(sid, events[sid], limit, *lines[sid])] for sid in wanted}
    for sid in wanted:
        measured[sid]["announcing"] = report(f"{name(sid)} alone", single[sid], sid)
    sid = core.STATION
    report(f"{name(sid)}, whole wave only", [a for limit in ANNOUNCE for a in announce(sid, events[sid], limit, lines[sid][0])], sid)
    if args.compare:
        bcip, piro, drk0 = core.CANDIDATES
        others = np.sort(np.array([t["time"] for sid in (piro, drk0) for t in raw(sid)]))
        report("BCIP and a trigger elsewhere", [a for a in single[bcip] if np.any(np.abs(others - a["onset"]) <= 45)], bcip)
        pair = []
        for limit in ANNOUNCE:
            second = announce(drk0, events[drk0], limit - 0.3, *lines[drk0])
            for a in announce(piro, events[piro], limit - 0.3, *lines[piro]):
                partner = [b for b in second if abs(b["onset"] - a["onset"]) <= 20]       # the two are 95 km apart: one wave reaches both within 20 s
                if partner and (a["reading"] + partner[0]["reading"]) / 2 >= limit:
                    pair.append({"sid": piro, "onset": a["onset"], "time": max(a["time"], partner[0]["time"]), "reading": (a["reading"] + partner[0]["reading"]) / 2, "limit": limit})
        report("PIRO and DRK0 together", pair, piro)
        print("\nlargest readings on the ordinary days:")
        for sid in wanted:
            tops = sorted((np.polyval(lines[sid][0], np.log10(max(max(t["sec"]), 1e-9))) for t in events[sid] if t["time"] < sequence_from and t["sec"]), reverse=True)[:6]
            print(f"  {name(sid)}: " + ", ".join(f"{x:.1f}" for x in tops))

    sid = core.STATION
    print(f"\ntrigger level, {name(sid)} announcing at M5: IGC M5+ announced | alerts in the sequence with no IGC earthquake | alerts on the ordinary days | triggers per day on ordinary days")
    want = seq[mags[seq] >= 5]
    for level in LEVELS:
        ev = core.station_events(raw(sid, level))
        mine = announce(sid, ev, 5.0, *lines[sid])
        caught, behind = judge(mine, waves, mags)
        print(f"  {level:4.0f}{' (in use)' if level == core.ON else '         '}: {sum(i in caught for i in want):2d}/{len(want)} | "
              f"{sum(b is None and a['time'] >= sequence_from for a, b in zip(mine, behind)):2d} | {sum(a['time'] < sequence_from for a in mine):2d} | "
              f"{sum(t['time'] < sequence_from for t in ev) / hours(sid)[1] * 24:5.1f}")

    result = {"made_utc": str(UTCDateTime())[:19], "station": sid, "recordings": [str(recordings.QUIET[0])[:19], str(end)[:19]], "split": str(SPLIT)[:19],
              "igc_earthquakes": {"ordinary_days": len(quiet), "fit": len(train), "test": len(test)}, **measured[sid]}
    (HERE / "calibration.json").write_text(json.dumps(result, indent=1))
    line = result["line"]
    print(f"\ncalibration.json, fitted on {line['fitted_on']} earthquakes; typical error in the test hours in brackets")
    print(f"  whole wave ({line['followed_s']} s): M = {line['mag_slope']:.3f} log10(counts) + {line['mag_offset']:.3f}  [{line['mag_error']}]")
    print(f"  first wave ({line['first_wave_s']} s): M = {line['first_slope']:.3f} log10(counts) + {line['first_offset']:.3f}  [{line['first_error']}]")


if __name__ == "__main__":
    main()
