"""Check the day's log on a stand-in channel (see standin.py): first post, edits of a message that has scrolled out of
the page, an alert taking precedence, a message that cannot be found any more, a new day, the lines around midnight,
a log longer than the page draws, a post that was not seen to go out, the most new messages in a day.
Nothing to do with WhatsApp: it opens a page of its own in a browser without a window, and writes to a temporary folder.

    .venv\\Scripts\\python.exe src\\tests\\book_test.py        (about a minute; ends with ALL PASS)
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]
import whatsapp_web as wa
from playwright.sync_api import sync_playwright
from standin import PAGE

tmp = Path(tempfile.mkdtemp(prefix="book_"))
wa.BOOK, wa.SENT = tmp / "bitacora.json", tmp / "sent.jsonl"
line = lambda i, day="2026-10-13": {"type": "log", "event": f"e{i}", "day": day,
                                    "text": f"{1 + i % 12}:{i % 60:02d} a. m. · M 3.{i % 10} · a {i} km al sur de Tonosí, Los Santos · sensor M 3.{i % 10}"}
checks = []


def check(name, good, shown=""):
    checks.append(bool(good))
    print(("PASS  " if good else "FAIL  ") + name + ("  | " + str(shown).encode("ascii", "backslashreplace").decode()[:170] if shown != "" else ""))


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    page = browser.new_page()
    page.set_content(PAGE)
    has = lambda key: page.evaluate("k => textOf(k)", key) or ""
    for i in range(8):
        page.evaluate("t => addMessage(t)", f"SISMO M 4.{i} mensaje viejo {i}")
    book = {}
    wa.book_add(book, line(1))
    again = wa.book_add(book, line(1))
    wa.book_add(book, line(2))
    out = wa.book_sync(page, "Stand-in", book)
    text = has("BITÁCORA")
    check("1 first lines of the day: posted as a new message", out == ("sent", True) and "2:01 a. m." in text and "3:02 a. m." in text and not again, out)
    check("  newest line first, title and source in it", text.index("3:02 a. m.") < text.index("2:01 a. m.") and text.startswith("📒 BITÁCORA DE SISMOS MENORES · 13/10/2026") and wa.SOURCE in text, text.split(chr(10))[0])
    for i in range(14):
        page.evaluate("t => addMessage(t)", f"POSIBLE SISMO alerta {i}")
    loaded = page.evaluate(wa.FIND, [wa.POSTS, wa.squash(wa.book_title(book))])
    wa.book_add(book, line(3))
    out = wa.book_sync(page, "Stand-in", book)
    text = has("BITÁCORA")
    bottom = page.evaluate("Math.abs(list.scrollHeight - list.clientHeight - list.scrollTop) < 5")
    check("2 a third line, the message 14 messages back and not drawn: edited in place", out == ("edited", True) and loaded is None and "4:03 a. m." in text and text.endswith("[Edited]"), out)
    check("  nothing new was posted, the list is back at its end and at the newest messages, the box is empty",
          page.evaluate("count()") == 23 and bottom and page.evaluate("atNewest()") and page.locator("footer div").inner_text() == "")
    wa.book_add(book, line(4))
    out = wa.book_sync(page, "Stand-in", book, urgent=lambda: True)
    check("3 an alert waiting: the edit steps aside, and that is not counted as a failure", out[0].startswith("postponed") and out[1] and book["shown"] == 3 and "5:04 a. m." not in has("BITÁCORA") and book["failed"] == 0, out)
    sent = wa.post(page, "Stand-in", "POSIBLE SISMO de prueba\nsegunda línea")
    check("  and the alert is posted as always", sent[0] == "sent", sent)
    out = wa.book_sync(page, "Stand-in", book)
    check("4 then the edit goes through with the line that waited", out == ("edited", True) and "5:04 a. m." in has("BITÁCORA"), out)
    real = wa.edit
    wa.edit = lambda *a, **k: real(*a, **{**k, "patience": 2})
    page.evaluate("k => dropMessage(k)", "BITÁCORA")
    wa.book_add(book, line(5))
    outs, fails = [], []
    for _ in range(3):
        outs.append(wa.book_sync(page, "Stand-in", book)[0][:9])
        fails.append(book["failed"])
    out = wa.book_sync(page, "Stand-in", book)
    text = has("parte 2")
    check("5 the message gone: after three tries the new line goes on in a second message", outs == ["not found"] * 3 and out == ("sent", True) and "6:05 a. m." in text and "5:04 a. m." not in text, (outs, out))
    check("  each try that failed was counted, and the count starts again with the new message", fails == [1, 2, 0] and book["failed"] == 0 and book["posts"] == 2, (fails, book["posts"]))
    wa.edit = real

    # ---- around midnight ----
    wa.book_add(book, line(6, "2026-10-14"))
    late = wa.book_add(book, line(7, "2026-10-13"))
    closed = wa.book_add(book, line(8, "2026-10-13"), live=False)
    out = wa.book_sync(page, "Stand-in", book)
    text = has("14/10/2026")
    check("6 a new day starts a new log", out == ("sent", True) and "7:06 a. m." in text and "6:05" not in text, out)
    check("  a line of the day before that comes late goes into it, marked; read again at a start it is left out", late and not closed and "\nayer 8:07 a. m." in text and "9:08" not in text, text.split(chr(10))[3:5])
    wa.book_add(book, line(9, "2026-10-14"))                      # not shown yet when the next day begins
    wa.book_add(book, line(10, "2026-10-15"))
    out = wa.book_sync(page, "Stand-in", book)
    text = has("15/10/2026")
    check("7 a line the channel had not shown when the day changed is carried over, marked", out == ("sent", True) and "\nayer 10:09 a. m." in text and "11:10 a. m." in text and text.index("11:10 a. m.") < text.index("ayer 10:09") and "ayer ayer" not in text and "8:07" not in text, text.split(chr(10))[3:5])

    # ---- a log longer than the page draws ----
    for i in range(11, 41):
        wa.book_add(book, line(i, "2026-10-15"))
    full = wa.book_text(book)
    before, edits = page.evaluate("count()"), page.evaluate("window.edits")
    out = wa.book_sync(page, "Stand-in", book)
    text, on_page = has("15/10/2026"), page.locator("#main [data-id]").last.inner_text()
    check("8 thirty more lines: the page cuts the message, and the edit is still seen to go through", out == ("edited", True) and len(full) > 2000 and "Read more" in on_page and "5:40 a. m." in text and "32 sismos" in text
          and book["shown"] == 32 and page.evaluate("count()") == before and page.evaluate("window.edits") == edits + 1, (out, len(full), book["shown"]))
    check("  the lines the page is asked for are its first ones only", len(wa.drawn(full)) < 12 and wa.drawn(full)[0].startswith("📒BITÁCORA") and len(wa.drawn("uno\ndos\n\ntres")) == 3, len(wa.drawn(full)))
    book2 = {}
    for i in range(41, 71):
        wa.book_add(book2, line(i, "2026-10-16"))
    before = page.evaluate("count()")
    out = wa.book_sync(page, "Stand-in", book2)
    check("9 a long log posted as a new message: seen to go out, once", out == ("sent", True) and page.evaluate("count()") == before + 1 and book2["shown"] == 30 and book2["posts"] == 1, (out, book2["posts"]))

    # ---- never two messages for one log ----
    book3 = {}
    wa.book_add(book3, line(71, "2026-10-17"))
    real_post = wa.post                                           # the message does go out; this program is told it did not
    wa.post = lambda page, channel, text: (real_post(page, channel, text) and None) or ("written in the box, but it did not post", 18.0)
    out = wa.book_sync(page, "Stand-in", book3)
    wa.post = real_post
    state = (book3["posted"], book3["posts"], book3["failed"], book3["shown"])
    wa.book_add(book3, line(72, "2026-10-17"))
    out2 = wa.book_sync(page, "Stand-in", book3)
    text = has("17/10/2026")
    check("10 a post not seen to go out: the next try finds the message and edits it, no second message", out[0].startswith("written") and state == (False, 1, 1, 0) and out2 == ("edited", True)
          and page.evaluate("k => having(k)", "17/10/2026") == 1 and "1:12 a. m." in text and "12:11 a. m." in text and book3["shown"] == 2 and book3["failed"] == 0, (out, state, out2))
    book4 = {}
    wa.book_add(book4, line(73, "2026-10-18"))
    book4["posts"] = 4
    before = page.evaluate("count()")
    out = wa.book_sync(page, "Stand-in", book4)
    check("11 four new messages in a day already: no fifth", out[0].startswith("not posted") and page.evaluate("count()") == before and book4["failed"] == 1, out)
    page.evaluate("window.deaf = true")
    wa.book_add(book3, line(74, "2026-10-17"))
    out = wa.book_sync(page, "Stand-in", book3)
    page.evaluate("window.deaf = false")
    fit = wa.clear(page, "Stand-in")
    check("12 an edit the page does not take: said so, counted, the page left fit for posting", out[0].startswith("not edited") and out[1] and fit and book3["failed"] == 1 and book3["shown"] == 2, out)
    # ---- the list left showing old messages, as WhatsApp's was on 2026-10-14 ----
    book5 = {}
    wa.book_add(book5, line(75, "2026-10-19"))
    first = wa.book_sync(page, "Stand-in", book5)
    for i in range(3):
        page.evaluate("t => addMessage(t)", f"SISMO M 4.{i} alerta posterior {i}")
    page.evaluate("drift()")
    page.wait_for_timeout(300)
    hidden = page.evaluate(wa.FIND, [wa.POSTS, wa.squash(wa.book_title(book5))])
    before = page.evaluate("window.reopened")
    wa.book_add(book5, line(76, "2026-10-19"))
    out = wa.book_sync(page, "Stand-in", book5)
    check("13 the list showing the oldest messages: the channel is opened afresh and the message edited",
          first == ("sent", True) and hidden is None and out == ("edited", True) and page.evaluate("window.reopened") == before + 1
          and "5:16 a. m." in has("19/10/2026") and page.evaluate("atNewest()"), (first, out, hidden))
    check("14 Delete was never clicked, the list of chats never read", not page.evaluate("window.deleted === true"))
    browser.close()
logged = [json.loads(x) for x in wa.SENT.read_text(encoding="utf-8").splitlines()]
events = sorted(s["event"] for s in logged)
check("15 every line shown is in the sent log, once", events == sorted(f"e{i}" for i in list(range(1, 8)) + list(range(9, 73)) + [75, 76]) and all(s["type"] == "log" for s in logged), (len(events), len(set(events))))
marked = wa.drawn("📢 AVISO\n• uno\n- dos\n* tres\n1. cuatro\n2:04 p. m. · M 3.3 · a 39 km\n1 sismo · actualizado")
check("16 a line with the mark of a list is looked for without it; an hour or a count is not such a mark",
      marked == ["📢AVISO", "uno", "dos", "tres", "cuatro", "2:04p.m.·M3.3·a39km", "1sismo·actualizado"], marked)
waits = [wa.BOOK_WAIT * min(2 ** failed, 15) for failed in range(7)]
check("17 the wait between tries: a minute, doubling after each failure, a quarter of an hour at most", waits == [60, 120, 240, 480, 900, 900, 900], waits)
print("ALL PASS" if all(checks) else "SOMETHING FAILED")
