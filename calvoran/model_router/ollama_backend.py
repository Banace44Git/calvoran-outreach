"""Ollama-Backend (lokale Gemma 4). Nativer /api/chat-Endpoint, JSON-Schema-Format.

Fallstricke adressiert: num_ctx aus Config (Default 4K zu klein), /api/chat statt
/v1 (zuverlässigerer format-Support), Timeout grosszügig.
"""

from __future__ import annotations

import time
from typing import Tuple, Type

import httpx
from pydantic import BaseModel

from .base import BackendError


class OllamaBackend:
    def __init__(self, cfg: dict) -> None:
        self.name = "ollama:" + cfg["model"]
        self.host = str(cfg["host"]).rstrip("/")
        self.model = cfg["model"]
        self.endpoint = cfg.get("endpoint", "/api/chat")
        self.options = dict(cfg.get("options", {}))
        self.timeout_s = float(cfg.get("timeout_s", 180))
        # Gemma 4 ist ein Reasoning-Modell; ohne think=false verbrennt es das
        # Token-Budget im (nicht zurückgegebenen) Thinking-Feld -> leerer content.
        self.think = cfg.get("think")

    def generate_structured(
        self, *, system: str, user: str, schema: Type[BaseModel], max_tokens: int
    ) -> Tuple[str, dict]:
        options = dict(self.options)
        if max_tokens:
            options["num_predict"] = max_tokens
        # Freier JSON-Modus statt strikter Schema-Grammar: das volle JSON-Schema
        # (mit $defs/$ref) lässt kleinere/quantisierte Modelle in Endlosschleifen
        # degenerieren. Die Schema-Treue stellen Skelett im Prompt + Pydantic-Repair sicher.
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": options,
        }
        if self.think is not None:
            payload["think"] = self.think
        t0 = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout_s) as c:
                r = c.post(self.host + self.endpoint, json=payload)
                r.raise_for_status()
                data = r.json()
        except httpx.HTTPError as e:
            raise BackendError(f"ollama request failed: {e}") from e
        elapsed = time.monotonic() - t0
        raw = (data.get("message") or {}).get("content", "")
        out_tok = data.get("eval_count")
        meta = {
            "backend": self.name,
            "elapsed_s": round(elapsed, 3),
            "input_tokens": data.get("prompt_eval_count"),
            "output_tokens": out_tok,
            "tokens_per_s": round(out_tok / elapsed, 1) if out_tok and elapsed else None,
        }
        return raw, meta
