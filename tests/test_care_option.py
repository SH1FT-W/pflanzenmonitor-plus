"""Option "care calendar": off by default (no calendar, no care buttons, no AI care plan), switchable both ways."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.storage import Store
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.pflanzenmonitor_plus.const import CONF_AI_TASK, CONF_CARE, CONF_WEATHER, DOMAIN

from .conftest import INSIDE, OUTSIDE, FakeAI, setup

pytestmark = pytest.mark.usefixtures("house", "stats", "forecast")

CALENDAR = "calendar.pflanzenpflege"
CARE_ENTITIES = {
    CALENDAR,
    "button.schlafzimmer_bogenhanf_gedungt",
    "button.schlafzimmer_bogenhanf_umgetopft",
    "button.terrasse_olivenbaum_gedungt",
    "button.terrasse_olivenbaum_umgetopft",
}
STORED = {"plants": {INSIDE: {"fertilized": "2026-10-01", "repotted": "2025-04-01", "state": "keiner"}}}


def registered(hass: HomeAssistant, entry: MockConfigEntry) -> set[str]:
    return {e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


async def stored(hass: HomeAssistant, entry: MockConfigEntry) -> Any:
    return await Store[dict[str, Any]](hass, 1, f"{DOMAIN}.{entry.entry_id}.care").async_load()


async def set_care(hass: HomeAssistant, entry: MockConfigEntry, on: bool) -> None:
    """Options dialog: keep AI and weather, switch only the care calendar."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    keep = {k: v for k, v in entry.options.items() if k in (CONF_AI_TASK, CONF_WEATHER)}
    await hass.config_entries.options.async_configure(result["flow_id"], keep | {CONF_CARE: on})
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_off_by_default(
    hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry, hass_client: ClientSessionGenerator
) -> None:
    """Off (also for installs of 26.11.1): no calendar, no care buttons, no AI care requests, no care repairs;
    the stored care dates stay."""
    await Store[dict[str, Any]](hass, 1, f"{DOMAIN}.{ai_entry.entry_id}.care").async_save(STORED)
    # Leftovers of 26.11.1: calendar, buttons and an open care repair
    registry = er.async_get(hass)
    registry.async_get_or_create("calendar", DOMAIN, f"{ai_entry.entry_id}-care", config_entry=ai_entry)
    registry.async_get_or_create("button", DOMAIN, f"{INSIDE}_fertilized", config_entry=ai_entry)
    ir.async_create_issue(
        hass, DOMAIN, f"care_{INSIDE}", is_fixable=True, severity=ir.IssueSeverity.WARNING, translation_key="care"
    )
    await setup(hass, ai_entry)

    assert ai_entry.runtime_data.care is None
    assert not registered(hass, ai_entry) & CARE_ENTITIES
    assert registry.async_get_entity_id("calendar", DOMAIN, f"{ai_entry.entry_id}-care") is None
    assert hass.states.get(CALENDAR) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"care_{INSIDE}") is None
    # The AI is asked for limits only, never for a care plan
    assert ai.calls
    assert ai.care_calls == []
    assert await stored(hass, ai_entry) == STORED
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "care_done", {"plant_entity": "sensor.schlafzimmer_bogenhanf", "care": "fertilized"}, blocking=True
        )
    assert err.value.translation_key == "care_off"
    diag = await get_diagnostics_for_config_entry(hass, hass_client, ai_entry)
    assert diag["plants"][INSIDE]["care"] is None


async def test_switch_on_off_on(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    """On: calendar, buttons, AI care plan, stored dates back. Off: entities removed from the registry, repairs gone,
    dates kept. On again: nothing lost."""
    await Store[dict[str, Any]](hass, 1, f"{DOMAIN}.{ai_entry.entry_id}.care").async_save(STORED)
    await setup(hass, ai_entry)
    assert hass.states.get(CALENDAR) is None

    await set_care(hass, ai_entry, True)
    assert ai_entry.options[CONF_CARE] is True
    assert registered(hass, ai_entry) >= CARE_ENTITIES
    assert hass.states.get(CALENDAR) is not None
    care = ai_entry.runtime_data.care
    assert care is not None
    assert care.record(INSIDE)["fertilized"] == "2026-10-01"
    # The AI care plan runs again (one plant per round) and opens a repair
    assert len(ai.care_calls) == 1
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"care_{INSIDE}") is not None
    await hass.services.async_call(
        DOMAIN, "care_done", {"plant_entity": "sensor.terrasse_olivenbaum", "care": "repotted"}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    saved = await stored(hass, ai_entry)

    await set_care(hass, ai_entry, False)
    assert ai_entry.runtime_data.care is None
    assert not registered(hass, ai_entry) & CARE_ENTITIES
    assert hass.states.get(CALENDAR) is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"care_{INSIDE}") is None
    assert await stored(hass, ai_entry) == saved
    calls = len(ai.care_calls)

    await set_care(hass, ai_entry, True)
    care = ai_entry.runtime_data.care
    assert care is not None
    assert care.records[INSIDE] == saved["plants"][INSIDE]
    assert care.record(OUTSIDE)["repotted"] == saved["plants"][OUTSIDE]["repotted"]
    assert registered(hass, ai_entry) >= CARE_ENTITIES
    # The suggestion of this season for the first plant is still there: not asked again (next plant's turn)
    assert [c["task_name"] for c in ai.care_calls[calls:]] == ["Pflanzenmonitor Plus care: Olivenbaum"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"care_{INSIDE}") is not None
