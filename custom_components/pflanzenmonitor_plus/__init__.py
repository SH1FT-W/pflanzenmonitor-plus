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
from .careplan import CARE_ISSUE, CarePlans
from .const import (
    CARE_KEYS,
    CONF_AI_TASK,
    CONF_CARE,
    CONF_RAIN_SENSOR,
    CONF_TODO,
    CONF_WEATHER,
    DOMAIN,
)
from .coordinator import PlusConfigEntry, PlusCoordinator
from .plant_monitor import sync_issues
from .plants import calendar_id, sensor_key, unique_ids
from .tasks import WateringTasks

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.NUMBER,
    Platform.SENSOR,
]
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
    _remove_stale(hass, entry, coordinator)
    if not entry.options.get(CONF_CARE):
        _remove_care(hass, entry, coordinator)
    sync_issues(hass, dict(entry.data))
    ai = entry.options.get(CONF_AI_TASK)
    if ai:
        advisor = coordinator.advisor = Advisor(hass, coordinator, ai)
        await advisor.async_load(set(coordinator.plants))
        advisor.sync_issues()
        entry.async_on_unload(advisor.shutdown)
    # Care calendar off: no plans at all (no entities, no AI requests); the stored dates stay for turning it on again
    if entry.options.get(CONF_CARE):
        plans = coordinator.care = CarePlans(
            hass, coordinator, ai, coordinator.advisor.lock if coordinator.advisor else None
        )
        await plans.async_load(set(coordinator.plants))
        plans.sync_issues()
        entry.async_on_unload(plans.shutdown)
    if todo := entry.options.get(CONF_TODO):
        coordinator.tasks = WateringTasks(hass, coordinator, todo)
        await coordinator.tasks.async_load(set(coordinator.plants))

    async def suggestions() -> None:
        """Limits first, then the care plan; both share one lock, so the AI never gets two requests at once."""
        if coordinator.advisor:
            await coordinator.advisor.async_run_due()
        if coordinator.care:
            await coordinator.care.async_run_due()

    @callback
    def updated() -> None:
        """After new data: due AI suggestions and (independently, a slow list must not wait for the AI) watering
        tasks in the background."""
        entry.async_create_background_task(hass, suggestions(), f"{DOMAIN} suggestions")
        if coordinator.tasks:
            entry.async_create_background_task(hass, coordinator.tasks.async_run(), f"{DOMAIN} watering tasks")

    entry.async_on_unload(coordinator.async_add_listener(updated))

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
    care = bool(entry.options.get(CONF_CARE))
    expected: set[str] = {calendar_id(entry.entry_id)} if care else set()
    for plant in coordinator.plants.values():
        expected |= set(unique_ids(plant, ai=ai, weather=weather, care=care))
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
        if issue_id.startswith("no_minimum_") or any(
            issue_id.startswith(prefix) and (not ai or issue_id.removeprefix(prefix) not in coordinator.plants)
            for prefix in (SUGGESTION_ISSUE, CARE_ISSUE)
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def _remove_care(hass: HomeAssistant, entry: PlusConfigEntry, coordinator: PlusCoordinator) -> None:
    """Care calendar off: remove its calendar, the care buttons and open care plan repairs."""
    care = {calendar_id(entry.entry_id)} | {f"{key}_{k}" for key in coordinator.plants for k in CARE_KEYS}
    registry = er.async_get(hass)
    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.unique_id in care:
            registry.async_remove(item.entity_id)
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(CARE_ISSUE):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def _rename(hass: HomeAssistant, entry: PlusConfigEntry, old: str, new: str) -> None:
    """Umbenannte Entität in den Optionen (Wetter, Regensensor, KI) und den Sensoren der Pflanzen nachziehen."""
    options: dict[str, Any] = dict(entry.options)
    for key in (CONF_AI_TASK, CONF_WEATHER, CONF_RAIN_SENSOR, CONF_TODO):
        if options.get(key) == old:
            options[key] = new
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
    """Hinweise unter Reparaturen und gespeicherte Daten (KI-Vorschläge, Grenzwerte, Live-Stand, Pflege, Aufgaben)
    mit entfernen."""
    for name in ("advice", "limits", "live", "care", "tasks"):
        await Store[dict[str, object]](hass, 1, f"{DOMAIN}.{entry.entry_id}.{name}").async_remove()
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
