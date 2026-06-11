"""Validierung + Repair-Loop + Eskalation für strukturierte Ausgaben.

Ablauf: Backend erzeugt JSON -> Pydantic-Validierung -> bei Fehler 1 Repair-Retry
mit angehängtem Fehlertext -> nach Erschöpfung Eskalation auf das nächste Backend
der Task-Kette (lokal -> API). Schlägt auch das fehl: extraction_failed loggen.
"""

from __future__ import annotations

from typing import Tuple, Type

from pydantic import BaseModel, ValidationError


def generate_validated(
    router, task: str, *, system: str, user: str, schema: Type[BaseModel],
    max_tokens: int, logger=None,
) -> Tuple[BaseModel, dict]:
    t = router.tasks.get(task, {})
    repair_cfg = router.repair_cfg or {}
    max_repair = int(repair_cfg.get("max_repair_retries", 1))

    # Backend-Kette: primary, dann optionale Eskalation.
    chain = []
    primary = t.get("primary")
    if primary in router.backends:
        chain.append(primary)
    escalate = t.get("escalate")
    if escalate and escalate in router.backends and escalate not in chain:
        chain.append(escalate)
    if not chain:
        raise RuntimeError(f"kein nutzbares Backend für Task '{task}'")

    last_err: Exception | None = None
    for idx, backend_name in enumerate(chain):
        backend = router.backends[backend_name]
        user_prompt = user
        for attempt in range(max_repair + 1):
            try:
                raw, meta = backend.generate_structured(
                    system=system, user=user_prompt, schema=schema, max_tokens=max_tokens
                )
            except Exception as e:  # BackendError u.a.
                last_err = e
                if logger:
                    logger.log("backend_error", task=task, backend=backend_name, error=str(e)[:200])
                break  # nächstes Backend
            try:
                obj = schema.model_validate_json(raw)
                meta["repair_count"] = attempt
                meta["escalated"] = idx > 0
                return obj, meta
            except ValidationError as e:
                last_err = e
                if logger:
                    logger.log("extraction_invalid", task=task, backend=backend_name,
                               attempt=attempt, errors=str(e)[:300])
                user_prompt = (
                    user
                    + "\n\n--- Deine letzte Ausgabe war ungültig ---\n"
                    + str(e)[:600]
                    + "\nGib AUSSCHLIESSLICH valides JSON exakt nach dem Schema zurück."
                )

    if logger:
        logger.log("extraction_failed", task=task, errors=str(last_err)[:300])
    assert last_err is not None
    raise last_err
