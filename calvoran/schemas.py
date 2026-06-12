"""Pydantic-Schemas für die Dossier-Extraktion (Konzept §3.2) plus Belege.

`Beleg` ist die strukturierte Belegpflicht: je Signal ein wörtliches Zitat und
die Quell-URL. Daraus speist sich später die `signals`-Tabelle (NOT-NULL auf
beleg_zitat/beleg_url) und die Belegtreue-Messung im Phase-0-Benchmark.
"""

from __future__ import annotations

import json
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _to_str(v):
    """Kleinere Modelle füllen String-Felder gelegentlich mit Objekten/Zahlen.
    Das hier glättet das, statt die ganze Extraktion zu verwerfen."""
    if v is None or isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


class _Lenient(BaseModel):
    # Zahlen in String-Feldern (z.B. generation: 2 -> "2") tolerieren.
    model_config = ConfigDict(coerce_numbers_to_str=True)


class Beleg(_Lenient):
    signal_type: str = Field(description="z.B. nachfolge, familienunternehmen, kaufm_funktion_fehlt, offene_kaufm_stelle, digitalisierung")
    aussage: str = Field(description="Die belegte Aussage in eigenen Worten")
    zitat: str = Field(description="Wörtliches Zitat von der Website, höchstens 25 Wörter")
    quelle_url: str = Field(description="URL der Seite, von der das Zitat stammt")


class Familienunternehmen(_Lenient):
    hinweis: bool = False
    generation: Optional[str] = None
    beleg: Optional[str] = None

    @field_validator("generation", "beleg", mode="before")
    @classmethod
    def _coerce(cls, v):
        return _to_str(v)


class Fuehrungsstruktur(_Lenient):
    gf_auf_website: List[str] = Field(default_factory=list)
    zweite_ebene_sichtbar: Optional[bool] = None
    kaufmaennische_funktion_besetzt: Optional[bool] = None


class Karriere(_Lenient):
    offene_stellen: List[str] = Field(default_factory=list)
    kaufm_stellen: List[str] = Field(default_factory=list)
    stand: Optional[str] = None

    @field_validator("stand", mode="before")
    @classmethod
    def _coerce(cls, v):
        return _to_str(v)


class NegativFilter(_Lenient):
    insolvenz_hinweis: bool = False
    reiner_onlineshop: bool = False
    tochter_eines_konzerns: bool = False


class Dossier(_Lenient):
    geschaeftsmodell: str = ""
    produkte_leistungen: List[str] = Field(default_factory=list)
    kundentyp: Optional[str] = None
    gruendungsjahr: Optional[int] = None
    familienunternehmen: Familienunternehmen = Field(default_factory=Familienunternehmen)
    fuehrungsstruktur: Fuehrungsstruktur = Field(default_factory=Fuehrungsstruktur)
    karriere: Karriere = Field(default_factory=Karriere)
    nachfolge_signale: List[str] = Field(default_factory=list)
    nachfolge_intern_geregelt: bool = False   # nächste Generation steht bereit -> kein Verkaufsanlass
    naechste_generation: Optional[str] = None  # Name/Beschreibung der bereitstehenden Nachfolge (belegt)
    digitalisierung: Optional[str] = None
    besonderheiten: Optional[str] = None
    tonalitaet_website: Optional[str] = None
    ansprache_hooks: List[str] = Field(default_factory=list)
    negativ_filter: NegativFilter = Field(default_factory=NegativFilter)
    belege: List[Beleg] = Field(default_factory=list)
    konfidenz: Optional[str] = None

    @field_validator(
        "geschaeftsmodell", "kundentyp", "naechste_generation", "digitalisierung",
        "besonderheiten", "tonalitaet_website", "konfidenz", mode="before",
    )
    @classmethod
    def _coerce_str(cls, v):
        return _to_str(v)
