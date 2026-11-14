"""Replay the recorded hours through the live detector code, packet by packet, and compare with the IGC list.

Usage:
    python src/replay.py            # data/ as fetched by recordings.py, announcing at M5
    python src/replay.py 4.5        # another announcing magnitude
    python src/replay.py 5 whole    # without the first-wave line, to see what it buys

calibrate.py works out what the station could announce; this runs the very code that runs live, on the
two ordinary days and the whole sequence, so it also counts the seconds the stream costs: packets are cut
to the length the real stream uses (measured live: 7 to 9 s) and each one "arrives" 5 s after its last
sample (measured live: 4 to 8 s, apart from the stream's occasional long delays, which are not replayed).
Nothing is sent anywhere and the logs of the live programs are not touched.
"""
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
from obspy import Stream, Trace, UTCDateTime

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibrate
import detect
import detector_core as core
import listen
import recordings

HERE = Path(__file__).resolve().parent
PACKET, NET = 8.0, 5.0             # seconds of signal in a packet; seconds from its last sample to its arrival


def main(limit, whole_wave_only=False):
    tmp = Path(tempfile.mkdtemp())
    detect.LOG, detect.VIEW, detect.print = tmp / "detections.jsonl", None, lambda *a, **k: None
    listen.OUTBOX, listen.LOGS, listen.CONFIG = tmp / "outbox.jsonl", tmp, {}
    clock = {}
    listen.now = lambda: clock["t"]
    d = detect.Detector.__new__(detect.Detector)             # the detector without its network connection
    d.line, d.min_mag, d.stop_at = json.loads((HERE / "calibration.json").read_text())["line"], limit, None
    if whole_wave_only:
        d.line["first_offset"] = -99.0                       # the first-wave line never reaches any announcing magnitude
    d.buffer, d.last, d.scanned, d.event, d.view = Stream(), UTCDateTime(0), UTCDateTime(0), None, None

    end, packets, hours = recordings.newest(), 0, {}
    for name, (a, b) in (("quiet", recordings.QUIET), ("sequence", (recordings.SEQUENCE, end))):
        hours[name] = 0.0
        for tr in recordings.load(core.STATION, a, b):
            rate = tr.stats.sampling_rate
            n, head = int(PACKET * rate), {k: tr.stats[k] for k in ("network", "station", "location", "channel", "sampling_rate")}
            hours[name] += tr.stats.npts / rate / 3600
            for i in range(0, tr.stats.npts - n, n):
                piece = Trace(tr.data[i:i + n], {**head, "starttime": tr.stats.starttime + i / rate})
                clock["t"] = (piece.stats.endtime + NET).datetime.replace(tzinfo=dt.timezone.utc)
                d.on_data(piece)
                packets += 1
    d.finish(UTCDateTime(clock["t"]))

    rows = [json.loads(line) for line in detect.LOG.open(encoding="utf-8")]
    stamp = lambda text: UTCDateTime(text).timestamp
    final = {r["trigger"]: r["reading"] for r in rows if r["kind"] == "closed"}
    alerts = [{"sid": core.STATION, "time": stamp(r["signal"]), "seen": stamp(r["seen"]), "first": r["reading"], "reading": final[r["trigger"]]}
              for r in rows if r["kind"] == "declared"]
    late = np.array([stamp(r["seen"]) - stamp(r["trigger"]) for r in rows if r["kind"] == "trigger"])
    cat = [e for e in calibrate.igc_catalogue(offline=True) if recordings.QUIET[0] + calibrate.WARM <= e["origin"] <= end - 150]
    mags, origins = np.array([e["mag"] for e in cat]), np.array([e["origin"].timestamp for e in cat])
    waves = calibrate.first_waves(cat, [core.STATION])
    window = (calibrate.GROWTH[0], calibrate.GROWTH[1] + PACKET)          # the reading is taken at the end of a packet
    caught, behind = calibrate.judge(alerts, waves, mags, window=window)
    sequence_from, split = recordings.SEQUENCE.timestamp, calibrate.SPLIT.timestamp
    seq = np.flatnonzero(origins >= sequence_from)
    want, want_test = seq[mags[seq] >= limit], seq[(mags[seq] >= limit) & (origins[seq] >= split)]
    in_seq = [(a, b) for a, b in zip(alerts, behind) if a["time"] >= sequence_from]
    in_test = [(a, b) for a, b in in_seq if a["time"] >= split]
    in_quiet = [a for a in alerts if a["time"] < sequence_from]
    w = waves[core.STATION]
    when = [min(a["seen"] for a in alerts if window[0] <= a["time"] - w[i] <= window[1]) - origins[i] for i in want if i in caught]
    err = np.array([a["reading"] - b for a, b in in_seq if b is not None])
    by_chance = np.mean([sum(mags[i] >= limit and origins[i] >= sequence_from for i in calibrate.judge(alerts, waves, mags, s, window)[0]) for s in calibrate.SHIFTS])
    print(f"replayed {hours['quiet']:.1f} h of ordinary days and {hours['sequence']:.1f} h of the sequence in {packets} packets, announcing at M{limit}")
    print(f"{sum(r['kind'] == 'trigger' for r in rows)} triggers, each known {np.median(late):.0f} s after its signal (slowest {late.max():.0f} s); "
          f"{len(alerts)} alerts, {sum(r['kind'] == 'update' for r in rows)} updates")
    print(f"IGC M{limit}+ announced: {sum(i in caught for i in want)} of {len(want)} in the whole sequence, {sum(i in caught for i in want_test)} of {len(want_test)} "
          f"in the hours the line was not fitted on ({by_chance:.1f} would match by chance)")
    print(f"the message is written {np.median(when):.0f} s after the earthquake (fastest {min(when):.0f}, three in four within {np.percentile(when, 75):.0f}, "
          f"nine in ten within {np.percentile(when, 90):.0f}, slowest {max(when):.0f} s)" if when else "no announced earthquake")
    print(f"alerts in the sequence: {len(in_seq)}, of which {sum(b is None for a, b in in_seq)} with no IGC earthquake behind them and "
          f"{sum(b is not None and b < limit - 0.5 for a, b in in_seq)} more than half a magnitude above the IGC's")
    print(f"  in the hours the line was not fitted on: {len(in_test)} alerts, {sum(b is None for a, b in in_test)} with no IGC earthquake")
    print(f"alerts on the ordinary days: {len(in_quiet)}")
    if len(err):
        print(f"final reading against the IGC magnitude of the earthquake behind it: half within {np.median(np.abs(err)):.2f}, nine in ten within "
              f"{np.percentile(np.abs(err), 90):.2f}, worst {np.abs(err).max():.2f} (n={len(err)})")
    large = [(str(cat[i]["origin"])[5:16], float(mags[i]),
              next((f"{a['first']:.1f} then {a['reading']:.1f}" for a in alerts if window[0] <= a["time"] - w[i] <= window[1]), "not announced")) for i in seq if mags[i] >= 5.5]
    print("IGC M5.5+ (time UTC, IGC magnitude, first and final reading):", large)
    print("the replay's own log is in", tmp)


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 5.0, "whole" in sys.argv[2:])
