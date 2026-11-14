"""Check listen.py's guard on large IGC reports (2026-10-14): one the Canal station did not feel is held back, and
goes out only if GFZ lists it or the station's signal shows it after all; it is dropped if the IGC takes it off its
list or nothing confirms it in time. Small reports and reports the station cannot judge go out as before.

Everything is made up and written to a temporary folder: the station's drawn signal (logs/live.json), the
detector's status, the clock. Nothing is sent. A few seconds.

    .venv\\Scripts\\python.exe src\\tests\\guard_test.py
"""
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import listen

tmp = Path(tempfile.mkdtemp(prefix="guard_"))
listen.LOGS, listen.SIGHTINGS, listen.OUTBOX, listen.REQUESTS, listen.DETECTIONS = tmp, tmp / "sightings.jsonl", tmp / "outbox.jsonl", tmp / "requests.jsonl", tmp / "detections.jsonl"
listen.CONFIG, listen.HOLD, listen.HOLD_STEP = {}, 900, 0.02
listen.log_below = lambda: 4.2
clock = {"t": datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)}
listen.now = lambda: clock["t"]
listen.recalled = True


async def nothing(record):
    pass


listen.deliver = nothing
checks = []


def check(name, good, shown=""):
    checks.append(bool(good))
    print(("PASS  " if good else "FAIL  ") + name + (f"  | {shown}" if shown != "" else ""))


def station(origin, peak, until=200, arrived_ago=3, hole=None, connected_ago=3600):
    """The station's drawn signal around `origin`: quiet (30 counts), `peak` counts from 40 to 60 s after it, drawn up
    to `until` s after it and arrived `arrived_ago` s ago; `hole` (from, to), in s after `origin`, drawn flat."""
    start, step = origin.timestamp() - 300, 0.5
    n = int((origin.timestamp() + until - start) / step)
    lo, hi = [], []
    for k in range(n):
        t = start + k * step - origin.timestamp()
        size = peak if 40 <= t < 60 else 30
        if hole and hole[0] <= t < hole[1]:
            size = 0
        lo.append(-size)
        hi.append(size)
    (tmp / "live.json").write_text(json.dumps({"station": "CU.BCIP.00.BHZ", "step": step, "start": start, "lo": lo, "hi": hi, "ratio": [1.0] * n,
                                               "end": start + n * step, "arrived": clock["t"].timestamp() - arrived_ago, "warming": False,
                                               "following": None, "followed_s": 60, "announces_from": 4.7}))
    (tmp / "status_detect.json").write_text(json.dumps({"connecting": (clock["t"] - timedelta(seconds=connected_ago)).isoformat(timespec="seconds"), "attempt": 1}))


def outbox():
    return [json.loads(x) for x in listen.OUTBOX.read_text(encoding="utf-8").splitlines()] if listen.OUTBOX.exists() else []


def held():
    return [json.loads(x) for x in (tmp / "held.jsonl").read_text(encoding="utf-8").splitlines()] if (tmp / "held.jsonl").exists() else []


def igc(ago, mag, lat, lon, place="Región Fronteriza Panamá-Colombia, a 46 km al este de La Palma"):
    """The IGC lists an earthquake of `ago` seconds ago. Returns its time and its entry's name."""
    origin = clock["t"] - timedelta(seconds=ago)
    ident = f"{origin - timedelta(hours=5):%Y-%m-%d %I:%M:%S%p}"
    listen.listed.add(ident)
    return origin, ident


def lists(origin, ident, mag, lat, lon, place="Región Fronteriza Panamá-Colombia, a 46 km al este de La Palma"):
    listen.sighting("igc", origin, lat, lon, mag, 10.0, place, "PRELIMINAR", False, ident)


def told(origin):
    """What was written to the outbox for the earthquake of `origin`."""
    return [r for r in outbox() if r["event"] == f"{origin:%Y%m%dT%H%M%S}"]


