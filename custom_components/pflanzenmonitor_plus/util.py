"""Kleine Helfer ohne Abhängigkeiten im Paket."""

from __future__ import annotations

import math

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant


def number_state(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Zahl aus einem Zustand; None bei fehlender Entität, unbekannt, nicht verfügbar, Text, NaN oder unendlich."""
    if entity_id is None or (state := hass.states.get(entity_id)) is None:
        return None
    if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    return value if math.isfinite(value) else None
