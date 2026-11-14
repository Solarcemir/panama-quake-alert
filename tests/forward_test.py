"""Run the sender's own loop, forward(), for a minute on a stand-in channel (see standin.py) with an outbox that fills
up meanwhile: the log read again at a start without its file, an alert at once, lines that wait their minute, a long
log, a page that refuses edits for a while (the tries must thin out) and an alert in the middle of that.
Nothing to do with WhatsApp: it opens a page of its own in a browser without a window, and writes to a temporary
folder. The minute between two tries is three seconds here.

    .venv\\Scripts\\python.exe src\\tests\\forward_test.py     (about a minute; ends with ALL PASS)
"""
import contextlib
import io
import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]
import whatsapp_web as wa
from playwright.sync_api import sync_playwright
from standin import PAGE

tmp = Path(tempfile.mkdtemp(prefix="fwd_"))
wa.LOGS, wa.OUTBOX, wa.SENT, wa.STATUS = tmp, tmp / "outbox.jsonl", tmp / "sent.jsonl", tmp / "status.json"
wa.BOOK, wa.REACTIONS, wa.BOOK_WAIT = tmp / "bitacora.json", tmp / "reactions.json", 3
iso = lambda ago=0: (datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat(timespec="seconds")
log = lambda i, day="2026-10-13", ago=0: {"created": iso(ago), "type": "log", "event": f"e{i}", "day": day, "source": "igc", "mag": 3.0,
                                          "text": f"{1 + i % 12}:{i % 60:02d} p. m. · M 3.{i % 10} · a {i} km al sur de Tonosí, Los Santos · sensor M 3.{i % 10}"}
alert = lambda i, ago=0: {"created": iso(ago), "type": "alert", "event": f"a{i}", "source": "igc", "mag": 4.5,
                          "text": f"SISMO M 4.5 número {i}\nA 12 km al sur de Tonosí\nFuente: https://sismosgeociencias.up.ac.pa/"}
write = lambda rows: wa.OUTBOX.open("a", encoding="utf-8").write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
write([log(90, "2026-10-12", 90000), log(91, "2026-10-12", 89000), alert(0, 600), log(1, ago=600), log(2, ago=500)])
plan = [(1.0, lambda: [alert(1)]), (1.5, lambda: [log(3)]), (5.0, lambda: [log(i) for i in range(4, 29)]), (10.5, lambda: [log(29)]), (45.0, lambda: [alert(2)])]
REFUSE, TAKE, END = 10.0, 40.0, 62.0


def feed(began):
    for at, rows in plan:
        time.sleep(max(0.0, began + at - time.time()))
        write(rows())


class Stop(Exception):
    pass


class Bell:
    """Stands in for the probe's request file, which the loop asks for at every turn: the test's clock."""
    def __init__(self, page, began):
        self.page, self.began, self.state = page, began, "takes"

    def exists(self):
        t = time.time() - self.began
        if t > END:
            raise Stop
        want = "refuses" if REFUSE <= t < TAKE else "takes"
        if want != self.state:
            self.state = want
            self.page.evaluate(f"window.garble = {'true' if want == 'refuses' else 'false'}")
        return False


def reloaded(*a):
    raise AssertionError("the page was loaded afresh")


wa.log_in = reloaded
checks, out = [], io.StringIO()


def check(name, good, shown=""):
    checks.append(bool(good))
    print(("PASS  " if good else "FAIL  ") + name + ("  | " + str(shown).encode("ascii", "backslashreplace").decode()[:190] if shown != "" else ""))


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page()
    page.set_content(PAGE)
    for i in range(8):
        page.evaluate("t => addMessage(t)", f"SISMO M 4.{i} mensaje viejo {i}")
    began = time.time()
    wa.PROBE = Bell(page, began)
    threading.Thread(target=feed, args=(began,), daemon=True).start()
    ended = "the loop returned"
    try:
        with contextlib.redirect_stdout(out):
            wa.forward(page, "Stand-in")
    except Stop:
        ended = "stopped by the test's clock"
    except Exception as e:
        ended = f"{type(e).__name__}: {str(e)[:200]}"
    said = out.getvalue().splitlines()
    about = [row for row in said if "log of " in row]
    text = page.evaluate("k => textOf(k)", "BITÁCORA") or ""
    total, logs, edits = page.evaluate("count()"), page.evaluate("k => having(k)", "BITÁCORA"), page.evaluate("window.edits")
    browser.close()
print("--- what the loop said ---")
for row in said:
    print("  ", row.encode("ascii", "backslashreplace").decode()[:160])
sent = [json.loads(x) for x in wa.SENT.read_text(encoding="utf-8").splitlines()]
book = json.loads(wa.BOOK.read_text(encoding="utf-8"))
secs = lambda row: datetime.strptime(row[:8], "%H:%M:%S")
check("0 the loop ran its minute", ended.startswith("stopped by the test"), ended)
check("1 no file of the log: the last day's lines only, in one new message", about and about[0].endswith("sent (2 of 2 lines shown)") and "2026-10-13" in about[0] and "a 90 km" not in text and "a 91 km" not in text and "a 1 km" in text, about[:1])
check("2 the alerts went out, the old one did not", [(s["event"], s["outcome"]) for s in sent if s["type"] == "alert"] == [("a1", "sent"), ("a2", "sent")], [(s["event"], s["outcome"]) for s in sent if s["type"] == "alert"])
check("3 a line that came within the wait went in with an edit, the 25 that came together with one edit", len(about) > 2 and about[1].endswith("edited (3 of 3 lines shown)") and about[2].endswith("edited (28 of 28 lines shown)"), about[1:3])
refused = [row for row in about if "not edited" in row]
gaps = [round((secs(b) - secs(a)).total_seconds()) for a, b in zip(refused, refused[1:])]
check("4 the page refusing edits for 30 s: three tries, further and further apart, not ten", len(refused) == 3 and gaps[0] >= 5 and gaps[1] >= 11, (len(refused), gaps))
check("5 then the line that waited went in", about[-1].endswith("edited (29 of 29 lines shown)") and "29 sismos" in text and book["shown"] == 29 and book["failed"] == 0, about[-1:])
check("6 one message for the log, eleven in all, three edits", (logs, total, edits) == (1, 11, 3), (logs, total, edits))
events = sorted(s["event"] for s in sent if s["type"] == "log")
check("7 every line in the sent log once", events == sorted(f"e{i}" for i in range(1, 30)), (len(events), len(set(events))))
made = {r["event"]: r["created"] for r in (json.loads(x) for x in wa.OUTBOX.read_text(encoding="utf-8").splitlines())}
waited = [(datetime.fromisoformat(s["at"]) - datetime.fromisoformat(made[s["event"]])).total_seconds() for s in sent if s["type"] == "alert"]
check("8 each alert was posted within two seconds of being written, the one that came while the log was failing too", len(waited) == 2 and max(waited) <= 2, waited)
print("ALL PASS" if all(checks) else "SOMETHING FAILED")
