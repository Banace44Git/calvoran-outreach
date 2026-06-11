"""Anthropic-Backend (Haiku/Sonnet). Strukturierte Ausgabe über erzwungenes Tool-Use.

Robuster als Freitext-JSON-Parsing: das Modell muss das `emit`-Tool mit dem
Pydantic-Schema als input_schema aufrufen; `block.input` ist bereits ein dict.
"""

from __future__ import annotations

import json
import time
from typing import Tuple, Type

from anthropic import Anthropic
from pydantic import BaseModel

from .base import BackendError


class AnthropicBackend:
    def __init__(self, cfg: dict, api_key: str) -> None:
        self.name = "anthropic:" + cfg["model"]
        self.model = cfg["model"]
        self.default_max = int(cfg.get("max_tokens", 1800))
        self.temperature = float(cfg.get("temperature", 0.0))
        self.client = Anthropic(api_key=api_key)

    def generate_structured(
        self, *, system: str, user: str, schema: Type[BaseModel], max_tokens: int
    ) -> Tuple[str, dict]:
        tool = {
            "name": "emit",
            "description": "Gib das strukturierte Ergebnis exakt nach Schema zurück.",
            "input_schema": schema.model_json_schema(),
        }
        t0 = time.monotonic()
        try:
            msg = self.client.messages.create(
                model=self.model,
                max_tokens=max_tokens or self.default_max,
                temperature=self.temperature,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": "emit"},
                messages=[{"role": "user", "content": user}],
            )
        except Exception as e:  # anthropic.APIError u.a.
            raise BackendError(f"anthropic request failed: {e}") from e
        elapsed = time.monotonic() - t0
        raw = ""
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "emit":
                raw = json.dumps(block.input, ensure_ascii=False)
                break
        meta = {
            "backend": self.name,
            "elapsed_s": round(elapsed, 3),
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
            "tokens_per_s": round(msg.usage.output_tokens / elapsed, 1) if elapsed else None,
        }
        return raw, meta
