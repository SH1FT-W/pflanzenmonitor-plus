"""Gemeinsames für Auslöser und Bedingungen: Zustands-Entitäten von Plus erkennen, Sensor einer Messgröße finden.

„Veraltet“ heißt: der echte Sensor ist nicht verfügbar/unbekannt oder hat so lange (Standard 24 Std.) nichts
gemeldet – auch eine Meldung mit gleichem Wert zählt (`last_reported`). Beobachtet wird der echte Sensor, nicht der
Spiegel von Plus.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from homeassistant.const import CONF_FOR, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import config_validation as cv, entity_registry as er
import voluptuous as vol

from . import health
from .const import DOMAIN
from .plants import find_plants

STALE_FOR: Final = timedelta(hours=24)
EXCLUDED: Final = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN})
STATUS_SUFFIX: Final = "-status"
STALE_SCHEMA: Final = vol.Schema(
    {
        vol.Required("target"): cv.TARGET_FIELDS,
        vol.Optional("options", default=dict): vol.Schema(
            {vol.Optional(CONF_FOR): cv.positive_time_period}, extra=vol.ALLOW_EXTRA
        ),
    },
    extra=vol.ALLOW_EXTRA,
)
# Messgrößen mit eigenem Sensor (können veralten); DLI und VPD werden gerechnet
STALE_MEASUREMENTS: Final = health.SOURCES


def is_status(hass: HomeAssistant, entity_id: str) -> bool:
    """Ist das die Zustands-Entität einer Pflanze von Plus?"""
    item = er.async_get(hass).async_get(entity_id)
    return item is not None and item.platform == DOMAIN and item.unique_id.endswith(STATUS_SUFFIX)


def source(hass: HomeAssistant, entity_id: str, measurement: str) -> str | None:
    """Echter Sensor einer Messgröße der Pflanze hinter der Zustands-Entität."""
    item = er.async_get(hass).async_get(entity_id)
    if item is None or item.platform != DOMAIN or not item.unique_id.endswith(STATUS_SUFFIX):
        return None
    key = item.unique_id.removesuffix(STATUS_SUFFIX)
    # Aus den Unterpunkten, nicht aus runtime_data: auch während Plus neu lädt
    for entry in hass.config_entries.async_entries(DOMAIN):
        if plant := find_plants(entry).get(key):
            found: str | None = plant.sensors.get(measurement)
            return found
    return None


@dataclass(frozen=True, slots=True)
class Stale:
    """Ist der Sensor veraltet, und warum („unavailable“ oder „no_update“)?"""

    is_stale: bool
    reason: str | None = None


def stale(state: State | None, good: datetime, now: datetime, duration: timedelta, *, seen: bool = True) -> Stale:
    """Veraltet: nicht verfügbar (nur wenn schon einmal gültig gesehen) oder seit `duration` keine gültige Meldung."""
    if (state is None or state.state in EXCLUDED) and seen:
        return Stale(True, "unavailable")
    if now - good >= duration:
        return Stale(True, "unavailable" if state is None or state.state in EXCLUDED else "no_update")
    return Stale(False)


def stale_for(options: dict[str, Any] | None) -> timedelta:
    found: timedelta | None = (options or {}).get(CONF_FOR)
    return found or STALE_FOR
