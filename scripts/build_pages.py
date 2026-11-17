"""Build docs/index.html (GitHub Pages) from the replay-mode demo: price charts, verdicts, bot transcript.

  python scripts/build_pages.py
Everything on the page comes from analyze()/daily_series() on the bundled synthetic fixture and from
the recorded chat; nothing is typed by hand. Stdlib only, inline SVG, no external assets.
"""
from __future__ import annotations

import contextlib
import html
import io
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from deal_hunter.deals import DEAL, FAKE, DealConfig, current_offers, daily_series  # noqa: E402
from deal_hunter.demo import load_store, run, tv55  # noqa: E402
from deal_hunter.text import fmt_tl  # noqa: E402

COLORS = {DEAL: "#1a7f37", FAKE: "#cf222e"}
W, H, PAD_L, PAD_R, PAD_T, PAD_B = 640, 230, 64, 104, 12, 26


def chart(v, obs, cfg: DealConfig) -> str:
    today = v.current_day
    series = daily_series(obs, today, cfg.ffill_days)
    start = today - timedelta(days=200)
    pts = sorted((d, p) for d, p in series.items() if d >= start)
    cur = current_offers(obs, today, cfg.max_age_days)
    lst = max((o.list_price for o in cur if o.list_price), default=None)
    med = v.stats.get("median_180")
    lines = [(med, "180 gün medyan", "#57606a", "6 4")] if med else []
    if med:
        lines.append((med * (1 - cfg.min_depth), f"fırsat eşiği (-%{cfg.min_depth * 100:.0f})", "#1a7f37", "2 4"))
    if lst:
        lines.append((lst, "üstü çizili fiyat", "#cf222e", "1 3"))
    vals = [p for _, p in pts] + [v.current] + [y for y, *_ in lines]
    lo, hi = min(vals) * 0.95, max(vals) * 1.03
    x = lambda d: PAD_L + (d - start).days / (today - start).days * (W - PAD_L - PAD_R)  # noqa: E731
    y = lambda p: PAD_T + (hi - p) / (hi - lo) * (H - PAD_T - PAD_B)  # noqa: E731
    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="fiyat geçmişi">']
    for frac in (0, 0.5, 1):
        p = lo + (hi - lo) * frac
        out.append(f'<text x="{PAD_L - 6}" y="{y(p) + 4:.0f}" class="ax" text-anchor="end">{fmt_tl(p)}</text>')
    out.append(f'<text x="{PAD_L}" y="{H - 6}" class="ax">{start:%d.%m.%Y}</text>'
               f'<text x="{W - PAD_R}" y="{H - 6}" class="ax" text-anchor="end">{today:%d.%m.%Y}</text>')
    ly = -99.0
    for val, label, color, dash in sorted(lines, key=lambda l: -l[0]):  # top to bottom; keep labels 13px apart
        ly = max(y(val) + 4, ly + 13)
        out.append(f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(val):.1f}" y2="{y(val):.1f}" stroke="{color}" '
                   f'stroke-dasharray="{dash}"/><text x="{W - PAD_R + 4}" y="{ly:.1f}" class="lb" '
                   f'fill="{color}">{label}</text>')
    # break the line where the series has gaps (no listing had a fresh price)
    segs, seg, prev = [], [], None
    for d, p in pts:
        if prev and (d - prev).days > 1:
            segs.append(seg); seg = []
        seg.append(f"{x(d):.1f},{y(p):.1f}"); prev = d
    segs.append(seg)
    for s in segs:
        if s:
            out.append(f'<polyline points="{" ".join(s)}" fill="none" stroke="#0969da" stroke-width="1.6"/>')
    out.append(f'<circle cx="{x(today):.1f}" cy="{y(v.current):.1f}" r="4.5" fill="{COLORS.get(v.verdict, "#9a6700")}"/>'
               f'<text x="{x(today) + 8:.1f}" y="{y(v.current) + 19:.1f}" class="lb" font-weight="600">bugün '
               f'{fmt_tl(v.current)}</text></svg>')
    return "".join(out)


