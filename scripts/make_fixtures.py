"""Generate fixtures/synthetic_history.csv: fictional Turkish price histories (stdlib only, deterministic).

Run: uv run --python 3.12 python scripts/make_fixtures.py

Scenario table (NOT written to the CSV). Expected verdict from deal_hunter.deals.analyze:
  #  cat      product            scenario                          expected
  1  tv 55"   Lumora LX-55Q7     genuine_deal (34k -> 21.5k, -35%) GERCEK FIRSAT
  2  tv 55"   Novix NV-55U5      cosmetic (24k, dips 21.8k)        KOZMETIK INDIRIM
  3  tv 55"   Orvia OR-55X9      fake_inflate (22k -> 28.5k -> 22k) SAHTE INDIRIM
  4  tv 55"   Kestrel KS-55M3    fake_listprice (23k, list 34,999)  SAHTE INDIRIM
  5  tv 65"   Zentra ZN-65Q8     genuine_deal (46k -> 30k, >25k)    GERCEK FIRSAT
  6  tv 55"   Tessa TS-55N1      new_product (20 days)              YETERSIZ VERI
  7  tv 50"   Lumora LX-50F2     normal (16.5k)                     NORMAL
  8  laptop   Orvia OB-16X       genuine_deal_plus_fake_list        GERCEK FIRSAT (+ list flag)
  9  laptop   Tessa TB-14A       volatile (+-18%, final 22.5k)      KOZMETIK INDIRIM (depth 25% < 30%)
 10  telefon  Kestrel KP-12      steady (18k)                       NORMAL
 11  telefon  Lumora LP-X7       genuine_deal (25k -> 16k)          GERCEK FIRSAT
 12  telefon  Zentra ZP-9        fake_inflate                       SAHTE INDIRIM
 13  kulaklik Tessa TK-ANC5      cosmetic                           KOZMETIK INDIRIM
 14  kulaklik Kestrel KK-AIR2    normal                             NORMAL
All stores are fictional (magaza-a/b/c).
"""
import csv
import json
import math
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deal_hunter.sources import CSV_COLUMNS  # noqa: E402

END = date(2026, 11, 15)
rng = random.Random(42)
STORE_LETTER = {"magaza-a": "a", "magaza-b": "b", "magaza-c": "c"}
STORE_BASE = {"magaza-a": 1001, "magaza-b": 2001, "magaza-c": 3001}


def retail(p):
    """x99 above 10k, x9.90 below."""
    return round(p / 100) * 100 - 1 if p >= 10000 else round(p / 10) * 10 - 0.1


def jit(level, pct):
    return level * (1 + rng.uniform(-pct, pct))


# ---- price scenarios: f(i) -> market level i days before END (i=0 is the final day) ----
def steady(level, pct=0.015):
    return lambda i: jit(level, pct)


def inflate(level, raised, n_raised=25):
    return lambda i: jit(raised, 0.01) if i <= n_raised else jit(level, 0.015)


def genuine(lo, hi):
    mid, amp = (lo + hi) / 2, (hi - lo) / 2
    return lambda i: jit(mid + amp * math.sin(i / 30), 0.02)


def cosmetic(level, dip, dip_days):
    return lambda i: jit(dip, 0.002) if i in dip_days else jit(level, 0.01)


def volatile(level, amp=0.18, period=60):
    # phase puts the last 30 days in a trough, so no "inflated recently" flag
    return lambda i: jit(level * (1 + amp * math.sin(2 * math.pi * i / period + math.pi)), 0.02)


# ---- titles: same product, different wording per store ----
def nodash(m):
    return m.replace("-", "")


def title(cat, s, brand, m, d):
    k = STORE_LETTER[s]
    if cat == "tv":
        cm = round(d["ekran_inc"] * 2.54)
        return {"a": f"{brand} {m} {d['ekran_inc']} inç {d['cozunurluk']} {d['panel']} Smart TV",
                "b": f"{brand} {nodash(m)} {d['ekran_inc']}\" {d['panel']} {d['cozunurluk']} Akıllı Televizyon Resmi Distribütör Garantili",
                "c": f"{d['panel']} {d['cozunurluk']} {cm} cm Ekran Televizyon {brand} {m}"}[k]
    if cat == "laptop":
        return {"a": f"{brand} {m} {d['ekran_inc']} inç {d['islemci']} {d['ram_gb']} GB RAM {d['depolama_gb']} GB SSD Dizüstü Bilgisayar",
                "b": f"{brand} {nodash(m)} Notebook {d['ekran_inc']}\" {d['ram_gb']}GB/{d['depolama_gb']}GB {d['islemci']} Resmi Distribütör Garantili",
                "c": f"{d['islemci']} {d['ram_gb']} GB {d['depolama_gb']} GB Laptop {brand} {m} {d['ekran_inc']} inç"}[k]
    if cat == "telefon":
        return {"a": f"{brand} {m} {d['depolama_gb']} GB {d['ram_gb']} GB RAM Akıllı Telefon",
                "b": f"{brand} {nodash(m)} Cep Telefonu {d['ram_gb']}/{d['depolama_gb']} GB {d['ekran_inc']}\" Ekran Resmi Distribütör Garantili",
                "c": f"{m} {d['depolama_gb']}GB Akıllı Telefon {brand} - Türkiye Garantili"}[k]
    return {"a": f"{brand} {m} {d['tip'].title()} Bluetooth Kulaklık" + (" Aktif Gürültü Engelleme" if d["anc"] else ""),
            "b": f"{brand} {nodash(m)} Kablosuz Kulaklık ({d['tip']})" + (" ANC" if d["anc"] else "") + " Resmi Distribütör Garantili",
            "c": f"{m} {brand} Bluetooth {d['tip']} Kulaklık" + (" ANC'li" if d["anc"] else "")}[k]


