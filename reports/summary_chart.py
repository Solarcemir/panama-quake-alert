"""Everything the IGC reported against what the sensor announced first, by magnitude, since the alerts began.

    python reports/summary_chart.py <png>

Prints the table and draws it (Spanish, for the channel's followers). An earthquake counts as announced when
compare.py pairs one of the sensor's alerts with it. Read-only on the logs.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

QA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(QA))
import compare

LOGS = QA / "logs"
out = Path(sys.argv[1])
BLUE, ORANGE, INK, INK2, GRID, BG = "#2a78d6", "#eb6834", "#0b0b0b", "#52514e", "#e1e0d9", "#fcfcfb"
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
BINS = [("Menos de M 3.5", 0, 3.5), ("M 3.5 a 3.9", 3.5, 4.0), ("M 4.0 a 4.2", 4.0, 4.3), ("M 4.3 a 4.9", 4.3, 5.0), ("M 5.0 o más", 5.0, 99)]

det = [json.loads(l) for l in (LOGS / "detections.jsonl").open(encoding="utf-8")]
sight = [json.loads(l) for l in (LOGS / "sightings.jsonl").open(encoding="utf-8")]
sent = [json.loads(l) for l in (LOGS / "sent.jsonl").open(encoding="utf-8")]
now = datetime.now(timezone.utc)
start = compare.parse(sorted(s["at"] for s in sent if s["type"] == "detection" and s["outcome"] == "sent")[0]) - timedelta(seconds=120)
rows, lone = compare.earthquakes(det, sight, start)
quakes = []
for r in rows:
    g = r["by"].get("igc")
    if g and compare.parse(r["origin"]) >= start:
        size = g["first_mag"] if g["first_mag"] is not None else g["mag"]
        quakes.append((compare.parse(r["origin"]), size, bool(r["sensor"] and r["sensor"].get("declared_s") is not None)))
lone = [d for d in lone if start <= compare.parse(d["seen"]) <= now - timedelta(minutes=10)]
table = [(name, sum(lo <= m < hi for _, m, _ in quakes), sum(lo <= m < hi and a for _, m, a in quakes)) for name, lo, hi in BINS]
total, told = len(quakes), sum(a for _, _, a in quakes)
pty = lambda t: t - timedelta(hours=5)
clock = lambda t: pty(t).strftime("%I:%M %p").lstrip("0").replace("AM", "a. m.").replace("PM", "p. m.")
print(f"from {pty(start):%d/%m} {clock(start)} to {pty(now):%d/%m} {clock(now)} (Panama)")
for name, n, a in table:
    print(f"  {name}: IGC {n} | announced first {a} ({100 * a / max(n, 1):.0f} %)")
print(f"  ALL: IGC {total} | announced first {told} | sensor alerts with no IGC earthquake paired: {len(lone)}")
days = {}
for t, m, a in quakes:
    d = days.setdefault(f"{pty(t):%d/%m}", [0, 0, 0, 0])
    d[0] += 1; d[1] += a; d[2] += m >= 4.3; d[3] += m >= 4.3 and a
for k, v in days.items():
    print(f"  {k}: IGC {v[0]}, announced {v[1]} | M4.3+: {v[2]}, announced {v[3]}")

W, L, R, TOP, ROW = 540, 132, 110, 150, 58
H = TOP + ROW * len(table) + 96
top = max(n for _, n, _ in table)
X = lambda n: n / top * (W - L - R)
o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}"><rect width="{W}" height="{H}" fill="{BG}"/>',
     '<text x="20" y="38" class="title">Sismos del IGC y avisos del sensor</text>',
     f'<text x="20" y="62" class="sub">Del {pty(start).day} al {pty(now).day} de {MONTHS[pty(now).month - 1]} de {pty(now).year}, hasta las {clock(now)} · {total} sismos reportados</text>',
     '<text x="20" y="84" class="sub">Por magnitud (primer dato del IGC). El sensor solo avisa desde M 4.3.</text>',
     f'<rect x="20" y="100" width="14" height="14" rx="3" fill="{BLUE}"/><text x="42" y="112" class="key">el sensor avisó antes que el IGC</text>',
     f'<rect x="272" y="100" width="14" height="14" rx="3" fill="{ORANGE}"/><text x="294" y="112" class="key">solo reporte del IGC</text>']
for k, (name, n, a) in enumerate(table):
    y = TOP + ROW * k
    o.append(f'<text x="{L - 12}" y="{y + 25}" class="lab" text-anchor="end">{name}</text>')
    if a:
        o.append(f'<rect x="{L}" y="{y + 6}" width="{X(a):.1f}" height="28" rx="4" fill="{BLUE}"/>')
    if n - a:
        o.append(f'<rect x="{L + X(a) + (2 if a else 0):.1f}" y="{y + 6}" width="{max(X(n - a) - (2 if a else 0), 2):.1f}" height="28" rx="4" fill="{ORANGE}"/>')
    o.append(f'<text x="{L + X(n) + 10:.1f}" y="{y + 25}" class="lab strong">{a} de {n}</text>')
base = TOP + ROW * len(table) + 34
big = sum(n for (_, lo, _), (_, n, _) in zip(BINS, table) if lo >= 4.3), sum(a for (_, lo, _), (_, _, a) in zip(BINS, table) if lo >= 4.3)
o.append(f'<text x="20" y="{base}" class="foot strong">De M 4.3 o más: el sensor avisó {big[1]} de {big[0]} antes que el IGC</text>')
o.append(f'<text x="20" y="{base + 22}" class="foot">Canal no oficial. La información oficial es la del IGC.</text></svg>')
style = ('<style>body{margin:0}svg{display:block;font-family:system-ui,-apple-system,"Segoe UI",sans-serif}.title{font-size:21px;font-weight:700;fill:%s}'
         '.sub{font-size:13px;fill:%s}.key{font-size:13.5px;fill:%s}.lab{font-size:14px;fill:%s}.strong{font-weight:700}.foot{font-size:13px;fill:%s}.foot.strong{fill:%s}</style>') % (INK, INK2, INK, INK, INK2, INK)
page = out.with_suffix(".html")
page.write_text('<!doctype html><meta charset="utf-8">' + style + "".join(o), encoding="utf-8")
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    tab = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=2)
    tab.goto(page.as_uri())
    tab.locator("svg").screenshot(path=str(out))
    browser.close()
page.unlink()
print("written", out)
