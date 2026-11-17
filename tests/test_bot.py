"""Recommendation bot: clarifying questions, recommendations, and the no-invented-specs guarantee."""
import re
import unittest
from pathlib import Path

from deal_hunter.bot import SPEC_LABELS, Session, parse_need_llm, run_telegram, spec_value
from deal_hunter.matching import group_listings
from deal_hunter.sources import CsvSource
from deal_hunter.store import Store

FIX = Path(__file__).resolve().parent.parent / "fixtures"


def fixture_store() -> Store:
    s = Store()
    s.add(CsvSource(str(FIX / "synthetic_history.csv")).fetch())
    s.set_product_keys(group_listings(s.listings()))
    return s


class BotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = fixture_store()

    def test_asks_clarifying_questions_then_recommends(self):
        s = Session(self.store)
        self.assertIn("Hangi ürünü", s.handle("Merhaba"))
        self.assertIn("bütçe", s.handle("televizyon lazım"))
        self.assertIn("Kaç inç", s.handle("25 bin"))
        reply = s.handle("55")
        self.assertIn("1 gerçek fırsat", reply)
        self.assertIn("Lumora LX", reply)
        self.assertIn("21.499 TL", reply)
        self.assertNotIn("ZN-65Q8", reply)  # 65" deal is over budget and wrong size

    def test_one_shot_request(self):
        reply = Session(self.store).handle("55 inç, 25.000 TL altı televizyon istiyorum")
        self.assertIn("GERÇEK FIRSAT", reply)
        self.assertIn("SAHTE İNDİRİM", reply)  # fake discounts are shown as such, not recommended

    def test_size_no_preference(self):
        s = Session(self.store)
        s.handle("televizyon 40.000 TL")
        reply = s.handle("fark etmez")
        self.assertIn("ZN-65Q8", reply.replace("ZN65Q8", "ZN-65Q8"))

    def test_reset_only_on_whole_message(self):
        s = Session(self.store)
        self.assertIn("bütçe", s.handle("yeni bir televizyon istiyorum"))  # "yeni" inside a request
        self.assertIn("baştan", s.handle("yeni"))
        self.assertIn("Hangi ürünü", s.handle("merhaba"))

    def test_stale_products_are_not_recommended(self):
        from datetime import date
        reply = Session(self.store, as_of=date(2026, 12, 31)).handle("55 inç 25.000 TL televizyon")
        self.assertIn("kayıtlı ürün bulamadım", reply)

    def test_no_deal_says_so(self):
        reply = Session(self.store).handle("kulaklık 5.000 TL")
        self.assertIn("gerçek bir fırsat yok", reply)

    def test_reply_never_invents_specs(self):
        """Every 'label: value' in a spec line must exist verbatim in that product's stored listing specs."""
        listings = self.store.listings()
        by_title = {l["title"]: l for l in listings}
        label_to_key = {v: k for k, v in SPEC_LABELS.items()}
        for request in ("55 inç 25.000 TL televizyon", "laptop 60.000 TL", "telefon 30 bin", "kulaklık 10.000 TL"):
            reply = Session(self.store).handle(request)
            blocks = re.split(r"\n(?=\d+\. |- )", reply)
            checked = 0
            for b in blocks:
                title = next((t for t in by_title if t in b.split("\n")[0]), None)
                spec_line = next((ln for ln in b.split("\n") if "Özellikler (mağaza kaydından):" in ln), None)
                if not spec_line:
                    continue
                self.assertIsNotNone(title, b)
                stored = by_title[title]["specs"]
                for pair in spec_line.split(":", 1)[1].split(";"):
                    label, value = (x.strip() for x in pair.split(":", 1))
                    key = label_to_key[label]
                    self.assertEqual(spec_value(stored[key]), value, (title, label))
                    checked += 1
            self.assertGreater(checked, 0, request)

    def test_llm_extraction_is_validated(self):
        bad = lambda system, user: {"category": "uçak", "max_price": -5, "inches": "büyük", "specs": {"x": 1}}  # noqa
        n = parse_need_llm("bir şey", bad)
        self.assertEqual((n.category, n.max_price, n.inches), (None, None, None))
        good = lambda system, user: {"category": "tv", "max_price": 25000, "inches": 55}  # noqa: E731
        s = Session(self.store, chat=good)
        self.assertIn("gerçek fırsat", s.handle("yirmi beş binlik elli beşlik bir tele"))

    def test_telegram_loop_with_fake_api(self):
        sent = []

        def api(method, **p):
            if method == "getUpdates":
                return {"result": [{"update_id": 1, "message": {"chat": {"id": 9}, "text": "televizyon 25 bin 55 inç"}}]}
            sent.append(p)
            return {"ok": True}
        run_telegram(self.store, api=api, once=True)
        self.assertEqual(sent[0]["chat_id"], 9)
        self.assertIn("GERÇEK FIRSAT", sent[0]["text"])


if __name__ == "__main__":
    unittest.main()
