"""Zero-key demo: deal verdicts on the bundled synthetic histories, then a scripted Turkish chat.

The deal rules need no model. The chat's optional LLM (it only turns a free-form message into
{category, max_price, inches}) is resolved automatically:
  1. GEMINI_API_KEY / DEEPSEEK_API_KEY set  -> that cloud provider
  2. local Ollama reachable with the model    -> local
  3. otherwise                                -> replay of fixtures/demo_chat_recording.json
`--replay` forces 3 (CI does this). A live failure (429, timeout) falls back to replay.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .bot import Session, describe
from .deals import (COSMETIC, DEAL, FAKE, INSUFFICIENT, NORMAL, STALE, DealConfig, analyze,
                    current_offers)
from .llm import BACKENDS, LLMError, chat_json, resolve
from .matching import group_listings
from .sources import CsvSource
from .store import Store

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
RECORDING = FIXTURES / "demo_chat_recording.json"
# Turn 1 is fully parsed by the rules; turn 3 has a spelled-out budget only a model can read.
SCRIPT = ["55 inç TV, 25.000 TL altı", "yeni", "Salona büyük ekran bir televizyon lazım, bütçem otuz bini geçmesin",
          "65 inç olsun"]


def load_store() -> Store:
    store = Store()
    store.add(CsvSource(str(FIXTURES / "synthetic_history.csv")).fetch())
    store.set_product_keys(group_listings(store.listings()))
    return store


def products(store: Store, cfg: DealConfig, category: str | None = None):
    """-> [(verdict, best current listing, listings, observations)] for every product."""
    as_of = store.latest_day()
    out = []
    for listings in store.products(category).values():
        obs = store.history([l["id"] for l in listings])
        v = analyze(obs, cfg, as_of)
        by_id = {str(l["id"]): l for l in listings}
        cur = current_offers(obs, as_of, cfg.max_age_days)
        best = by_id[min(cur, key=lambda o: o.price).listing] if cur else min(listings, key=lambda l: l["id"])
        out.append((v, best, listings, obs))
    return out


ORDER = [DEAL, COSMETIC, FAKE, NORMAL, INSUFFICIENT, STALE]


def tv55(store: Store, cfg: DealConfig):
    """The 55-inch TVs (one real deal, one cosmetic, two kinds of fake, one too new to judge)."""
    ps = [p for p in products(store, cfg, "tv") if p[1]["specs"].get("ekran_inc") == 55]
    return sorted(ps, key=lambda p: (ORDER.index(p[0].verdict), -p[0].score))


def _key(system: str, user: str) -> str:
    return hashlib.sha256(json.dumps([system, user], ensure_ascii=False).encode()).hexdigest()


def _ollama_has(model: str) -> bool:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=2) as r:
            return any(m["name"] == model for m in json.loads(r.read())["models"])
    except (OSError, ValueError, KeyError):
        return False


class DemoChat:
    """LLM callable for the demo: live model if one is available, else the committed recording."""

    def __init__(self, force_replay: bool = False):
        rec = json.loads(RECORDING.read_text(encoding="utf-8")) if RECORDING.exists() else {"pairs": []}
        self.recorded = rec
        self.replay = {p["key"]: p["response"] for p in rec["pairs"]}
        self.live, self.misses, self.fallbacks, self.log = None, 0, 0, []
        if not force_replay:
            if any(os.environ.get(BACKENDS[b][1]) for b in ("gemini", "deepseek")):
                self.live = resolve()[:3:2]
            elif _ollama_has(os.environ.get("DEAL_HUNTER_MODEL") or BACKENDS["ollama"][2]):
                self.live = resolve("ollama")[:3:2]

    @property
    def label(self) -> str:
        if self.live:
            return f"mode: live ({self.live[0]}/{self.live[1]})"
        r = self.recorded
        return f"mode: replay (recorded {r.get('backend')}/{r.get('model')}, {r.get('recorded_at', '?')[:10]})"

    def __call__(self, system: str, user: str) -> dict:
        key = _key(system, user)
        if self.live:
            try:
                out = chat_json(system, user, backend=self.live[0], model=self.live[1], timeout=40, retries=0)
                self.log.append({"key": key, "user": user, "response": out})
                return out
            except LLMError as e:  # quota or outage: never loop, use the recording
                print(f"(canlı model hatası, kayda geçiliyor: {str(e)[:80]})")
                self.live, self.fallbacks = None, self.fallbacks + 1
        if key in self.replay:
            return self.replay[key]
        self.misses += 1
        raise LLMError("message not in the recording")

    def save_recording(self) -> None:
        b, m = self.live
        RECORDING.write_text(json.dumps({
            "note": "Recorded by `python -m deal_hunter demo --record`; replayed when no model is available.",
            "backend": b, "model": m, "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pairs": self.log}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def run(force_replay: bool = False, record: bool = False, cfg: DealConfig = DealConfig()) -> list[tuple[str, str, str]]:
    """Print the demo; return the chat transcript as (user, parsed_by, reply) for the Pages builder."""
    from .__main__ import deals_table
    store = load_store()
    print("== 1. Fiyat geçmişinden karar (sentetik veri, model gerekmez) ==\n" + deals_table(store, None, cfg) + "\n")
    print("== 2. Gerekçeler: 55 inç televizyonlar ==")
    for v, best, listings, _ in tv55(store, cfg):
        print(describe(v, best, listings) + "\n")
    chat = DemoChat(force_replay)
    print(f"== 3. Öneri botu ile örnek sohbet ==\n[{chat.label}; model yalnızca kuralların okuyamadığı mesajı ayrıştırır]\n")
    s = Session(store, cfg, chat=chat)
    transcript = []
    for msg in SCRIPT:
        reply = s.handle(msg)
        by = "kurallar" if s.llm_parse is None else f"kurallar + model {json.dumps(s.llm_parse, ensure_ascii=False)}"
        print(f"> {msg}\n[ayrıştırma: {by}]\n{reply}\n")
        transcript.append((msg, by, reply))
    if chat.misses:
        print(f"UYARI: {chat.misses} mesaj kayıtta yok; model yanıtı olmadan devam edildi.")
    if record:
        if not chat.live or chat.fallbacks:
            raise SystemExit("--record needs a working live model")
        chat.save_recording()
        print(f"Kayıt yazıldı: {RECORDING.name} ({len(chat.log)} yanıt)")
    return transcript
