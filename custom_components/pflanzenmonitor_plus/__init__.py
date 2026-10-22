"""Pflanzenmonitor Plus: Pflanzen überwachen, lernen, wann gegossen wurde und wann wieder, Grenzwerte per KI."""

from __future__ import annotations

from typing import Any

from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from . import health, services
from .advisor import SUGGESTION_ISSUE, Advisor
from .const import CONF_AI_TASK, CONF_OUTDOOR, CONF_RAIN_SENSOR, CONF_WEATHER, DOMAIN
from .coordinator import PlusConfigEntry, PlusCoordinator
from .migrate import legacy_entries, sync_issues
from .plants import sensor_key, unique_ids

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Aktionen einmal für die Integration anmelden."""
    services.async_setup(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PlusConfigEntry) -> bool:
    """Pflanzen laden, Entitäten anlegen; gelernt wird erst, wenn HA (und der Recorder) gestartet ist."""
    coordinator = PlusCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.limits.async_load(set(coordinator.plants))
    await coordinator.live.async_load(coordinator.plants)
    # Solange der Plant Monitor noch Pflanzen hat, nichts wegräumen: dessen Geräte tragen noch Entitäten von Plus
    if not legacy_entries(hass):
        _remove_stale(hass, entry, coordinator)
    sync_issues(hass, entry)
    if ai := entry.options.get(CONF_AI_TASK):
        advisor = coordinator.advisor = Advisor(hass, coordinator, ai)
        await advisor.async_load(set(coordinator.plants))
        advisor.sync_issues()

        @callback
        def updated() -> None:
            """Nach neuen Daten fällige KI-Vorschläge im Hintergrund holen."""
            entry.async_create_background_task(hass, advisor.async_run_due(), f"{DOMAIN} KI-Vorschläge")

        entry.async_on_unload(coordinator.async_add_listener(updated))
        entry.async_on_unload(advisor.shutdown)

    coordinator.async_track_moisture()
    for unsub in coordinator.live.async_start():
        entry.async_on_unload(unsub)

    @callback
    def registry_changed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        """Ein benutzter Sensor wurde umbenannt: in den Pflanzen und Optionen nachziehen (lädt danach neu)."""
        data = event.data
        if data["action"] == "update" and isinstance(old := data.get("old_entity_id"), str):
            _rename(hass, entry, old, data["entity_id"])

    entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_changed))
    entry.async_on_unload(entry.add_update_listener(_changed))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def first(_hass: HomeAssistant) -> None:
        await coordinator.async_refresh()

    # Erst nach den Entitäten: dann gibt es die Spiegel schon, deren Verlauf beim Lernen mitzählt
    entry.async_on_unload(async_at_started(hass, first))
    return True


async def _changed(hass: HomeAssistant, entry: PlusConfigEntry) -> None:
    """Optionen oder Pflanzen geändert: einmal neu laden (auch wenn sich mehreres auf einmal ändert)."""
    if not entry.runtime_data.reload_pending:
        entry.runtime_data.reload_pending = True
        hass.config_entries.async_schedule_reload(entry.entry_id)


def _remove_stale(hass: HomeAssistant, entry: PlusConfigEntry, coordinator: PlusCoordinator) -> None:
    """Entitäten und Geräte entfernen, die es nicht mehr geben soll (Sensor abgewählt, KI/Wetter aus, nach drinnen)."""
    ai, weather = bool(entry.options.get(CONF_AI_TASK)), bool(entry.options.get(CONF_WEATHER))
    expected: set[str] = set()
    for plant in coordinator.plants.values():
        expected |= set(unique_ids(plant, ai=ai, weather=weather))
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.unique_id not in expected:
            registry.async_remove(item.entity_id)
    devices = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if not any(domain == DOMAIN and key in coordinator.plants for domain, key in device.identifiers):
            devices.async_remove_device(device.id)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain != DOMAIN:
            continue
        # Bis 26.10: Hinweis „keine Untergrenze“ (Plus hat jetzt immer eine)
        if issue_id.startswith("no_minimum_") or (
            issue_id.startswith(SUGGESTION_ISSUE)
            and (not ai or issue_id.removeprefix(SUGGESTION_ISSUE) not in coordinator.plants)
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def _rename(hass: HomeAssistant, entry: PlusConfigEntry, old: str, new: str) -> None:
    """Umbenannte Entität in den Optionen (Wetter, Regensensor, KI) und den Sensoren der Pflanzen nachziehen."""
    options: dict[str, Any] = dict(entry.options)
    for key in (CONF_AI_TASK, CONF_WEATHER, CONF_RAIN_SENSOR):
        if options.get(key) == old:
            options[key] = new
    # Bis zum Umzug: Pflanzen draußen als plant.*-Liste
    if isinstance(outdoor := options.get(CONF_OUTDOOR), list) and old in outdoor:
        options[CONF_OUTDOOR] = [new if v == old else v for v in outdoor]
    if options != dict(entry.options):
        hass.config_entries.async_update_entry(entry, options=options)
    for subentry in entry.subentries.values():
        changed = {sensor_key(m): new for m in health.SOURCES if subentry.data.get(sensor_key(m)) == old}
        if changed:
            hass.config_entries.async_update_subentry(entry, subentry, data={**subentry.data, **changed})


async def async_unload_entry(hass: HomeAssistant, entry: PlusConfigEntry) -> bool:
    """Entitäten entfernen; Live-Stand und Grenzwerte vorher speichern (der neu geladene Eintrag liest sie)."""
    await entry.runtime_data.live.async_flush()
    await entry.runtime_data.limits.async_save()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: PlusConfigEntry) -> None:
    """Hinweise unter Reparaturen und gespeicherte Daten (KI-Vorschläge, Grenzwerte, Live-Stand) mit entfernen."""
    for name in ("advice", "limits", "live"):
        await Store[dict[str, object]](hass, 1, f"{DOMAIN}.{entry.entry_id}.{name}").async_remove()
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
