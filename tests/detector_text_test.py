"""Check what the sensor's alerts say since 2026-10-14: a range ("entre M 4.9 y M 5.7"), no word for the size (leve,
moderado, fuerte), and the reading kept in the record. The live detector code is fed a recording of 2026-10-10
(an M5.2 and an M5.5 three minutes apart, IGC sizes) packet by packet, as replay.py does; nothing is sent, all is
written to a temporary folder. Needs the recordings in data/ (recordings.py). About half a minute.

    .venv\\Scripts\\python.exe src\\tests\\detector_text_test.py
"""
import datetime as dt
import json
import re
import sys
import tempfile
from pathlib import Path

from obspy import Stream, Trace, UTCDateTime

QA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QA))
import detect
import detector_core as core
import listen
import recordings

A, B = UTCDateTime("2026-10-10T06:18:00"), UTCDateTime("2026-10-10T06:40:00")
tmp = Path(tempfile.mkdtemp(prefix="dtext_"))
detect.LOG, detect.VIEW = tmp / "detections.jsonl", None
listen.OUTBOX, listen.LOGS, listen.CONFIG = tmp / "outbox.jsonl", tmp, {}
detect.announcing = lambda default: 4.7
clock = {}
listen.now = lambda: clock["t"]
d = detect.Detector.__new__(detect.Detector)
d.line, d.min_mag, d.stop_at = json.loads((QA / "calibration.json").read_text())["line"], 4.7, None
d.buffer, d.last, d.scanned, d.event, d.view = Stream(), UTCDateTime(0), UTCDateTime(0), None, None
for tr in recordings.load(core.STATION, A, B):
    rate, n = tr.stats.sampling_rate, int(8.0 * tr.stats.sampling_rate)
    head = {k: tr.stats[k] for k in ("network", "station", "location", "channel", "sampling_rate")}
    for i in range(0, tr.stats.npts - n, n):
        piece = Trace(tr.data[i:i + n], {**head, "starttime": tr.stats.starttime + i / rate})
        clock["t"] = (piece.stats.endtime + 5).datetime.replace(tzinfo=dt.timezone.utc)
        d.on_data(piece)
d.finish(UTCDateTime(clock["t"]))
sent = [json.loads(x) for x in listen.OUTBOX.read_text(encoding="utf-8").splitlines()] if listen.OUTBOX.exists() else []
checks = []


def check(name, good, shown=""):
    checks.append(bool(good))
    print(("PASS  " if good else "FAIL  ") + name + ("  | " + str(shown).encode("ascii", "replace").decode()[:220] if shown != "" else ""))


alerts = [r for r in sent if r["text"].startswith("POSIBLE")]
updates = [r for r in sent if r["text"].startswith("ACTUALIZ")]
check("1 alerts were written for the recording's large earthquakes (three at the level of 4.7)", len(alerts) >= 2, [r["text"].split("\n")[2] for r in alerts])
words = re.compile(r"\b(LEVE|MODERADO|FUERTE|leve|moderado|fuerte)\b")
check("2 no word for the size in any message", sent and not any(words.search(r["text"]) for r in sent), [r["text"].split("\n")[0] for r in sent])
ends = json.loads((QA / "config.json").read_text(encoding="utf-8")).get("reading_range", list(listen.RANGE))
ok = True
for r in alerts + updates:
    m = re.search(r"entre M (\d\.\d) y M (\d\.\d) si viene de Azuero", r["text"])
    ok &= bool(m) and abs(float(m.group(2)) - float(m.group(1)) - (ends[1] - ends[0])) < 0.11 and abs(float(m.group(1)) - (r["reads"] + ends[0])) < 0.051
check(f"3 each gives its reading as the range of config.json (R{ends[0]:+.1f} to R{ends[1]:+.1f}), and keeps the reading in the record", ok and all("reads" in r for r in sent),
      [(r["reads"], re.search(r"entre M [\d.]+ y M [\d.]+", r["text"]).group(0)) for r in alerts + updates])
check("4 the first line is what listen.py looks for to pair the IGC's report with the alert", all(r["text"].split("\n")[0] == "POSIBLE SISMO detectado por un sensor" for r in alerts))
print("ALL PASS" if all(checks) else "SOMETHING FAILED")
