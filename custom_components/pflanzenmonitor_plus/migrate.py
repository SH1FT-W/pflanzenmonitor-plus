"""Umzug aus dem Plant Monitor (HACS, Olen): Pflanzen übernehmen, dessen Einträge entfernen, Entitäts-IDs behalten.

Was übernommen wird, liest Plus im laufenden HA: die echten Grenzwerte stehen nur im Zustand der number-Entitäten
des Plant Monitor (im Config-Eintrag liegen bloß die Startwerte), die Sensoren in „external_sensor“ seiner
Spiegelsensoren. Namen, Symbole, Labels, Räume und Anzeige-Optionen aus den Registern ziehen mit um.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigSubentry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from . import health
from .const import (
    CONF_AI_TASK,
    CONF_GRACE,
    CONF_IMAGE,
    CONF_KEY,
    CONF_NAME,
    CONF_OUTDOOR,
    CONF_PROBLEMS,
    CONF_SPECIES,
    CONF_WEATHER,
    DOMAIN,
    LEGACY_DOMAIN,
    SUBENTRY_PLANT,
)
from .plants import from_subentry, sensor_key, status_id, unique_ids

if TYPE_CHECKING:
    from .coordinator import PlusConfigEntry

_LOGGER = logging.getLogger(__name__)

ISSUE_MIGRATE: Final = "migrate"
ISSUE_REFERENCES: Final = "references"
# Im Eintrag: alte Entitäts-ID der Pflanze → neue, alte Geräte-ID → neue (für den Hinweis auf Automationen, die noch
# die alten nutzen)
DATA_MIGRATED: Final = "migrated"
DATA_MIGRATED_DEVICES: Final = "migrated_devices"


@dataclass(slots=True)
class Legacy:
    """Eine Pflanze des Plant Monitor, wie sie umzieht."""

    entry: ConfigEntry
    data: dict[str, Any]
    limits: dict[str, float]
    # Live-Stand wie in live.py gespeichert (Lichtsumme, letzte Werte, Zustände, gemeldete Probleme)
    live: dict[str, Any] = field(default_factory=dict)
    # Registereinträge der Pflanze (alte Entitäten) und ihr Gerät
    entities: list[er.RegistryEntry] = field(default_factory=list)
    device: dr.DeviceEntry | None = None


def legacy_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Pflanzen-Einträge des Plant Monitor."""
    return [e for e in hass.config_entries.async_entries(LEGACY_DOMAIN) if "plant_info" in e.data]


def _number(hass: HomeAssistant, entity_id: str | None) -> tuple[float, str | None] | None:
    state = hass.states.get(entity_id) if entity_id else None
    if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None
    try:
        value = float(state.state)
    except ValueError:
        return None
    unit = state.attributes.get("unit_of_measurement")
    return value, str(unit) if unit else None


def _celsius(value: float, unit: str | None) -> float:
    if unit in (UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.KELVIN):
        return round(TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS), 1)
    return value


def read(hass: HomeAssistant, plus: ConfigEntry, entry: ConfigEntry) -> Legacy:
    """Alles, was zu einer Pflanze des Plant Monitor gehört."""
    info: dict[str, Any] = dict(entry.data["plant_info"])
    options = entry.options
    registry = er.async_get(hass)
    key = entry.entry_id

    def legacy_id(domain: str, suffix: str) -> str | None:
        return registry.async_get_entity_id(domain, LEGACY_DOMAIN, f"{key}-{suffix}")

    plant_id = registry.async_get_entity_id(LEGACY_DOMAIN, LEGACY_DOMAIN, key)
    data: dict[str, Any] = {CONF_KEY: key, CONF_NAME: str(info.get("name") or entry.title)}
    species = options.get("display_pid") or info.get("display_pid") or options.get("species") or info.get("species")
    data[CONF_SPECIES] = str(species or "").strip()
    data[CONF_IMAGE] = str(options.get("entity_picture") or info.get("entity_picture") or "").strip()
    data[CONF_OUTDOOR] = plant_id is not None and plant_id in (plus.options.get(CONF_OUTDOOR) or [])
    for m in health.SOURCES:
        # Der Spiegel weiß den aktuellen Sensor (der Plant Monitor überschreibt den Eintrag damit beim Start)
        mirror = hass.states.get(legacy_id("sensor", f"current-{m.replace('_', '-')}") or "")
        external = mirror.attributes.get("external_sensor") if mirror else None
        source = external if isinstance(external, str) and external else info.get(sensor_key(m))
        data[sensor_key(m)] = source or None
    data[CONF_PROBLEMS] = [m for m in health.MEASUREMENTS if options.get(f"{m}_trigger", m != "vpd")]
    data[CONF_GRACE] = int(options.get("moisture_grace_period") or 0)

    stored: dict[str, Any] = dict(info.get("limits") or {})
    stored_unit = info.get("limits_temperature_unit") or hass.config.units.temperature_unit
    limits: dict[str, float] = {}
    for name in health.LIMITS:
        live = _number(hass, legacy_id("number", name.replace("_", "-")))
        if live is not None:
            value, unit = live
        elif isinstance(stored.get(name), (int, float)):
            value, unit = float(stored[name]), stored_unit
        elif name == "lux_to_ppfd" and isinstance(info.get("lux_to_ppfd"), (int, float)):
            value, unit = float(info["lux_to_ppfd"]), None
        else:
            continue
        is_temperature = name.split("_", 1)[1] in health.TEMPERATURES
        limits[name] = _celsius(value, unit) if is_temperature else value

    device = dr.async_get(hass).async_get_device_by_identifier((LEGACY_DOMAIN, key), key)
    state = _live(hass, plant_id, legacy_id)
    return Legacy(entry, data, limits, state, er.async_entries_for_config_entry(registry, key), device)


