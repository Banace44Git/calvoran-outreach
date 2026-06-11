"""JSON-Zeilen-Logger, portiert aus hr-engine (daemon.log).

Schreibt nach `~/.local/state/calvoran/<logfile>` plus stdout. Ein Record je
Zeile: {ts, event, **kwargs}. Maschinenlesbar (jq, tail -f).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def state_dir() -> Path:
    d = Path.home() / ".local" / "state" / "calvoran"
    d.mkdir(parents=True, exist_ok=True)
    return d


class JsonLogger:
    def __init__(self, logfile: str = "pipeline.log", *, echo: bool = True) -> None:
        self.path = state_dir() / logfile
        self.echo = echo

    def log(self, event: str, **kw) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
        line = json.dumps(rec, ensure_ascii=False, default=str)
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if self.echo:
            print(line, file=sys.stdout, flush=True)