async def run():
    async def later(seconds=0.0):
        clock["t"] += timedelta(seconds=seconds)
        await asyncio.sleep(0.1)

    # 1. the night of 2026-10-13: a large report, the station listening and quiet
    origin, ident = igc(280, 6.2, 8.559, -77.745)
    station(origin, peak=40)
    lists(origin, ident, 6.2, 8.559, -77.745)
    await later()
    check("1 an M6.2 the station did not feel (it read about M1.7): held back, not posted", told(origin) == [] and held()[-1]["what"] == "held back", held()[-1].get("station"))
    await later(950)
    check("  and dropped when nothing has confirmed it in fifteen minutes", told(origin) == [] and held()[-1]["what"] == "dropped", held()[-1].get("why"))

    # 2. a real large one: the station shook
    await later(600)
    origin, ident = igc(150, 6.3, 7.69, -81.49)
    station(origin, peak=3_000_000)
    lists(origin, ident, 6.3, 7.69, -81.49, "a 17 km al oeste de Río Grande")
    await later()
    record = told(origin)
    check("2 an M6.3 the station felt (it read about 6.2): posted at once", len(record) == 1 and record[0]["type"] == "alert" and "SISMO M 6.3" in record[0]["text"], [r["type"] for r in record])

    # 3. the station without signal: no word from it, as before the guard
    await later(600)
    origin, ident = igc(150, 5.8, 8.4, -78.3)
    station(origin, peak=40, until=-250, arrived_ago=400)      # the signal stopped 400 s ago, before the earthquake
    lists(origin, ident, 5.8, 8.4, -78.3)
    await later()
    check("3 an M5.8 while the station had no signal: posted at once", len(told(origin)) == 1)

    # 4. held, then GFZ lists the same earthquake
    await later(600)
    origin, ident = igc(150, 6.0, 8.2, -77.9)
    station(origin, peak=40)
    lists(origin, ident, 6.0, 8.2, -77.9)
    await later()
    first = told(origin)
    await later(400)
    listen.sighting("gfz", origin + timedelta(seconds=4), 8.25, -77.95, 6.1, 15.0, None, None, False, "gfz-1")
    await later()
    record = told(origin)
    check("4 held, then GFZ lists it 400 s later: posted then", first == [] and any(r["type"] == "alert" for r in record) and held()[-1]["what"] == "posted late",
          held()[-1].get("why"))
    await later(1000)                                        # (case 4 is over)

    # 5. held, then the IGC takes it off its list
    origin, ident = igc(150, 6.1, 8.3, -77.6)
    station(origin, peak=40)
    lists(origin, ident, 6.1, 8.3, -77.6)
    await later(30)
    listen.listed.discard(ident)
    await later()
    check("5 held, then gone from the IGC's list: dropped at once", told(origin) == [] and held()[-1]["what"] == "dropped" and "off its list" in held()[-1]["why"], held()[-1].get("why"))

    # 6. the signal of those minutes not all in yet: held, then the station shows it
    await later(600)
    origin, ident = igc(110, 6.0, 8.0, -78.2)
    station(origin, peak=40, until=100)
    lists(origin, ident, 6.0, 8.0, -78.2)
    await later()
    first, why = told(origin), held()[-1].get("station")
    clock["t"] += timedelta(seconds=50)
    station(origin, peak=2_000_000, until=160)
    await later()
    record = told(origin)
    check("6 the signal still coming: held, then the shaking shows: posted", first == [] and len(record) == 1 and held()[-1]["what"] == "posted late"
          and "felt it after all" in held()[-1]["why"], (why, held()[-1].get("why")))

    # 7. a hole in the signal across the minutes: the station cannot tell
    await later(600)
    origin, ident = igc(150, 6.4, 8.1, -78.0)
    station(origin, peak=40, hole=(10, 90))
    lists(origin, ident, 6.4, 8.1, -78.0)
    await later()
    check("7 a hole in the signal across those minutes: posted at once", len(told(origin)) == 1)

    # 8. a detector that had just started
    await later(600)
    origin, ident = igc(150, 6.4, 8.15, -77.9)
    station(origin, peak=40, connected_ago=200)
    lists(origin, ident, 6.4, 8.15, -77.9)
    await later()
    check("8 the detector started two minutes before: posted at once", len(told(origin)) == 1)

    # 9. sizes the guard does not look at
    await later(600)
    origin, ident = igc(150, 5.4, 7.4, -80.4)
    station(origin, peak=40)
    lists(origin, ident, 5.4, 7.4, -80.4, "a 10 km al sur de Tonosí")
    origin2, ident2 = igc(100, 3.9, 7.1, -80.4)
    lists(origin2, ident2, 3.9, 7.1, -80.4, "a 30 km al sur de Tonosí")
    await later()
    check("9 an M5.4 and an M3.9, the station quiet: an alert and a line of the day's log, as before",
          [r["type"] for r in told(origin)] == ["alert"] and [r["type"] for r in told(origin2)] == ["log"])

    # 10. the threshold: an M6.2 needs a reading of 4.7 to count as felt
    await later(600)
    origin, ident = igc(150, 6.2, 8.35, -78.1)
    station(origin, peak=20_000)                             # 0.909 log10(20000) + 0.279 = 4.19
    lists(origin, ident, 6.2, 8.35, -78.1)
    await later(400)
    origin2, ident2 = igc(150, 6.2, 7.9, -77.5)
    station(origin2, peak=80_000)                            # 4.73
    lists(origin2, ident2, 6.2, 7.9, -77.5)
    await later()
    check("10 an M6.2 the station read at 4.2: held; one read at 4.7: posted", told(origin) == [] and len(told(origin2)) == 1)
    await later(1000)
    check("11 nothing else was held: three dropped, two posted late", [h["what"] for h in held()].count("dropped") == 3
          and [h["what"] for h in held()].count("posted late") == 2, [h["what"] for h in held()])


asyncio.run(run())
print("ALL PASS" if all(checks) else "SOMETHING FAILED")
