"""Pluggable price sources. A Source is anything with `name` and `fetch() -> Iterable[Observation]`.

Shipped sources:
  CsvSource          - manual/exported price history (the default way to get history for sites
                       whose terms do not allow automated collection).
  OpenPricesSource   - Open Prices (Open Food Facts) public API, ODbL data. Real, permitted.
  PageSnapshotSource - our own periodic snapshot of a product page's schema.org Offer price.
                       Only for domains the operator has put on an allowlist after reading that
                       site's terms; checks robots.txt (with wildcard support), rate-limits, and
                       stops on 401/403/429 or a challenge page. It never retries around a block.
"""
from __future__ import annotations

import csv
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterable, Protocol

from .store import Observation

USER_AGENT = "deal-hunter/0.1 (+https://github.com/oguzhanozfe/deal-hunter)"


class Source(Protocol):
    name: str

    def fetch(self) -> Iterable[Observation]: ...


class Blocked(RuntimeError):
    """The site refused us (robots.txt, terms allowlist, 4xx block or challenge). Do not retry."""


# ---------- CSV ----------

CSV_COLUMNS = ["source", "source_ref", "title", "brand", "model_code", "category", "url", "specs_json",
               "ts", "price", "list_price", "in_stock", "synthetic"]


class CsvSource:
    def __init__(self, path: str):
        self.path, self.name = path, f"csv:{path}"

    def fetch(self) -> Iterable[Observation]:
        with open(self.path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                yield Observation(
                    source=row["source"], source_ref=row["source_ref"], title=row["title"],
                    price=float(row["price"]), ts=datetime.fromisoformat(row["ts"]),
                    in_stock=row.get("in_stock", "1") not in ("0", "false", "False", ""),
                    list_price=float(row["list_price"]) if row.get("list_price") else None,
                    brand=row.get("brand") or None, model_code=row.get("model_code") or None,
                    category=row.get("category") or None, url=row.get("url") or None,
                    specs=json.loads(row["specs_json"]) if row.get("specs_json") else {},
                    synthetic=row.get("synthetic", "0") in ("1", "true", "True"))


# ---------- polite HTTP ----------

def robots_allows(robots_txt: str, url: str, agent: str = "deal-hunter") -> bool:
    """robots.txt check with '*' and '$' wildcards (urllib.robotparser ignores them).

    Longest matching rule wins; Allow wins ties. Group for our agent if present, else '*'.
    """
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    for raw in robots_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, val = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(val.lower())
        elif key in ("allow", "disallow") and agents:
            rules.append((key, val))
    if agents:
        groups.append((agents, rules))
    mine = [r for a, r in groups if agent.lower() in a]
    chosen = mine or [r for a, r in groups if "*" in a]
    applicable = [rule for rs in chosen for rule in rs]

    p = urllib.parse.urlsplit(url)
    path = (p.path or "/") + (f"?{p.query}" if p.query else "")
    best = ("allow", -1)
    for kind, pat in applicable:
        if not pat:
            continue
        if not pat.startswith(("/", "*")):
            pat = "/" + pat
        rx = "^" + re.escape(pat).replace(r"\*", ".*").replace(r"\$", "$")
        if re.match(rx, path):
            if len(pat) > best[1] or (len(pat) == best[1] and kind == "allow"):
                best = (kind, len(pat))
    return best[0] == "allow"


class PoliteClient:
    def __init__(self, min_interval: float = 10.0, opener=None):
        self.min_interval = min_interval
        self.open = opener or (lambda req: urllib.request.urlopen(req, timeout=30))
        self._last: dict[str, float] = {}
        self._robots: dict[str, str] = {}

    def _raw(self, url: str) -> tuple[int, str]:
        host = urllib.parse.urlsplit(url).netloc
        wait = self._last.get(host, 0) + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "tr-TR"})
        try:
            with self.open(req) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""

    def get(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            code, body = self._raw(base + "/robots.txt")
            if code in (401, 403) or code >= 500:
                raise Blocked(f"{base}/robots.txt returned {code}; treating the site as off-limits")
            self._robots[base] = body if code == 200 else ""
        if not robots_allows(self._robots[base], url):
            raise Blocked(f"robots.txt disallows {url}")
        code, body = self._raw(url)
        if code in (401, 403, 429) or code >= 500:
            raise Blocked(f"{url} returned {code}; stopping (no retry, no bypass)")
        if re.search(r"captcha|cf-chl|challenge-platform|Güvenlik doğrulaması", body, re.I):
            raise Blocked(f"{url} served a challenge page; stopping")
        return body


# ---------- Open Prices ----------

class OpenPricesSource:
    """Real price observations in TRY from Open Prices (https://prices.openfoodfacts.org), ODbL 1.0.

    Contributor handles and proof images are not stored.
    """
    name = "openprices"
    API = "https://prices.openfoodfacts.org/api/v1/prices"

    def __init__(self, currency: str = "TRY", pages: int = 5, client: PoliteClient | None = None,
                 raw: list[dict] | None = None):
        self.currency, self.pages = currency, pages
        self.client = client or PoliteClient(min_interval=2.0)
        self.raw = raw  # pre-recorded items (fixture) instead of the network

    def download(self) -> list[dict]:
        items: list[dict] = []
        for page in range(1, self.pages + 1):
            q = urllib.parse.urlencode({"currency": self.currency, "size": 100, "page": page, "order_by": "-date"})
            data = json.loads(self.client.get(f"{self.API}?{q}"))
            items += [slim_open_price(it) for it in data["items"]]
            if page >= data.get("pages", 1):
                break
        return items

    def fetch(self) -> Iterable[Observation]:
        for it in self.raw if self.raw is not None else self.download():
            if it.get("type") != "PRODUCT" or not it.get("product_code"):
                continue
            yield Observation(
                source=self.name,
                source_ref=f"{it['product_code']}@{it.get('location_osm_id')}",
                title=(it.get("product_name") or it["product_code"]).strip(),
                price=float(it["price"]), ts=datetime.fromisoformat(it["date"]),
                list_price=it.get("price_without_discount") if it.get("price_is_discounted") else None,
                brand=it.get("brands"), category="market",
                specs={k: v for k, v in (("barkod", it["product_code"]), ("mağaza", it.get("location_name")),
                                         ("şehir", it.get("location_city"))) if v},
                currency=it.get("currency", "TRY"))


def slim_open_price(it: dict) -> dict:
    """Keep only price facts; drop contributor handles ('owner'), proof images and ids."""
    prod, loc = it.get("product") or {}, it.get("location") or {}
    return {
        "type": it.get("type"), "product_code": it.get("product_code"),
        "product_name": prod.get("product_name") or it.get("product_name"),
        "brands": prod.get("brands"),
        "price": it.get("price"), "currency": it.get("currency"), "date": it.get("date"),
        "price_is_discounted": it.get("price_is_discounted"),
        "price_without_discount": it.get("price_without_discount"),
        "location_osm_id": it.get("location_osm_id"),
        "location_name": loc.get("osm_name"), "location_city": loc.get("osm_address_city"),
    }


# ---------- own page snapshots ----------

class PageSnapshotSource:
    """Snapshot schema.org/Product JSON-LD offers from product pages on allowlisted domains.

    `allowed_domains` starts empty on purpose: add a domain only after reading its terms.
    None of Akakçe, Cimri, Trendyol or Hepsiburada qualify (see README).
    """

    def __init__(self, urls: list[str], allowed_domains: set[str], client: PoliteClient | None = None,
                 source: str = "snapshot", category: str | None = None):
        self.urls, self.allowed, self.name, self.category = urls, allowed_domains, source, category
        self.client = client or PoliteClient()

    def fetch(self) -> Iterable[Observation]:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for url in self.urls:
            host = urllib.parse.urlsplit(url).netloc.lower()
            if host not in self.allowed:
                raise Blocked(f"{host} is not on the allowlist; read its terms first")
            obs = parse_jsonld_offer(self.client.get(url), url, self.name, now)
            if obs:
                obs.category = obs.category or self.category
                yield obs


def parse_jsonld_offer(html: str, url: str, source: str, ts: datetime) -> Observation | None:
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else data.get("@graph", [data]):
            if not isinstance(node, dict) or node.get("@type") != "Product":
                continue
            offer = node.get("offers") or {}
            if isinstance(offer, list):
                offer = offer[0] if offer else {}
            price = offer.get("price") or offer.get("lowPrice")
            if price is None:
                continue
            brand = node.get("brand")
            return Observation(
                source=source, source_ref=node.get("sku") or url, title=node.get("name", url),
                price=float(str(price).replace(",", ".")), ts=ts, url=url,
                in_stock="InStock" in str(offer.get("availability", "InStock")),
                brand=brand.get("name") if isinstance(brand, dict) else brand,
                model_code=node.get("mpn"), currency=offer.get("priceCurrency", "TRY"))
    return None
