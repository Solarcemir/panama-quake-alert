"""What the channel's followers did with each message: how many saw it and how many reacted, by kind and size.

Usage:
    python src/reactions.py

whatsapp_web.py notes in logs/reactions.json what WhatsApp Web shows under each message of the channel, as
written there: "...con ✅9•2:07PM..." (9 saw it) and a label such as "Reactions ✅ 3 in total. View reactions"
or "reaction 👍. View reactions". This reads those notes, finds each message in logs/outbox.jsonl by its
text, and counts. Followers are asked to react with a tick to what they felt, so the share of messages with a
reaction, and reactions per view, are a floor for "felt": not everyone who feels an earthquake reacts.
Only the reactions' total and the emoji WhatsApp shows first are known: the split by emoji is behind a click
this project does not make.
"""
import json
import re
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
LOGS = HERE / "logs"
SEEN = re.compile(r"(\d+(?:[.,]\d+)?)([KkMm]?)•\d{1,2}:\d{2}")        # "9•2:07PM": how many saw it, then when it was posted
TOTAL = re.compile(r"Reac\w+ (.*?) (\d+) in total", re.I)                  # "Reactions ✅ 3 in total. View reactions"
ONE = re.compile(r"reac\w+ (.*?)\. View", re.I)                            # "reaction 👍. View reactions": a single one
TICK = "✅"


def squash(text):
    return re.sub(r"[\s​﻿]+", "", text)


def shown(note):
    """(views, reactions, the emoji shown) of one message as noted; views is None if WhatsApp wrote none."""
    views = None
    found = SEEN.findall(note["text"])
    if found:
        number, unit = found[-1]
        views = round(float(number.replace(",", ".")) * {"": 1, "k": 1e3, "m": 1e6}[unit.lower()])
    if note.get("views"):                                                    # the most it ever showed, kept by whatsapp_web.py
        views = max(views or 0, note["views"])
    count, emoji = 0, ""
    for label in note["labels"]:
        many, one = TOTAL.search(label), ONE.search(label)
        if many:
            count, emoji = int(many.group(2)), many.group(1)
        elif one:
            count, emoji = 1, one.group(1)
    return views, count, emoji


def messages():
    """Every noted message that is in the outbox: its record there, and what it showed."""
    notes = json.loads((LOGS / "reactions.json").read_text(encoding="utf-8"))
    notes = [n for n in notes.values() if n.get("text")]
    out = []
    for line in (LOGS / "outbox.jsonl").open(encoding="utf-8"):
        record = json.loads(line)
        if record["type"] not in ("alert", "detection"):
            continue
        # the first two lines name the message: what it is, and the earthquake's or the shaking's time
        keys = [squash(part) for part in record["text"].split("\n")[:2]]
        note = next((n for n in notes if all(key in n["text"] for key in keys)), None)
        if note:
            views, count, emoji = shown(note)
            kind = "update" if record["type"] == "detection" and not record["text"].startswith("POSIBLE") else record["type"]
            size = record.get("mag")
            if kind == "detection":                                          # the alert's own reading: "cerca de M 4.5" until 2026-10-14, then a field
                read = re.search(r"cerca de M (\d\.\d)", record["text"])
                size = record.get("reads") or (float(read.group(1)) if read else None)
            out.append({"kind": kind, "created": record["created"][:19], "size": size, "views": views, "reactions": count, "emoji": emoji})
    return out, len(notes)


def main():
    rows, noted = messages()
    print(f"{noted} messages noted from the channel, {len(rows)} of them found in the outbox "
          f"({sum(r['kind'] == 'detection' for r in rows)} sensor alerts, {sum(r['kind'] == 'alert' for r in rows)} agency reports)")
    groups = [("sensor alerts", lambda r: r["kind"] == "detection"), ("agency reports, all", lambda r: r["kind"] == "alert")]
    groups += [(f"agency reports, M{lo} to {hi}", lambda r, lo=lo, hi=hi: r["kind"] == "alert" and r["size"] is not None and lo <= r["size"] < hi)
               for lo, hi in ((0, 3.8), (3.8, 4.3), (4.3, 9.9))]
    print("\ngroup | messages | views: median (lowest to highest) | with a reaction | reactions in all | reactions per 100 views")
    for name, belongs in groups:
        mine = [r for r in rows if belongs(r)]
        views = [r["views"] for r in mine if r["views"] is not None]
        if not mine or not views:
            continue
        reacted, total = sum(r["reactions"] > 0 for r in mine), sum(r["reactions"] for r in mine)
        print(f"{name} | {len(mine)} | {median(views):.0f} ({min(views)} to {max(views)}) | {reacted} ({100 * reacted / len(mine):.0f} %) | {total} | {100 * total / sum(views):.1f}")
    print("\nmessages with a reaction (UTC, kind, size, views, reactions, emoji shown)")
    for r in rows:
        if r["reactions"]:
            print(f"  {r['created'][5:16]} {'sensor alert' if r['kind'] == 'detection' else 'agency report':13s} M{r['size']} | {r['views']} views | {r['reactions']} {r['emoji']}")
    print("\nviews by when the message was posted (UTC hour: messages, median views)")
    hours = {}
    for r in rows:
        if r["views"] is not None:
            hours.setdefault(r["created"][5:13], []).append(r["views"])
    print("  " + "  ".join(f"{hour[3:5]}/{hour[:2]} {hour[6:]}h: {len(v)}, {median(v):.0f}" for hour, v in sorted(hours.items())))


if __name__ == "__main__":
    main()
