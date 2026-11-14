"""Listen to the earthquake feeds that matter for Panama and report each earthquake the moment one of them has it.

Usage:
    python src/listen.py                 # run until stopped
    python src/listen.py --minutes 45    # stop after 45 minutes

Feeds: IGC (Instituto de Geociencias, Universidad de Panama: the national network, an HTML list, first in every
case measured), GFZ GEOFON (independent, about ten minutes later, M4.7 and up: the fallback if the IGC page is
down) and the Pacific Tsunami Warning Center's bulletins (logged). EMSC, USGS and the US national tsunami
centre were dropped on 2026-10-10: see README.md (Results) for what was measured.
Alert texts are appended to logs/outbox.jsonl. They are also sent, at once, to whatever config.json names
(see config.example.json): a WhatsApp channel through a WAHA gateway, a Telegram channel, or both.
Without config.json nothing is sent.
Every first sighting goes to logs/sightings.jsonl with the wall-clock time, so latency.py can measure
how late each feed is. Every request goes to logs/requests.jsonl, which monitor.html shows.
"""
import argparse
import asyncio
import ctypes
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
LOGS = HERE / "logs"
SIGHTINGS, OUTBOX, REQUESTS = LOGS / "sightings.jsonl", LOGS / "outbox.jsonl", LOGS / "requests.jsonl"
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8")) if (HERE / "config.json").exists() else {}

PANAMA = (5.0, 11.5, -84.5, -76.0)             # south, north, west, east: the country and its seas
MIN_MAG = 4.0                                  # a second agency's figure for a smaller earthquake is logged, not announced
MAX_AGE = 1800                                 # seconds; nothing is announced about an earthquake older than this
SAME_EVENT = (30, 300)                         # seconds and km within which two reports are one earthquake
UA = {"User-Agent": "Mozilla/5.0 (panama-quake-alerts)"}
POLLS = {"igc": 10, "gfz": 30, "ptwc": 30}     # seconds between requests to each feed

IGC = "https://sismosgeociencias.up.ac.pa/"
GFZ = "https://geofon.gfz.de/eqinfo/list.php?fmt=geojson&nmax=40"
PTWC = "https://www.tsunami.gov/events/xml/PHEBAtom.xml"
NAMES = {"igc": "Instituto de Geociencias (UP)", "gfz": "GFZ GEOFON"}
EXACT = {"igc": "la magnitud del IGC es la más exacta", "gfz": "la magnitud de GFZ es la más exacta"}      # said next to a sensor reading
DETECTIONS = LOGS / "detections.jsonl"         # detect.py's log
WAVES, EXPECT = (20, 55), 34                   # seconds from an Azuero earthquake to the start of its shaking at the station, and the usual
RANGE = (-0.6, 0.2)                            # how the messages give a sensor reading R: "entre M R-0.6 y M R+0.2" (see shown())
VERIFY_FROM = 5.5                              # an IGC report this large waits for the station's word first (maintainer's request)
HOLD, HOLD_STEP = 900, 10                      # seconds such a report, not felt, waits for another agency; seconds between looks
listed = set()                                 # the entries of the IGC's list as last read (their own time stamps)

# Reference places for "a 20 km al este de Tonosi" when a feed gives no Spanish description.
PLACES = {"Ciudad de Panamá": (8.98, -79.52), "Colón": (9.36, -79.90), "David": (8.43, -82.43), "Santiago": (8.10, -80.98),
          "Chitré": (7.96, -80.43), "Las Tablas": (7.77, -80.28), "Tonosí": (7.41, -80.44), "Pedasí": (7.53, -80.03),
          "Macaracas": (7.73, -80.55), "Soná": (8.01, -81.32), "Penonomé": (8.52, -80.36), "Aguadulce": (8.24, -80.54),
          "La Chorrera": (8.88, -79.78), "Changuinola": (9.43, -82.52), "Puerto Armuelles": (8.28, -82.87),
          "Isla de Coiba": (7.47, -81.78), "La Palma": (8.41, -78.15), "Jaqué": (7.52, -78.16)}

events = []                                    # one dict per earthquake, shared by all feeds
recalled = False                               # the last run's log was read: what the lists show at start is not all old news
clock_error = 0.0                             # seconds this computer's clock runs ahead, measured at start


