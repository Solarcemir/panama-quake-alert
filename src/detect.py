"""Detect strong shaking from one live seismometer, before any agency has published the earthquake.

Usage:
    python src/detect.py                 # run until stopped
    python src/detect.py --minutes 35    # stop after 35 minutes
    python src/detect.py --min-mag 4.5   # announce smaller earthquakes too
    ("sensor_min_mag" in config.json, if there, is used instead, and can be changed while this runs)

It follows CU.BCIP (Barro Colorado, Panama Canal), the station that calibrate.py found best against the
IGC list. A trigger marks an onset: the energy of the last 1.5 s reaching five times that of the last 30 s.
Triggers come when the ground moves, not at fixed intervals; after one, no new one starts for 75 s.
A trigger is followed for 60 s, and with every packet the largest motion since it is turned into a
magnitude with the lines in calibration.json, fitted on IGC magnitudes of the Azuero sequence: one line for
the first 15 s (the first wave alone, so an alert need not wait for the strongest waves 25 s later) and
one for the whole wave, the better reading. It announces the moment either reading reaches 5 (or
--min-mag), nothing is waited for, and sends an update when the reading later grows by half a magnitude.
It knows no location, and the reading is right only for an earthquake about as far away as Azuero
(200 km): the message says so. An earthquake the IGC has already reported is not announced.
Everything is logged in logs/detections.jsonl with the wall-clock time, so compare.py can measure the head
start over the IGC. Messages go to the same outbox and destinations as listen.py. The last ten minutes of
signal are kept in logs/live.json for monitor.html.
"""
import argparse
import asyncio
import ctypes
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# The maths library under numpy readies one thread per core and commits memory for each: on this 32-thread laptop
# 1,607 MB, against 130 MB with one thread, for the same 135 MB in use (measured 2026-10-14, when the machine was at
# 90 % of its commit limit). A 180 s buffer of one station needs no more than one. Set before numpy is imported.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np
from obspy import Stream, UTCDateTime
from obspy.clients.seedlink.easyseedlink import EasySeedLinkClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detector_core as core
import listen

HERE = Path(__file__).resolve().parent
LOG = HERE / "logs" / "detections.jsonl"
BUFFER = 180.0                                   # seconds of signal kept
GAP = 10.0                                       # seconds missing from the stream after which the detector starts again
SAME = (-80.0, 110.0)                            # seconds from the time the IGC gives an earthquake to its shaking at the station:
                                                 # 20 to 110 s by the waves alone, and the IGC's first time can be a minute late (seen: 52 s)
STALE = 60.0                                     # seconds: shaking that arrives older than this is not announced. A station that
                                                 # stalls hands over its backlog in one go (2026-10-11: signal 449 s old read M4.6)
VIEW = HERE / "logs" / "live.json"               # the signal drawn by monitor.html; None switches it off
STEP, SPAN = 0.5, 600.0                          # the drawn trace has a point every STEP seconds and is SPAN seconds long
# A reading of CHECK_FROM or more is announced only if another station shook too. On 2026-10-22 at 21:34 and 21:44
# UTC CU.BCIP jumped by 23 million counts, read M6.9 twice and two false alerts went out, while TC.DRK0 and TC.PIRO in
# Costa Rica, on the same server, showed nothing (0.6 to 1.8 times their largest motion of the two minutes before).
# In real large earthquakes they moved 4 to 2,700 times more (the M6.3 near Coiba: DRK0 2,677 times). Their waves
# come up to about 20 s after BCIP's, so such an alert waits for them, CHECK_WAIT s at most; with neither of them
# sending, it goes out as before.
HELPERS = ("TC.DRK0..HHZ", "TC.PIRO..HHZ")
CHECK_FROM, CHECK_GAIN, CHECK_WAIT = 5.0, 3.0, 45.0


shown = listen.shown                             # a reading as a range, "entre M 4.2 y M 5.0": the same in every message

def announcing(default):
    """The magnitude from which a reading is announced: "sensor_min_mag" in config.json when it is there, else --min-mag.

    Read every time it is needed, so it can be changed there while everything keeps running."""
    try:
        return float(json.loads((HERE / "config.json").read_text(encoding="utf-8"))["sensor_min_mag"])
    except (OSError, ValueError, KeyError, TypeError):
        return default


def stamp(when):
    return str(when)[:22]


class TimeUp(Exception):
    """--minutes has passed."""


