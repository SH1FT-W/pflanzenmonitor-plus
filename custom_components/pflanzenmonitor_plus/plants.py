"""Pflanzen aus den Unterpunkten des Eintrags und wo ihre Entitäten stehen."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from . import health
from .const import (
    AI_KEYS,
    CONF_AREA,
    CONF_GRACE,
    CONF_IMAGE,
    CONF_KEY,
    CONF_NAME,
    CONF_OUTDOOR,
    CONF_PROBLEMS,
    CONF_SPECIES,
    DOMAIN,
    OUTDOOR_KEYS,
    SENSOR_KEYS,
    SUBENTRY_PLANT,
)


def sensor_key(measurement: str) -> str:
    """Feld des Sensors im Unterpunkt."""
    return f"{measurement}_sensor"


@dataclass(frozen=True, slots=True)
class Plant:
    """Eine Pflanze mit ihren Sensoren (echte Sensoren, nicht die Spiegel von Plus)."""

    key: str
    subentry_id: str
    name: str
    species: str | None = None
    image: str | None = None
    # Raum beim Anlegen (Name); danach gilt der Raum des Geräts
    area: str | None = None
    outdoor: bool = False
    sensors: dict[str, str] = field(default_factory=dict)
    # Messgrößen, die als Problem zählen
    problems: frozenset[str] = frozenset(health.DEFAULT_PROBLEMS)
    # Wartezeit nach dem Gießen in Sekunden (so lange zählt „zu nass“ nicht)
    grace: int = 0

    @property
    def moisture(self) -> str | None:
        return self.sensors.get("moisture")

    def has(self, measurement: str) -> bool:
        return health.measured(measurement, self.sensors)


def from_subentry(subentry: ConfigSubentry) -> Plant:
    """Pflanze aus einem Unterpunkt; der Schlüssel bleibt nach dem Umzug der aus dem Plant Monitor."""
    data: Mapping[str, Any] = subentry.data
    sensors = {m: str(data[sensor_key(m)]) for m in health.SOURCES if data.get(sensor_key(m))}
    problems = data.get(CONF_PROBLEMS)
    return Plant(
        key=str(data.get(CONF_KEY) or subentry.subentry_id),
        subentry_id=subentry.subentry_id,
        name=str(data.get(CONF_NAME) or subentry.title),
        species=str(data.get(CONF_SPECIES) or "").strip() or None,
        image=str(data.get(CONF_IMAGE) or "").strip() or None,
        area=str(data.get(CONF_AREA) or "") or None,
        outdoor=bool(data.get(CONF_OUTDOOR)),
        sensors=sensors,
        problems=frozenset(problems) if isinstance(problems, list) else frozenset(health.DEFAULT_PROBLEMS),
        grace=int(data.get(CONF_GRACE) or 0),
    )


def find_plants(entry: ConfigEntry) -> dict[str, Plant]:
    """Alle Pflanzen des Eintrags, nach Schlüssel."""
    plants = [from_subentry(s) for s in entry.subentries.values() if s.subentry_type == SUBENTRY_PLANT]
    return {p.key: p for p in plants}


def status_id(key: str) -> str:
    """unique_id der Zustands-Entität (Ersatz für plant.* des Plant Monitor)."""
    return f"{key}-status"


def unique_ids(plant: Plant, *, ai: bool, weather: bool) -> dict[str, str]:
    """Alle Entitäten, die Plus für die Pflanze anlegt: unique_id → Domain."""
    key = plant.key
    ids = {status_id(key): "sensor"}
    ids |= {f"{key}-current-{m.replace('_', '-')}": "sensor" for m in plant.sensors}
    if plant.has("vpd"):
        ids[f"{key}-current-vpd"] = "sensor"
    if plant.has("dli"):
        ids |= dict.fromkeys((f"{key}-current-ppfd", f"{key}-ppfd-integral", f"{key}-dli", f"{key}-dli-24h"), "sensor")
    ids |= {f"{key}-{name.replace('_', '-')}": "number" for name in health.limit_fields(plant.sensors)}
    if plant.moisture:
        ids |= {f"{key}_{k}": "sensor" for k in SENSOR_KEYS}
        if ai:
            ids |= {f"{key}_suggestion": "sensor"} | {f"{key}_{k}": "button" for k in AI_KEYS if k != "suggestion"}
        if plant.outdoor and weather:
            ids |= {f"{key}_{k}": "binary_sensor" for k in OUTDOOR_KEYS}
    return ids


def entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str | None:
    """Entitäts-ID einer Entität von Plus."""
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


def device(hass: HomeAssistant, entry_id: str, plant: Plant) -> dr.DeviceEntry | None:
    """Gerät der Pflanze."""
    return dr.async_get(hass).async_get_device_by_identifier((DOMAIN, plant.key), entry_id)