def now():
    return datetime.now(timezone.utc) - timedelta(seconds=clock_error)


def write(path, record):
    LOGS.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(a))


def in_panama(lat, lon):
    return PANAMA[0] <= lat <= PANAMA[1] and PANAMA[2] <= lon <= PANAMA[3]


def describe(lat, lon):
    """Spanish description relative to the nearest reference place."""
    name, (plat, plon) = min(PLACES.items(), key=lambda kv: km(lat, lon, *kv[1]))
    d = km(lat, lon, plat, plon)
    if d < 5:
        return f"en {name}"
    bearing = math.degrees(math.atan2((lon - plon) * math.cos(math.radians(plat)), lat - plat)) % 360
    side = ["norte", "noreste", "este", "sureste", "sur", "suroeste", "oeste", "noroeste"][int((bearing + 22.5) // 45) % 8]
    return f"a {d:.0f} km al {side} de {name}"


def alert_text(ev, source, sensor=""):
    """The agency's report as the channel shows it. `sensor` is a line about what the channel's own sensor did.

    The report is the agency's, and official when it is the IGC's; the channel only copies it, and says so."""
    s = ev["by"][source]
    local = ev["origin"] - timedelta(hours=5)                          # Panama has no daylight saving time
    hour = local.strftime("%I:%M %p").lstrip("0").replace("AM", "a. m.").replace("PM", "p. m.")
    where = s.get("place") or describe(ev["lat"], ev["lon"])
    depth = f" · profundidad {s['depth']:.0f} km" if s.get("depth") is not None else ""
    mag = f"M {s['mag']:.1f}" if s.get("mag") is not None else "magnitud por confirmar"
    # The IGC's report ends with the address of its list, where anyone can check it (maintainer's request). The list also has a
    # link for each earthquake, but it is 200 characters of map settings, and the Google Maps link stays the message's first.
    whose = {"igc": ("reporte oficial del Instituto de Geociencias", f"Dato copiado automáticamente del Instituto de Geociencias (UP); puede cambiar.\nFuente: {IGC}"),
             "gfz": ("reporte del centro alemán GFZ", "Dato copiado automáticamente de GFZ GEOFON; el Instituto de Geociencias aún no lo ha publicado.")}[source]
    return (f"SISMO {mag} (preliminar) · {whose[0]}\n{hour} hora de Panamá, {local:%d/%m}\n{where[0].upper() + where[1:]}{depth}\n"
            f"Mapa: https://maps.google.com/?q={ev['lat']:.3f},{ev['lon']:.3f}\n"
            f"Puede que lo haya sentido o no, según dónde esté.\n{sensor}{whose[1]}\n"
            "¿Lo sintió? Reaccione a este mensaje con ✅")     # the last line, as in the sensor's alert (maintainer's request)


async def deliver(record):
    """Send one outbox record to every destination in config.json; log how long each took."""
    targets = []
    waha, telegram = CONFIG.get("whatsapp_waha"), CONFIG.get("telegram")
    if waha:
        targets.append(("whatsapp", waha["url"].rstrip("/") + "/api/sendText",
                        {"session": waha.get("session", "default"), "chatId": waha["chat_id"], "text": record["text"]},
                        {"X-Api-Key": waha["api_key"]} if waha.get("api_key") else {}))
    if telegram:
        targets.append(("telegram", f"https://api.telegram.org/bot{telegram['token']}/sendMessage",
                        {"chat_id": telegram["chat_id"], "text": record["text"]}, {}))
    async with aiohttp.ClientSession() as session:
        for name, url, body, headers in targets:
            started, outcome = now(), "sent"
            for attempt in range(3):
                try:
                    async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as r:
                        r.raise_for_status()
                    break
                except Exception as e:
                    outcome = f"failed: {type(e).__name__}: {e}"[:200]
                    await asyncio.sleep(2 * (attempt + 1))
            else:
                attempt = 3
            write(LOGS / "sent.jsonl", {"at": now().isoformat(timespec="seconds"), "to": name, "event": record["event"], "type": record["type"],
                                        "outcome": outcome if attempt == 3 else "sent", "seconds": round((now() - started).total_seconds(), 2)})


def announce(record):
    write(OUTBOX, record)
    if record["type"] == "alert" or CONFIG.get("send_updates"):
        asyncio.get_running_loop().create_task(deliver(record))


def moment(text):
    """A time as detect.py writes it: UTC, without saying so."""
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def sensor_log():
    """The newest records of detect.py's log; a line still being written is left out."""
    records = []
    if DETECTIONS.exists():
        for line in DETECTIONS.read_text(encoding="utf-8").splitlines()[-400:]:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and "kind" in record and "trigger" in record:
                records.append(record)
    return records


def sensor_alert(origin, seen, mag, log, live):
    """detect.py's alert for this earthquake, if it sent one: (seconds ago, the time of its trigger). Else None.

    The alert is this earthquake's if the station's trigger is 80 s before to 110 s after the time the agency
    gives it (the waves take 20 to 55 s, and the IGC's first time can be a minute late) and what it read is within
    one magnitude of the agency's size: an M3.5 a minute before an M4.7 is not what the alert was about, though
    its waves set the trigger off (2026-10-11, 03:17 UTC). Of two such alerts, the one nearer to when the waves
    were due."""
    best = None
    if OUTBOX.exists():
        for line in OUTBOX.read_text(encoding="utf-8").splitlines()[-30:]:
            record = json.loads(line)
            if record["type"] == "detection" and record["text"].startswith("POSIBLE"):
                gap, read = (moment(record["event"]) - origin).total_seconds(), sensor_read(log, live, record["event"])
                if -80 <= gap <= 110 and (read is None or mag is None or abs(read - mag) <= 1.0):
                    if best is None or abs(gap - EXPECT) < abs(best[0] - EXPECT):
                        best = (gap, record)
    return best and ((seen - moment(best[1]["created"])).total_seconds(), best[1]["event"])


def sensor_read(log, live, trigger):
    """What detect.py read for the trigger of that time (as far as the time is written): the larger of its two
    readings, the one it holds against the announcing level. None if it has read nothing yet."""
    read = None
    for r in log:
        if r["trigger"].startswith(trigger):
            if r["kind"] == "closed":
                return max(r["reading"], r.get("first_wave") or r["reading"])      # the log's oldest rows have one reading
            if r["kind"] in ("declared", "update"):
                read = r["reading"]
    following = (live or {}).get("following")                  # still being followed: what it reads so far
    if following and abs(following["since"] - moment(trigger).timestamp()) < 1:
        read = max(read or 0.0, following["reads"])
    return read


def sensor_trigger(log, live, origin, mag):
    """detect.py's trigger on this earthquake's shaking, if it has one: the nearest to when the waves were due and,
    if it has been read, within one magnitude of the agency's size. In the sequence the station triggers every
    few minutes, so anything looser would hand an earthquake another one's reading; three in four of the
    triggers that are an entry's own fall in this window (measured on 43 pairs)."""
    best = None
    for r in log:
        if r["kind"] == "trigger":
            gap, read = (moment(r["trigger"]) - origin).total_seconds(), sensor_read(log, live, r["trigger"])
            if WAVES[0] <= gap <= WAVES[1] and (read is None or mag is None or abs(read - mag) <= 1.0):
                if best is None or abs(gap - EXPECT) < abs(best[0] - EXPECT):
                    best = (gap, r)
    return best and best[1]


def watching(origin, seen, live):
    """Could detect.py have triggered on this earthquake? The station's signal for those seconds must have arrived,
    and the detector must have had its two minutes of signal since it last connected."""
    try:
        status = json.loads((LOGS / "status_detect.json").read_text(encoding="utf-8"))
        settled = datetime.fromisoformat(status["connecting"]).timestamp() + 180
    except (OSError, ValueError, KeyError):
        settled = 0.0
    shaking = origin.timestamp() + EXPECT
    return seen.timestamp() - live["arrived"] < 120 and not live["warming"] and live["end"] >= shaking + 40 and shaking >= settled


def sensor_said(origin, seen, mag=None, source="igc"):
    """What the channel's own sensor did about this earthquake: (what, its reading or None, a line for the agency's report).

    `what` is "alerted" (its alert went out first), "under" (it read less than its announcing level), "in_time"
    (it read enough but had not announced), "no_signal" (the station's signal was not arriving), "plain" (no
    trigger that can be called this earthquake's with confidence: the line only says from what reading the
    sensor announces) or "none" (detect.py has never run here: nothing is said). Next to a reading the line
    says the agency's magnitude is the more exact one (maintainer's request)."""
    log, exact = sensor_log(), EXACT[source]
    try:
        live = json.loads((LOGS / "live.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        live = None
    alert = sensor_alert(origin, seen, mag, log, live)
    if alert:
        read = sensor_read(log, live, alert[1])
        return "alerted", read, f"El sensor de este canal lo avisó {alert[0]:.0f} s antes" + (f" y leyó {shown(read)} ({exact}).\n" if read is not None else ".\n")
    try:
        level, found = live["announces_from"], sensor_trigger(log, live, origin, mag)
        if found and found["stream_delay_s"] <= 60:            # on signal as old as detect.py's STALE it announces nothing
            read = sensor_read(log, live, found["trigger"])
            if read is not None and read < level:
                return "under", read, (f"El sensor de este canal lo detectó y leyó {shown(read)} ({exact}). "
                                       f"No avisó antes: su lectura no llegó a M {level:.1f}.\n")
            return "in_time", read, ("El sensor de este canal lo detectó" + (f" y leyó {shown(read)} ({exact})" if read is not None else "")
                                     + ", pero no avisó antes de este reporte.\n")
        if found or not watching(origin, seen, live):
            return "no_signal", None, "El sensor de este canal no avisó: en ese momento no le llegaba bien la señal de la estación.\n"
        # No trigger that is surely this earthquake's: too weak at the station, hidden in the minute after another
        # earthquake's trigger, or timed too far off by the agency to pair. What is true in all three is said.
        return "plain", None, f"El sensor de este canal no avisó: solo avisa cuando lee M {level:.1f} o más.\n"
    except (KeyError, TypeError):                              # no live signal file, or one of another shape
        return "none", None, ""


def sensor_line(origin, seen, mag=None, source="igc"):
    return sensor_said(origin, seen, mag, source)[2]


def shown(read, short=False):
    """A sensor reading as every message gives it since 2026-10-14: a range, "entre M 4.2 y M 5.0" ("M 4.2–5.0" in a
    line of the day's log). At the moment of the alert the reading was 0.20 above the IGC's first size on average,
    give or take 0.38 (62 alerts to 2026-10-14), and more once the announcing level went up to 4.7, since only the
    readings that come out high pass it; this range held the IGC's first size for 53 of those 62 (85 %). Until then
    the messages said "cerca de M R" or "leyó M R", and the alert added a word for R (leve, moderado, fuerte): an R
    of 5.7 went out as "FUERTE" for an IGC M4.9. the maintainer asked for the range in the alert, then in the IGC's report.
    On 2026-10-15 he had it moved 0.2 lower, "para que no sea tan preocupante": "reading_range" in config.json, read
    each time, gives the two ends; R-0.8 to R+0.0 held the IGC's latest size for 50 of 66 alerts (76 %) where R-0.6
    to R+0.2 held 60 (91 %), the IGC's size then above the range in 15 against 4."""
    try:
        ends = json.loads((HERE / "config.json").read_text(encoding="utf-8"))["reading_range"]
        lo_end, hi_end = float(ends[0]), float(ends[1])
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        lo_end, hi_end = RANGE
    low, high = read + lo_end, read + hi_end
    return f"M {low:.1f}–{high:.1f}" if short else f"entre M {low:.1f} y M {high:.1f}"


def log_below():
    """Reports of earthquakes smaller than this go to the day's log, which is edited, not to a message of their own:
    "log_below" in config.json, read each time it is needed. None (not there): every report is a message."""
    try:
        return float(json.loads((HERE / "config.json").read_text(encoding="utf-8"))["log_below"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def log_line(ev, source, what, read):
    """A small earthquake as one line of the day's log: its hour, the agency's size and place, and what the sensor read."""
    s = ev["by"][source]
    local = ev["origin"] - timedelta(hours=5)
    hour = local.strftime("%I:%M %p").lstrip("0").replace("AM", "a. m.").replace("PM", "p. m.")
    sensor = f"sensor {shown(read, short=True)}" if read is not None else "sensor sin señal" if what == "no_signal" else "sensor sin lectura"
    return f"{hour} · M {s['mag']:.1f} · {s.get('place') or describe(ev['lat'], ev['lon'])} · {sensor}"


def recall():
    """Take up the earthquakes the last run logged, so that starting again neither repeats a report nor loses one.

    Without its log everything on the lists at start has to count as old news, and a report published while this
    program was down is never announced (2026-10-11: an M4.2, during a 19-minute stop)."""
    global recalled
    if not SIGHTINGS.exists():
        return
    known = {}
    for line in SIGHTINGS.read_text(encoding="utf-8").splitlines()[-2000:]:
        try:
            r = json.loads(line)
            if r["source"] not in NAMES:                       # a feed this program no longer reads
                continue
            ev = known.get(r["event"])
            if ev is None:
                ev = known[r["event"]] = {"id": r["event"], "origin": datetime.strptime(r["event"], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc),
                                          "lat": r["lat"], "lon": r["lon"], "by": {}, "old": r["startup"]}
            ev["by"][r["source"]] = {"mag": r["mag"], "depth": r["depth"], "place": r["place"], "status": r["status"], "ident": r["id"],
                                     "seen": datetime.fromisoformat(r["seen"]), "origin": datetime.fromisoformat(r["origin"])}
        except (ValueError, KeyError, TypeError):              # a line of another shape, or cut short
            continue
    events.extend(known.values())
    recalled = True


def report(ev, source, origin, seen, mag, delay):
    """Tell the first report of an earthquake. Every earthquake an agency lists is told, whatever its size (maintainer's request). Since 2026-10-13 a small one the sensor had not announced is told quietly: a line in the day's log,
    a message that is edited and so rings no phone. A report of one the sensor did announce is always a message of
    its own: its confirmation."""
    what, read, line = sensor_said(origin, seen, mag, source)
    quiet = log_below()
    if quiet is not None and what != "alerted" and mag is not None and mag < quiet:
        announce({"created": seen.isoformat(timespec="seconds"), "type": "log", "event": ev["id"], "source": source, "mag": mag,
                  "delay_s": round(delay), "day": f"{origin - timedelta(hours=5):%Y-%m-%d}", "sensor": what, "text": log_line(ev, source, what, read)})
    else:
        announce({"created": seen.isoformat(timespec="seconds"), "type": "alert", "event": ev["id"], "source": source, "mag": mag,
                  "delay_s": round(delay), "text": alert_text(ev, source, line)})


def station_felt(origin, mag):
    """Did the Canal station feel an earthquake this large at this time? (felt or not, its reading); "later" while
    the signal for those minutes is still coming in; None when it cannot tell (no signal, a detector just started,
    a hole in the signal, minutes no longer drawn).

    An earthquake of M5.5 or more anywhere the IGC lists shakes the station hard: the M6.3 near Coiba of 2026-10-12
    read 6.0 there. The IGC's M7.2 near La Palma of 2026-10-13, which did not happen (the USGS has nothing, and the
    IGC took it off its list), read 3.96, and its M6.2 of that night 3.38. The reading is taken from a minute before
    the listed time to 150 s after it (the farthest one's waves, and the IGC's time can be a minute off), on the
    signal detect.py draws for monitor.html, with calibration.json's whole-wave line. Felt: a reading of at least
    the IGC's size less 1.5, and at least 4.0."""
    try:
        live = json.loads((LOGS / "live.json").read_text(encoding="utf-8"))
        status = json.loads((LOGS / "status_detect.json").read_text(encoding="utf-8"))
        settled = datetime.fromisoformat(status["connecting"]).timestamp() + 180
        line = json.loads((HERE / "calibration.json").read_text())["line"]
        a, b = origin.timestamp() - 60, origin.timestamp() + 150
        if live["warming"] or a < settled or a < live["start"]:
            return None
        if live["end"] < b:
            return "later" if now().timestamp() - live["arrived"] < 60 else None
        i, j = int((a - live["start"]) / live["step"]), int(math.ceil((b - live["start"]) / live["step"]))
        lo, hi = live["lo"][i:j], live["hi"][i:j]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    run = longest = 0
    for low, high in zip(lo, hi):                              # detect.py draws a hole in the signal flat, at zero
        run = run + 1 if low == 0 and high == 0 else 0
        longest = max(longest, run)
    if not lo or longest * live["step"] > 10:
        return None
    peak = max(max(map(abs, lo)), max(map(abs, hi)))
    read = line["mag_slope"] * math.log10(max(peak, 1)) + line["mag_offset"]
    return read >= max(4.0, mag - 1.5), round(read, 2)


def keep_back(ev, source, origin, mag, felt):
    """Hold back an IGC report of a large earthquake the station did not feel, or has not had the signal to feel yet.

    It goes out, late, if GFZ lists the same earthquake, if the station's signal shows it after all, or if the
    station could not tell after all; it is dropped if the IGC takes it off its list, or if nothing has confirmed
    it within HOLD seconds. Each step goes to logs/held.jsonl. A restart of this program forgets a report held then."""
    def note(what, **more):
        entry = ev["by"][source]
        write(LOGS / "held.jsonl", {"at": now().isoformat(timespec="seconds"), "event": ev["id"], "source": source, "mag": entry["mag"],
                                    "place": entry.get("place"), "what": what, **more})
        print(f"{now():%H:%M:%S} IGC M{entry['mag']} of {origin:%H:%M:%S} {what}: {next(iter(more.values()), '')}", flush=True)

    note("held back", station="the signal of those minutes is still coming" if felt == "later" else f"the station read only M{felt[1]}")

    async def wait():
        began, word = now(), felt
        while (now() - began).total_seconds() < HOLD:
            await asyncio.sleep(HOLD_STEP)
            entry = ev["by"][source]
            if "gfz" in ev["by"]:
                why = "GFZ lists it too"
                break
            if listed and entry.get("ident") and entry["ident"] not in listed:
                note("dropped", why="the IGC took it off its list")
                return
            later = station_felt(origin, entry["mag"])
            if later not in (None, "later") and later[0]:
                why = f"the station felt it after all (M{later[1]})"
                break
            if later is None and word == "later":
                why = "the station could not tell after all"
                break
            if later not in (None, "later"):
                word = later
        else:
            note("dropped", why=f"not felt by the station and not confirmed within {HOLD // 60} minutes")
            return
        note("posted late", why=why)
        report(ev, source, origin, now(), ev["by"][source]["mag"], (now() - origin).total_seconds())

    asyncio.get_running_loop().create_task(wait())


def sighting(source, origin, lat, lon, mag, depth=None, place=None, status=None, startup=False, ident=None):
    """Record that `source` reports an earthquake; announce it if it is the first report of a Panama event."""
    global noted
    seen = now()

    def same(e):
        """Same earthquake: close in time and place, and not a different entry of this same feed."""
        gap = abs((e["origin"] - origin).total_seconds())
        mine = e["by"].get(source)
        # A feed revising its own entry can move the origin time by several seconds (seen: 9 s at IGC).
        if mine and mine["ident"] != ident and abs((mine["origin"] - origin).total_seconds()) > 20:
            return False
        return gap <= SAME_EVENT[0] and km(e["lat"], e["lon"], lat, lon) <= SAME_EVENT[1]

    ev = min(filter(same, events), key=lambda e: abs((e["origin"] - origin).total_seconds()), default=None)
    if ev is None:
        # At start, an earthquake the last run's log does not have was published while this program was down.
        ev = {"id": f"{origin:%Y%m%dT%H%M%S}", "origin": origin, "lat": lat, "lon": lon, "by": {}, "old": startup and not recalled}
        events.append(ev)
    if source in ev["by"]:
        old = ev["by"][source]
        if old.get("mag") == mag and old.get("status") == status:
            return
        kind = "revision"
    else:
        kind = "first" if not ev["by"] else "also"
    ev["by"][source] = {"mag": mag, "depth": depth, "place": place, "status": status, "seen": seen, "ident": ident, "origin": origin}
    delay = (seen - origin).total_seconds()
    write(SIGHTINGS, {"seen": seen.isoformat(timespec="seconds"), "source": source, "kind": kind, "startup": startup,
                      "event": ev["id"], "origin": origin.isoformat(timespec="seconds"), "delay_s": round(delay), "mag": mag,
                      "lat": round(lat, 3), "lon": round(lon, 3), "depth": depth, "place": place, "status": status, "id": ident,
                      "panama": in_panama(lat, lon)})
    noted += 1
    if ev["old"] or not in_panama(lat, lon):                  # nothing is announced about what was old news when a run started
        return
    print(f"{seen:%H:%M:%S} {source:5s} {kind:8s} M{mag} origin {origin:%H:%M:%S} (+{delay:.0f}s) {place or describe(lat, lon)}", flush=True)
    if delay > MAX_AGE:                                        # a late listing of an old earthquake is not news
        return
    if kind == "first":
        # A large IGC report is checked against the station first (maintainer's request): see station_felt() and keep_back().
        if source == "igc" and mag is not None and mag >= VERIFY_FROM:
            felt = station_felt(origin, mag)
            if felt == "later" or (felt and not felt[0]):
                keep_back(ev, source, origin, mag, felt)
                return
        report(ev, source, origin, seen, mag, delay)
    elif kind == "also" and mag is not None and mag >= MIN_MAG:
        mags = ", ".join(f"{NAMES[k]} {v['mag']:.1f}" for k, v in ev["by"].items() if v.get("mag") is not None)
        announce({"created": seen.isoformat(timespec="seconds"), "type": "update", "event": ev["id"], "source": source,
                  "delay_s": round(delay), "text": f"Actualización del sismo de las {ev['origin'] - timedelta(hours=5):%H:%M}: {mags}."})


parked = {}                                    # feed -> its request, held until the answer has been read
noted = 0                                      # sightings and bulletins logged so far


def request_log(feed, started, status, size=0, new=False, note=None, hold=False):
    """One line per request for monitor.html; `hold` keeps it back for answered()."""
    record = {"at": started.isoformat(timespec="milliseconds"), "feed": feed, "status": status,
              "ms": round((now() - started).total_seconds() * 1000), "bytes": size, "new": new, "note": note}
    if hold:
        parked[feed] = record
    else:
        write(REQUESTS, record)


def answered(feed, before):
    """Log the held request of `feed`: its answer was new if it added to what was `noted` before reading it."""
    if feed in parked:
        write(REQUESTS, {**parked.pop(feed), "new": noted > before})


async def fetch(session, feed, url, cache=None):
    """GET `url`; with `cache` (a dict) the request is conditional and None means "not changed".

    The caller reads the answer and then calls answered(feed, ...), with no await in between."""
    headers = dict(UA)
    if cache and cache.get("modified"):
        headers["If-Modified-Since"] = cache["modified"]
    started = now()
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status == 304:
                request_log(feed, started, 304)
                return None
            r.raise_for_status()
            if cache is not None:
                cache["modified"] = r.headers.get("Last-Modified")
            raw = await r.read()
    except Exception as e:
        request_log(feed, started, type(e).__name__, note=str(e)[:120] or None)
        raise
    request_log(feed, started, r.status, len(raw), hold=True)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:                  # the IGC page is not UTF-8
        return raw.decode("latin-1")


async def poll(name, every, step):
    """Run `step(session, startup)` forever, `every` seconds apart; errors are logged and the loop goes on."""
    startup = True
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await step(session, startup)
                startup = False
            except Exception as e:
                for feed in list(parked):       # an answer arrived and could not be read
                    write(REQUESTS, {**parked.pop(feed), "status": "unreadable", "note": f"{type(e).__name__}: {e}"[:120]})
                write(LOGS / "errors.log", {"at": now().isoformat(timespec="seconds"), "feed": name, "error": f"{type(e).__name__}: {e}"[:300]})
            await asyncio.sleep(every)


igc_cache = {}


async def igc(session, startup):
    html = await fetch(session, "igc", IGC, igc_cache)
    if html is None:
        return
    before, rows = noted, set()
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S)[:60]:         # newest first; older rows are history
        c = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S)]
        if len(c) < 8 or not re.fullmatch(r"[\d.]+", c[6]):
            continue
        rows.add(f"{c[1]} {c[2]}")
        origin = datetime.strptime(f"{c[1]} {c[2]}", "%Y-%m-%d %I:%M:%S%p").replace(tzinfo=timezone.utc) + timedelta(hours=5)
        # "Localizado en Panamá a 32km al sur de Santiago" -> "a 32 km al sur de Santiago"; other regions keep their name
        place = re.sub(r"^Localizado en\s*", "", c[7]).strip()
        place = re.sub(r"^Panamá\s+(?=a \d)", "", place)
        place = re.sub(r"\s+a (\d+)\s*km", r", a \g<1> km", " " + place).strip(" ,") or None
        sighting("igc", origin, float(c[3]), float(c[4]), float(c[6]), float(c[5].replace("km", "") or 0), place, c[0], startup, f"{c[1]} {c[2]}")
    if rows:                                   # what the list holds now: a held report that leaves it is dropped
        listed.clear()
        listed.update(rows)
    answered("igc", before)


async def gfz(session, startup):
    features, before = json.loads(await fetch(session, "gfz", GFZ))["features"], noted
    for f in features:
        p, (lon, lat, depth) = f["properties"], (f["geometry"]["coordinates"] + [None])[:3]
        origin = datetime.fromisoformat(p["time"].replace("Z", "+00:00"))
        origin = origin if origin.tzinfo else origin.replace(tzinfo=timezone.utc)
        if in_panama(lat, lon):
            sighting("gfz", origin, lat, lon, float(p["mag"]) if p.get("mag") is not None else None, depth, None, (p.get("status") or "")[:1], startup, f.get("id"))
    answered("gfz", before)


bulletins = set()


async def ptwc(session, startup):
    """Bulletins of the Pacific Tsunami Warning Center, which also covers the Caribbean. Logged, not announced."""
    global noted
    xml, before = await fetch(session, "ptwc", PTWC), noted
    for entry in re.findall(r"<entry>(.*?)</entry>", xml, flags=re.S):
        ident = (re.findall(r"<id>(.*?)</id>", entry) or [""])[0]
        if ident in bulletins:
            continue
        bulletins.add(ident)
        noted += 1
        title = (re.findall(r"<title>(.*?)</title>", entry, flags=re.S) or [""])[0].strip()
        updated = (re.findall(r"<updated>(.*?)</updated>", entry) or [""])[0]
        write(LOGS / "bulletins.jsonl", {"seen": now().isoformat(timespec="seconds"), "centre": "ptwc", "startup": startup,
                                         "updated": updated, "title": title[:200], "id": ident})
    answered("ptwc", before)


async def tidy():
    """The request log is only there to be watched: keep its last 2,000 lines."""
    while True:
        if REQUESTS.exists() and REQUESTS.stat().st_size > 1_000_000:
            REQUESTS.write_text("\n".join(REQUESTS.read_text(encoding="utf-8").splitlines()[-2000:]) + "\n", encoding="utf-8")
        await asyncio.sleep(600)


async def clock_offset():
    """Seconds this computer's clock runs ahead of three web servers' clocks (median; their headers have 1 s steps)."""
    offsets = []
    async with aiohttp.ClientSession() as s:
        # servers that stamp each answer as they send it: a feed served from a cache can carry a minute-old date
        for url in ("https://www.google.com/generate_204", "https://www.seismicportal.eu/", "https://www.cloudflare.com/"):
            try:
                async with s.head(url, headers=UA, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    offsets.append((datetime.now(timezone.utc) - parsedate_to_datetime(r.headers["Date"])).total_seconds() - 0.5)
            except Exception:
                pass
    return sorted(offsets)[len(offsets) // 2] if offsets else 0.0


async def main(minutes):
    global clock_error
    # Windows puts an idle computer to sleep even while this runs, which silently stops the alerts.
    if hasattr(ctypes, "windll"):
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)      # keep awake until this program exits
    clock_error = await clock_offset()
    print(f"this computer's clock runs {clock_error:+.1f} s against web servers; times below are corrected", flush=True)
    LOGS.mkdir(exist_ok=True)
    recall()
    print(f"{len(events)} earthquakes taken up from the last run's log", flush=True)
    (LOGS / "status_listen.json").write_text(json.dumps({
        "pid": os.getpid(), "started": now().isoformat(timespec="seconds"), "clock_error_s": round(clock_error, 1), "polls": POLLS,
        "sends_to": [k for k in ("whatsapp_web", "whatsapp_waha", "telegram") if CONFIG.get(k)]}))
    steps = {"igc": igc, "gfz": gfz, "ptwc": ptwc}
    tasks = [asyncio.create_task(t) for t in (tidy(), *(poll(name, every, steps[name]) for name, every in POLLS.items()))]
    await asyncio.sleep(minutes * 60 if minutes else 10 ** 9)
    for t in tasks:
        t.cancel()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--minutes", type=float, default=0)
    args = ap.parse_args()
    try:
        asyncio.run(main(args.minutes))
    except KeyboardInterrupt:
        pass
