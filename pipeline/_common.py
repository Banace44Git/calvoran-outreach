"""Gemeinsame Helfer für die pipeline/-Skripte (sys.path-Bootstrap, Normalisierung)."""

from __future__ import annotations

import os
import re
import sys

# Projekt-Wurzel auf den Pfad, damit `import calvoran` aus pipeline/ funktioniert.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PROJECT_ROOT = _ROOT
CSV_DEFAULT = "/Users/johannesbreuers/Downloads/zielliste-fractional-cfo_Stand2026-06-07 - zielliste.csv"
OUTPUT_DIR = "/Users/johannesbreuers/projects/os/01-projects/fractional-cfo/outreach"


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def wz2(branche_wz: str) -> str:
    """'43.21.0' -> '43'."""
    m = re.match(r"\s*(\d{2})", branche_wz or "")
    return m.group(1) if m else ""
