"""Cross-site product matching: deterministic rules first, an LLM only for pairs the rules cannot settle.

Rules return SAME, DIFF or UNSURE:
  DIFF   brand differs; accessory ("... için ...") or refurbished on one side only; variant words
         (Pro/Lite/Max/Air/...) differ; model codes differ; any spec both sides state differs
         (screen size in inch/cm, GB capacities, litres, colour, connector, 4G/5G, panel, resolution).
  SAME   same brand and same model code (dash/space ignored; a single trailing region letter like
         'T' ignored) with no conflicting spec.
  UNSURE no model code on one side; decided by the LLM, or, rules-only, by >= 3 equal specs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .text import fold

SAME, DIFF, UNSURE = "same", "diff", "unsure"

VARIANTS = {"pro", "lite", "max", "plus", "air", "mini", "ultra", "go", "slim", "studio", "fe", "neo", "se"}
COLOURS = {"siyah", "beyaz", "gri", "gumus", "inox", "mavi", "kirmizi", "yesil", "altin", "pembe", "mor", "lacivert",
           "siyahcam", "beyazcam"}
GROUPS = {  # mutually exclusive attribute values
    "connector": {"typec", "jack", "lightning"},
    "network": {"5g", "4g"},
    "panel": {"oled", "qled", "miniled", "led"},
    "resolution": {"hd", "fhd", "4k", "8k"},
}
ACCESSORY = re.compile(r"\bicin\b|\bkilif\b|\byedek\b|\bkumanda\b|\baski\s*aparati\b|\bstand\b|\bcontasi?\b|"
                       r"\blastigi?\b|\bfiltresi?\b|\bpedi?\b|\bsarj aleti\b|\bekran koruyucu\b")
REFURB = re.compile(r"\byenilenmis\b|\bteshir\b|\b2\.\s*el\b|\bikinci el\b")
NOT_CODE = re.compile(r"^(\d+(gb|tb|mb|l|lt|hz|w|mah|mm|cm)|[45]g|[ir][3579]|n\d{3}|i[3579]\d{4,5}[a-z]*|"
                      r"\d{4,5}[a-z]{1,2}|r[3579]\d{4}[a-z]*|\d+k|\d+x\d+)$")


@dataclass
class Features:
    brand: str | None
    codes: set[str]
    variants: set[str]
    inches: list[float]
    caps: list[int]
    litres: set[int]
    colours: set[str]
    attrs: dict[str, str]
    accessory: bool
    refurb: bool
    cpu: set[str]


def _prep(title: str) -> str:
    t = fold(title)
    for a, b in ((r"4k\s*ultra\s*hd|ultra\s*hd|\buhd\b", "4k"), (r"full\s*hd|\bfhd\b", "fhd"),
                 (r"mini\s*led", "miniled"), (r"type[\s-]*c", "typec"), (r"3[.,]5\s*mm(\s*jack)?|\bjack\b", "jack"),
                 (r"siyah\s*cam", "siyahcam"), (r"beyaz\s*cam", "beyazcam")):
        t = re.sub(a, b, t)
    return t


def features(title: str, brands: set[str], brand: str | None = None, model_code: str | None = None) -> Features:
    t = _prep(title)
    words = re.findall(r"[a-z0-9]+(?:[-.][a-z0-9]+)*", t)
    found_brand = fold(brand) if brand else next((w for w in words if w in brands), None)
    inches = [float(x.replace(",", ".")) for x in re.findall(r"(\d{1,2}(?:[.,]\d)?)\s*(?:\"|''|inc\b|inch\b|”)", t)]
    inches += [round(int(x) / 2.54, 1) for x in re.findall(r"(\d{2,3}(?:[.,]\d)?)\s*cm\b", t.replace(",", "."))
               if "." not in x] + [round(float(x) / 2.54, 1) for x in re.findall(r"(\d{2}\.\d)\s*cm\b", t)]
    inches += [round(int(x) / 2.54, 1) for x in re.findall(r"\b(\d{2,3})\s+ekran\b", t)]
    t = re.sub(r"\b(\d+)\s*/\s*(\d+)\s*(gb|tb)\b", r"\1 \3 \2 \3", t)  # "8/256 GB" -> "8 gb 256 gb"
    caps = sorted(int(n) * (1024 if u == "tb" else 1) for n, u in re.findall(r"(\d+)\s*(gb|tb)\b", t))
    litres = {int(n) for n in re.findall(r"(\d{2,4})\s*(?:l|lt|litre)\b", t)}
    cpu = {re.sub(r"[-\s]", "", c) for c in re.findall(r"\b((?:i[3579]|r[3579])[-\s]?\d{4,5}[a-z]*|\d{4}[a-z]{1,2}|n\d{3})\b", t)}
    attrs = {g: v for g, vals in GROUPS.items() for v in vals if re.search(rf"\b{v}\b", t)}
    codes = set()
    for w in words + ([fold(model_code)] if model_code else []):
        c = w.replace("-", "").replace(".", "")
        if re.search(r"[a-z]", c) and re.search(r"\d", c) and not NOT_CODE.match(c) and c not in GROUPS["network"]:
            codes.add(c)
    return Features(found_brand, codes, VARIANTS & set(words), inches, caps, litres,
                    COLOURS & set(words), attrs, bool(ACCESSORY.search(t)), bool(REFURB.search(t)), cpu)


def _code_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(long_) == len(short) + 1 and long_.startswith(short) and long_[-1].isalpha() and short[-1].isdigit()


def rule_decision(fa: Features, fb: Features) -> tuple[str, str]:
    if fa.brand and fb.brand and fa.brand != fb.brand:
        return DIFF, "marka farklı"
    if fa.accessory != fb.accessory:
        return DIFF, "biri aksesuar"
    if fa.refurb != fb.refurb:
        return DIFF, "biri yenilenmiş"
    if fa.variants != fb.variants:
        return DIFF, f"varyant farklı ({sorted(fa.variants ^ fb.variants)})"
    if fa.inches and fb.inches and not any(abs(x - y) <= 0.6 for x in fa.inches for y in fb.inches):
        return DIFF, "ekran boyutu farklı"
    ca, cb = set(fa.caps), set(fb.caps)
    if ca and cb and not (ca <= cb or cb <= ca):  # one side may omit RAM
        return DIFF, "kapasite farklı"
    if fa.litres and fb.litres and not fa.litres & fb.litres:
        return DIFF, "hacim farklı"
    if fa.colours and fb.colours and fa.colours != fb.colours:
        return DIFF, "renk farklı"
    if fa.cpu and fb.cpu and not fa.cpu & fb.cpu:
        return DIFF, "işlemci farklı"
    for g in GROUPS:
        if g in fa.attrs and g in fb.attrs and fa.attrs[g] != fb.attrs[g]:
            return DIFF, f"{g} farklı"
    if fa.codes and fb.codes:
        if any(_code_eq(a, b) for a in fa.codes for b in fb.codes):
            return SAME, "aynı marka ve model kodu"
        return DIFF, "model kodu farklı"
    return UNSURE, "model kodu bir tarafta yok"


def spec_fallback(fa: Features, fb: Features) -> bool:
    equal = sum([bool(fa.inches and fb.inches), bool(fa.caps and fb.caps), bool(fa.litres and fb.litres),
                 bool(fa.cpu and fb.cpu), bool(fa.brand and fa.brand == fb.brand)])
    return equal >= 3


LLM_SYSTEM = (
    "You decide whether two Turkish e-commerce listing titles are the SAME sellable product "
    "(same brand, model, variant, capacity, size and condition; seller wording, word order, warranty text, "
    "and colour mentioned on only one side do not matter; an accessory 'için' a product, a refurbished "
    "item, or a different capacity/size/variant is NOT the same). "
    'Answer only JSON: {"same": true|false, "reason": "<short>"}'
)


def llm_decision(title_a: str, title_b: str, chat=None) -> tuple[bool, str]:
    from .llm import chat_json
    out = (chat or chat_json)(LLM_SYSTEM, f"A: {title_a}\nB: {title_b}")
    return bool(out.get("same")), str(out.get("reason", ""))[:200]


def decide(title_a: str, title_b: str, brands: set[str], mode: str = "rules", chat=None,
           meta_a: dict | None = None, meta_b: dict | None = None) -> tuple[bool, str, str]:
    """-> (same?, decided_by, reason). mode: 'rules' | 'rules+llm' | 'llm'."""
    if mode == "llm":
        same, why = llm_decision(title_a, title_b, chat)
        return same, "llm", why
    fa = features(title_a, brands, **(meta_a or {}))
    fb = features(title_b, brands, **(meta_b or {}))
    d, why = rule_decision(fa, fb)
    if d != UNSURE:
        return d == SAME, "rules", why
    if mode == "rules+llm":
        same, why = llm_decision(title_a, title_b, chat)
        return same, "llm", why
    return spec_fallback(fa, fb), "rules-fallback", why


def group_listings(listings: list[dict], mode: str = "rules", chat=None) -> dict[int, str]:
    """Union-find over same-category listing pairs. -> {listing_id: product_key}."""
    brands = {fold(l["brand"]) for l in listings if l.get("brand")}
    parent = {l["id"]: l["id"] for l in listings}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(listings):
        for b in listings[i + 1:]:
            if a.get("category") != b.get("category") or a["source"] == b["source"] or find(a["id"]) == find(b["id"]):
                continue
            meta = lambda l: {"brand": l.get("brand"), "model_code": l.get("model_code")}  # noqa: E731
            same, _, _ = decide(a["title"], b["title"], brands, mode, chat, meta(a), meta(b))
            if same:
                parent[find(b["id"])] = find(a["id"])
    return {i: f"P{find(i)}" for i in parent}