class Detector(EasySeedLinkClient):
    def __init__(self, line, min_mag, stop_at):
        # ObsPy 1.5.1 leaves the connection timeout unset and then fails on it: set it before connecting.
        super().__init__(f"{core.SEEDLINK[0]}:{core.SEEDLINK[1]}", autoconnect=False)
        self.conn.timeout = 30
        self.connect()
        self.line, self.min_mag, self.stop_at = line, min_mag, stop_at
        self.buffer = Stream()
        self.last = UTCDateTime(0)               # time of the last trigger
        self.scanned = UTCDateTime(0)            # signal before this time has had its chance to trigger
        self.event = None                        # the trigger whose motion is being followed
        self.view = None                         # the trace drawn by monitor.html
        net, sta, loc, cha = core.STATION.split(".")
        self.select_stream(net, sta, loc + cha)
        self.helpers = {}                        # the other stations' last 300 s, for corroborated()
        for sid in HELPERS:
            hn, hs, hl, hc = sid.split(".")
            self.select_stream(hn, hs, hl + hc)
            self.helpers[sid] = Stream()

    def on_data(self, trace):
        if self.stop_at and time.time() > self.stop_at:
            # ObsPy's loop cannot be asked to end: close() only disconnects and the loop connects again, and
            # terminate() is cleared on its next pass. Leaving through an exception is what works.
            self.close()
            raise TimeUp
        if trace.id in getattr(self, "helpers", {}):
            kept = self.helpers[trace.id]
            kept += trace
            kept.merge(method=1, fill_value=0)
            kept.trim(kept[0].stats.endtime - 300, kept[0].stats.endtime)
            return
        if trace.id != core.STATION:
            return
        arrived = UTCDateTime(listen.now())
        buf = self.buffer
        # A hole in the stream would be bridged with a straight line, and the signal's return then looks like an
        # onset (tried on recordings: a trigger within a second of the return). Start again instead.
        if len(buf) and trace.stats.starttime - buf[0].stats.endtime > GAP:
            buf.clear()
        buf += trace
        buf.merge(method=1, fill_value="interpolate")
        buf.trim(buf[0].stats.endtime - BUFFER, buf[0].stats.endtime)
        tr = buf[0]
        if tr.stats.npts < 2 * core.LTA * tr.stats.sampling_rate:
            return
        prepared = core.prepare(tr)
        # Just after starting, the long average is not settled and noise looks like a trigger: wait two minutes.
        warming = tr.stats.npts < 120 * tr.stats.sampling_rate
        if VIEW:
            try:
                self.show(prepared, arrived, warming)
            except Exception as e:               # the picture must never stop the detector
                listen.write(HERE / "logs" / "errors.log", {"at": str(arrived)[:19], "feed": "monitor view", "error": f"{type(e).__name__}: {e}"[:300]})
        # Only signal that has just arrived may trigger. Every packet recomputes the ratio from the start of the
        # buffer, and once a strong arrival has left the buffer the ratio of the signal after it rises: looked at
        # again, old signal triggered 40 to 145 s late (seen live on 2026-10-10).
        since = max(tr.stats.starttime + 2 * core.LTA, self.last + core.DEAD, self.scanned)
        self.scanned = tr.stats.endtime
        if warming:
            return
        found = core.find_triggers(tr, since, prepared)
        if found:
            self.finish(arrived)
            self.last = found[0]["time"]
            self.event = {"first": found[0]["time"], "peak": 0.0, "whole": 0.0, "early": 0.0, "reads": 0.0, "declared": False, "late": False}
            listen.write(LOG, {"kind": "trigger", "trigger": stamp(self.last), "seen": stamp(arrived),
                               "stream_delay_s": round(float(arrived - tr.stats.endtime), 1), "ratio": round(found[0]["ratio"], 1)})
            print(f"{str(arrived)[11:19]} trigger (signal time {str(self.last)[11:19]}, stream {arrived - tr.stats.endtime:.0f} s behind)", flush=True)
        ev = self.event
        if ev and tr.stats.endtime - ev["first"] > core.PEAK_WINDOW + 15:
            self.finish(arrived)
        elif ev:
            ev["peak"] = core.peak_since(prepared, ev["first"])
            ev["whole"] = core.magnitude(ev["peak"], self.line)
            ev["early"] = core.magnitude(core.peak_since(prepared, ev["first"], core.FIRST_WAVE), self.line, "first")
            ev["reads"] = max(ev["whole"], ev["early"])
            if not ev["declared"] and not ev.get("doubted"):
                if ev["reads"] >= announcing(self.min_mag) and not reported(tr.stats.endtime, ev["reads"]):
                    word = self.corroborated(ev["first"]) if ev["reads"] >= CHECK_FROM else True
                    if arrived - tr.stats.endtime > STALE:   # old shaking is not news, and by now the IGC has it or soon will
                        if not ev["late"]:
                            print(f"{str(arrived)[11:19]} not announced: the signal reading M{ev['reads']:.1f} arrived "
                                  f"{arrived - tr.stats.endtime:.0f} s old", flush=True)
                        ev["late"] = True
                    elif word is False:
                        ev["doubted"] = True
                        print(f"{str(arrived)[11:19]} not announced: M{ev['reads']:.1f} at {core.STATION} alone, "
                              f"the other stations did not shake", flush=True)
                    elif word or tr.stats.endtime - ev["first"] >= CHECK_WAIT:
                        ev["declared"], ev["announced"] = True, ev["reads"]
                        self.declare(tr.stats.endtime, arrived)
            elif ev["declared"] and ev["reads"] >= ev["announced"] + 0.5:
                ev["announced"] = ev["reads"]
                self.update(arrived)

    def corroborated(self, first):
        """Did another station shake when CU.BCIP did? True; False when they had signal over the minute and did not;
        None when it cannot tell yet (their waves still to come) or at all (they send nothing)."""
        quiet = False
        for kept in getattr(self, "helpers", {}).values():
            if not len(kept) or kept[0].stats.starttime > first - 150:
                continue
            prepared = core.prepare(kept[0])[0]
            before, after = np.abs(prepared.slice(first - 150, first - 30).data), np.abs(prepared.slice(first - 10, first + 90).data)
            if not len(before) or not len(after):
                continue
            if after.max() >= CHECK_GAIN * max(float(before.max()), 1.0):
                return True
            if kept[0].stats.endtime >= first + 40:
                quiet = True
        return False if quiet else None

    def finish(self, arrived):
        """Stop following the current trigger and log what its motion read in the end (the whole wave) and at first."""
        ev, self.event = self.event, None
        if ev:
            listen.write(LOG, {"kind": "closed", "trigger": stamp(ev["first"]), "seen": stamp(arrived), "peak": round(ev["peak"]),
                               "reading": round(ev["whole"], 2), "first_wave": round(ev["early"], 2), "declared": ev["declared"],
                               "late": ev["late"], "doubted": ev.get("doubted", False)})          # late: it read enough to announce, but the signal arrived too old

    def show(self, prepared, arrived, warming):
        """Add the newest signal to the trace monitor.html draws: lowest, highest and top ratio of every STEP seconds."""
        tr, ratio = prepared
        rate, start = tr.stats.sampling_rate, tr.stats.starttime.timestamp
        n = int(round(STEP * rate))
        v = self.view
        nxt = v["start"] + len(v["lo"]) * STEP if v else None
        first = math.ceil((start + 5) / STEP) * STEP        # the first seconds of a buffer carry the filter's start-up
        if v and nxt < start and first - nxt < SPAN:         # started again after a hole: draw the hole flat
            for key in ("lo", "hi", "ratio"):
                v[key] += [0] * int(round((first - nxt) / STEP))
            nxt = first
        elif nxt is None or nxt < start:                     # nothing drawn yet
            nxt, v = first, {"start": first, "lo": [], "hi": [], "ratio": []}
            self.view = v
        i = int(round((nxt - start) * rate))
        bins = (tr.stats.npts - i) // n
        if bins > 0:
            data = tr.data[i:i + bins * n].reshape(bins, n)
            v["lo"] += np.round(data.min(axis=1)).astype(int).tolist()
            v["hi"] += np.round(data.max(axis=1)).astype(int).tolist()
            v["ratio"] += np.round(ratio[i:i + bins * n].reshape(bins, n).max(axis=1), 1).tolist()
            extra = len(v["lo"]) - int(SPAN / STEP)
            if extra > 0:
                v["start"] += extra * STEP
                for key in ("lo", "hi", "ratio"):
                    del v[key][:extra]
        ev = self.event
        VIEW.parent.mkdir(exist_ok=True)
        tmp = VIEW.with_suffix(".tmp")
        tmp.write_text(json.dumps({"station": core.STATION, "step": STEP, **v, "end": tr.stats.endtime.timestamp, "arrived": arrived.timestamp,
                                   "warming": warming, "following": ev and {"since": ev["first"].timestamp, "reads": round(ev["reads"], 2)},
                                   "followed_s": core.PEAK_WINDOW, "announces_from": announcing(self.min_mag)}, separators=(",", ":")))
        try:
            os.replace(tmp, VIEW)
        except PermissionError:                  # the monitor is reading the file right now: the next packet will do
            pass

    def declare(self, signal, arrived):
        ev = self.event
        local = (signal - 5 * 3600).strftime("%I:%M:%S %p").lstrip("0").replace("AM", "a. m.").replace("PM", "p. m.")
        text = ("POSIBLE SISMO detectado por un sensor\n"
                f"Sacudida registrada a las {local} (hora de Panamá) en la estación del Canal de Panamá\n"
                f"Lectura inicial: {shown(ev['reads'])} si viene de Azuero. Sin ubicación todavía.\n"
                "Aviso automático sin confirmar: el reporte oficial del Instituto de Geociencias llega en 2 a 3 minutos.\n"
                "¿Lo sintió? Reaccione a este mensaje con ✅")
        self.send(text, arrived)
        listen.write(LOG, {"kind": "declared", "trigger": stamp(ev["first"]), "signal": stamp(signal), "seen": stamp(arrived), "reading": round(ev["reads"], 2)})
        print(f"{str(arrived)[11:19]} DECLARED: reading M{ev['reads']:.1f}", flush=True)

    def update(self, arrived):
        ev = self.event
        text = ("ACTUALIZACIÓN del posible sismo: la sacudida siguió creciendo.\n"
                f"Ahora se lee {shown(ev['reads'])} si viene de Azuero. Falta el dato del Instituto de Geociencias.")
        self.send(text, arrived)
        listen.write(LOG, {"kind": "update", "trigger": stamp(ev["first"]), "seen": stamp(arrived), "reading": round(ev["reads"], 2)})
        print(f"{str(arrived)[11:19]} UPDATE: reading now M{ev['reads']:.1f}", flush=True)

    def send(self, text, arrived):
        record = {"created": str(arrived)[:19], "type": "detection", "event": str(self.event["first"])[:19], "source": "sensor", "delay_s": None,
                  "reads": round(self.event["reads"], 2), "text": text}
        listen.write(listen.OUTBOX, record)
        if listen.CONFIG:
            asyncio.run(listen.deliver(record))


