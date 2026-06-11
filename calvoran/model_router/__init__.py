"""Modell-Router: einheitliches Interface, Backends per config/models.yaml wechselbar."""

from __future__ import annotations

import os
from typing import Tuple, Type

from pydantic import BaseModel

from ..db import ensure_env
from .anthropic_backend import AnthropicBackend
from .ollama_backend import OllamaBackend
from .repair import generate_validated


class ModelRouter:
    def __init__(self, models_cfg: dict) -> None:
        ensure_env()
        self.cfg = models_cfg
        self.backends: dict = {}
        for name, bc in models_cfg.get("backends", {}).items():
            btype = bc.get("type")
            if btype == "ollama":
                self.backends[name] = OllamaBackend(bc)
            elif btype == "anthropic":
                self.backends[name] = AnthropicBackend(bc, os.environ["ANTHROPIC_API_KEY"])
            else:
                raise ValueError(f"unbekannter Backend-Typ: {btype}")
        self.tasks = models_cfg.get("tasks", {})
        self.repair_cfg = models_cfg.get("repair", {})

    def has_backend(self, name: str) -> bool:
        return name in self.backends

    def extract(
        self, *, task: str, system: str, user: str, schema: Type[BaseModel],
        max_tokens: int = 1800, logger=None,
    ) -> Tuple[BaseModel, dict]:
        """Schema-validierte Extraktion über die Backend-Kette des Tasks."""
        return generate_validated(
            self, task, system=system, user=user, schema=schema,
            max_tokens=max_tokens, logger=logger,
        )

    def run_backend(
        self, backend_name: str, *, system: str, user: str, schema: Type[BaseModel],
        max_tokens: int = 1800, logger=None,
    ) -> Tuple[BaseModel, dict]:
        """Erzwingt ein bestimmtes Backend (für den Benchmark: Gemma vs. Haiku direkt)."""
        # Temporäre Ein-Backend-Task, damit der Repair-Loop greift.
        tmp_task = f"__direct__{backend_name}"
        self.tasks[tmp_task] = {"primary": backend_name}
        try:
            return generate_validated(
                self, tmp_task, system=system, user=user, schema=schema,
                max_tokens=max_tokens, logger=logger,
            )
        finally:
            self.tasks.pop(tmp_task, None)


__all__ = ["ModelRouter"]