def _live(hass: HomeAssistant, plant_id: str | None, legacy_id: Callable[[str, str], str | None]) -> dict[str, Any]:
    """Stand des Plant Monitor: Lichtsumme (gesamt, heute, gestern), letzte Messwerte, Zustände, Probleme."""
    values: dict[str, list[Any]] = {}
    for m in health.SOURCES:
        if found := _number(hass, legacy_id("sensor", f"current-{m.replace('_', '-')}")):
            values[m] = [found[0], found[1]]
    light: dict[str, Any] = {"day": dt_util.now().date().isoformat()}
    if found := _number(hass, legacy_id("sensor", "ppfd-integral")):
        light["total"] = found[0]
    if found := _number(hass, legacy_id("sensor", "dli")):
        light["today"] = found[0]
    dli = hass.states.get(legacy_id("sensor", "dli") or "")
    try:
        light["yesterday"] = float(dli.attributes["last_period"]) if dli else None
    except (KeyError, TypeError, ValueError):
        light["yesterday"] = None
    plant = hass.states.get(plant_id) if plant_id else None
    attrs = plant.attributes if plant else {}
    statuses = {m: attrs.get(f"{m}_status") for m in health.MEASUREMENTS if attrs.get(f"{m}_status")}
    problems = attrs.get("problems")
    logged = (
        sorted({str(p.get("sensor_type")) for p in problems if isinstance(p, dict)})
        if isinstance(problems, list)
        else []
    )
    return {"values": values, "light": light, "statuses": statuses, "logged": logged}


def _new_unique_id(key: str, old: er.RegistryEntry) -> str:
    """unique_id bei Plus: wie im Plant Monitor, nur plant.* wird zur Zustands-Entität."""
    return status_id(key) if old.domain == LEGACY_DOMAIN else old.unique_id


async def async_migrate(hass: HomeAssistant, entry: PlusConfigEntry) -> list[str]:
    """Alle Pflanzen übernehmen; gibt ihre Namen zurück.

    Reihenfolge: lesen → Grenzwerte speichern → Plant-Monitor-Einträge entfernen (gibt die Entitäts-IDs frei) →
    ohne await dazwischen Unterpunkte, Geräte und Registereinträge mit den alten IDs anlegen. Erst danach lädt Plus
    neu (Update-Listener) und findet alles vor.
    """
    found = legacy_entries(hass)
    if not found:
        return []
    # Nur geladene Einträge haben ihre echten Grenzwerte (number-Zustände); sonst gingen sie mit dem Entfernen verloren
    if any(e.state is not ConfigEntryState.LOADED for e in found):
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="legacy_not_loaded")
    legacy = [read(hass, entry, e) for e in found]
    entities, devices = er.async_get(hass), dr.async_get(hass)
    for item in legacy:
        # Eigene Entitäten von Plus hängen noch am Gerät des Plant Monitor: lösen, sonst löscht HA sie mit dem Gerät
        if item.device:
            for own in er.async_entries_for_device(entities, item.device.id, include_disabled_entities=True):
                if own.platform == DOMAIN:
                    entities.async_update_entity(own.entity_id, device_id=None)
        entry.runtime_data.limits.values[item.data[CONF_KEY]] = dict(item.limits)
        entry.runtime_data.live.seed(item.data[CONF_KEY], item.live)
    await entry.runtime_data.limits.async_save()
    await entry.runtime_data.live.async_flush()
    # Erst alle entladen: klappt das nicht, ist noch nichts entfernt (sonst schrieben alte Entitäten weiter)
    for item in legacy:
        if not await hass.config_entries.async_unload(item.entry.entry_id):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="legacy_unload_failed")
    for item in legacy:
        await hass.config_entries.async_remove(item.entry.entry_id)
        # Lief der Plant Monitor nicht (Fehler beim Start), stehen seine Zustände noch da und blockieren die IDs
        for old in item.entities:
            hass.states.async_remove(old.entity_id)
    _create(hass, entry, legacy, entities, devices)
    names = [str(item.data[CONF_NAME]) for item in legacy]
    _LOGGER.info("Aus dem Plant Monitor übernommen: %s", ", ".join(names))
    return names


