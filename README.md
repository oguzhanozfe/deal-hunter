# deal-hunter

A Turkish e-commerce deal detector that separates a **real deal** from a **cosmetic or fake discount** by comparing today's price with the product's own price history. On top of it sits a Turkish recommendation chat (CLI, optionally Telegram).

The question it answers: *"This 55-inch TV is 24% off. Is that real?"* A TV that is 10% off but has sold around that price for the last six months is not a deal. A TV that is 35% below its long-run price, at a level it has rarely reached, is one.

Stdlib-only Python 3.10+, SQLite, and an optional LLM: Gemini by default, DeepSeek or a local Ollama as alternatives. Tests and CI need no key and no model.

> **Data note.** Product prices in the demo are **synthetic** (fictional brands and stores, `synthetic=1` on every row). The one real collection run is from Open Prices (see below), which does not cover electronics. Read [Data access](#data-access-decisions-per-site) before pointing this at any real site.

## Quick start

```bash
python -m unittest -v              # 45 offline tests
python -m deal_hunter demo         # verdicts for the synthetic fixture + a scripted chat
```

Using your own data:

```bash
python -m deal_hunter import-csv my_prices.csv     # columns: see fixtures/synthetic_history.csv
python -m deal_hunter match                        # merge the same product across sources
python -m deal_hunter deals --category tv
python -m deal_hunter chat                         # Turkish chat; add --llm to let an LLM parse requests
TELEGRAM_BOT_TOKEN=... python -m deal_hunter chat --telegram   # optional
```

## The deal rule

All thresholds are in `DealConfig` ([deal_hunter/deals.py](deal_hunter/deals.py)); `--min-depth` changes the main one from the CLI.

0. **Only a fresh, in-stock price counts as "current".** The current price comes from each listing's latest row, if it is in stock and at most 3 days old relative to "now" (`--as-of`, default: the newest price in the database). Otherwise the verdict is `GÜNCEL FİYAT YOK` (no current price) and the product is never recommended.
1. **Refuse to judge on thin history.** It needs at least 90 days of history and 8 days with a real observation. Otherwise the verdict is `YETERSİZ VERİ` (insufficient data).
2. **Build a fair daily series.** Out-of-stock rows are dropped. Several snapshots on one day collapse to that day's minimum, and each price is carried forward until the next observation (at most 14 days). Snapshots are irregular, so this time-weighting stops a burst of snapshots during one sale week from dragging the median down. When a product is sold by several stores, the daily price is the cheapest store.
3. **Compare today's price with the 30/90/180-day median, min, max and percentiles** of the history before today.
4. **`GERÇEK FIRSAT` (real deal):** at least **30% below the 180-day median** *and* rare. On at most 10% of the last 180 days was the price at or below today's price (+3%).
5. **`SAHTE İNDİRİM` (fake discount):** not a real deal, the claimed discount is at least 10%, and either
   - the price was **raised in the last 30 days** to a new high (30-day peak ≥ median +15% *and* ≥ 5% above the peak of the 150 days before), so the "discount" is measured from an inflated price. A product that always swings does not trip this on one of its usual peaks. Or:
   - the **struck-through list price was never actually charged** in the last 180 days.

   A real deal that also shows an inflated list price stays a deal and carries a warning.
6. **`KOZMETİK İNDİRİM` (cosmetic):** at least 3% below the median, but not deep enough or not rare enough.
7. Otherwise **`NORMAL`**.

**Score (0–100)** = depth below the 180-day median × min(1, history days / 180) ÷ (1 + coefficient of variation of the 180-day history). A deep cut on a long, stable history scores higher than the same cut on a short or jumpy one. The verdict, not the score, decides what is called a deal.

### Examples (each one is a unit test in [tests/test_deals.py](tests/test_deals.py))

| History | Today | Verdict | Test |
|---|---|---|---|
| ~25,000 TL for 180 days, sold at 22,500 five times | 22,500 (list 24,999) | KOZMETİK İNDİRİM. 10% below the median, but seen before | `test_steady_price_with_10pct_dip_is_not_a_deal` |
| ~33,000 TL for 180 days | 21,450 | GERÇEK FIRSAT (−35%) | `test_long_run_price_minus_35pct_is_a_deal` |
| ~20,000 TL, raised to 26,000 for the last 3 weeks | 19,500 ("25% off") | SAHTE İNDİRİM: inflate-then-cut | `test_inflate_then_cut_is_fake` |
| ~23,000 TL for 180 days | 22,499 with list price 34,999 | SAHTE İNDİRİM: list price never charged | `test_list_price_never_charged_is_fake` |
| ~42,000 TL for 180 days | 27,000 with list price 59,999 | GERÇEK FIRSAT + warning about the list price | `test_real_deal_with_fake_list_price_stays_deal_with_warning` |
| 40 days of history | −50% | YETERSİZ VERİ | `test_refuses_to_judge_short_history` |
| swings between 20,000 and 40,000 | 19,900 | not a deal: −50% vs median, but that price is common | `test_deep_but_not_rare_in_volatile_history_is_not_a_deal` |
| ~30,000, plus 50 snapshots at 20,000 on one sale day | — | the 180-day median stays 30,000 | `test_snapshot_burst_does_not_drag_median` |
| ±18% swings, one usual peak inside the last 30 days | back at the median | not fake | `test_natural_swing_peaking_recently_is_not_fake` |
| last in-stock row is 30 days old, or newest row is out of stock | — | GÜNCEL FİYAT YOK | `test_stale_price_is_not_current`, `test_latest_row_out_of_stock_is_not_current` |

## Cross-site product matching

Different stores title the same product differently (`Lumora LM-55QX700 139 cm 4K Ultra HD Akıllı TV` vs `Lumora LM55QX700 55 inç 4K Smart TV`). [deal_hunter/matching.py](deal_hunter/matching.py) merges listings in two stages:

- **Deterministic rules first**, with Turkish-aware normalisation (`İ`/`ı`, `inç`/`"`/`cm`/`Ekran`, `8/256 GB`). Different brand, accessory ("… için …", spare parts), refurbished, Pro/Lite/Max/Air variants, model code, size, capacity, litres, colour, connector, 4G/5G, panel or resolution → **different**. The same brand and model code (ignoring dashes and a single regional suffix letter) → **same**.
- **The LLM is consulted only when the rules cannot decide** (usually a model code missing on one side). It sees the two titles and returns `{"same": bool, "reason": str}`.

**Measured on a labelled set** ([fixtures/match_pairs.csv](fixtures/match_pairs.csv)): 120 pairs, 54 same and 66 different. The negatives include near-misses such as 55 vs 65 inch, 128 vs 256 GB, Pro vs non-Pro, accessories and refurbished items. Titles use fictional brands. The set was **written and labelled by a separate AI agent that never saw the matcher**. No human has audited the labels. It is split into dev (48 pairs, used to tune the rules) and a held-out test split (72 pairs, not used for tuning).

| Mode (test split, 72 pairs) | Model | Precision | Recall | LLM calls | Results file |
|---|---|---|---|---|---|
| Rules only | none | **0.962** (25/26) | 0.714 (25/35) | 0 | [matching_rules_test.json](results/matching_rules_test.json) |
| Rules + LLM (default) | `gemini-3.5-flash-lite` (Gemini API) | **0.969** (31/32) | 0.886 (31/35) | 6 | [matching_rules_llm_test_gemini-3.5-flash-lite.json](results/matching_rules_llm_test_gemini-3.5-flash-lite.json) |
| Rules + LLM (local) | `qwen3.5:9b` (Ollama) | **0.968** (30/31) | 0.857 (30/35) | 6 | [matching_rules_llm_test_qwen3.5-9b.json](results/matching_rules_llm_test_qwen3.5-9b.json) |
| LLM only (baseline, no rules) | `gemma-4-31b-it` (Gemini API) | **1.000** (32/32) | 0.941 (32/34) | 70 answered of 72; 2 skipped after repeated HTTP 503 | [matching_llm_test_gemma-4-31b-it.json](results/matching_llm_test_gemma-4-31b-it.json) |

With rules + LLM, the LLM only sees the 6 test pairs the rules could not settle, so the two LLM rows differ by one pair. That difference is too small to rank the models.

The LLM-only baseline uses a different model, so it is not a like-for-like comparison with the rules + LLM rows. On this set, a 31B model reading every pair beats the rules on recall without losing precision. It needed 72 calls instead of 6, though, at about 30 s each on the shared free tier, and 2 pairs never got an answer. Rules + LLM stays the default because it costs 12 times fewer calls and works offline for every pair the rules settle.

The dev split scores precision 1.00 / recall 0.95 with rules only. That is the split the rules were tuned on, so the test-split numbers are the honest ones. Precision matters more than recall here: a wrong merge mixes two products' price histories and can create a fake "deal". A missed merge only means one store's history is used.

The remaining test errors are listed in the results files. They include a GPU difference the rules do not read (RTX 4050 vs 4060), `Air2` vs `Air 2`, `Plus` vs `+`, and `Ryzen 7` vs `R7`.

## Recommendation bot

```
> Merhaba, televizyon bakıyorum
Televizyon için bütçe üst sınırınız nedir? (örn. 25.000 TL)
> 25.000 TL
Kaç inç ekran istiyorsunuz? (örn. 55; fark etmezse "fark etmez" yazın)
> 55 inç
55 inç televizyon, en fazla 25.000 TL için 1 gerçek fırsat buldum:
1. QLED 4K UHD 140 cm Ekran Televizyon Lumora LX-55Q7 - 21.499 TL (magaza-c) [sentetik veri]
   Karar: GERÇEK FIRSAT (puan 36/100)
   Neden: Fiyat 180 günlük medyanın %37 altında; son 180 günde bu fiyat veya altı görülen gün oranı yalnızca %0.
   Kanıt: 180 gün medyan 34.099 TL, 90 gün medyan 34.699 TL, 180 gün en düşük 32.399 TL, ...
   Özellikler (mağaza kaydından): Çözünürlük: 4K UHD; Ekran (inç): 55; Panel: QLED; ...
Bütçenize uyan ama fırsat sayılmayan ürünler:
- ... KOZMETİK İNDİRİM / SAHTE İNDİRİM / YETERSİZ VERİ with the reason
```

Full output: [results/demo_output.txt](results/demo_output.txt).

- It asks for what is missing (category, budget, screen size for TVs) and accepts "fark etmez" (no preference).
- **It never invents specs.** The optional LLM (`--llm`) only extracts `{category, max_price, inches}` from the message, and the result is validated. Every line of the reply is a template filled from stored listing fields and the deal analysis. Specs are printed verbatim from the listing's stored `specs`. A listing without a stored screen size is excluded from a size-filtered search, never guessed. `test_reply_never_invents_specs` checks every `label: value` in replies against the store.
- Telegram is optional (`TELEGRAM_BOT_TOKEN`, and `TELEGRAM_ALLOWED_CHAT_IDS` to restrict who may use it). Long polling, stdlib only. It is tested with a fake API and has not been run against a live bot.

## Data access decisions per site

Checked on 2026-11-17 with an identifying user agent (`deal-hunter/0.1 (+repo URL)`).

| Site | robots.txt | Terms / feeds | Decision |
|---|---|---|---|
| **Akakçe** | Product pages allowed for `*`; `/c/`, search and query parameters disallowed. | The [usage agreement](https://www.akakce.com/kullanim-sozlesmesi/) lists copying, reproducing, distributing or processing the site's "veritabanlarını, katalogları ve listeleri" without permission among the grounds for ending a user's access. Users also undertake not to compete with akakce.com through such acts. No public API found. | **Not collected.** |
| **Cimri** | Product pages allowed for `*`; `/api/`, search and filters disallowed. | The terms page returned **403** to our client, so it could not be read. No public API found. | **Not collected.** Terms not retrievable, so treated as not permitted. |
| **Trendyol** | Product pages allowed for `*`; many query parameters disallowed. | Every non-robots page we requested, including the likely terms URLs, returned **403** to our client, so the terms could not be read. The official API ([developers.trendyol.com](https://developers.trendyol.com)) is for sellers' own stores, not a price feed. | **Not collected.** |
| **Hepsiburada** | `robots.txt` itself is behind a bot-protection page (403). | Not retrievable either; no public price API found. | **Not collected.** We do not work around bot protection. |
| **Open Prices** (Open Food Facts) | Empty robots.txt (200); documented public API. | Data under **ODbL 1.0**; attribution required. | **Collected** (real run below). |
| marketfiyati.org.tr (TÜBİTAK) | Allows all. | No published terms or documented API found. | **Not collected** until terms are published. |

So for the four Turkish shopping sites, price history must come from **your own CSV/manual records** (`import-csv`), for example prices you note down, exports you are entitled to, or a partner feed you have access to. It can also come from **your own periodic snapshots** of pages on sites whose terms you have checked. `PageSnapshotSource` does this. It reads the schema.org `Product`/`Offer` JSON-LD of a page and:

- only fetches domains on an explicit allowlist (empty by default);
- checks robots.txt with `*` and `$` wildcard support (Python's `urllib.robotparser` ignores wildcards, which these sites rely on, e.g. `Disallow: /*?q=`);
- waits 10 s between requests to the same host;
- **stops** on 401/403/429/5xx or a challenge page, with no retry and no bypass.

The `Source` interface is just `name` + `fetch() -> Iterable[Observation]`. CSV, Open Prices and page snapshots are the three implementations.

### Real collection run: Open Prices, TRY prices

`python -m deal_hunter collect-openprices --save fixtures/openprices_tr.json` downloaded **31** crowd-sourced price records in TRY (supermarket items, mostly from Istanbul and Izmir districts). 30 loaded as observations; one was an exact duplicate (same product, store and day). Contributor handles and proof images are stripped before saving. The recorded run is [fixtures/openprices_tr.json](fixtures/openprices_tr.json) (ODbL 1.0, © Open Prices / Open Food Facts contributors), and the deal output is [results/openprices_deals.txt](results/openprices_deals.txt).

Every product comes out as **`YETERSİZ VERİ`**. Almost all have a single observation. This run proves the real-source pipeline and the "refuse to judge" path. It does not test the deal rule on real electronics prices. That would need months of permitted snapshots or a licensed feed.

## Known limits

- **No real electronics price history ships with the repo.** The deal-rule examples are crafted tests, and the demo is synthetic.
- **Nominal prices.** Turkish lira inflation pushes prices up over 180 days, so a nominal median overstates the "usual" price of older months. A 30% cut against a 180-day nominal median is therefore a bit easier to hit than in a low-inflation market. Deflating by CPI is not implemented.
- **Thresholds are judgement calls** (30%, 10% rarity, 90 days, 15% inflation). They are configurable, not learned.
- **List prices are only as good as the source.** The "never charged" check needs the struck-through price to be recorded.
- **Matching** was measured on 120 fictional-brand pairs written by an AI labeller, not on real listings. Rules do not read GPU models or `Plus`/`+` spellings. LLM responses from the eval runs are cached in [results/llm_cache.jsonl](results/llm_cache.jsonl), so reruns cost no quota.
- The chat understands a small, rule-based Turkish vocabulary (four categories, budget, screen size). `--llm` widens what it understands, but not what it can say.

## Layout

```
deal_hunter/deals.py      deal rule and scoring
deal_hunter/store.py      SQLite store (listings, prices)
deal_hunter/sources.py    Source interface: CSV, Open Prices, page snapshots; robots.txt + polite client
deal_hunter/matching.py   rules + LLM product matching
deal_hunter/bot.py        Turkish chat, Telegram front-end
deal_hunter/llm.py        JSON chat client for Gemini / DeepSeek / Ollama / any OpenAI-compatible API
scripts/make_fixtures.py  deterministic synthetic history generator
scripts/eval_matching.py  matching precision/recall -> results/
```

### LLM backends

The LLM is optional: the rules, the deal scoring and the chat all work without it. `DEAL_HUNTER_LLM_BACKEND` picks the backend (default `auto`):

| Backend | Needs | Default model |
|---|---|---|
| `gemini` (first choice in `auto`) | `GEMINI_API_KEY` | `gemini-3.5-flash-lite` |
| `deepseek` | `DEEPSEEK_API_KEY` | `deepseek-chat` |
| `ollama` (used by `auto` when no key is set) | a local Ollama at `OLLAMA_HOST` | `qwen3.5:9b` |
| `openai` | `OPENAI_BASE_URL` + `OPENAI_API_KEY` | set `DEAL_HUNTER_MODEL` |

`DEAL_HUNTER_MODEL` overrides the model. Keys are read from the environment only.

**What leaves the machine with a cloud backend:** product listing titles (matching), and the user's chat message (with `--llm`). No price history and no keys are sent.

---

## Türkçe özet

**deal-hunter**, bir ürünün bugünkü fiyatını kendi fiyat geçmişiyle karşılaştırır. **Gerçek fırsatı**, kozmetik ya da sahte indirimden ayırır. Üzerinde Türkçe konuşan bir öneri botu çalışır.

**Kural:** Bir fiyatın fırsat sayılması için iki koşul gerekir. Fiyat **180 günlük medyanın en az %30 altında** olmalı. Ayrıca bu fiyat **nadir** olmalı: son 180 günün en fazla %10'unda bu fiyat veya altı görülmüş olmalı. 90 günden kısa ya da 8 gözlem gününden az geçmişte karar verilmez (`YETERSİZ VERİ`). İki durum **sahte indirim** sayılır: fiyat son 30 günde şişirilip "indirim" bu fiyattan hesaplanıyorsa, ya da üstü çizili liste fiyatı son 180 günde hiç uygulanmamışsa. Örneğin 6 aydır 22.500 TL civarında satılan bir TV'nin "%10 indirimle" 22.500 TL olması fırsat değil, kozmetik indirimdir. 33.000 TL'den 21.450 TL'ye düşen TV ise gerçek fırsattır. Tüm örnekler birim testleriyle doğrulanır.

**Veri erişimi:** Akakçe'nin kullanım sözleşmesi veritabanı ve listelerin kopyalanmasını yasaklıyor. Cimri ve Trendyol koşul sayfaları erişimimize 403 döndürdü. Hepsiburada robots.txt'yi bile bot korumasının arkasında tutuyor. Bu yüzden **bu dört siteden veri çekilmiyor**. Fiyat geçmişi CSV ile içe aktarılabilir. Koşullarını okuyup izin listesine eklediğiniz sitelerden kendi periyodik anlık görüntülerinizle de toplanabilir; bu yol robots.txt kontrolü, hız sınırı ve engelde durma içerir. Gerçek veri toplama örneği Open Prices'tan (ODbL) yapıldı. Ürünlerin neredeyse hepsinde tek gözlem olduğu için sistem doğru biçimde karar vermeyi reddetti.

**Bot:** "55 inç, 25.000 TL altı televizyon" gibi bir isteğe eksik bilgiyi sorarak yanıt verir. Yalnızca kayıtlı gerçek fırsatları önerir. Her öneride medyan, en düşük fiyat ve geçmiş uzunluğu gibi kanıtlar bulunur. **Ürün özelliği uydurmaz**: özellikler yalnızca mağaza kaydından aynen aktarılır.

## License

MIT for the code. `fixtures/openprices_tr.json` is Open Prices data under the [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/), © Open Prices / Open Food Facts contributors.