def card(v, best, listings, obs, cfg) -> str:
    e = html.escape
    color = COLORS.get(v.verdict, "#9a6700")
    body = "".join(f"<li>{e(r)}</li>" for r in v.reasons) + "".join(f'<li class="warn">{e(f)}</li>' for f in v.flags)
    s = v.stats
    ev = (f"180 gün medyan {fmt_tl(s['median_180'])} · en düşük {fmt_tl(s['min_180'])} · en yüksek {fmt_tl(s['max_180'])}"
          f" · {s['history_days']} günlük geçmiş, {len(listings)} mağaza") if "median_180" in s else \
        f"{s.get('history_days', '?')} günlük geçmiş, {len(listings)} mağaza"
    return (f'<section class="card"><h3>{e(best["title"])}</h3>'
            f'<p><span class="tag" style="background:{color}">{e(v.verdict)}</span> '
            f'<b>{fmt_tl(v.current)}</b> · {e(best["source"])}</p>{chart(v, obs, cfg)}'
            f'<ul>{body}</ul><p class="ev">{e(ev)}</p></section>')


def main() -> None:
    cfg = DealConfig()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        transcript = run(force_replay=True, cfg=cfg)
    mode = next(l for l in buf.getvalue().splitlines() if "mode: replay" in l).strip("[]").split(";")[0]
    store = load_store()
    cards = "".join(card(*p, cfg) for p in tv55(store, cfg))
    e = html.escape
    chat = "".join(f'<div class="u">{e(u)}</div><div class="by">ayrıştırma: {e(by)}</div><pre class="b">{e(r)}</pre>'
                   for u, by, r in transcript)
    page = TEMPLATE.format(cards=cards, chat=chat, mode=e(mode), depth=f"{cfg.min_depth * 100:.0f}")
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs/index.html").write_text(page, encoding="utf-8")
    print(f"docs/index.html written ({len(page) // 1024} KB)")


TEMPLATE = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>deal-hunter demo: gerçek fırsat mı, sahte indirim mi?</title>
<style>
body{{font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;background:#f6f8fa;color:#1f2328}}
main{{max-width:900px;margin:0 auto;padding:24px 16px 48px}}
h1{{font-size:26px;margin:0 0 6px}} h2{{margin-top:36px}} h3{{margin:0 0 6px;font-size:16px}}
.note{{background:#fff8c5;border:1px solid #d4a72c;padding:8px 12px;border-radius:6px}}
.card{{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:14px 16px;margin:14px 0}}
.tag{{color:#fff;font-weight:600;font-size:13px;padding:2px 8px;border-radius:12px}}
svg{{width:100%;height:auto;display:block;margin:6px 0}} .ax{{font-size:11px;fill:#57606a}} .lb{{font-size:11px}}
ul{{margin:6px 0;padding-left:20px}} .warn{{color:#cf222e}} .ev{{color:#57606a;font-size:13px;margin:4px 0 0}}
.u{{background:#0969da;color:#fff;padding:6px 12px;border-radius:12px;display:inline-block;margin-top:14px}}
.by{{font-size:12px;color:#57606a;margin:4px 0}}
pre.b{{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:10px 12px;white-space:pre-wrap;font-size:13px;margin:0}}
a{{color:#0969da}}
</style></head><body><main>
<h1>deal-hunter: gerçek fırsat mı, sahte indirim mi?</h1>
<p>Bugünkü fiyatı ürünün kendi fiyat geçmişiyle karşılaştırır. Kural: fiyat 180 günlük medyanın en az
%{depth} altındaysa <b>ve</b> bu seviye geçmişte nadiren görüldüyse <b>gerçek fırsat</b>. Şişirilip indirilen
fiyatlar ve hiç uygulanmamış üstü çizili fiyatlar <b>sahte indirim</b> sayılır. Geçmiş kısaysa karar verilmez.</p>
<p class="note">Bu sayfadaki fiyatlar <b>sentetik veridir</b> (hayali markalar ve mağazalar). Karar kuralları model
kullanmaz. Kod ve açıklama: <a href="https://github.com/oguzhanozfe/deal-hunter">github.com/oguzhanozfe/deal-hunter</a></p>
<h2>55 inç televizyonlar: fiyat geçmişi ve karar</h2>
<p>Mavi çizgi: mağazalar arasındaki günlük en düşük fiyat. Gri kesikli: 180 günlük medyan. Yeşil noktalı: fırsat eşiği.</p>
{cards}
<h2>Öneri botu: örnek sohbet</h2>
<p>Bot yanıtları şablondur; özellikler mağaza kaydından aynen yazılır. Model yalnızca kuralların okuyamadığı
mesajı (ör. yazıyla yazılmış bütçe) yapılandırılmış isteğe çevirir. <code>{mode}</code></p>
{chat}
<p class="ev">Bu sayfa <code>python scripts/build_pages.py</code> ile üretilir; aynı çıktı
<code>python -m deal_hunter demo</code> komutunda da görülür.</p>
</main></body></html>
"""

if __name__ == "__main__":
    main()
