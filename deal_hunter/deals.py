"""Deal scoring: is today's price a real deal against the product's own history?

Rule (defaults in DealConfig):
  0. "Current" means: the latest row of each listing, if it is in stock and at most
     `max_age_days` old relative to `as_of` (default: the newest row of any listing).
     No such row -> GÜNCEL FİYAT YOK (no current price; never recommended).
  1. Refuse to judge without enough history (span and number of observed days).
  2. Build a daily price series: out-of-stock rows dropped, several snapshots on one
     day collapse to that day's minimum, and each price is carried forward until the
     next observation (max `ffill_days`). This time-weights irregular snapshots, so a
     burst of snapshots during one sale week cannot drag the median down.
     With several listings (stores) of one product, the daily price is the minimum
     across listings.
  3. Compare the current price with the 30/90/180-day median, min and percentiles of
     the history *before* today.
  4. GERÇEK FIRSAT (real deal): at least `min_depth` below the 180-day median AND
     rare: on at most `rare_share` of the last 180 days was the price at or below
     today's price (+`rare_tolerance`).
  5. SAHTE İNDİRİM (fake discount): not a real deal, a claimed discount of at least
     `claim_min` exists, and either
       - the price was raised in the last 30 days to a level not seen in the 150 days before
         (30-day peak >= median * (1+inflate_pct) and >= the earlier peak * (1+new_high_pct)),
         so the "discount" is measured from an inflated price, or
       - the struck-through list price was never actually charged in 180 days.
     A real deal that also shows a never-charged list price stays a deal; the flag is
     reported as a warning.
  6. KOZMETİK İNDİRİM: below the median, but not deep or not rare enough.
  7. Otherwise NORMAL.
Score (0-100) = depth below 180-day median x history-length factor / (1 + volatility),
where volatility is the coefficient of variation of the 180-day history.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

DEAL = "GERÇEK FIRSAT"
FAKE = "SAHTE İNDİRİM"
COSMETIC = "KOZMETİK İNDİRİM"
NORMAL = "NORMAL"
INSUFFICIENT = "YETERSİZ VERİ"
STALE = "GÜNCEL FİYAT YOK"


@dataclass(frozen=True)
class DealConfig:
    min_depth: float = 0.30          # >= 30% below the 180-day median
    rare_share: float = 0.10         # and the history was this low on at most 10% of days
    rare_tolerance: float = 0.03     # "this low" = within 3% above today's price
    min_history_days: int = 90       # history must span this many days before today
    min_observed_days: int = 8       # with at least this many days that have a real observation
    ffill_days: int = 14             # carry a price forward at most this long
    inflate_pct: float = 0.15        # 30-day peak this far above the median = "inflated"
    new_high_pct: float = 0.05       # ...and this far above the peak of days 31-180 (a new high, not a usual swing)
    max_age_days: int = 3            # a "current" price must be at most this old
    claim_min: float = 0.10          # a claimed discount smaller than this is ignored
    list_tolerance: float = 0.02     # list price within 2% of a charged price counts as charged
    cosmetic_min: float = 0.03       # below median by less than this is just noise


@dataclass(frozen=True)
class Obs:
    day: date
    price: float
    list_price: float | None = None
    in_stock: bool = True
    listing: str = ""


@dataclass
class Verdict:
    verdict: str
    score: float = 0.0
    current: float | None = None
    current_day: date | None = None
    stats: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def is_deal(self) -> bool:
        return self.verdict == DEAL


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile, p in [0, 100]."""
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def daily_series(obs: list[Obs], until: date, ffill_days: int) -> dict[date, float]:
    """Per-day price before `until`: per-listing daily min, carried forward, min across listings."""
    by_listing: dict[str, dict[date, float]] = {}
    for o in obs:
        if not o.in_stock or o.day >= until:
            continue
        d = by_listing.setdefault(o.listing, {})
        d[o.day] = min(o.price, d.get(o.day, o.price))
    merged: dict[date, float] = {}
    for days in by_listing.values():
        ordered = sorted(days)
        for i, day in enumerate(ordered):
            nxt = ordered[i + 1] if i + 1 < len(ordered) else until
            end = min(nxt, day + timedelta(days=ffill_days + 1), until)
            cur = day
            while cur < end:
                merged[cur] = min(days[day], merged.get(cur, days[day]))
                cur += timedelta(days=1)
    return merged


def _window(series: dict[date, float], today: date, days: int) -> list[float]:
    start = today - timedelta(days=days)
    return [p for d, p in series.items() if start <= d < today]


def current_offers(obs: list[Obs], as_of: date, max_age: int) -> list[Obs]:
    """Latest row per listing, kept only if in stock and fresh."""
    latest: dict[str, Obs] = {}
    for o in obs:
        if o.listing not in latest or o.day >= latest[o.listing].day:
            latest[o.listing] = o
    return [o for o in latest.values() if o.in_stock and (as_of - o.day).days <= max_age]


