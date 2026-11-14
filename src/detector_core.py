"""Shared pieces of the shaking detector: which station, how its signal is filtered and how triggers are found.

Used by detect.py (live stream), replay.py and calibrate.py (recordings against the IGC list).
The detector listens to one station, CU.BCIP on Barro Colorado in the Panama Canal. Three stream openly in
real time in or near Panama (EarthScope SeedLink server); calibrate.py --compare measured them against
the IGC list and the two in southern Costa Rica were dropped: they see fewer of the IGC's earthquakes,
size them worse and fire on local events several times a day. README.md (Results) has the numbers.
"""
import math

import numpy as np
from obspy.signal.trigger import recursive_sta_lta, trigger_onset

SEEDLINK = ("rtserve.earthscope.org", 18000)    # was rtserve.iris.washington.edu, gone from DNS by 2026-10-20 (IRIS is now EarthScope)
STATION = "CU.BCIP.00.BHZ"         # the station the detector uses
CANDIDATES = {                     # stream id -> (latitude, longitude): the stations compared by calibrate.py --compare
    "CU.BCIP.00.BHZ": (9.1665, -79.8373),
    "TC.PIRO..HHZ": (8.41, -83.32),
    "TC.DRK0..HHZ": (9.26, -83.25),
}
BAND = (1.0, 8.0)                  # Hz: where nearby earthquakes are strong and distant ones weak
STA, LTA = 1.5, 30.0               # seconds: short and long averaging windows of the trigger
ON, OFF = 5.0, 1.5                 # ratio at which a trigger starts and ends
PEAK_WINDOW = 60.0                 # seconds after the trigger in which the largest motion is measured: nine in ten earthquakes
                                   # peak within 43 s, and a longer window starts taking in the next earthquake
FIRST_WAVE = 15.0                  # seconds after the trigger that hold only the first wave: the strongest waves of an Azuero
                                   # earthquake reach the station about 25 s after its first one
DEAD = 75.0                        # seconds after a trigger in which no new one starts (the same earthquake's later waves)


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(a))


def prepare(trace):
    """Band-passed copy of an ObsPy trace and its short/long average ratio."""
    tr = trace.copy()
    tr.detrend("demean")
    tr.filter("bandpass", freqmin=BAND[0], freqmax=min(BAND[1], 0.45 * tr.stats.sampling_rate), corners=4, zerophase=False)
    rate = tr.stats.sampling_rate
    ratio = recursive_sta_lta(tr.data.astype("float64"), int(STA * rate), int(LTA * rate))
    ratio[:int(LTA * rate)] = 0                     # the ratio means nothing until the long window is full
    return tr, ratio


def peak_since(prepared, start, seconds=PEAK_WINDOW):
    """Largest filtered motion from `start` to `seconds` later, as far as the trace reaches."""
    tr = prepared[0]
    rate = tr.stats.sampling_rate
    i = max(int(round((start - tr.stats.starttime) * rate)), 0)
    seg = np.abs(tr.data[i:i + int(seconds * rate)])
    return float(seg.max()) if seg.size else 0.0


def find_triggers(trace, since=None, prepared=None, level=ON):
    """Triggers in a trace: list of dicts with the trigger time and the top ratio reached.

    `since` (UTCDateTime) keeps only triggers that start at or after that time. `prepared` reuses the
    result of prepare(trace)."""
    tr, ratio = prepared or prepare(trace)
    rate, start = tr.stats.sampling_rate, tr.stats.starttime
    out = []
    for on, off in trigger_onset(ratio, level, OFF):
        if since is None or start + on / rate >= since:
            out.append({"time": start + on / rate, "ratio": float(ratio[on:off + 1].max())})
    return out


def station_events(triggers):
    """Drop the later triggers of one earthquake: its strongest waves and its tail trigger again."""
    kept = []
    for t in sorted(triggers, key=lambda t: t["time"]):
        if not kept or t["time"] - kept[-1]["time"] > DEAD:
            kept.append(t)
    return kept


def magnitude(peak, line, wave="mag"):
    """Magnitude read from the largest motion (in counts) with one of the station's two lines in calibration.json.

    "mag": the largest motion of the whole PEAK_WINDOW, the better reading (the strongest waves are in it).
    "first": the largest motion of the first FIRST_WAVE seconds, on its own line: a little worse, and there 25 s sooner."""
    return line[f"{wave}_slope"] * math.log10(max(peak, 1e-9)) + line[f"{wave}_offset"]
