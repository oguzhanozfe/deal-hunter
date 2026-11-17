"""Text parsing, matching, sources and store. All offline."""
import io
import itertools
import json
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path

from deal_hunter.matching import decide, group_listings
from deal_hunter.sources import (Blocked, CsvSource, OpenPricesSource, PageSnapshotSource, PoliteClient,
                                 parse_jsonld_offer, robots_allows, slim_open_price)
from deal_hunter.store import Observation, Store
from deal_hunter.text import fmt_tl, fold, parse_amount, parse_inches, tr_lower

FIX = Path(__file__).resolve().parent.parent / "fixtures"
BRANDS = {"lumora", "novix", "orvia", "kestrel", "tessa", "zentra"}


class TextTests(unittest.TestCase):
    def test_turkish_lowercase(self):
        self.assertEqual(tr_lower("İSTANBUL IŞIK"), "istanbul ışık")
        self.assertEqual(fold("Dizüstü Kulaklık"), "dizustu kulaklik")

    def test_amounts(self):
        for text, want in [("25.000 TL altı", 25000), ("25 bin", 25000), ("24.999,90 TL", 24999.9),
                           ("bütçem 30k", 30000), ("en fazla 18500 tl", 18500), ("55 inç", None)]:
            self.assertEqual(parse_amount(text), want, text)

    def test_inches(self):
        for text, want in [("55 inç", 55), ('55"', 55), ("65 inch", 65), ("139 cm ekran", 55), ("tv", None)]:
            self.assertEqual(parse_inches(text), want, text)

    def test_fmt(self):
        self.assertEqual(fmt_tl(21499.0), "21.499 TL")


class MatchingTests(unittest.TestCase):
    def check(self, a, b, want):
        same, by, why = decide(a, b, BRANDS)
        self.assertEqual(same, want, f"{a} || {b}: {by} {why}")

    def test_same_product_different_wording(self):
        self.check("Lumora LM55QX700 55 inç 4K Smart TV", "LUMORA LM-55QX700 139 cm 4K Ultra HD Akıllı TV", True)
        self.check("Orvia S12 128 GB 8 GB RAM", "Orvia S12 Cep Telefonu 8/128 GB", True)

    def test_hard_negatives(self):
        self.check("Lumora LM55QX700 55 inç", "Lumora LM65QX700 65 inç", False)
        self.check("Orvia S12 128 GB 8 GB RAM", "Orvia S12 256 GB 8 GB RAM", False)
        self.check("Orvia S12 128 GB", "Orvia S12 Pro 128 GB", False)
        self.check("Lumora LM55QX700 55 inç TV", "Lumora LM55QX700 için Uzaktan Kumanda", False)
        self.check("Zentra ZR600NF 600 L", "Yenilenmiş Zentra ZR600NF 600 L", False)
        self.check("Lumora LM55QX700 55 inç", "Novix NV55QX700 55 inç", False)

    def test_region_suffix_is_same(self):
        self.check("Lumora LM55QX700T 55 inç", "Lumora LM55QX700 55 inç", True)

    def test_unsure_pairs_go_to_llm(self):
        calls = []
        fake = lambda system, user: calls.append(user) or {"same": True, "reason": "ok"}  # noqa: E731
        same, by, _ = decide("Lumora BudsX Pro Siyah", "Lumora BudsX Pro Kablosuz Kulaklık", BRANDS, "rules+llm", fake)
        self.assertTrue(same)
        self.assertEqual((by, len(calls)), ("llm", 1))
        decide("Lumora LM55QX700 55 inç", "Lumora LM65QX700 65 inç", BRANDS, "rules+llm", fake)
        self.assertEqual(len(calls), 1)  # clear cases never reach the LLM

    def test_fixture_groups_by_true_model(self):
        s = Store()
        s.add(CsvSource(str(FIX / "synthetic_history.csv")).fetch())
        ls = s.listings()
        keys = group_listings(ls)
        truth = {l["id"]: (l["brand"], l["model_code"].replace("-", "")) for l in ls}
        for a, b in itertools.combinations(ls, 2):
            self.assertEqual(keys[a["id"]] == keys[b["id"]], truth[a["id"]] == truth[b["id"]], (a["title"], b["title"]))


AKAKCE_LIKE = "User-agent:*\nAllow: /\nDisallow: /c/\nDisallow: /*?s=*\nDisallow: /*&s=*\n\nUser-agent: BLEXBot\nDisallow: /\n"


