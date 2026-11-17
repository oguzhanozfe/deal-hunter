"""Turkish recommendation chat: asks clarifying questions, then recommends from current genuine deals.

No-invention guarantee: the optional LLM only turns the user's message into constraints (JSON,
validated). Every sentence in a reply is a template filled from stored listing fields and the
deal analysis; specs are printed verbatim from the listing's stored `specs`, never generated.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .deals import DEAL, FAKE, INSUFFICIENT, DealConfig, analyze
from .store import Store
from .text import fmt_tl, fold, parse_amount, parse_inches

CATEGORY_WORDS = {  # telefon first: "tele" is a prefix of "telefon"
    "telefon": ("telefon", "cep"),
    "tv": ("tv", "televizyon", "tele"),
    "laptop": ("laptop", "dizustu", "notebook", "bilgisayar"),
    "kulaklik": ("kulaklik",),
}
CATEGORY_NAMES = {"tv": "televizyon", "laptop": "dizüstü bilgisayar", "telefon": "telefon", "kulaklik": "kulaklık"}
SPEC_LABELS = {"ekran_inc": "Ekran (inç)", "cozunurluk": "Çözünürlük", "panel": "Panel", "yenileme_hz": "Yenileme (Hz)",
               "smart": "Akıllı TV", "ram_gb": "RAM (GB)", "depolama_gb": "Depolama (GB)", "islemci": "İşlemci",
               "tip": "Tip", "anc": "Aktif gürültü engelleme"}
ANY = "any"
NO_PREF = re.compile(r"fark\s*etmez|farketmez|hepsi|onemli degil|herhangi")


def spec_value(v) -> str:
    return {True: "var", False: "yok"}.get(v, str(v)) if isinstance(v, bool) else str(v)


@dataclass
class Need:
    category: str | None = None
    max_price: float | None = None
    inches: float | str | None = None  # number, ANY, or None (not asked yet)


def parse_need(text: str, asked: str | None = None) -> Need:
    t = fold(text)
    n = Need()
    for cat, words in CATEGORY_WORDS.items():
        if any(re.search(rf"\b{w}", t) for w in words):
            n.category = cat
            break
    n.max_price = parse_amount(text)
    n.inches = parse_inches(text)
    bare = re.fullmatch(r"\s*(\d+(?:[.,]\d+)*)\s*", t)
    if asked == "budget" and n.max_price is None and bare:
        n.max_price = float(bare.group(1).replace(".", "").replace(",", "."))
    if asked == "inches" and n.inches is None:
        if bare:
            n.inches = float(bare.group(1).replace(",", "."))
        elif NO_PREF.search(t):
            n.inches = ANY
    return n


LLM_SYSTEM = (
    "Extract a shopping request into JSON. Keys: category (one of tv, laptop, telefon, kulaklik, or null), "
    "max_price (number in TL or null), inches (screen size number or null). Only use what the user said. "
    'Example: "55 inç 25 bin altı televizyon" -> {"category":"tv","max_price":25000,"inches":55}'
)


def parse_need_llm(text: str, chat) -> Need:
    """LLM extraction, validated; anything outside the allowed shape is dropped."""
    try:
        d = chat(LLM_SYSTEM, text)
    except Exception:  # noqa: BLE001 - the rules parser still works without the LLM
        return Need()
    n = Need()
    if d.get("category") in CATEGORY_WORDS:
        n.category = d["category"]
    for key in ("max_price", "inches"):
        v = d.get(key)
        if isinstance(v, (int, float)) and 0 < v < 10_000_000:
            setattr(n, key, float(v))
    return n


@dataclass
class Session:
    store: Store
    cfg: DealConfig = field(default_factory=DealConfig)
    chat: object | None = None  # LLM callable (system, user) -> dict, optional
    need: Need = field(default_factory=Need)
    asked: str | None = None

    def handle(self, text: str) -> str:
        if re.search(r"\b(yeni|sifirla|bastan)\b", fold(text)):
            self.need, self.asked = Need(), None
            return "Tamam, baştan başlayalım. Ne arıyorsunuz?"
        got = parse_need(text, self.asked)
        if self.chat and (got.category is None or got.max_price is None):
            extra = parse_need_llm(text, self.chat)
            for k in ("category", "max_price", "inches"):
                if getattr(got, k) is None:
                    setattr(got, k, getattr(extra, k))
        for k in ("category", "max_price", "inches"):
            if getattr(got, k) is not None:
                setattr(self.need, k, getattr(got, k))
        n = self.need
        if n.category is None:
            self.asked = "category"
            return "Hangi ürünü arıyorsunuz? (televizyon, dizüstü bilgisayar, telefon, kulaklık)"
        if n.max_price is None:
            self.asked = "budget"
            return f"{CATEGORY_NAMES[n.category].capitalize()} için bütçe üst sınırınız nedir? (örn. 25.000 TL)"
        if n.category == "tv" and n.inches is None:
            self.asked = "inches"
            return "Kaç inç ekran istiyorsunuz? (örn. 55; fark etmezse \"fark etmez\" yazın)"
        self.asked = None
        return recommend(self.store, n, self.cfg)


def _candidates(store: Store, need: Need, cfg: DealConfig):
    for listings in store.products(need.category).values():
        v = analyze(store.history([l["id"] for l in listings]), cfg)
        if v.current is None:
            continue
        by_id = {str(l["id"]): l for l in listings}
        today = [o for o in store.history(list(map(int, by_id))) if o.day == v.current_day and o.in_stock]
        best = by_id[min(today, key=lambda o: o.price).listing]
        if v.current > need.max_price:
            continue
        if isinstance(need.inches, float):
            size = best["specs"].get("ekran_inc")
            if size is None or abs(float(size) - need.inches) > 0.6:
                continue  # size unknown in stored data -> do not guess
        yield v, best, listings


def describe(v, best: dict, listings: list[dict], rank: int | None = None) -> str:
    s = v.stats
    head = f"{rank}. " if rank else "- "
    tag = " [sentetik veri]" if best.get("synthetic") else ""
    lines = [f"{head}{best['title']} - {fmt_tl(v.current)} ({best['source']}){tag}"]
    lines.append(f"   Karar: {v.verdict}" + (f" (puan {v.score:.0f}/100)" if v.verdict == DEAL else ""))
    lines += [f"   Neden: {r}" for r in v.reasons]
    if v.verdict != INSUFFICIENT:
        lines.append(f"   Kanıt: 180 gün medyan {fmt_tl(s['median_180'])}, 90 gün medyan {fmt_tl(s['median_90'])}, "
                     f"180 gün en düşük {fmt_tl(s['min_180'])}, en yüksek {fmt_tl(s['max_180'])}; "
                     f"{s['history_days']} günlük geçmiş, {s['observed_days']} gözlem günü, {len(listings)} mağaza.")
    lines += [f"   Uyarı: {f}" for f in v.flags]
    if best["specs"]:
        specs = "; ".join(f"{SPEC_LABELS.get(k, k)}: {spec_value(val)}" for k, val in best["specs"].items())
        lines.append(f"   Özellikler (mağaza kaydından): {specs}")
    return "\n".join(lines)


def recommend(store: Store, need: Need, cfg: DealConfig = DealConfig(), limit: int = 3) -> str:
    cands = list(_candidates(store, need, cfg))
    deals = sorted((c for c in cands if c[0].verdict == DEAL), key=lambda c: -c[0].score)
    others = sorted((c for c in cands if c[0].verdict != DEAL), key=lambda c: c[0].current)
    size = f"{need.inches:g} inç " if isinstance(need.inches, float) else ""
    what = f"{size}{CATEGORY_NAMES[need.category]}, en fazla {fmt_tl(need.max_price)}"
    out = []
    if deals:
        out.append(f"{what} için {len(deals)} gerçek fırsat buldum:\n")
        out += [describe(*c, rank=i + 1) for i, c in enumerate(deals[:limit])]
    else:
        out.append(f"{what}: şu an kayıtlı fiyat geçmişine göre gerçek bir fırsat yok.")
    if others:
        out.append("\nBütçenize uyan ama fırsat sayılmayan ürünler:")
        out += [describe(*c) for c in others[:limit + 2]]
        if any(c[0].verdict == FAKE for c in others):
            out.append("\nNot: 'SAHTE İNDİRİM' olanlarda görünen indirim gerçek fiyat geçmişiyle desteklenmiyor.")
    if not cands:
        out.append("Bu kriterlere uyan kayıtlı ürün bulamadım. Bütçeyi veya ekran boyutunu değiştirmek ister misiniz?")
    out.append(f"\n(Kural: 180 günlük medyanın en az %{cfg.min_depth * 100:.0f} altında ve nadir görülen fiyat.)")
    return "\n".join(out)


# ---------- optional Telegram front-end (long polling, stdlib only) ----------

def telegram_api(token: str):
    def call(method: str, **params):
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data, timeout=40) as r:
            return json.loads(r.read())
    return call


def run_telegram(store: Store, api=None, chat=None, once: bool = False) -> None:
    """Needs TELEGRAM_BOT_TOKEN. Optional TELEGRAM_ALLOWED_CHAT_IDS=comma list restricts who may use it."""
    api = api or telegram_api(os.environ["TELEGRAM_BOT_TOKEN"])
    allowed = {s.strip() for s in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if s.strip()}
    sessions: dict[int, Session] = {}
    offset = 0
    while True:
        try:
            updates = api("getUpdates", offset=offset, timeout=30).get("result", [])
        except OSError:
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            chat_id, text = (msg.get("chat") or {}).get("id"), msg.get("text")
            if chat_id is None or not text or (allowed and str(chat_id) not in allowed):
                continue
            s = sessions.setdefault(chat_id, Session(store, chat=chat))
            reply = "Merhaba! Ne arıyorsunuz? Örn: \"55 inç, 25.000 TL altı televizyon\"" if text == "/start" \
                else s.handle(text)
            api("sendMessage", chat_id=chat_id, text=reply[:4000])
        if once:
            return