@callback
def _create(
    hass: HomeAssistant,
    entry: PlusConfigEntry,
    legacy: list[Legacy],
    entities: er.EntityRegistry,
    devices: dr.DeviceRegistry,
) -> None:
    # Erst nach allen Änderungen einmal neu laden (sonst lädt schon die erste Pflanze neu, ohne die übrigen)
    entry.runtime_data.reload_pending = True
    migrated: dict[str, str] = dict(entry.data.get(DATA_MIGRATED) or {})
    moved: dict[str, str] = dict(entry.data.get(DATA_MIGRATED_DEVICES) or {})
    ai, weather = bool(entry.options.get(CONF_AI_TASK)), bool(entry.options.get(CONF_WEATHER))
    for item in legacy:
        key = item.data[CONF_KEY]
        subentry = ConfigSubentry(
            data=MappingProxyType(item.data), subentry_type=SUBENTRY_PLANT, title=item.data[CONF_NAME], unique_id=key
        )
        hass.config_entries.async_add_subentry(entry, subentry)
        plant = from_subentry(subentry)
        device = devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=subentry.subentry_id,
            identifiers={(DOMAIN, key)},
            name=plant.name,
            model=plant.species,
            manufacturer="Pflanzenmonitor Plus",
        )
        if item.device:
            devices.async_update_device(
                device.id,
                area_id=item.device.area_id,
                labels=set(item.device.labels),
                name_by_user=item.device.name_by_user,
            )
        wanted = unique_ids(plant, ai=ai, weather=weather)
        for old in item.entities:
            unique_id = _new_unique_id(key, old)
            domain = wanted.get(unique_id)
            if domain is None:
                continue
            object_id = old.entity_id.split(".", 1)[1]
            new = entities.async_get_or_create(
                domain,
                DOMAIN,
                unique_id,
                suggested_object_id=object_id,
                config_entry=entry,
                config_subentry_id=subentry.subentry_id,
                device_id=device.id,
                disabled_by=old.disabled_by if old.disabled_by == er.RegistryEntryDisabler.USER else None,
                hidden_by=old.hidden_by,
            )
            entities.async_update_entity(
                new.entity_id,
                aliases=old.aliases,
                area_id=old.area_id,
                categories=old.categories,
                icon=old.icon,
                labels=set(old.labels),
                name=old.name,
            )
            # Anzeige (sensor.display_precision usw.), Sprachassistent (conversation.should_expose) …
            for option_domain, values in old.options.items():
                entities.async_update_entity_options(new.entity_id, option_domain, dict(values))
            if old.domain == LEGACY_DOMAIN:
                migrated[old.entity_id] = new.entity_id
        # Automationen mit Geräte-Auslösern zeigen auf die alte Geräte-ID
        if item.device:
            moved[item.device.id] = device.id
    options = {k: v for k, v in entry.options.items() if k != CONF_OUTDOOR}
    data = {**entry.data, DATA_MIGRATED: migrated, DATA_MIGRATED_DEVICES: moved}
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_MIGRATE)
    hass.config_entries.async_schedule_reload(entry.entry_id)


@callback
def sync_issues(hass: HomeAssistant, entry: PlusConfigEntry) -> None:
    """Hinweise: Pflanzen im Plant Monitor zum Übernehmen; Automationen, die noch alte plant.*-IDs nutzen."""
    legacy = legacy_entries(hass)
    if legacy:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_MIGRATE,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_MIGRATE,
            translation_placeholders={"plants": ", ".join(sorted(e.title for e in legacy))},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_MIGRATE)
    lines = _references(
        hass, dict(entry.data.get(DATA_MIGRATED) or {}), dict(entry.data.get(DATA_MIGRATED_DEVICES) or {})
    )
    if lines:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_REFERENCES,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_REFERENCES,
            translation_placeholders={"references": "\n".join(lines)},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_REFERENCES)


def _references(hass: HomeAssistant, migrated: dict[str, str], moved: dict[str, str]) -> list[str]:
    """Automationen und Skripte, die noch eine alte plant.*-Entität oder ein altes Gerät nutzen.

    Dashboards kann HA nicht durchsuchen.
    """
    found: list[str] = []
    for component in ("automation", "script"):
        if component not in hass.config.components:
            continue
        if component == "automation":
            from homeassistant.components.automation import (
                automations_with_device as with_device,
                automations_with_entity as with_entity,
            )
        else:
            from homeassistant.components.script import (
                scripts_with_device as with_device,
                scripts_with_entity as with_entity,
            )
        pairs = [(with_entity(hass, old), f"{old} → {new}") for old, new in migrated.items()]
        pairs += [(with_device(hass, old), f"device_id {old} → {new}") for old, new in moved.items()]
        for users, what in pairs:
            for user in users:
                state = hass.states.get(user)
                found.append(f"- {state.name if state else user}: {what}")
    return sorted(set(found))
