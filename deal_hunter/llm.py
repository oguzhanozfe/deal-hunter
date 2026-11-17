"""Minimal JSON-mode chat client: Ollama by default, any OpenAI-compatible API via env vars.

  DEAL_HUNTER_MODEL     model name (default qwen3.5:9b)
  OLLAMA_HOST           default http://127.0.0.1:11434
  OPENAI_BASE_URL + OPENAI_API_KEY   use an OpenAI-compatible endpoint instead (optional)
"""
from __future__ import annotations

import json
import os
import time
import urllib.request


class LLMError(RuntimeError):
    pass


def chat_json(system: str, user: str, model: str | None = None, timeout: float = 120, retries: int = 2) -> dict:
    model = model or os.environ.get("DEAL_HUNTER_MODEL", "qwen3.5:9b")
    base = os.environ.get("OPENAI_BASE_URL")
    if base:
        url = base.rstrip("/") + "/chat/completions"
        body = {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"}
    else:
        url = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/") + "/api/chat"
        body = {"model": model, "stream": False, "format": "json", "think": False,
                "options": {"temperature": 0, "seed": 0},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        headers = {}
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
            text = data["choices"][0]["message"]["content"] if base else data["message"]["content"]
            return json.loads(text)
        except (OSError, ValueError, KeyError) as e:  # contention, timeouts, bad JSON
            last = e
            time.sleep(2 * (attempt + 1))
    raise LLMError(f"LLM call failed after {retries + 1} attempts: {last}")
