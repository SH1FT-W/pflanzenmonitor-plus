"""Grenzwerte aller Pflanzen (Temperaturen in °C), gespeichert im Store; die number-Entitäten zeigen sie nur an."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import math
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store

from . import health
from .const import DOMAIN

# Kurz warten, damit mehrere Werte (KI-Vorschlag, Umzug) in einem Schreibvorgang landen
SAVE_DELAY = 1


class Limits:
    """Grenzwerte je Pflanze; fehlt ein Wert, gilt der Startwert."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry_id}.limits")
        self.values: dict[str, dict[str, float]] = {}
        self._listeners: dict[str, list[Callable[[], None]]] = {}

    async def async_load(self, keys: set[str]) -> None:
        """Gespeicherte Werte laden; die gelöschter Pflanzen fallen beim nächsten Speichern weg."""
        stored = await self._store.async_load() or {}
        plants = stored.get("plants") if isinstance(stored, dict) else None
        self.values = {
            key: {f: float(v) for f, v in values.items() if f in health.DEFAULTS and _finite(v)}
            for key, values in (plants or {}).items()
            if key in keys and isinstance(values, dict)
        }

    def get(self, key: str, name: str) -> float:
        return self.values.get(key, {}).get(name, float(health.DEFAULTS[name]))

    def plant(self, key: str) -> dict[str, float]:
        """Alle Grenzwerte einer Pflanze."""
        return {name: self.get(key, name) for name in health.LIMITS}

    @callback
    def async_set(self, key: str, values: Mapping[str, float]) -> None:
        """Werte setzen, speichern und die Pflanze neu prüfen lassen."""
        self.values.setdefault(key, {}).update({n: float(v) for n, v in values.items()})
        self._store.async_delay_save(self._data, SAVE_DELAY)
        for listener in list(self._listeners.get(key, [])):
            listener()

    async def async_save(self) -> None:
        """Sofort speichern (Umzug: der neu geladene Eintrag liest danach vom Datenträger)."""
        await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        await self._store.async_remove()

    @callback
    def async_listen(self, key: str, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.setdefault(key, []).append(listener)

        @callback
        def remove() -> None:
            self._listeners[key].remove(listener)

        return remove

    def _data(self) -> dict[str, Any]:
        return {"plants": self.values}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)
