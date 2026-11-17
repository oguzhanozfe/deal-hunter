"""SQLite price-history store: listings (one per source item) and price observations."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from .deals import Obs

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  title TEXT NOT NULL,
  brand TEXT, model_code TEXT, category TEXT, url TEXT,
  specs TEXT NOT NULL DEFAULT '{}',
  currency TEXT NOT NULL DEFAULT 'TRY',
  synthetic INTEGER NOT NULL DEFAULT 0,
  product_key TEXT,
  UNIQUE (source, source_ref)
);
CREATE TABLE IF NOT EXISTS prices (
  listing_id INTEGER NOT NULL REFERENCES listings(id),
  ts TEXT NOT NULL,
  price REAL NOT NULL CHECK (price > 0),
  list_price REAL,
  in_stock INTEGER NOT NULL DEFAULT 1,
  UNIQUE (listing_id, ts)
);
CREATE INDEX IF NOT EXISTS prices_listing ON prices(listing_id, ts);
"""


@dataclass
class Observation:
    """One price seen for one listing at one time. Every Source yields these."""
    source: str
    source_ref: str
    title: str
    price: float
    ts: datetime
    in_stock: bool = True
    list_price: float | None = None
    brand: str | None = None
    model_code: str | None = None
    category: str | None = None
    url: str | None = None
    specs: dict = field(default_factory=dict)
    currency: str = "TRY"
    synthetic: bool = False


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add(self, observations) -> int:
        n = 0
        with self.db:
            for o in observations:
                self.db.execute(
                    """INSERT INTO listings (source, source_ref, title, brand, model_code, category, url,
                                             specs, currency, synthetic)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT (source, source_ref) DO UPDATE SET
                         title=excluded.title, url=COALESCE(excluded.url, url),
                         brand=COALESCE(excluded.brand, brand), model_code=COALESCE(excluded.model_code, model_code),
                         category=COALESCE(excluded.category, category), specs=excluded.specs""",
                    (o.source, o.source_ref, o.title, o.brand, o.model_code, o.category, o.url,
                     json.dumps(o.specs, ensure_ascii=False, sort_keys=True), o.currency, int(o.synthetic)))
                lid = self.db.execute("SELECT id FROM listings WHERE source=? AND source_ref=?",
                                      (o.source, o.source_ref)).fetchone()[0]
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO prices (listing_id, ts, price, list_price, in_stock) VALUES (?,?,?,?,?)",
                    (lid, o.ts.isoformat(timespec="seconds"), o.price, o.list_price, int(o.in_stock)))
                n += cur.rowcount
        return n

    def listings(self, category: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM listings", ()
        if category:
            q, args = q + " WHERE category=?", (category,)
        rows = [dict(r) for r in self.db.execute(q + " ORDER BY id", args)]
        for r in rows:
            r["specs"] = json.loads(r["specs"])
        return rows

    def set_product_keys(self, keys: dict[int, str]) -> None:
        with self.db:
            self.db.executemany("UPDATE listings SET product_key=? WHERE id=?", [(k, i) for i, k in keys.items()])

    def products(self, category: str | None = None) -> dict[str, list[dict]]:
        """product_key -> its listings. Unmatched listings are their own product."""
        out: dict[str, list[dict]] = {}
        for r in self.listings(category):
            out.setdefault(r["product_key"] or f"L{r['id']}", []).append(r)
        return out

    def latest_day(self) -> date | None:
        row = self.db.execute("SELECT MAX(ts) FROM prices").fetchone()[0]
        return date.fromisoformat(row[:10]) if row else None

    def history(self, listing_ids: list[int]) -> list[Obs]:
        marks = ",".join("?" * len(listing_ids))
        rows = self.db.execute(
            f"SELECT listing_id, ts, price, list_price, in_stock FROM prices WHERE listing_id IN ({marks}) ORDER BY ts",
            listing_ids)
        return [Obs(date.fromisoformat(r["ts"][:10]), r["price"], r["list_price"], bool(r["in_stock"]),
                    str(r["listing_id"])) for r in rows]
