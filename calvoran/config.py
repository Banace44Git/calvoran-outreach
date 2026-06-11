"""Lädt YAML-Configs aus config/ mit ${VAR:-default}-ENV-Auflösung."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            var, default = m.group(1), m.group(2)
            return os.environ.get(var, default if default is not None else "")
        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def load(name: str) -> dict:
    """`load('models')` -> geparstes config/models.yaml mit aufgelösten ENV-Vars."""
    path = _CONFIG_DIR / f"{name}.yaml"
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return _resolve_env(data)