def tv(inc, res, panel, hz):
    return dict(ekran_inc=inc, cozunurluk=res, panel=panel, yenileme_hz=hz, smart=True)


def lap(inc, ram, dep, cpu):
    return dict(ekran_inc=inc, ram_gb=ram, depolama_gb=dep, islemci=cpu)


def tel(dep, ram, inc):
    return dict(depolama_gb=dep, ram_gb=ram, ekran_inc=inc)


def kul(tip, anc):
    return dict(tip=tip, anc=anc)


A, B, C = "magaza-a", "magaza-b", "magaza-c"
D = (40, 41, 95, 96, 150)
# cat, brand, model, specs, stores, history days N, level fn, final (price, list) for the LAST store, force-observed days
P = [
    ("tv", "Lumora", "LX-55Q7", tv(55, "4K UHD", "QLED", 120), [A, B, C], 195, genuine(33000, 35000), (21499, 34999), ()),
    ("tv", "Novix", "NV-55U5", tv(55, "4K UHD", "LED", 60), [A, B, C], 190, cosmetic(24000, 21800, D), (21899, 24299), D),
    ("tv", "Orvia", "OR-55X9", tv(55, "4K UHD", "QLED", 100), [A, C], 185, inflate(22000, 28500), (21999, 28999), ()),
    ("tv", "Kestrel", "KS-55M3", tv(55, "4K UHD", "LED", 60), [B, C], 190, steady(23000), (22499, 34999), ()),
    ("tv", "Zentra", "ZN-65Q8", tv(65, "4K UHD", "QLED", 120), [A, B], 195, steady(46000, 0.02), (29999, 49999), ()),
    ("tv", "Tessa", "TS-55N1", tv(55, "4K UHD", "LED", 60), [A, B], 20, steady(19500), (18999, None), ()),
    ("tv", "Lumora", "LX-50F2", tv(50, "Full HD", "LED", 60), [C], 190, steady(16500), None, ()),
    ("laptop", "Orvia", "OB-16X", lap(16, 16, 512, "Novix N5 8 çekirdek"), [A, B, C], 195, steady(42000, 0.02), (26999, 59999), ()),
    ("laptop", "Tessa", "TB-14A", lap(14, 8, 256, "Zentra Z3 4 çekirdek"), [A, B], 190, volatile(30000), (22500, None), ()),
    ("telefon", "Kestrel", "KP-12", tel(128, 6, 6.5), [A, C], 190, steady(18000), None, ()),
    ("telefon", "Lumora", "LP-X7", tel(256, 8, 6.7), [A, B, C], 195, steady(25000, 0.02), (15999, 24999), ()),
    ("telefon", "Zentra", "ZP-9", tel(128, 8, 6.6), [B, C], 185, inflate(21000, 27000), (20999, 27499), ()),
    ("kulaklik", "Tessa", "TK-ANC5", kul("kulak üstü", True), [A, B], 190, cosmetic(2800, 2490, D), (2499.9, 2799.9), D),
    ("kulaklik", "Kestrel", "KK-AIR2", kul("kulak içi", False), [C], 190, steady(1900), None, ()),
]

rows, listings = [], 0
nth = dict.fromkeys(STORE_BASE, 0)
for cat, brand, model, specs, stores, N, level, final, force in P:
    market = {i: level(i) for i in range(0, N + 1)}
    for si, s in enumerate(stores):
        listings += 1
        off = 0.0 if si == 0 else rng.uniform(0.005, 0.04)
        ref = f"{STORE_LETTER[s]}-{STORE_BASE[s] + nth[s]}"
        nth[s] += 1
        t, ts_time = title(cat, s, brand, model, specs), f"{rng.randint(8, 22):02d}:{rng.choice([0, 15, 30, 45]):02d}:00"
        spec_json = json.dumps(specs, ensure_ascii=False, separators=(",", ":"))

        def row(i, price, lp="", stock=1):
            day = END - timedelta(days=i)
            return [s, ref, t, brand, model, cat, "", spec_json, f"{day.isoformat()}T{ts_time}",
                    f"{price:.2f}", "" if lp in ("", None) else f"{lp:.2f}", stock, 1]

        p = rng.uniform(0.3, 0.6)
        seen = {i for i in range(N, 0, -1) if i == N or (si == 0 and i in force) or rng.random() < p}
        for i in sorted(seen, reverse=True):
            rows.append(row(i, retail(market[i] * (1 + off))))
        # final day: the deal/fake story is on the last store only
        if final and si == len(stores) - 1:
            rows.append(row(0, final[0], final[1]))
        else:
            rows.append(row(0, retail(market[0] * (1 + off))))
        # a couple of out-of-stock rows with odd prices (must be ignored)
        if rng.random() < 0.6:
            for i in rng.sample([d for d in range(1, N) if d not in seen], 2):
                rows.append(row(i, retail(market[i] * rng.choice([0.35, 2.5])), stock=0))

rows.sort(key=lambda r: (r[0], r[1], r[8]))
out = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic_history.csv"
out.parent.mkdir(exist_ok=True)
with open(out, "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(CSV_COLUMNS)
    w.writerows(rows)
print(f"{len(rows)} rows, {listings} listings -> {out}")
