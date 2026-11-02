"""Care calendar in the test house: events, buttons, action care_done, AI suggestions for the care plan."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pflanzenmonitor_plus.const import DOMAIN
from custom_components.pflanzenmonitor_plus.repairs import async_create_fix_flow

from .conftest import INSIDE, OUTSIDE, FakeAI, now, pending, setup

pytestmark = pytest.mark.usefixtures("house", "stats", "forecast")

CALENDAR = "calendar.pflanzenpflege"
FERTILIZED = "button.schlafzimmer_bogenhanf_gedungt"
REPOTTED = "button.schlafzimmer_bogenhanf_umgetopft"
ISSUE = f"care_{INSIDE}"
ALL_YEAR = {"fertilize_interval": 14, "fertilize_from": 1, "fertilize_to": 12}


async def events(hass: HomeAssistant, start: str, end: str) -> list[dict[str, Any]]:
    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": CALENDAR, "start_date_time": start, "end_date_time": end},
        blocking=True,
        return_response=True,
    )
    found: list[dict[str, Any]] = result[CALENDAR]["events"]  # type: ignore[index]
    return found


def state_of(hass: HomeAssistant, entity_id: str) -> Any:
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def press(hass: HomeAssistant, entity_id: str) -> None:
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)


async def care_done(hass: HomeAssistant, entity_id: str, kind: str, day: str | None = None) -> None:
    data = {"plant_entity": entity_id, "care": kind} | ({"date": day} if day else {})
    await hass.services.async_call(DOMAIN, "care_done", data, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_default_plan_waits_for_spring(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Mid-October, default plan: nothing until March (winter pause), then fertilizing every 3 weeks."""
    await setup(hass, entry)
    state = state_of(hass, CALENDAR)
    assert state.state == "off"
    assert state.attributes["message"] == "Bogenhanf düngen"
    assert state.attributes["start_time"] == "2027-03-01 00:00:00"
    assert state.attributes["description"] == "Düngen März bis September alle 21 Tage, im Winter Pause."

    assert await events(hass, "2026-10-16T00:00:00+02:00", "2027-02-28T00:00:00+01:00") == []
    found = await events(hass, "2027-03-01T00:00:00+01:00", "2027-03-31T00:00:00+02:00")
    assert [(e["start"], e["summary"]) for e in found] == [
        ("2027-03-01", "Bogenhanf düngen"),
        ("2027-03-01", "Olivenbaum düngen"),
        ("2027-03-01", "Bogenhanf umtopfen"),
        ("2027-03-01", "Olivenbaum umtopfen"),
        ("2027-03-22", "Bogenhanf düngen"),
        ("2027-03-22", "Olivenbaum düngen"),
    ]
    assert found[2]["description"] == "Umtopfen alle 2 Jahre im März."


