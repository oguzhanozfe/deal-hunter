"""Crafted histories for the deal rule. README examples mirror these tests."""
import unittest
from datetime import date, timedelta

from deal_hunter.deals import COSMETIC, DEAL, FAKE, INSUFFICIENT, NORMAL, DealConfig, Obs, analyze

TODAY = date(2026, 11, 15)


def history(prices_by_age: dict[int, float], **today_kw) -> list[Obs]:
    """{days_ago: price}; days_ago=0 is today and may carry list_price."""
    out = []
    for age, p in sorted(prices_by_age.items(), reverse=True):
        kw = today_kw if age == 0 else {}
        out.append(Obs(TODAY - timedelta(days=age), p, **kw))
    return out


def steady(price: float, days: int = 180, every: int = 3, wobble: float = 0.01) -> dict[int, float]:
    # deterministic +-1% wobble
    return {age: round(price * (1 + wobble * ((age * 7) % 5 - 2) / 2)) for age in range(days, 0, -every)}


class DealRuleTests(unittest.TestCase):
    def test_steady_price_with_10pct_dip_is_not_a_deal(self):
        # Owner's example: "10% off, but it has been around that price for 6 months".
        h = steady(25_000)
        for age in (150, 120, 90, 60, 30):  # it has sold at ~22.5k before
            h[age] = 22_500
        h[0] = 22_500
        v = analyze(history(h, list_price=24_999))
        self.assertNotEqual(v.verdict, DEAL)
        self.assertEqual(v.verdict, COSMETIC)

    def test_long_run_price_minus_35pct_is_a_deal(self):
        h = steady(33_000)
        h[0] = 21_450
        v = analyze(history(h, list_price=33_000))
        self.assertEqual(v.verdict, DEAL)
        self.assertAlmostEqual(v.stats["depth"], 0.35, delta=0.01)
        self.assertGreater(v.score, 30)
        self.assertEqual(v.flags, [])

    def test_inflate_then_cut_is_fake(self):
        h = steady(20_000)
        for age in range(21, 0, -1):  # raised to 26k for the last 3 weeks
            h[age] = 26_000
        h[0] = 19_500  # "25% off!"
        v = analyze(history(h, list_price=25_999))
        self.assertEqual(v.verdict, FAKE)
        self.assertTrue(any("şişirilmiş" in f for f in v.flags))

    def test_list_price_never_charged_is_fake(self):
        h = steady(23_000)
        h[0] = 22_499
        v = analyze(history(h, list_price=34_999))
        self.assertEqual(v.verdict, FAKE)
        self.assertTrue(any("hiç uygulanmamış" in f for f in v.flags))

    def test_real_deal_with_fake_list_price_stays_deal_with_warning(self):
        h = steady(42_000)
        h[0] = 27_000
        v = analyze(history(h, list_price=59_999))
        self.assertEqual(v.verdict, DEAL)
        self.assertTrue(any("hiç uygulanmamış" in f for f in v.flags))

    def test_refuses_to_judge_short_history(self):
        h = steady(30_000, days=40)
        h[0] = 15_000
        self.assertEqual(analyze(history(h)).verdict, INSUFFICIENT)

    def test_refuses_to_judge_sparse_history(self):
        h = {170: 30_000, 100: 30_000, 40: 30_000, 0: 15_000}  # long span, 3 observations
        self.assertEqual(analyze(history(h)).verdict, INSUFFICIENT)

    def test_price_at_median_is_normal(self):
        h = steady(16_500)
        h[0] = 16_500
        self.assertEqual(analyze(history(h)).verdict, NORMAL)

    def test_deep_but_not_rare_in_volatile_history_is_not_a_deal(self):
        # swings between 20k and 40k: 26k is -32% vs a 38k-ish median but seen often
        h = {age: (20_000 if (age // 9) % 3 == 0 else 40_000) for age in range(180, 0, -3)}
        h[0] = 19_900
        v = analyze(history(h))
        self.assertNotEqual(v.verdict, DEAL, v.stats)

    def test_snapshot_burst_does_not_drag_median(self):
        h = [Obs(TODAY - timedelta(days=a), 30_000) for a in range(180, 0, -10)]
        sale_day = TODAY - timedelta(days=100)
        h += [Obs(sale_day, 20_000, listing=str(i)) for i in range(50)]  # 50 snapshots on one sale day
        h.append(Obs(TODAY, 30_000))
        v = analyze(h)
        self.assertEqual(v.stats["median_180"], 30_000)

    def test_out_of_stock_rows_are_ignored(self):
        h = history(steady(30_000))
        h += [Obs(TODAY - timedelta(days=a), 5_000, in_stock=False) for a in range(100, 0, -2)]
        h.append(Obs(TODAY, 30_000))
        v = analyze(h)
        self.assertEqual(v.stats["min_180"] > 25_000, True)
        self.assertEqual(v.verdict, NORMAL)

    def test_cross_store_uses_cheapest_daily_price(self):
        a = [Obs(TODAY - timedelta(days=d), 30_000, listing="a") for d in range(180, -1, -4)]
        b = [Obs(TODAY - timedelta(days=d), 31_000, listing="b") for d in range(178, 0, -4)]
        b.append(Obs(TODAY, 19_000, listing="b"))
        v = analyze(a + b)
        self.assertEqual(v.current, 19_000)
        self.assertEqual(v.stats["median_180"], 30_000)
        self.assertEqual(v.verdict, DEAL)

    def test_threshold_is_configurable(self):
        h = steady(30_000)
        h[0] = 22_500  # -25%
        self.assertNotEqual(analyze(history(h)).verdict, DEAL)
        self.assertEqual(analyze(history(h), DealConfig(min_depth=0.20)).verdict, DEAL)


if __name__ == "__main__":
    unittest.main()