class SourceTests(unittest.TestCase):
    def test_robots_wildcards(self):
        self.assertTrue(robots_allows(AKAKCE_LIKE, "https://x.com/en-ucuz-tv-fiyati,123.html"))
        self.assertFalse(robots_allows(AKAKCE_LIKE, "https://x.com/c/?x=1"))
        self.assertFalse(robots_allows(AKAKCE_LIKE, "https://x.com/arama?q=tv&s=2"))  # stdlib robotparser says yes
        self.assertFalse(robots_allows("User-agent: deal-hunter\nDisallow: /\n", "https://x.com/a"))
        self.assertTrue(robots_allows("User-agent: *\nDisallow: /private\nAllow: /private/ok\n", "https://x.com/private/ok"))
        self.assertTrue(robots_allows("", "https://x.com/a"))

    def _client(self, pages: dict):
        class Resp(io.BytesIO):
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def opener(req):
            body = pages.get(req.full_url)
            if isinstance(body, int):
                raise urllib.error.HTTPError(req.full_url, body, "x", {}, None)
            return Resp((body or "").encode())
        return PoliteClient(min_interval=0, opener=opener)

    def test_client_stops_on_block_and_challenge(self):
        c = self._client({"https://a.com/robots.txt": "", "https://a.com/p": 403, "https://a.com/q": "<div>captcha</div>"})
        with self.assertRaises(Blocked):
            c.get("https://a.com/p")
        with self.assertRaises(Blocked):
            c.get("https://a.com/q")
        with self.assertRaises(Blocked):  # robots.txt itself behind a wall -> whole site off-limits
            self._client({"https://b.com/robots.txt": 403}).get("https://b.com/x")

    def test_snapshot_needs_allowlist_and_parses_jsonld(self):
        html = ('<script type="application/ld+json">{"@type":"Product","name":"Novix TV","sku":"N1","mpn":"NV-1",'
                '"brand":{"name":"Novix"},"offers":{"price":"18999.90","priceCurrency":"TRY",'
                '"availability":"https://schema.org/InStock"}}</script>')
        c = self._client({"https://shop.test/robots.txt": "", "https://shop.test/p": html})
        with self.assertRaises(Blocked):
            list(PageSnapshotSource(["https://shop.test/p"], set(), c).fetch())
        obs = list(PageSnapshotSource(["https://shop.test/p"], {"shop.test"}, c).fetch())
        self.assertEqual((obs[0].price, obs[0].brand, obs[0].model_code, obs[0].in_stock), (18999.9, "Novix", "NV-1", True))
        self.assertIsNone(parse_jsonld_offer("<html></html>", "u", "s", datetime(2026, 1, 1)))

    def test_open_prices_slim_drops_contributor_data(self):
        item = {"type": "PRODUCT", "product_code": "869", "price": 49.9, "currency": "TRY", "date": "2026-09-04",
                "price_is_discounted": True, "price_without_discount": 64.5, "owner": "someone",
                "proof": {"file_path": "x.png", "owner": "someone"}, "location_osm_id": 7,
                "product": {"product_name": "Çikolata"}, "location": {"osm_name": "Market", "osm_address_city": "Fatih"}}
        slim = slim_open_price(item)
        self.assertNotIn("someone", json.dumps(slim))
        o = next(iter(OpenPricesSource(raw=[slim]).fetch()))
        self.assertEqual((o.title, o.price, o.list_price, o.source_ref), ("Çikolata", 49.9, 64.5, "869@7"))

    def test_recorded_open_prices_run_loads(self):
        data = json.loads((FIX / "openprices_tr.json").read_text(encoding="utf-8"))
        self.assertEqual(data["license"], "ODbL 1.0")
        self.assertNotIn('"owner"', json.dumps(data))
        s = Store()
        self.assertGreater(s.add(OpenPricesSource(raw=data["items"]).fetch()), 0)


class StoreTests(unittest.TestCase):
    def test_upsert_and_dedupe(self):
        s = Store()
        o = Observation("m", "1", "TV", 100.0, datetime(2026, 1, 1), specs={"ekran_inc": 55})
        self.assertEqual(s.add([o, o]), 1)
        self.assertEqual(s.listings()[0]["specs"], {"ekran_inc": 55})
        self.assertEqual(len(s.history([s.listings()[0]["id"]])), 1)


if __name__ == "__main__":
    unittest.main()
