"""Recordings of the detector's station, kept in data/ as six-hour files and downloaded when missing.

Usage:
    python src/recordings.py            # fetch everything calibrate.py and replay.py use, up to now

Two stretches are kept: two ordinary days before the 2026-10-09 mainshock (how often does the detector fire
when nothing is going on?) and everything since (how many IGC earthquakes does it catch?). The archive
(EarthScope) runs some minutes behind real time, so the newest file is fetched again until its six hours
are complete. calibrate.py --compare fetches the other candidate stations the same way.
"""
import sys
from pathlib import Path

from obspy import Stream, UTCDateTime, read
from obspy.clients.fdsn import Client

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detector_core as core

DATA = Path(__file__).resolve().parent / "data"
QUIET = (UTCDateTime("2026-10-07T18:00:00"), UTCDateTime("2026-10-09T17:50:00"))     # two ordinary days
SEQUENCE = QUIET[1]                                # the aftershock sequence is everything from here on (mainshock 17:56)
BLOCK = 6 * 3600


def blocks(start, end):
    """Six-hour blocks, aligned to 00, 06, 12 and 18 UTC, that cover start..end."""
    t = UTCDateTime(int(start.timestamp // BLOCK) * BLOCK)
    while t < end:
        yield t
        t += BLOCK


def fetch(stations=(core.STATION,), start=QUIET[0]):
    """Download every block that is missing or was incomplete when last fetched."""
    end = UTCDateTime() - 20 * 60                  # the archive is not complete closer to now than this
    client = None
    for sid in stations:
        net, sta, loc, cha = sid.split(".")
        (DATA / sid).mkdir(parents=True, exist_ok=True)
        for t in blocks(start, end):
            whole, part = DATA / sid / f"{t.strftime('%Y%m%dT%H')}.mseed", DATA / sid / f"{t.strftime('%Y%m%dT%H')}.part.mseed"
            if whole.exists():
                continue
            client = client or Client("IRIS")
            try:
                st = client.get_waveforms(net, sta, loc or "--", cha, t, min(t + BLOCK, end))
            except Exception as e:
                print(f"  {sid} {t.strftime('%m-%d %H:%M')}: {type(e).__name__} {str(e)[:90]}", flush=True)
                continue
            part.unlink(missing_ok=True)
            st.write(str(whole if t + BLOCK <= end else part), format="MSEED")
            print(f"  {sid} {t.strftime('%m-%d %H:%M')}: {sum(tr.stats.npts for tr in st) / st[0].stats.sampling_rate / 3600:.2f} h", flush=True)


def load(sid, start, end):
    """The unbroken stretches of one station between start and end, as a list of traces.

    A hole in the recording stays a hole (the live detector also starts again after one)."""
    st = Stream()
    for t in blocks(start, end):
        for path in (DATA / sid).glob(f"{t.strftime('%Y%m%dT%H')}*.mseed"):
            st += read(str(path))
    st.merge(method=1)
    st.trim(start, end)
    return [tr for tr in st.split() if tr.stats.npts > 1]


def newest(sid=core.STATION):
    """Where the recordings of a station stop."""
    files = sorted((DATA / sid).glob("*.mseed"))
    return read(str(files[-1]), headonly=True)[-1].stats.endtime if files else QUIET[0]


if __name__ == "__main__":
    fetch()
    files = list((DATA / core.STATION).glob("*.mseed"))
    print(f"{core.STATION}: {len(files)} files, {sum(f.stat().st_size for f in files) / 1e6:.0f} MB, up to {str(newest())[:19]} UTC")