def reported(signal, reads):
    """Has an agency alert already gone out for the earthquake that is shaking the station now?

    listen.py names each alert after the time the agency gives the earthquake; SAME says how far from it the
    shaking can be, and the alert must be for an earthquake of about the size read (within one magnitude).
    Going by the earthquake's time and size, not by when the alert was written, keeps an alert about the
    previous earthquake from silencing a new one: in a sequence the IGC publishes every few minutes, and half
    of the time some alert of its is under four minutes old."""
    if not listen.OUTBOX.exists():
        return False
    for line in listen.OUTBOX.read_text(encoding="utf-8").splitlines()[-20:]:
        record = json.loads(line)
        if record["type"] in ("alert", "log"):     # "log": a small one, told in the day's log and not in a message of its own
            origin = datetime.strptime(record["event"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()
            size = record.get("mag")
            if SAME[0] <= signal.timestamp - origin <= SAME[1] and (size is None or abs(size - reads) <= 1.0):
                return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--minutes", type=float, default=0)
    ap.add_argument("--min-mag", type=float, default=5.0)
    args = ap.parse_args()
    line = json.loads((HERE / "calibration.json").read_text())["line"]
    # Windows puts an idle computer to sleep even while this runs, which silently stops the alerts.
    if hasattr(ctypes, "windll"):
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)      # keep awake until this program exits
    listen.clock_error = asyncio.run(listen.clock_offset())
    level = announcing(args.min_mag)             # as of now: config.json can change it while this runs
    print(f"clock corrected by {listen.clock_error:+.1f} s; announcing when {core.STATION} reads M{level} or more", flush=True)
    (HERE / "logs").mkdir(exist_ok=True)
    status = {"pid": os.getpid(), "started": listen.now().isoformat(timespec="seconds"), "clock_error_s": round(listen.clock_error, 1),
              "min_mag": level, "station": core.STATION, "server": f"{core.SEEDLINK[0]}:{core.SEEDLINK[1]}"}
    stop_at = time.time() + args.minutes * 60 if args.minutes else None
    attempt = 0
    while not stop_at or time.time() < stop_at:
        # A station that goes silent ends the connection after two minutes and this loop connects again. The time of
        # each attempt lets monitor.html tell a silent station from a detector that is not running.
        attempt += 1
        (HERE / "logs" / "status_detect.json").write_text(json.dumps({**status, "connecting": listen.now().isoformat(timespec="seconds"), "attempt": attempt}))
        try:
            Detector(line, args.min_mag, stop_at).run()
        except TimeUp:
            break
        except Exception as e:                   # dropped connection: start again
            listen.write(HERE / "logs" / "errors.log", {"at": str(UTCDateTime())[:19], "feed": "seedlink", "error": f"{type(e).__name__}: {e}"[:300]})
            time.sleep(30)                       # the server slows down clients that reconnect in a hurry


if __name__ == "__main__":
    main()
