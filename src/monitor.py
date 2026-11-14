"""A local page that shows the alert programs at work: requests, sensor signal, detections and messages.

Usage:
    python src/monitor.py                    # page for programs that are already running
    python src/monitor.py --start --open     # also start the alert programs, and open the page
    python src/monitor.py --start --min-mag 4.5 --minutes 120

The page is monitor.html, at http://127.0.0.1:8791; it asks this program for /state once a second.
This program only reads the files in logs/ (written by the alert programs) and answers on this computer
alone. With --start it starts listen.py, detect.py and, if config.json names a WhatsApp channel,
whatsapp_web.py; it starts each again if it stops, and stops them when it ends (Ctrl+C, the window closed,
or after --minutes; without --minutes there is no time limit). A second copy refuses to start while the
first holds the port. start_monitor.bat runs it this way in a window of its own.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compare

HERE = Path(__file__).resolve().parent
LOGS = HERE / "logs"
VENV = HERE.parent / ".venv" / "Scripts" / "python.exe"
PYTHON = str(VENV) if VENV.exists() else sys.executable        # the detector needs ObsPy, which is in the project's environment
FINE = (200, 304)                                              # request outcomes that are not failures

signal = None                                                  # the detector's signal file as last read whole


def tail(path, lines, size=300_000):
    """The last `lines` records of a JSON-lines file; a line cut in half by the read is skipped."""
    if not path.exists():
        return []
    with path.open("rb") as f:
        f.seek(max(path.stat().st_size - size, 0))
        text = f.read().decode("utf-8", "replace").splitlines()
    out = []
    for line in text[-lines:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):                              # not there, or being rewritten at this instant
        return None


def stamp(records, *fields):
    """Add epoch seconds next to the named time fields (`seen` gets `seen_t`): the two programs write times differently."""
    for r in records:
        for field in fields:
            if r.get(field):
                r[field + "_t"] = compare.parse(r[field]).timestamp()
    return records


def state():
    global signal
    status = {"listen": read_json(LOGS / "status_listen.json"), "detect": read_json(LOGS / "status_detect.json")}
    clock_error = next((s["clock_error_s"] for s in status.values() if s), 0.0)
    det, sightings = tail(LOGS / "detections.jsonl", 400), tail(LOGS / "sightings.jsonl", 500)
    rows, lone = compare.earthquakes(det, sightings)
    for row in stamp(rows, "origin"):
        stamp(row["by"].values(), "seen")
        if row["sensor"]:
            stamp([row["sensor"]], "declared_seen")
    closed = {d["trigger"]: d for d in det if d["kind"] == "closed"}                   # what each trigger read in the end
    triggers = [{**d, **{k: closed[d["trigger"]][k] for k in ("peak", "reading", "declared") if d["trigger"] in closed}}
                for d in det if d["kind"] == "trigger"][-40:]
    requests, feeds = stamp(tail(LOGS / "requests.jsonl", 700), "at"), {}
    for r in requests:
        feed = feeds.setdefault(r["feed"], {"count": 0, "failed": 0, "new_t": None})
        feed["count"] += 1
        feed["failed"] += r["status"] not in FINE
        feed["last"] = r
        if r["new"]:
            feed["new_t"] = r["at_t"]
    signal = read_json(LOGS / "live.json") or signal
    return {"now": time.time() - clock_error, **status, "feeds": feeds, "requests": requests[-60:],
            "earthquakes": rows[-14:], "lone": stamp(lone[-6:], "signal", "seen"), "triggers": stamp(triggers, "trigger", "seen"),
            "outbox": stamp(tail(LOGS / "outbox.jsonl", 12), "created"), "sent": tail(LOGS / "sent.jsonl", 40),
            "bulletins": tail(LOGS / "bulletins.jsonl", 3), "errors": tail(LOGS / "errors.log", 5), "signal": signal,
            "line": (read_json(HERE / "calibration.json") or {}).get("line"), "whatsapp": stamp([read_json(LOGS / "status_whatsapp.json") or {}], "at")[0]}


class Server(ThreadingHTTPServer):
    # Windows lets a second program listen on a port that is "reusable"; a second monitor must be turned away instead.
    allow_reuse_address = os.name != "nt"


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/state":
                body, kind = json.dumps(state(), ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8"
            elif path in ("/", "/monitor.html"):
                body, kind = (HERE / "monitor.html").read_bytes(), "text/html; charset=utf-8"
            else:
                return self.send_error(404)
        except Exception as e:                                 # shown by the page instead of a silent stop
            return self.send_error(500, f"{type(e).__name__}: {e}"[:200])
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):                              # a request a second would bury everything else
        pass


def keep_running(programs, stop):
    """Start each program, start it again whenever it has stopped, and end them all once `stop` is set."""
    running = {}
    while not stop.is_set():
        for name, extra in programs.items():
            if name in running and running[name].poll() is None:
                continue
            try:
                if name in running:
                    with (LOGS / "errors.log").open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "feed": "monitor",
                                            "error": f"{name}.py stopped with code {running[name].returncode}; started again"}) + "\n")
                console = (LOGS / f"{name}_console.log").open("a", encoding="utf-8")
                running[name] = subprocess.Popen([PYTHON, "-u", "-W", "ignore", str(HERE / f"{name}.py"), *extra], stdout=console,
                                                 stderr=subprocess.STDOUT, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            except OSError as e:                               # a file held by another program, say: the next round tries again,
                print(f"{name}.py could not be started: {e}", flush=True)       # for nothing else restarts the programs
        stop.wait(30)                                          # unhurried: the sensor server slows down clients that reconnect fast
    for process in running.values():
        process.terminate()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--start", action="store_true", help="also start listen.py and detect.py")
    ap.add_argument("--min-mag", type=float, default=5.0, help="announcing magnitude passed to detect.py")
    ap.add_argument("--minutes", type=float, default=0, help="stop after this long (default: run until stopped)")
    ap.add_argument("--open", action="store_true", help="open the page in the browser")
    args = ap.parse_args()
    LOGS.mkdir(exist_ok=True)
    url = f"http://127.0.0.1:{args.port}/"
    try:
        server = Server(("127.0.0.1", args.port), Page)
    except OSError:                                            # before anything is started twice
        sys.exit(f"Port {args.port} is taken: a monitor is probably running already. Its page is {url}")
    stop, minder = threading.Event(), None
    if args.start:
        programs = {"listen": [], "detect": ["--min-mag", str(args.min_mag)]}
        if (read_json(HERE / "config.json") or {}).get("whatsapp_web"):
            programs["whatsapp_web"] = []                      # the browser window that posts to the WhatsApp channel
        minder = threading.Thread(target=keep_running, args=(programs, stop))
        minder.start()
    if args.minutes:
        threading.Timer(args.minutes * 60, server.shutdown).start()
    print(f"monitor at {url}  (Ctrl+C stops it{' and the programs it started' if args.start else ''})", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if minder:
            minder.join(10)


if __name__ == "__main__":
    main()
