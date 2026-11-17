"""Measure matching precision/recall on fixtures/match_pairs.csv and write results/matching_<mode>_<split>.json.

  python scripts/eval_matching.py --mode rules --split test
  python scripts/eval_matching.py --mode rules+llm --split test     # needs an LLM backend (see deal_hunter/llm.py)

LLM responses are cached in results/llm_cache.jsonl (keyed by backend, model and prompt), so a rerun
costs no quota. On the first HTTP 429 the run stops calling the provider; remaining LLM pairs are
recorded as skipped and precision/recall cover the answered pairs only.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DEAL_HUNTER_LLM_CACHE", str(ROOT / "results" / "llm_cache.jsonl"))
from deal_hunter.llm import USAGE, LLMError, QuotaError, chat_json, resolve  # noqa: E402
from deal_hunter.matching import decide  # noqa: E402

BRANDS = {"lumora", "novix", "orvia", "kestrel", "tessa", "zentra"}


def model_digest(model: str) -> str | None:
    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    try:
        with urllib.request.urlopen(host + "/api/tags", timeout=5) as r:
            for m in json.loads(r.read())["models"]:
                if m["name"] == model:
                    return m["digest"]
    except OSError:
        return None
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="rules", choices=["rules", "rules+llm", "llm"])
    ap.add_argument("--split", default="test", choices=["dev", "test", "all"])
    a = ap.parse_args()
    rows = [r for r in csv.DictReader(open(ROOT / "fixtures/match_pairs.csv", encoding="utf-8"))
            if a.split == "all" or r["split"] == a.split]
    tp = fp = fn = tn = llm_calls = 0
    per_pair, skipped = [], []
    quota_hit = False

    def no_quota(system, user):
        return chat_json(system, user, offline=True)  # cache only; LLMError if missing
    t0 = time.time()
    for r in rows:
        gold = r["same"] == "1"
        try:
            same, by, why = decide(r["title_a"], r["title_b"], BRANDS, a.mode, chat=None if not quota_hit else no_quota)
        except QuotaError as e:  # stop calling the provider; never loop on 429
            quota_hit = True
            skipped.append({"id": r["id"], "error": str(e)[:200]})
            continue
        except LLMError as e:  # quota/outage: record and keep going; metrics cover answered pairs only
            skipped.append({"id": r["id"], "error": str(e)[:200]})
            continue
        llm_calls += by == "llm"
        tp += same and gold
        fp += same and not gold
        fn += (not same) and gold
        tn += (not same) and not gold
        per_pair.append({"id": r["id"], "gold": gold, "pred": same, "by": by, "reason": why})
    backend, model = (resolve()[0], resolve()[2]) if a.mode != "rules" else (None, None)
    out = {
        "mode": a.mode, "split": a.split, "pairs": len(rows), "answered": len(per_pair), "skipped": skipped,
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / (tp + fn), 4) if tp + fn else None,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "llm_calls": llm_calls,
        "backend": backend, "model": model,
        "model_digest": model_digest(model) if backend == "ollama" else None,
        "usage": dict(USAGE) if a.mode != "rules" else None,
        "seconds": round(time.time() - t0, 1),
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "errors": [p for p in per_pair if p["gold"] != p["pred"]],
    }
    (ROOT / "results").mkdir(exist_ok=True)
    suffix = f"_{model.replace(':', '-')}" if model else ""
    path = ROOT / f"results/matching_{a.mode.replace('+', '_')}_{a.split}{suffix}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "errors"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