async def test_buttons_and_action_move_dates(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    pending(hass, INSIDE).update(ALL_YEAR)
    await setup(hass, entry)
    # Never fertilized, in season: due today
    state = state_of(hass, CALENDAR)
    assert state.state == "on"
    assert state.attributes["message"] == "Bogenhanf düngen"

    await press(hass, FERTILIZED)
    assert entry.runtime_data.care.next_date(INSIDE, "fertilized") == date(2026, 10, 30)
    state = state_of(hass, CALENDAR)
    assert state.state == "off"
    assert state.attributes["start_time"] == "2026-10-30 00:00:00"

    # Season from January: repotting moves to January in two years
    await press(hass, REPOTTED)
    assert entry.runtime_data.care.next_date(INSIDE, "repotted") == date(2028, 1, 1)
    # Action with a date, by the plant's status entity or any other entity of the plant
    await care_done(hass, "sensor.schlafzimmer_bogenhanf", "repotted", "2025-04-01")
    assert entry.runtime_data.care.next_date(INSIDE, "repotted") == date(2027, 1, 1)
    await care_done(hass, "sensor.terrasse_olivenbaum_temperatur", "repotted", "2026-04-01")
    assert entry.runtime_data.care.next_date(OUTSIDE, "repotted") == date(2028, 3, 1)

    # Survives a reload
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.runtime_data.care.next_date(INSIDE, "fertilized") == date(2026, 10, 30)
    assert entry.runtime_data.care.last(OUTSIDE, "repotted") == date(2026, 4, 1)


async def test_action_errors(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    for target, day, key in (
        ("sensor.gibt_es_nicht", None, "not_a_plant"),
        (CALENDAR, None, "not_a_plant"),
        ("sensor.schlafzimmer_bogenhanf", (now() + timedelta(days=2)).date().isoformat(), "date_in_future"),
    ):
        with pytest.raises(ServiceValidationError) as err:
            await care_done(hass, target, "fertilized", day)
        assert err.value.translation_key == key
    with pytest.raises(ServiceValidationError) as err:
        await entry.runtime_data.care.async_done("GONE", "fertilized")
    assert err.value.translation_key == "plant_removed"
    # Without AI there is nothing to accept or reject
    plans = entry.runtime_data.care
    async with plans._lock:
        with pytest.raises(ServiceValidationError) as err:
            await plans.async_apply(INSIDE)
        assert err.value.translation_key == "busy"
    for action in (plans.async_apply, plans.async_reject):
        with pytest.raises(ServiceValidationError) as err:
            await action(INSIDE)
        assert err.value.translation_key == "no_suggestion"
    assert await hass.config_entries.async_unload(entry.entry_id)
    # Unloaded: an old instance stores nothing anymore
    await plans.async_done(INSIDE, "fertilized")
    store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.care")
    assert INSIDE not in ((await store.async_load()) or {}).get("plants", {})
    with pytest.raises(ServiceValidationError) as err:
        await care_done(hass, "sensor.schlafzimmer_bogenhanf", "fertilized")
    assert err.value.translation_key == "not_loaded"


async def test_stored_records_of_removed_plants_dropped(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.care")
    await store.async_save({"plants": {INSIDE: {"fertilized": "2026-10-01", "state": "kaputt"}, "GONE": {}}})
    await setup(hass, entry)
    care = entry.runtime_data.care
    assert set(care.records) == {INSIDE}
    assert care.record(INSIDE)["state"] == "keiner"
    assert care.last(INSIDE, "fertilized") == date(2026, 10, 1)


async def open_flow(hass: HomeAssistant, issue_id: str = ISSUE) -> Any:
    manager = repairs_flow_manager(hass)
    assert manager is not None
    return await manager.async_init(DOMAIN, data={"issue_id": issue_id})


async def choose(hass: HomeAssistant, flow_id: str, step: str) -> Any:
    manager = repairs_flow_manager(hass)
    assert manager is not None
    result = await manager.async_configure(flow_id, {"next_step_id": step})
    await hass.async_block_till_done(wait_background_tasks=True)
    return result


async def next_round(hass: HomeAssistant, minutes: int) -> None:
    async_fire_time_changed(hass, now() + timedelta(minutes=minutes))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_ai_suggestion_accept_and_reject(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    assert await async_setup_component(hass, "repairs", {})
    await setup(hass, ai_entry)
    # One plant per round, one request per season
    assert len(ai.care_calls) == 1
    call = ai.care_calls[0]
    assert call["task_name"] == "Pflanzenmonitor Plus care: Bogenhanf"
    assert '"hemisphere": "north"' in call["instructions"]
    assert "fertilize_interval" in call["structure"]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)
    assert issue is not None
    assert issue.translation_placeholders == {"plant": "Bogenhanf"}

    result = await open_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["description_placeholders"] == {
        "plant": "Bogenhanf",
        "current": "Düngen März bis September alle 21 Tage, im Winter Pause; Umtopfen alle 2 Jahre im März.",
        "proposed": "Düngen April bis August alle 14 Tage, im Winter Pause; Umtopfen alle 3 Jahre im April.",
        "reason": "Wächst langsam.",
    }
    result = await choose(hass, result["flow_id"], "accept")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Written into the plant (reloaded), state kept, repair gone
    plant = ai_entry.runtime_data.plants[INSIDE]
    assert plant.care == {"fertilize_interval": 14, "fertilize_from": 4, "fertilize_to": 8, "repot_years": 3}
    assert ai_entry.runtime_data.care.record(INSIDE)["state"] == "uebernommen"
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None

    # Next round: the other plant; reject keeps its plan
    await next_round(hass, 10)
    assert len(ai.care_calls) == 2
    result = await open_flow(hass, f"care_{OUTSIDE}")
    result = await choose(hass, result["flow_id"], "reject")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert ai_entry.runtime_data.plants[OUTSIDE].care == {}
    assert ai_entry.runtime_data.care.record(OUTSIDE)["state"] == "abgelehnt"
    # Nothing open anymore: a stale dialog stops
    for key in (INSIDE, OUTSIDE):
        flow = await async_create_fix_flow(hass, f"care_{key}", {"key": key})
        flow.hass = hass
        result = await flow.async_step_init()
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "no_suggestion"

    # Same season: no more requests
    await next_round(hass, 20)
    assert len(ai.care_calls) == 2

    # One lock for both kinds of suggestions: "Ask AI now" waits while a care request runs, and vice versa
    care, advisor = ai_entry.runtime_data.care, ai_entry.runtime_data.advisor
    assert advisor is not None
    async with care._lock:
        with pytest.raises(ServiceValidationError) as err:
            await advisor.async_ask(INSIDE)
        assert err.value.translation_key == "busy"
    async with advisor.lock:
        care.records[INSIDE]["season"] = None
        await next_round(hass, 30)
    assert len(ai.care_calls) == 2


async def test_ai_next_season_no_change_and_errors(
    hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry, freezer: FrozenDateTimeFactory
) -> None:
    ai.care_answer = {"fertilize_interval": 21, "fertilize_from": 3, "fertilize_to": 9, "repot_years": 2}
    await setup(hass, ai_entry)
    care = ai_entry.runtime_data.care
    # Same as the current plan: no repair
    assert care.record(INSIDE)["state"] == "keine_aenderung"
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None

    # Unusable answer: blocked for 6 hours, then asked again
    ai.care_answer = {"reason": "?"}
    await next_round(hass, 10)
    assert care.record(OUTSIDE)["error"] == "unusable answer"
    calls = len(ai.care_calls)
    await next_round(hass, 20)
    assert len(ai.care_calls) == calls

    # Next season (December): both plants are asked again, one per round
    ai.care_answer = {"fertilize_interval": 30, "fertilize_from": 4, "fertilize_to": 9, "repot_years": 2}
    freezer.move_to("2026-12-02 12:00:00+00:00")
    async_fire_time_changed(hass, dt_now := now() + timedelta(days=47))
    await hass.async_block_till_done(wait_background_tasks=True)
    async_fire_time_changed(hass, dt_now + timedelta(minutes=10))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert care.record(INSIDE)["state"] == "offen"
    assert care.record(INSIDE)["season"] == "2027-0"
    assert care.record(OUTSIDE)["state"] == "offen"


async def test_repair_outdated_removed_and_ai_off(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    assert await async_setup_component(hass, "repairs", {})
    await setup(hass, ai_entry)
    care = ai_entry.runtime_data.care
    # The plant's subentry is gone (removed, reload pending): nothing is marked as accepted
    plant = care.coordinator.plants[INSIDE]
    care.coordinator.plants[INSIDE] = dataclasses.replace(plant, subentry_id="GONE")
    with pytest.raises(ServiceValidationError):
        await care.async_apply(INSIDE)
    assert care.record(INSIDE)["state"] == "offen"
    care.coordinator.plants[INSIDE] = plant
    result = await open_flow(hass)
    # A newer suggestion meanwhile: not accepted unseen
    care.records[INSIDE]["asked"] = "2026-10-16T13:00:00+00:00"
    result = await choose(hass, result["flow_id"], "accept")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "suggestion_outdated"
    # Plant removed between opening and accepting
    result = await open_flow(hass)
    subentry = ai_entry.runtime_data.plants[INSIDE].subentry_id
    care.coordinator.plants.pop(INSIDE)
    result = await choose(hass, result["flow_id"], "accept")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_suggestion"
    # Issue of a plant that is gone disappears with the next save
    care.records[OUTSIDE] = {"state": "keiner"}
    await care.async_done(OUTSIDE, "fertilized")
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE) is None
    flow = await async_create_fix_flow(hass, ISSUE, {"key": INSIDE})
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["reason"] == "no_suggestion"
    assert hass.config_entries.async_remove_subentry(ai_entry, subentry)
    await hass.async_block_till_done(wait_background_tasks=True)

    # AI turned off: open care repairs go away
    ai_entry.runtime_data.care.records[OUTSIDE] = {"state": "offen", "proposal": {}}
    await ai_entry.runtime_data.care.async_done(OUTSIDE, "fertilized")
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"care_{OUTSIDE}") is not None
    hass.config_entries.async_update_entry(ai_entry, options={})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"care_{OUTSIDE}") is None
