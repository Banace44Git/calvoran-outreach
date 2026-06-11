"""Backend-Protokoll für den Modell-Router."""

from __future__ import annotations

from typing import Protocol, Tuple, Type, runtime_checkable

from pydantic import BaseModel


class BackendError(Exception):
    pass


@runtime_checkable
class Backend(Protocol):
    name: str

    def generate_structured(
        self, *, system: str, user: str, schema: Type[BaseModel], max_tokens: int
    ) -> Tuple[str, dict]:
        """Erzeugt eine schema-getriebene Ausgabe.

        Rückgabe: (raw_json_str, meta). meta enthält backend, elapsed_s,
        input_tokens, output_tokens, tokens_per_s (soweit verfügbar). Die
        Validierung gegen `schema` passiert im Repair-Loop, nicht hier.
        """
        ...
