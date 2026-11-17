"""deal-hunter CLI.

  python -m deal_hunter demo                      # offline: synthetic fixture + scripted chat
  python -m deal_hunter import-csv FILE           # add a CSV price history (see fixtures/ for columns)
  python -m deal_hunter collect-openprices        # real run against the Open Prices API (TRY prices)
  python -m deal_hunter import-openprices FILE    # load a recorded Open Prices run
  python -m deal_hunter match [--mode rules|rules+llm]
  python -m deal_hunter deals [--category tv]
  python -m deal_hunter chat [--llm] [--telegram]
DB path: --db or $DEAL_HUNTER_DB (default deal_hunter.db).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from .bot import Session, run_telegram
from .deals import DealConfig, analyze
from .matching import group_listings
from .sources import CsvSource, OpenPricesSource
from .store import Store
from .text import fmt_tl

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
DEMO_SCRIPT = ["Merhaba, televizyon bakıyorum", "25.000 TL", "55 inç"]


def deals_table(store: Store, category: str | None, cfg: DealConfig, as_of=None) -> str:
    rows = []
    as_of = as_of or store.latest_day()
    for listings in store.products(category).values():
        v = analyze(store.history([l["id"] for l in listings]), cfg, as_of)
        title = min(listings, key=lambda l: l["id"])["title"]
        depth = f"{-v.stats['depth'] * 100:+.0f}%" if "depth" in v.stats else "-"
        cur = fmt_tl(v.current) if v.current else "-"
        rows.append((v.verdict, v.score, f"{v.verdict:<17} {v.score:5.1f} {depth:>6} {cur:>12}  "
                                         f"[{len(listings)} kaynak] {title[:60]}" + (" !" if v.flags else "")))
    rows.sort(key=lambda r: (r[0] != "GERÇEK FIRSAT", -r[1]))
    head = f"{'KARAR':<17} {'PUAN':>5} {'MED.':>6} {'FİYAT':>12}  ÜRÜN\n(MED. = 180 günlük medyana göre fark; ! = sahte indirim işareti)"
    return "\n".join([head] + [r[2] for r in rows])


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="deal_hunter")
    ap.add_argument("--db", default=os.environ.get("DEAL_HUNTER_DB", "deal_hunter.db"))
    ap.add_argument("--as-of", type=date.fromisoformat,
                    help="date of 'now' (YYYY-MM-DD); default: newest price in the DB")
    ap.add_argument("--min-depth", type=float, default=0.30, help="deal threshold vs 180-day median (0.30 = 30%%)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    p = sub.add_parser("import-csv"); p.add_argument("path")
    p = sub.add_parser("collect-openprices"); p.add_argument("--pages", type=int, default=5)
    p.add_argument("--save", help="write the slimmed raw items to this JSON file")
    p = sub.add_parser("import-openprices"); p.add_argument("path")
    p = sub.add_parser("match"); p.add_argument("--mode", default="rules", choices=["rules", "rules+llm"])
    p = sub.add_parser("deals"); p.add_argument("--category")
    p = sub.add_parser("chat"); p.add_argument("--llm", action="store_true"); p.add_argument("--telegram", action="store_true")
    a = ap.parse_args(argv)
    cfg = DealConfig(min_depth=a.min_depth)

    if a.cmd == "demo":
        store = Store()
        store.add(CsvSource(str(FIXTURES / "synthetic_history.csv")).fetch())
        store.set_product_keys(group_listings(store.listings()))
        print("== Sentetik fixture: tüm ürünler ==\n" + deals_table(store, None, cfg) + "\n")
        s = Session(store, cfg)
        for msg in DEMO_SCRIPT:
            print(f"> {msg}\n{s.handle(msg)}\n")
        return

    store = Store(a.db)
    if a.cmd == "import-csv":
        print(f"{store.add(CsvSource(a.path).fetch())} fiyat gözlemi eklendi.")
    elif a.cmd == "collect-openprices":
        src = OpenPricesSource(pages=a.pages)
        raw = src.download()
        if a.save:
            meta = {"source": "https://prices.openfoodfacts.org/api/v1/prices", "license": "ODbL 1.0",
                    "attribution": "(c) Open Prices / Open Food Facts contributors",
                    "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "query": {"currency": "TRY", "order_by": "-date"}, "items": raw}
            Path(a.save).write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"{len(raw)} kayıt indirildi, {store.add(OpenPricesSource(raw=raw).fetch())} gözlem eklendi.")
    elif a.cmd == "import-openprices":
        raw = json.loads(Path(a.path).read_text(encoding="utf-8"))["items"]
        print(f"{store.add(OpenPricesSource(raw=raw).fetch())} gözlem eklendi.")
    elif a.cmd == "match":
        keys = group_listings(store.listings(), a.mode)
        store.set_product_keys(keys)
        print(f"{len(keys)} ilan {len(set(keys.values()))} ürüne gruplandı.")
    elif a.cmd == "deals":
        print(deals_table(store, a.category, cfg, a.as_of))
    elif a.cmd == "chat":
        chat = None
        if a.llm:
            from .llm import chat_json
            chat = chat_json
        if a.telegram:
            run_telegram(store, chat=chat)
            return
        s = Session(store, cfg, chat=chat, as_of=a.as_of)
        print("Ne arıyorsunuz? (çıkmak için Ctrl+D, baştan başlamak için 'yeni')")
        for line in sys.stdin:
            if line.strip():
                print(s.handle(line.strip()) + "\n")


if __name__ == "__main__":
    main()
