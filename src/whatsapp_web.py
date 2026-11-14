"""Post the alert messages to a WhatsApp channel through WhatsApp Web, in a browser window of its own.

Usage:
    python src/whatsapp_web.py --test "hola"    # open the window, post one message to the channel, stop
    python src/whatsapp_web.py                  # stay open and post every new message of logs/outbox.jsonl
    (monitor.py --start runs the second form by itself when config.json names a channel; only one window can
    use the profile at a time, so stop that one before running --test)

Meta's official API cannot post to a channel, so this does what a person does: it opens the channel named
in config.json ("whatsapp_web": {"channel": "Test"}), puts the text into the box and presses Enter.
The channel is found by its name: after renaming it in WhatsApp, write the new name in config.json. The
window then waits, open, and finds it within 15 s; nothing has to be restarted.
The first time, the window shows WhatsApp's QR code: on the phone, WhatsApp > Linked devices > Link a
device. After that the window logs in by itself. It uses its own browser profile, whatsapp_profile/, which
is a logged-in session: keep that folder private. Leave the window open (minimised is fine) and do not
type or click in it while it runs.
WhatsApp's terms do not allow automated use, so the number can be restricted: a number kept for this
channel is the safe choice.
Alerts and sensor detections are posted; the "update" messages only with "send_updates": true.
Every two minutes it also notes, in logs/reactions.json, what the channel's newest messages show under them:
followers mark the earthquakes they felt with a reaction. It reads the page for that and clicks nothing.
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import Error as PageError
from playwright.sync_api import TimeoutError as Timeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
LOGS, PROFILE = HERE / "logs", HERE / "whatsapp_profile"
OUTBOX, SENT, STATUS = LOGS / "outbox.jsonl", LOGS / "sent.jsonl", LOGS / "status_whatsapp.json"
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8")) if (HERE / "config.json").exists() else {}
LOGIN_WAIT = 15 * 60               # seconds the window waits for the phone to link it
BOX = '#main footer div[contenteditable="true"]'             # the box an update is written in
POSTS = "#main [data-id]"                                    # every message of the channel shown
# What a part of the page says, without white space (the page breaks lines its own way) and with each emoji as its
# character: WhatsApp can draw an emoji as a small picture, which the page's own text would leave out.
PLAIN = r"""const plain = node => {
    const copy = node.cloneNode(true);
    copy.querySelectorAll('img[alt]').forEach(picture => picture.replaceWith(picture.alt));
    return copy.textContent.replace(/[\s\u200b\ufeff]+/g, '');
};"""
IN_BOX = "box => { " + PLAIN + " return plain(document.querySelector(box)); }"
# Posted: the box is empty again and the channel's last message holds every line of the text. Every line, because
# two reports in a row share whole sentences and one line alone would be found in the message before. (Every line
# the page draws: see drawn().)
POSTED = "([posts, box, lines]) => { " + PLAIN + """
    const all = document.querySelectorAll(posts);
    if (!all.length || plain(document.querySelector(box))) return false;
    const last = plain(all[all.length - 1]);
    return lines.every(line => last.includes(line));
}"""
# The whole text goes into the box at once, as a paste would put it. Typed key by key (2 to 4 s for an alert) a
# message was once cut off half-written and left in the box, and the next alert was typed into the middle of it.
PASTE = """([box, text]) => {
    const target = document.querySelector(box);
    target.focus();
    const data = new DataTransfer();
    data.setData('text/plain', text);
    target.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}));
}"""
FOCUS = "box => document.querySelector(box).focus()"
REACTIONS = LOGS / "reactions.json"                          # message id -> what it showed when last looked at
# What the channel's messages show, as WhatsApp writes it: the text says which message it is, and its end and the
# labels of its parts hold how many saw it and the reactions ("8•2:30PM ... ✅3", "Reactions ✅ 3 in total").
# Kept raw: reactions.py makes sense of it.
SEEN = "([posts, newest]) => { " + PLAIN + """
    return [...document.querySelectorAll(posts)].slice(-newest).map(node => {
        const labels = [...node.querySelectorAll('[aria-label]')].map(part => part.getAttribute('aria-label').slice(0, 120));
        return {id: node.getAttribute('data-id'), text: plain(node).slice(0, 1200), labels: labels.slice(0, 16)};
    });
}"""
# The list of messages moved to its top (WhatsApp then loads older ones) or back to its end. Returns how many it holds.
SCROLL = """async ([posts, where]) => {
    let list = document.querySelector(posts);
    while (list && !(list.scrollHeight > list.clientHeight + 50 && /auto|scroll/.test(getComputedStyle(list).overflowY))) list = list.parentElement;
    if (!list) return -1;
    if (where === 'bottom') list.scrollTop = list.scrollHeight;
    else if (where === 'up' && list.scrollTop >= 2) list.scrollTop = Math.max(0, list.scrollTop - 0.8 * list.clientHeight);
    else {                                        // 'top', or 'up' at the top already: older messages are asked for
        if (list.scrollTop < 2) {                 // a list that does not move asks for no older messages
            list.scrollTop = Math.min(80, list.scrollHeight - list.clientHeight);
            await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
        }
        list.scrollTop = 0;
    }
    return document.querySelectorAll(posts).length;
}"""
BACKFILL = LOGS / "reactions_backfill.request"               # while this file exists, the next start also reads the older messages
VIEWS = re.compile(r"(\d+)•\d{1,2}:\d{2}")              # "9•2:07PM" at the end of a message's text: nine accounts saw it

# ---- a development aid (2026-10-13), see probe(): steps tried on the open channel when a file asks for them ----
PROBE, PROBED = LOGS / "probe.request.json", LOGS / "probe.result.json"
# What WhatsApp draws over the page (menus, dialogs): never the list of chats, never the channel's own messages.
POPUPS = """[...document.querySelectorAll('[role="application"], [role="menu"], [role="dialog"], [role="alertdialog"], [role="listbox"]')]
    .filter(e => !e.closest('#side, #pane-side, #main') && !e.querySelector('#side, #pane-side, #main'))"""
OUTLINE = """const outline = root => {
    const out = [];
    const walk = el => {
        if (out.length >= 150) return;
        const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).filter(Boolean).join(' ').slice(0, 70);
        const item = {tag: el.tagName.toLowerCase()};
        for (const a of ['role', 'aria-label', 'data-icon', 'contenteditable', 'title', 'aria-placeholder', 'data-tab', 'type'])
            if (el.hasAttribute(a)) item[a] = el.getAttribute(a).slice(0, 80);
        if (own) item.text = own;
        if (Object.keys(item).length > 1) out.push(item);
        for (const child of el.children) walk(child);
    };
    if (root) walk(root);
    return out;
};"""
LOOK = "([what, id]) => { " + OUTLINE + """
    const away = e => e.closest('#side, #pane-side');
    if (what === 'row') return outline(document.querySelector(`#main [data-id="${id}"]`));
    if (what === 'popups') return """ + POPUPS + """.slice(-4).map(outline);
    if (what === 'editables') return [...document.querySelectorAll('[contenteditable="true"]')].filter(e => !away(e)).map(e => ({
        in_main: !!e.closest('#main'), in_footer: !!e.closest('footer'), label: e.getAttribute('aria-label'), placeholder: e.getAttribute('aria-placeholder'),
        text: e.textContent.slice(0, 90), length: e.textContent.length, focused: e === document.activeElement || e.contains(document.activeElement)}));
    if (what === 'controls') return [...document.querySelectorAll('[aria-label]')].filter(e => !away(e) && !e.closest('#main [data-id]')).slice(0, 90)
        .map(e => ({tag: e.tagName.toLowerCase(), role: e.getAttribute('role'), label: e.getAttribute('aria-label').slice(0, 70), in_main: !!e.closest('#main'), in_footer: !!e.closest('footer')}));
    return null;
}"""
TARGET = "([scope, id, label, text]) => { const popups = " + POPUPS + """;
    const roots = scope === 'row' ? [document.querySelector(`#main [data-id="${id}"]`)] : scope === 'footer' ? [document.querySelector('#main footer')]
                : scope === 'page' ? [document.body] : popups;
    for (const root of roots.filter(Boolean).reverse()) {
        const hit = [root, ...root.querySelectorAll('*')].filter(e => !e.closest('#side, #pane-side')
            && ((label && (e.getAttribute('aria-label') || '').trim() === label) || (text && !e.children.length && e.textContent.trim() === text)));
        if (hit.length) return hit[hit.length - 1];
    }
    return null;
}"""
EDITABLE = """which => {
    const boxes = [...document.querySelectorAll('[contenteditable="true"]')].filter(e => !e.closest('#side, #pane-side'));
    return (which === 'active' ? boxes.find(e => e === document.activeElement || e.contains(document.activeElement)) : boxes[which]) || null;
}"""
FIND = "([posts, key]) => { " + PLAIN + """
    const hit = [...document.querySelectorAll(posts)].filter(node => plain(node).includes(key));
    return hit.length ? hit[hit.length - 1].getAttribute('data-id') : null;
}"""
TEXT_OF = "id => { " + PLAIN + """ const node = document.querySelector(`#main [data-id="${id}"]`); return node ? plain(node) : null; }"""
PLAIN_OF = "node => { " + PLAIN + " return plain(node); }"
PASTE_IN = """(node, text) => {
    const data = new DataTransfer();
    data.setData('text/plain', text);
    node.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}));
}"""


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


said = []                          # the last state printed, so the heartbeat does not fill the console


def status(state, channel, **more):
    LOGS.mkdir(exist_ok=True)
    STATUS.write_text(json.dumps({"at": utc(), "state": state, "channel": channel, **more}), encoding="utf-8")
    line = " | ".join([state, *map(str, more.values())])
    if said != [line]:
        said[:] = [line]
        print(f"{utc()[11:19]} {line}", flush=True)


def log_in(page, channel):
    """Wait until WhatsApp Web shows the account: at once with a linked profile, after the QR code otherwise."""
    page.goto("https://web.whatsapp.com/", wait_until="domcontentloaded")
    try:
        page.wait_for_selector("#side, #pane-side", timeout=25_000)
    except Timeout:
        status("waiting for the phone: WhatsApp > Linked devices > Link a device, and scan the code in the window", channel)
        page.wait_for_selector("#side, #pane-side", timeout=LOGIN_WAIT * 1000)
    status("logged in", channel)


def is_open(page, channel):
    """Is the page showing this channel? Its header reads the name and, under it, how many followers it has."""
    if page.locator("#main header").count() == 0:
        return False
    head = [line.strip() for line in page.locator("#main header").inner_text().split("\n")]
    return head[0] == channel and any(re.search(r"follower|seguidor", line, re.I) for line in head[1:3])


class NoChannel(Exception):
    """The account's list of channels has none with the name asked for (renamed in WhatsApp, or mistyped in config.json)."""


