"""Aktion „Sensor tauschen“: einem Messwert einer Pflanze einen anderen echten Sensor geben (oder ihn entfernen)."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
import voluptuous as vol

from . import health
from .const import DOMAIN
from .plants import sensor_key

SERVICE_REPLACE_SENSOR: Final = "replace_sensor"
ATTR_METER: Final = "meter_entity"
ATTR_NEW: Final = "new_sensor"
SCHEMA: Final = vol.Schema(
    {vol.Required(ATTR_METER): cv.entity_id, vol.Optional(ATTR_NEW): vol.Any(None, "", cv.entity_id)}
)


@callback
def async_setup(hass: HomeAssistant) -> None:
    hass.services.async_register(DOMAIN, SERVICE_REPLACE_SENSOR, _replace, schema=SCHEMA)


async def _replace(call: ServiceCall) -> None:
    """Sensor im Unterpunkt der Pflanze ersetzen; Plus lädt danach neu (ohne Sensor verschwinden seine Entitäten)."""
    hass = call.hass
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    entry = entries[0]
    meter = call.data[ATTR_METER]
    item = er.async_get(hass).async_get(meter)
    measurement = None
    key = None
    if item is not None and item.platform == DOMAIN and item.domain == Platform.SENSOR:
        for m in health.SOURCES:
            suffix = f"-current-{m.replace('_', '-')}"
            if item.unique_id.endswith(suffix):
                measurement, key = m, item.unique_id.removesuffix(suffix)
    plant = entry.runtime_data.plants.get(key) if key else None
    if plant is None or measurement is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_a_meter", translation_placeholders={"entity": meter}
        )
    new = call.data.get(ATTR_NEW) or None
    if new is not None and (not new.startswith(f"{Platform.SENSOR}.") or hass.states.get(new) is None):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="sensor_not_found", translation_placeholders={"entity": new}
        )
    subentry = entry.subentries[plant.subentry_id]
    hass.config_entries.async_update_subentry(entry, subentry, data={**subentry.data, sensor_key(measurement): new})
