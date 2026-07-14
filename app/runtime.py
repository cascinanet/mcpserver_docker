"""Flag di runtime modificabili dalla admin UI (persistiti su file).

Tenuti in memoria per il processo (1 worker) e salvati in DATA_DIR/runtime.json.
Usati ad es. per attivare/disattivare il logging diagnostico delle richieste.
"""

from __future__ import annotations

import json

from app.config import get_settings

_state: dict[str, object] = {"request_logging": False}


def _file():
    return get_settings().data_dir / "runtime.json"


def load() -> None:
    try:
        with _file().open("r", encoding="utf-8") as fh:
            _state.update(json.load(fh))
    except (OSError, ValueError):
        pass


def _save() -> None:
    try:
        with _file().open("w", encoding="utf-8") as fh:
            json.dump(_state, fh)
    except OSError:
        pass


def request_logging_enabled() -> bool:
    return bool(_state.get("request_logging"))


def set_request_logging(enabled: bool) -> None:
    _state["request_logging"] = bool(enabled)
    _save()
