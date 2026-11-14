"""Check the second-station check (2026-10-22): the two glitches of CU.BCIP that read M6.9 must not be announced, real
large earthquakes must. EarthScope's archive for BCIP, DRK0 and PIRO is fed, packets in time order, to the live
detector's code; nothing is sent, all is written to a temporary folder. Needs the internet. A few minutes.

    python tests/glitch_test.py
"""
import datetime as dt
import json
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
from obspy import Stream, Trace, UTCDateTime
from obspy.clients.fdsn import Client

QA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QA))
import detect
import listen

client = Client("IRIS", timeout=120)
CASES = [("2026-10-22T21:28:00", "2026-10-22T21:50:00", "the two glitches of 2026-10-22", 0),
         ("2026-10-12T23:15:00", "2026-10-12T23:26:00", "the M6.3 near Coiba, 2026-10-12", 1),
         ("2026-10-14T02:55:00", "2026-10-14T03:05:00", "an IGC M4.9 read 5.7, 2026-10-14", 1)]
checks = []
for a, b, name, wanted in CASES:
    A, B = UTCDateTime(a), UTCDateTime(b)
    tmp = Path(tempfile.mkdtemp(prefix="glitch_"))
    detect.LOG, detect.VIEW = tmp / "detections.jsonl", None
    listen.OUTBOX, listen.LOGS, listen.CONFIG = tmp / "outbox.jsonl", tmp, {}
    detect.announcing = lambda default: 4.0
    clock = {}
    listen.now = lambda: clock["t"]
    d = detect.Detector.__new__(detect.Detector)
    d.line, d.min_mag, d.stop_at = json.loads((QA / "calibration.json").read_text())["line"], 4.0, None
    d.buffer, d.last, d.scanned, d.event, d.view = Stream(), UTCDateTime(0), UTCDateTime(0), None, None
    d.helpers = {sid: Stream() for sid in detect.HELPERS}
    packets = []
    for net, sta, loc, cha in (("CU", "BCIP", "00", "BHZ"), ("TC", "DRK0", "", "HHZ"), ("TC", "PIRO", "", "HHZ")):
        st = client.get_waveforms(net, sta, loc, cha, A, B)
        st.merge(method=1)
        for tr in st.split():
            rate, n = tr.stats.sampling_rate, int(8 * tr.stats.sampling_rate)
            head = {k: tr.stats[k] for k in ("network", "station", "location", "channel", "sampling_rate")}
            for i in range(0, tr.stats.npts - n, n):
                piece = Trace(tr.data[i:i + n].astype(np.int32), {**head, "starttime": tr.stats.starttime + i / rate})
                packets.append(piece)
    for piece in sorted(packets, key=lambda p: p.stats.endtime):
        clock["t"] = (piece.stats.endtime + 5).datetime.replace(tzinfo=dt.timezone.utc)
        d.on_data(piece)
    d.finish(UTCDateTime(clock["t"]))
    rows = [json.loads(x) for x in detect.LOG.open(encoding="utf-8")] if detect.LOG.exists() else []
    declared = [r for r in rows if r["kind"] == "declared"]
    doubted = [r for r in rows if r["kind"] == "closed" and r.get("doubted")]
    good = (len(declared) >= 1) == bool(wanted)
    checks.append(good)
    print(("PASS  " if good else "FAIL  ") + f"{name}: alerts {[(r['trigger'][11:19], r['reading']) for r in declared]}, held back as one station alone {[(r['trigger'][11:19], round(max(r['reading'], r['first_wave']), 2)) for r in doubted]}")
print("ALL PASS" if all(checks) else "SOMETHING FAILED")