def analyze(obs: list[Obs], cfg: DealConfig = DealConfig(), as_of: date | None = None) -> Verdict:
    if not obs:
        return Verdict(STALE, reasons=["Hiç fiyat gözlemi yok."])
    as_of = as_of or max(o.day for o in obs)
    offers = current_offers(obs, as_of, cfg.max_age_days)
    if not offers:
        return Verdict(STALE, reasons=[f"{as_of.isoformat()} itibarıyla stokta olan güncel bir fiyat yok "
                                       f"(son {cfg.max_age_days} gün)."])
    cur_obs = min(offers, key=lambda o: o.price)
    cur, today = cur_obs.price, cur_obs.day
    stocked = [o for o in obs if o.in_stock]

    series = daily_series(obs, today, cfg.ffill_days)
    observed_days = {o.day for o in stocked if o.day < today and o.day >= today - timedelta(days=180)}
    span = (today - min(series)).days if series else 0
    if span < cfg.min_history_days or len(observed_days) < cfg.min_observed_days:
        return Verdict(
            INSUFFICIENT, current=cur, current_day=today,
            stats={"history_days": span, "observed_days": len(observed_days)},
            reasons=[f"Geçmiş çok kısa: {span} gün / {len(observed_days)} gözlem günü "
                     f"(en az {cfg.min_history_days} gün ve {cfg.min_observed_days} gözlem günü gerekir). "
                     "Karar verilmedi."],
        )

    stats: dict = {"history_days": span, "observed_days": len(observed_days)}
    for w in (30, 90, 180):
        vals = _window(series, today, w)
        if vals:
            stats[f"median_{w}"] = statistics.median(vals)
            stats[f"min_{w}"] = min(vals)
            stats[f"max_{w}"] = max(vals)
            stats[f"p10_{w}"] = percentile(vals, 10)
            stats[f"p25_{w}"] = percentile(vals, 25)
    h180 = _window(series, today, 180)
    med = stats["median_180"]
    depth = 1 - cur / med
    cv = statistics.pstdev(h180) / statistics.fmean(h180)
    share_low = sum(p <= cur * (1 + cfg.rare_tolerance) for p in h180) / len(h180)
    stats.update(depth=depth, volatility=cv, share_days_this_low=share_low)
    score = max(0.0, depth) * min(1.0, span / 180) / (1 + cv) * 100

    flags, claims = [], [0.0]
    peak30 = stats.get("max_30", med)
    earlier = [p for d, p in series.items() if today - timedelta(days=180) <= d < today - timedelta(days=30)]
    new_high = not earlier or peak30 >= max(earlier) * (1 + cfg.new_high_pct)
    if peak30 >= med * (1 + cfg.inflate_pct) and new_high:
        claims.append(1 - cur / peak30)
        if 1 - cur / peak30 >= cfg.claim_min:
            flags.append(f"Son 30 günde fiyat {peak30:,.0f} TL'ye çıkarılmış (180 günlük medyan {med:,.0f} TL); "
                         "indirim bu şişirilmiş fiyattan hesaplanıyor.".replace(",", "."))
    lp = cur_obs.list_price
    if lp and lp > cur:
        claims.append(1 - cur / lp)
        if lp > stats["max_180"] * (1 + cfg.list_tolerance):
            flags.append(f"Üstü çizili liste fiyatı ({lp:,.0f} TL) son 180 günde hiç uygulanmamış; "
                         f"görülen en yüksek fiyat {stats['max_180']:,.0f} TL.".replace(",", "."))
    claimed = max(claims)

    v = Verdict(NORMAL, score=round(score, 1), current=cur, current_day=today, stats=stats, flags=flags)
    pct = f"%{depth * 100:.0f}"
    rare = share_low <= cfg.rare_share
    if depth >= cfg.min_depth and rare:
        v.verdict = DEAL
        v.reasons.append(f"Fiyat 180 günlük medyanın {pct} altında; son 180 günde bu fiyat veya altı "
                         f"görülen gün oranı yalnızca %{share_low * 100:.0f}.")
    elif flags and claimed >= cfg.claim_min:
        v.verdict = FAKE
        v.reasons.append(f"Görünen indirim %{claimed * 100:.0f}, ama 180 günlük medyana göre fark yalnızca {pct}.")
    elif depth >= cfg.cosmetic_min:
        v.verdict = COSMETIC
        why = (f"bu fiyat veya altı son 180 günde günlerin %{share_low * 100:.0f} kadarında görülmüş" if not rare
               else f"eşik %{cfg.min_depth * 100:.0f}")
        v.reasons.append(f"Medyanın {pct} altında ama gerçek fırsat değil ({why}).")
    else:
        v.reasons.append(f"Fiyat 180 günlük medyana yakın ({pct}).")
    return v