def named():
    """The channel's name as config.json gives it now: it is read again each time, so a rename needs no restart."""
    try:
        return json.loads((HERE / "config.json").read_text(encoding="utf-8"))["whatsapp_web"]["channel"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def open_channel(page, channel):
    """Show the channel's own page, with the box to write an update in."""
    tab = page.locator('button[aria-label="Channels"], button[aria-label="Canales"], [role="button"][aria-label="Channels"], [role="button"][aria-label="Canales"]')
    tab.first.click(timeout=20_000)
    try:
        page.locator(f'span[title="{channel}"]').first.click(timeout=8_000)
    except Timeout:
        raise NoChannel(f"no channel named {channel!r} in this account's list of channels") from None
    page.wait_for_selector(BOX, timeout=20_000)
    page.wait_for_timeout(500)
    if not is_open(page, channel):
        raise RuntimeError(f"the page open is not the channel {channel!r}")       # never write an alert into some other chat


def squash(text):
    return re.sub(r"[\s\u200b\ufeff]+", "", text)


# WhatsApp draws a line that begins with "• ", "- ", "* " or "1. " as an item of a list, without that mark: a notice
# with three such lines went out on 2026-10-13 and was taken for not posted. A line is looked for by what follows.
MARK = re.compile(r"^\s*(?:[•*-]|\d+\.)\s+")


def drawn(text):
    """The lines of `text`, white space out, that the page is sure to draw, each without the mark of a list.

    WhatsApp Web shows a long message's beginning only, about 768 characters and then "Read more" (four messages of
    the channel were cut there, 2026-10-13). A text that long is known in the page by its first lines; to wait for
    its last ones would be to take a message that went out for one that did not."""
    lines = text.split("\n")
    if len(text) > 700:
        sure, used = [], 0
        for line in lines:
            used += len(line) + 1
            if used > 600:
                break
            sure.append(line)
        lines = sure or [lines[0][:300]]
    return [squash(MARK.sub("", line)) for line in lines if line.strip()]


def in_box(page):
    """What the box holds, with all white space taken out."""
    return page.evaluate(IN_BOX, BOX)


def empty(page):
    """Clear the box, whatever was left in it."""
    for _ in range(3):
        if not in_box(page):
            return True
        page.evaluate(FOCUS, BOX)
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(100)
    return not in_box(page)


def fill(page, text):
    """Put `text` in the emptied box. Says how ("paste" or "lines"), or "" if the box does not hold exactly that text."""
    wanted = squash(text)
    if empty(page):
        page.evaluate(PASTE, [BOX, text])
        page.wait_for_timeout(150)
        if in_box(page) == wanted:
            return "paste"
    if empty(page):                                          # the page did not take the paste: line by line, each line at once
        page.evaluate(FOCUS, BOX)
        for i, line in enumerate(text.split("\n")):
            if i:
                page.keyboard.press("Shift+Enter")          # a new line; Enter alone would send
            page.keyboard.insert_text(line)
        page.wait_for_timeout(150)
    return "lines" if in_box(page) == wanted else ""


def post(page, channel, text):
    """Write one message into the channel and send it. Returns the outcome and the seconds it took.

    Nothing is sent unless the box holds exactly the message: a half-written or mixed-up text is cleared, not posted."""
    started = time.time()
    if not is_open(page, channel):
        open_channel(page, channel)
    if not fill(page, text):
        empty(page)
        return "not sent: the box did not hold the text as written", round(time.time() - started, 1)
    lines = drawn(text)
    outcome = "written in the box, but it did not post"
    for wait in (6_000, 12_000):                             # Enter again if the first did nothing; on an empty box it does nothing
        page.evaluate(FOCUS, BOX)
        page.keyboard.press("Enter")
        try:
            page.wait_for_function(POSTED, arg=[POSTS, BOX, lines], timeout=wait)
            outcome = "sent"
            break
        except Timeout:
            pass
    if outcome != "sent":
        empty(page)                                          # so that it cannot end up inside the next message
    return outcome, round(time.time() - started, 1)


def rehearse(page):
    """Before saying "ready": can the box be filled and cleared? Nothing is sent, no Enter is pressed. Returns how it was filled."""
    how = fill(page, "ensayo de escritura ✅\nsegunda línea, sin enviar: https://maps.google.com/?q=7.6,-80.8")
    if not (how and empty(page)):
        raise RuntimeError("the box of the channel does not take text the way this program writes it: WhatsApp Web may have changed")
    return how


def reactions(page, known, newest=60):
    """Note what the channel's newest messages show under them now; `known` is the file's content, added to.

    A message keeps what it last showed after it has left the page, so the file ends up with every message's
    final count. Read only: nothing on the page is clicked or scrolled."""
    changed = False
    for row in page.evaluate(SEEN, [POSTS, newest]):
        new, old = {k: row[k] for k in ("text", "labels")}, known.get(row["id"]) or {}
        # WhatsApp at times redraws a message without its reactions, and they were lost from these notes (2026-10-12):
        # what it showed before is kept. The number that saw it only grows.
        if not any("eact" in label for label in new["labels"]):
            new["labels"] = new["labels"] + [label for label in old.get("labels", []) if "eact" in label]
        seen = VIEWS.findall(new["text"])
        new["views"] = max(int(seen[-1]) if seen else 0, old.get("views", 0))
        if row["id"] and row["text"] and any(old.get(k) != v for k, v in new.items()):
            known[row["id"]] = {"first": old.get("first", utc()), **new, "at": utc()}
            changed = True
    if changed:
        tmp = REACTIONS.with_suffix(".tmp")
        tmp.write_text(json.dumps(known, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, REACTIONS)


def backfill(page, known, seconds=40):
    """Once, when asked for: scroll the channel up so WhatsApp loads its older messages, note what each shows, and
    come back down. Done before posting starts, and kept short: a message written meanwhile waits for it."""
    until, held, still = time.time() + seconds, 0, 0
    while time.time() < until and still < 5:
        count = page.evaluate(SCROLL, [POSTS, "top"])
        if count < 0:
            break
        reactions(page, known, newest=5000)                  # as it goes: what scrolls far away may be dropped from the page
        still, held = (still + 1 if count == held else 0), count
        page.wait_for_timeout(800)
    page.evaluate(SCROLL, [POSTS, "bottom"])
    page.wait_for_timeout(600)
    return held


def settled(page, channel):
    """After a probe: back on the plain channel, at its end, nothing drawn over it and the box empty? Says so.

    Escape closes a menu, a dialog or an edit under way; it can also close the channel's page, which is then opened again."""
    for _ in range(2):
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
    if not is_open(page, channel):
        open_channel(page, channel)
    page.evaluate(SCROLL, [POSTS, "bottom"])
    page.wait_for_timeout(300)
    return page.evaluate("() => " + POPUPS + ".length") == 0 and empty(page) and is_open(page, channel)


def probe(page, channel):
    """A development aid (2026-10-13): try, on the open channel, the steps a file asks for, and note what the page shows.

    It is there to work out how a message of the channel is edited without restarting this window for every try.
    It looks at and clicks the channel's own page and what is drawn over it, never the list of chats. Returns
    whether the page is fit for posting again; the result goes to logs/probe.result.json."""
    steps = json.loads(PROBE.read_text(encoding="utf-8"))["steps"]
    PROBE.unlink()
    notes, row = [], None
    try:
        for step in steps:
            do, note = step["do"], None
            mine = page.locator(f'#main [data-id="{row}"]') if row else None
            if do == "find":
                note = row = page.evaluate(FIND, [POSTS, squash(step["text"])])
            elif do == "list":                               # the whole list to its top (older messages load) or its end
                note = page.evaluate(SCROLL, [POSTS, step.get("to", "top")])
            elif do == "scroll":
                mine.scroll_into_view_if_needed(timeout=4_000)
            elif do == "hover":
                mine.hover(timeout=4_000)
            elif do == "rightclick":
                part = mine.locator(step["on"]).first if step.get("on") else mine
                part.click(button="right", timeout=4_000, position=step.get("position"))
            elif do == "click":
                target = page.evaluate_handle(TARGET, [step.get("scope", "popup"), row, step.get("label"), step.get("text")]).as_element()
                note = bool(target)
                if target:
                    target.click(timeout=4_000)
            elif do == "press":
                page.keyboard.press(step["key"])
            elif do == "wait":
                page.wait_for_timeout(step["ms"])
            elif do == "look":
                note = page.evaluate(LOOK, [step["what"], row])
            elif do == "write":
                box = page.evaluate_handle(EDITABLE, step.get("which", "active")).as_element()
                if box:
                    box.focus()
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                    page.wait_for_timeout(120)
                    box.evaluate(PASTE_IN, step["text"])
                    page.wait_for_timeout(300)
                    note = {"holds_the_text": box.evaluate(PLAIN_OF) == squash(step["text"]), "holds": box.evaluate(PLAIN_OF)[:60]}
            elif do == "read":
                note = page.evaluate(TEXT_OF, row)
            notes.append({"do": do, "note": note})
    except Exception as e:
        notes.append({"error": f"{type(e).__name__}: {str(e)[:300]}"})
    try:
        fit = settled(page, channel)
    except Exception as e:
        fit = False
        notes.append({"error_settling": f"{type(e).__name__}: {str(e)[:300]}"})
    notes.append({"fit_for_posting": fit})
    PROBED.write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{utc()[11:19]} probe: {len(steps)} steps, fit for posting again: {fit}", flush=True)
    return fit


# ---- editing a message, and the day's log of small earthquakes (2026-10-13) ----
# How WhatsApp Web edits a channel's message, found with probe(): a right click on it opens a menu of buttons with
# role "menuitem"; "Edit" opens a dialog with a text box holding the message's text and a button that sends it.
EDIT_ITEM = '[role="menuitem"][aria-label="Edit"], [role="menuitem"][aria-label="Editar"]'
EDIT_BOX = '[role="dialog"] [role="textbox"][contenteditable="true"]'
EDIT_SEND = '[role="dialog"] [aria-label="Send edited message"], [role="dialog"] [aria-label="Enviar mensaje editado"]'
# Edited: the dialog is gone and that message holds every line of the new text that the page draws (see drawn()).
EDITED = "([id, lines]) => { " + PLAIN + """
    const node = document.querySelector(`#main [data-id="${id}"]`);
    if (!node || document.querySelector('[role="dialog"] [role="textbox"]')) return false;
    const shows = plain(node);
    return lines.every(line => shows.includes(line));
}"""
BOOK = LOGS / "bitacora.json"                                # the day's log: its lines, and how many of them the channel shows
BOOK_WAIT = 60                                               # seconds between two tries to bring the log's message up to date
SOURCE = "https://sismosgeociencias.up.ac.pa/"


def clear(page, channel):
    """Close what is drawn over the channel (a menu, the edit dialog), go to the list's end, and say whether the page
    is fit for posting: the channel open, nothing over it, the box empty.

    Escape is pressed only while something is open: with nothing open it closes the channel's own page."""
    for _ in range(3):
        if page.evaluate("() => " + POPUPS + ".length") == 0:
            break
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    page.evaluate(SCROLL, [POSTS, "bottom"])
    page.wait_for_timeout(200)
    return page.evaluate("() => " + POPUPS + ".length") == 0 and is_open(page, channel) and empty(page)


def fresh(page, channel):
    """Show the channel as when it is opened: at its newest messages. Escape, with nothing drawn over the page, closes
    the channel's page, and opening it again starts the list anew; if Escape did not close it, the list is moved
    to its end instead."""
    if page.evaluate("() => " + POPUPS + ".length") == 0:
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
    if is_open(page, channel):
        page.evaluate(SCROLL, [POSTS, "bottom"])
    else:
        open_channel(page, channel)
    page.wait_for_timeout(600)


def edit(page, channel, key, text, urgent=lambda: False, patience=25):
    """Replace the text of the channel's newest message that holds `key`. Returns the outcome and whether the page is
    fit for posting afterwards.

    An edit rings no follower's phone, which is the point of it. WhatsApp keeps only part of the messages loaded,
    so the list is moved up until the message is there, for `patience` seconds at most; `urgent()` is asked on the
    way, and an alert that has come meanwhile goes first ("postponed"). Nothing is sent unless the edit box holds
    exactly the new text. A list the search has moved is put back at the newest messages afterwards."""
    if not is_open(page, channel):
        open_channel(page, channel)
    started, outcome, step, moved, row = time.time(), "", "finding the message", 0, None    # moved: 1 opened afresh, 2 scrolled
    try:
        while True:
            if urgent():
                outcome = "postponed: an alert came first"
                break
            row = page.evaluate(FIND, [POSTS, squash(key)])
            if row:
                break
            if not moved:
                # The page's list can be left showing old messages and not the newest: on 2026-10-14 a search found
                # it on messages of two days before, its jumps to the top took it further back for 25 s, and the
                # log's message, twelve back from the newest, was never among them. So the search starts from the
                # channel opened afresh, at its newest messages, and goes up a screen at a time.
                step, moved = "opening the channel afresh", 1
                fresh(page, channel)
                step = "finding the message"
                continue
            if time.time() - started > patience:
                outcome = "not found among the messages the page can load"
                break
            page.evaluate(SCROLL, [POSTS, "up"])
            moved = 2
            page.wait_for_timeout(700)
        if row:
            outcome = change(page, row, text, step)
    except PageError as e:
        outcome = failed(e, step)
    fit = clear(page, channel)
    if moved > 1 and fit:                                    # the list was moved up: back to the newest messages
        try:
            fresh(page, channel)
            fit = clear(page, channel)
        except PageError:
            fit = False
    return outcome, fit


def failed(error, step):
    """A refusal of the page as an outcome, with the step it happened at and what was in the way if the page said:
    on 2026-10-13 a try ended with "Locator.click: Timeout" and nothing told which of the three clicks it was."""
    said = str(error).splitlines()
    way = next((line.strip(" -") for line in said if "intercepts pointer events" in line), "")
    return f"not edited, at {step}: {said[0][:80]}" + (f" ({way[:110]})" if way else "")


def change(page, row, text, step):
    """The edit itself, on the message `row` the page holds: menu, dialog, new text, send. Returns the outcome."""
    try:
        mine = page.locator(f'#main [data-id="{row}"]')
        step = "bringing the message into view"
        mine.scroll_into_view_if_needed(timeout=4_000)
        step = "the right click on the message"
        mine.click(button="right", timeout=4_000, position={"x": 150, "y": 70})
        step = 'the menu\'s "Edit"'
        page.locator(EDIT_ITEM).first.click(timeout=4_000)
        step = "the edit box"
        box = page.locator(EDIT_BOX).first
        box.wait_for(timeout=6_000)
        box.focus()
        page.keyboard.press("Control+A")
        page.keyboard.press("Backspace")
        page.wait_for_timeout(150)
        box.evaluate(PASTE_IN, text)
        page.wait_for_timeout(300)
        if box.evaluate(PLAIN_OF) != squash(text):
            return "not edited: the edit box did not hold the text as written"
        step = "the button that sends the edit"
        page.locator(EDIT_SEND).first.click(timeout=4_000)
        step = "waiting for the message to show the new text"
        page.wait_for_function(EDITED, arg=[row, drawn(text)], timeout=12_000)
        return "edited"
    except PageError as e:                                   # a wait that ran out, or any other refusal of the page
        return failed(e, step)


def book_load():
    try:
        return json.loads(BOOK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def book_save(book):
    """Write the log's state for the next start. Says whether it was written.

    Windows refuses to replace a file that some other program has open at that instant (a virus scanner just after
    the last save, the monitor, a person looking at it): in a test of 2026-10-13 that ended this program with
    "Access is denied". So it is tried a few times, and if it still cannot be written the state stays in memory and
    goes out with the next save: this file is for the next start, and must never stop the posting."""
    tmp = BOOK.with_suffix(".tmp")
    for wait in (0, 0.05, 0.1, 0.2, 0.4):
        time.sleep(wait)
        try:
            tmp.write_text(json.dumps(book, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, BOOK)
            return True
        except OSError:
            continue
    print(f"{utc()[11:19]} the log's file could not be written; its state is kept and written with the next change", flush=True)
    return False


def book_add(book, record, live=True):
    """Take a small earthquake's line into the day's log; the first line of a new day starts a new log. Says whether it was new.

    Around midnight a line can belong to the day before: one that comes after the new day's log has begun, or one
    the channel had not shown yet when it began. Such a line goes into the new day's log marked "ayer", so that none
    is dropped without a word. `live` is false for the outbox read again at a start, where the days before are closed."""
    line, day = record["text"], record.get("day", "")
    if day < book.get("day", ""):
        if not live:
            return False
        line = "ayer " + line
    elif book.get("day") != day:
        late = [{"event": entry["event"], "line": "ayer " + entry["line"]} for entry in book.get("entries", [])[book.get("shown", 0):]
                if not entry["line"].startswith("ayer ")]
        book.clear()
        book.update(day=day, entries=late, shown=0, start=0, part=1, posted=False, misses=0, posts=0, failed=0, tried=0)
    if any(entry["event"] == record["event"] for entry in book["entries"]):
        return False
    book["entries"].append({"event": record["event"], "line": line})
    book_save(book)
    return True


def book_title(book):
    """The words that name the day's log message; it is found again by them when it has to be edited."""
    day = datetime.strptime(book["day"], "%Y-%m-%d")
    return f"BITÁCORA DE SISMOS MENORES · {day:%d/%m/%Y}" + (f" · parte {book['part']}" if book["part"] > 1 else "")


def book_text(book):
    """The day's log as the channel shows it: the newest line first, in sight without opening the message."""
    try:
        below = f"de menos de M {float(json.loads((HERE / 'config.json').read_text(encoding='utf-8'))['log_below']):.1f} "
    except (OSError, ValueError, KeyError, TypeError):
        below = ""
    mine = book["entries"][book["start"]:]
    hour = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%I:%M %p").lstrip("0").replace("AM", "a. m.").replace("PM", "p. m.")
    return "\n".join(["📒 " + book_title(book),
                      f"Sismos {below}reportados por el IGC. Este mensaje se actualiza solo y no envía notificación. Los más recientes van arriba.",
                      "", *[entry["line"] for entry in reversed(mine)], "",
                      f"{len(mine)} sismo{'' if len(mine) == 1 else 's'} · actualizado a las {hour} (hora de Panamá)", f"Fuente: {SOURCE}"])


def book_sync(page, channel, book, urgent=lambda: False):
    """Bring the channel's log message up to the day's lines: post it if the day has none yet, else edit it.
    Returns the outcome and whether the page is fit for posting."""
    started, text, fit = time.time(), book_text(book), True
    book["tried"] = started
    if not is_open(page, channel):
        open_channel(page, channel)
    # A new message rings every follower's phone, so the log is never posted twice. If the page shows it already (a
    # post that was not seen to go out), it is edited; and four new messages in a day is the most, whatever the fault.
    if not book["posted"] and page.evaluate(FIND, [POSTS, squash(book_title(book))]):
        book["posted"] = True
    if book["posted"]:
        (outcome, fit), how = edit(page, channel, book_title(book), text, urgent), "edited"
    elif book.get("posts", 0) >= 4:
        outcome, how = "not posted: four log messages today already", ""
    else:
        outcome, how = post(page, channel, text)[0], "new message"
        if not outcome.startswith("not sent"):               # Enter was pressed: it counts, seen to go out or not
            book["posts"] = book.get("posts", 0) + 1
    if outcome in ("sent", "edited"):
        fresh = book["entries"][book["shown"]:]
        book.update(posted=True, shown=len(book["entries"]), misses=0, failed=0)
        with SENT.open("a", encoding="utf-8") as f:
            for entry in fresh:
                f.write(json.dumps({"at": utc(), "to": "whatsapp", "event": entry["event"], "type": "log",
                                    "outcome": f"in the day's log ({how})", "seconds": round(time.time() - started, 1)}) + "\n")
    elif not outcome.startswith("postponed"):
        book["failed"] = book.get("failed", 0) + 1           # the wait before the next try doubles, see forward()
        if outcome.startswith("not found"):
            book["misses"] = book.get("misses", 0) + 1
            if book["misses"] >= 3:                          # out of reach for good: the lines not yet shown go on in a new message
                book.update(posted=False, part=book["part"] + 1, start=book["shown"], misses=0, failed=0)
    book_save(book)
    return outcome, fit


def new_records(offset):
    """Records appended to the outbox since `offset` (bytes), and the new offset. A replaced file is read from its start."""
    if not OUTBOX.exists():
        return [], 0
    size = OUTBOX.stat().st_size
    if size < offset:
        offset = 0
    with OUTBOX.open("rb") as f:
        f.seek(offset)
        chunk = f.read()
    whole = chunk[:chunk.rfind(b"\n") + 1]                   # a line still being written waits for the next look
    return [json.loads(line) for line in whole.decode("utf-8").splitlines() if line.strip()], offset + len(whole)


def forward(page, channel):
    """Post every new message of the outbox to the channel, until the channel cannot be found any more."""
    # Start from what is in the outbox now, but not blind: a message written in the last minute and a half
    # while this window was down or waiting is still news, unless it was posted.
    records, offset = new_records(0)
    posted = {(s["event"], s["type"]) for s in (json.loads(line) for line in SENT.read_text(encoding="utf-8").splitlines())} if SENT.exists() else set()
    age = lambda r: (datetime.now(timezone.utc) - datetime.fromisoformat(r["created"]).replace(tzinfo=timezone.utc)).total_seconds()
    waiting = [r for r in records[-20:] if age(r) <= 90 and (r["event"], r["type"]) not in posted]
    beat = time.time()
    try:
        known = json.loads(REACTIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    looked = 0.0
    # The day's log of small earthquakes: its lines are taken from the outbox again at every start, so none is lost
    # while this window is down; how many the channel shows is in the log's own file. Without that file nothing
    # tells what was shown: then the last day's lines only, not every day's.
    book = book_load()
    lines = [r for r in records if r["type"] == "log"]
    for record in lines if book else [r for r in lines if r["day"] == lines[-1]["day"]]:
        book_add(book, record, live=False)
    urgent = lambda: any(r["type"] in ("alert", "detection") for r in new_records(offset)[0])
    while True:
        records, offset = (waiting, offset) if waiting else new_records(offset)
        waiting = []
        for record in sorted(records, key=lambda r: r["type"] == "log"):       # a line for the log can wait, an alert cannot
            if record["type"] == "update" and not CONFIG.get("send_updates"):
                continue
            if record["type"] == "log":
                book_add(book, record)
                continue
            try:
                outcome, seconds = post(page, channel, record["text"])
            except NoChannel:
                outcome, seconds = "not sent: the channel was not found under its name", 0
            with SENT.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": utc(), "to": "whatsapp", "event": record["event"], "type": record["type"], "outcome": outcome, "seconds": seconds}) + "\n")
            if outcome.startswith("not sent: the channel"):
                return
            status("ready", channel, last=f"{record['type']} {outcome} in {seconds} s")
            beat = time.time()
        if time.time() - beat > 30:                  # a heartbeat for monitor.html, and a check that the page is still the account's and the channel's
            page.wait_for_selector("#side, #pane-side", timeout=10_000)
            if not is_open(page, channel):
                try:
                    open_channel(page, channel)
                except NoChannel:
                    return
            status("ready", channel)
            beat = time.time()
        if time.time() - looked > 120:               # the reactions: never allowed to get in the way of posting
            looked = time.time()
            try:
                reactions(page, known)
            except Exception as e:
                print(f"{utc()[11:19]} reactions not read: {type(e).__name__}: {str(e)[:150]}", flush=True)
        # At most once a minute: several small earthquakes go in with one edit. After a try that failed the wait
        # doubles, up to a quarter of an hour, and it is kept in the log's file: a fault cannot turn into a try a minute.
        if book.get("entries") and book["shown"] < len(book["entries"]) and time.time() - book.get("tried", 0) > BOOK_WAIT * min(2 ** book.get("failed", 0), 15):
            try:
                outcome, fit = book_sync(page, channel, book, urgent)
            except NoChannel:
                return
            print(f"{utc()[11:19]} log of {book['day']}: {outcome} ({book['shown']} of {len(book['entries'])} lines shown)", flush=True)
            status("ready", channel, last=f"log {outcome}")
            beat = time.time()
            if not fit:                              # as after a probe that left the page in doubt
                log_in(page, channel)
                return
        if PROBE.exists() and not probe(page, channel):
            # The page was left in a state this program cannot vouch for: load it afresh, and let main() open the
            # channel and try the box again before anything is posted.
            log_in(page, channel)
            return
        time.sleep(0.3)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--test", metavar="TEXT", help="post this one message and stop")
    ap.add_argument("--channel", help="channel name (default: the one in config.json, read again whenever the channel has to be found)")
    args = ap.parse_args()
    channel = args.channel or named()
    if not channel:
        sys.exit('no channel: put "whatsapp_web": {"channel": "<its name>"} in config.json or pass --channel')
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(str(PROFILE), channel="msedge", headless=False, locale="en-US", no_viewport=True,
                                                       args=["--window-size=1100,820"])
        page = browser.pages[0] if browser.pages else browser.new_page()
        try:
            log_in(page, channel)
            if args.test:
                open_channel(page, channel)
                rehearse(page)
                outcome, seconds = post(page, channel, args.test)
                status(f"test message {outcome} in {seconds} s", channel)
                time.sleep(3)
                return
            while True:
                # The channel is found by its name. When it is not in the list (renamed in WhatsApp, say) the window stays
                # open and looks again every 15 s, under the name config.json gives by then: stopping would only make
                # monitor.py open a new window every 30 s.
                channel = args.channel or named() or channel
                try:
                    open_channel(page, channel)
                except NoChannel as e:
                    status(f"waiting: {e}. Put the channel's present name in config.json", channel)
                    time.sleep(15)
                    continue
                print(f"{utc()[11:19]} the box takes text by {rehearse(page)}", flush=True)
                if BACKFILL.exists():
                    BACKFILL.unlink()                        # first, so that a failure here cannot repeat at every start
                    try:
                        known = json.loads(REACTIONS.read_text(encoding="utf-8")) if REACTIONS.exists() else {}
                        print(f"{utc()[11:19]} older messages read: the page held {backfill(page, known)}, {len(known)} noted", flush=True)
                    except Exception as e:
                        print(f"{utc()[11:19]} older messages not read: {type(e).__name__}: {str(e)[:150]}", flush=True)
                status("ready", channel)
                forward(page, channel)
        except Exception as e:
            status(f"stopped: {type(e).__name__}: {str(e)[:200]}", channel)
            if isinstance(e, RuntimeError):
                time.sleep(600)                              # a page this program cannot use will not be better in 30 s: leave the window up
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
