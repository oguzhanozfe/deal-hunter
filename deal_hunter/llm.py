"""Minimal JSON-mode chat client over OpenAI-compatible endpoints. Keys come from env vars only.

Backend (DEAL_HUNTER_LLM_BACKEND, default "auto"):
  auto      gemini if GEMINI_API_KEY is set, else deepseek if DEEPSEEK_API_KEY, else ollama
  gemini    Google Gemini, OpenAI-compatible endpoint, GEMINI_API_KEY      (default model gemini-3.5-flash-lite)
  deepseek  DeepSeek, DEEPSEEK_API_KEY                                     (default model deepseek-chat)
  ollama    local Ollama at OLLAMA_HOST (default http://127.0.0.1:11434)   (default model qwen3.5:9b)
  openai    any OpenAI-compatible server: OPENAI_BASE_URL + OPENAI_API_KEY (model required)
DEAL_HUNTER_MODEL overrides the model. DEAL_HUNTER_LLM_CACHE=path.jsonl caches responses by input hash.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

BACKENDS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-3.5-flash-lite"),
    "deepseek": ("https://api.deepseek.com", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "ollama": (None, None, "qwen3.5:9b"),
    "openai": (None, "OPENAI_API_KEY", None),
}


class LLMError(RuntimeError):
    pass


def resolve(backend: str | None = None, model: str | None = None) -> tuple[str, str, str, str | None]:
    """-> (backend, base_url, model, api_key). Never logs the key."""
    b = backend or os.environ.get("DEAL_HUNTER_LLM_BACKEND", "auto")
    if b == "auto":
        b = "gemini" if os.environ.get("GEMINI_API_KEY") else "deepseek" if os.environ.get("DEEPSEEK_API_KEY") else "ollama"
    if b not in BACKENDS:
        raise LLMError(f"unknown backend {b!r}")
    base, key_env, default_model = BACKENDS[b]
    if b == "ollama":
        base = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/") + "/v1"
    elif b == "openai":
        base = os.environ.get("OPENAI_BASE_URL", "")
    key = os.environ.get(key_env) if key_env else None
    if key_env and not key:
        raise LLMError(f"{key_env} is not set")
    m = model or os.environ.get("DEAL_HUNTER_MODEL") or default_model
    if not base or not m:
        raise LLMError(f"backend {b!r} needs a base URL and a model")
    return b, base.rstrip("/"), m, key


_LOCK = threading.Lock()


class QuotaError(LLMError):
    """HTTP 429: the provider's quota is used up. Never retried in a loop."""


def _cache_path() -> str | None:
    return os.environ.get("DEAL_HUNTER_LLM_CACHE")


def _cache_get(key: str) -> dict | None:
    path = _cache_path()
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row["key"] == key:
                return row["response"]
    return None


def _cache_put(key: str, meta: dict, response: dict) -> None:
    path = _cache_path()
    if path:
        with _LOCK, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key, **meta, "response": response}, ensure_ascii=False) + "\n")


def parse_reply(text: str) -> dict:
    """Strip <thought>...</thought> (Gemma via Gemini API) and code fences, then parse JSON."""
    text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.S).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON object in reply: {text[:120]!r}")
    return json.loads(m.group(0))


def chat_json(system: str, user: str, model: str | None = None, backend: str | None = None,
              timeout: float = 120, retries: int = 2, offline: bool = False) -> dict:
    """One JSON answer. Responses are cached by input hash when DEAL_HUNTER_LLM_CACHE names a .jsonl file."""
    b, base, m, key = resolve(backend, model)
    ckey = hashlib.sha256(json.dumps([b, m, system, user], ensure_ascii=False).encode()).hexdigest()
    cached = _cache_get(ckey)
    if cached is not None:
        USAGE["cache_hits"] += 1
        return cached
    if offline:
        raise LLMError("not in cache and offline=True")
    json_mode = not m.startswith("gemma")  # Gemma on the Gemini API has no JSON mode / system role
    messages = ([{"role": "system", "content": system}, {"role": "user", "content": user}] if json_mode
                else [{"role": "user", "content": f"{system}\n\n{user}"}])
    body = {"model": m, "temperature": 0, "messages": messages}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if b == "ollama":
        body["reasoning_effort"] = "none"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base + "/chat/completions", json.dumps(body).encode(), headers)
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            out = parse_reply(data["choices"][0]["message"]["content"] or "")
            usage = data.get("usage") or {}
            with _LOCK:
                USAGE["calls"] += 1
                USAGE["prompt_tokens"] += usage.get("prompt_tokens", 0)
                USAGE["completion_tokens"] += usage.get("completion_tokens", 0)
            _cache_put(ckey, {"backend": b, "model": m}, out)
            return out
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf-8", "replace")
            if e.code == 429:
                raise QuotaError(f"HTTP 429 from {b}/{m}: {detail}") from None
            last = LLMError(f"HTTP {e.code} from {b}: {detail}")
            if e.code not in (500, 502, 503, 504):
                break
        except (OSError, ValueError, KeyError) as e:  # timeouts, bad JSON
            last = e
        time.sleep(5 * (attempt + 1))
    raise LLMError(f"LLM call to {b}/{m} failed: {last}")


USAGE = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cache_hits": 0}
