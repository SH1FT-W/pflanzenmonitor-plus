"""Auslöser und Bedingungen in Automationen (wie im Plant Monitor, für die Zustands-Entitäten von Plus)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pflanzenmonitor_plus.condition import async_get_conditions
from custom_components.pflanzenmonitor_plus.trigger import async_get_triggers

from .conftest import setup

pytestmark = pytest.mark.usefixtures("house", "stats", "forecast")

STATUS = "sensor.schlafzimmer_bogenhanf"
SOURCE = "sensor.bogenhanf_bodenfeuchtigkeit"


@pytest.fixture
def calls(hass: HomeAssistant) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    async def record(call: ServiceCall) -> None:
        found.append(dict(call.data))

    hass.services.async_register("test", "record", record)
    return found


def automation(name: str, trigger: dict[str, Any], condition: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "alias": name,
        "triggers": [trigger],
        "conditions": [condition] if condition else [],
        "actions": [{"action": "test.record", "data": {"name": name}}],
    }


async def moisture(hass: HomeAssistant, value: str) -> None:
    hass.states.async_set(SOURCE, value, {"unit_of_measurement": "%"})
    await hass.async_block_till_done()


async def test_state_triggers_and_conditions(
    hass: HomeAssistant, entry: MockConfigEntry, calls: list[dict[str, Any]]
) -> None:
    await setup(hass, entry)
    target = {"entity_id": STATUS}
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                automation("problem", {"trigger": "pflanzenmonitor_plus.problem_detected", "target": target}),
                automation("ok", {"trigger": "pflanzenmonitor_plus.problem_cleared", "target": target}),
                automation(
                    "trocken",
                    {"trigger": "pflanzenmonitor_plus.moisture_became_low", "target": target},
                    {"condition": "pflanzenmonitor_plus.has_problem", "target": target},
                ),
                automation(
                    "nass",
                    {"trigger": "pflanzenmonitor_plus.moisture_became_high", "target": target},
                    {"condition": "pflanzenmonitor_plus.moisture_is_high", "target": target},
                ),
                # Nur Pflanzen: ein anderer Sensor im Ziel zählt nicht
                automation(
                    "fremd",
                    {"trigger": "pflanzenmonitor_plus.problem_detected", "target": {"entity_id": SOURCE}},
                ),
            ]
        },
    )
    await moisture(hass, "10")
    assert [c["name"] for c in calls] == ["problem", "trocken"]
    await moisture(hass, "40")
    assert [c["name"] for c in calls][-1] == "ok"
    calls.clear()
    await moisture(hass, "90")
    assert [c["name"] for c in calls] == ["problem", "nass"]


async def test_stale_trigger_and_condition(
    hass: HomeAssistant, entry: MockConfigEntry, calls: list[dict[str, Any]], freezer: Any
) -> None:
    await setup(hass, entry)
    target = {"entity_id": STATUS}
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                automation(
                    "veraltet",
                    {
                        "trigger": "pflanzenmonitor_plus.moisture_sensor_became_stale",
                        "target": target,
                        "options": {"for": {"hours": 1}},
                    },
                ),
                automation(
                    "zurück",
                    {
                        "trigger": "pflanzenmonitor_plus.moisture_sensor_became_fresh",
                        "target": target,
                        "options": {"for": {"hours": 1}},
                    },
                    {
                        "condition": "pflanzenmonitor_plus.temperature_sensor_is_stale",
                        "target": target,
                        "options": {"for": {"minutes": 30}},
                    },
                ),
                automation(
                    "bodenfeuchte veraltet",
                    {"trigger": "pflanzenmonitor_plus.temperature_sensor_became_stale", "target": target},
                    {
                        "condition": "pflanzenmonitor_plus.moisture_sensor_is_stale",
                        "target": target,
                        "options": {"for": {"hours": 1}},
                    },
                ),
            ]
        },
    )
    # Ausfall: sofort veraltet
    await moisture(hass, "unavailable")
    assert [c["name"] for c in calls] == ["veraltet"]
    await moisture(hass, "unavailable")
    assert len(calls) == 1
    # Meldet wieder; Temperatursensor hat sich seit über 30 Min. nicht gemeldet → Bedingung wahr
    freezer.tick(timedelta(minutes=40))
    await moisture(hass, "45")
    assert [c["name"] for c in calls] == ["veraltet", "zurück"]
    # Meldung mit gleichem Wert zählt als Meldung: nach 50 Min. gleiche 45 → nach weiteren 20 Min. nicht veraltet
    freezer.tick(timedelta(minutes=50))
    await moisture(hass, "45")
    freezer.tick(timedelta(minutes=20))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert [c["name"] for c in calls] == ["veraltet", "zurück"]
    # Eine Stunde ohne Meldung → veraltet („no_update“)
    freezer.tick(timedelta(minutes=41))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert [c["name"] for c in calls][-1] == "veraltet"
    # Meldet wieder mit gleichem Wert (keine Zustandsänderung) → beim nächsten Nachsehen „zurück“
    await moisture(hass, "45")
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert [c["name"] for c in calls][-1] == "zurück"
    # Temperatursensor über 24 Std. still → veraltet; Bodenfeuchte seit über einer Stunde still (Bedingung wahr)
    freezer.tick(timedelta(hours=23))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert "bodenfeuchte veraltet" in [c["name"] for c in calls]
    await hass.services.async_call("automation", "turn_off", {"entity_id": "all"}, blocking=True)


async def test_stale_at_start(
    hass: HomeAssistant, entry: MockConfigEntry, calls: list[dict[str, Any]], freezer: Any
) -> None:
    """Schon beim Start nicht verfügbar (z. B. nach einem Neustart): erst nach `for` als veraltet melden."""
    hass.states.async_set(SOURCE, "unavailable")
    await setup(hass, entry)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                automation(
                    "veraltet",
                    {
                        "trigger": "pflanzenmonitor_plus.moisture_sensor_became_stale",
                        "target": {"entity_id": STATUS},
                        "options": {"for": {"minutes": 10}},
                    },
                ),
                automation(
                    "zurück",
                    {
                        "trigger": "pflanzenmonitor_plus.moisture_sensor_became_fresh",
                        "target": {"entity_id": STATUS},
                        "options": {"for": {"minutes": 10}},
                    },
                ),
            ]
        },
    )
    await moisture(hass, "unavailable")
    assert not calls
    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert [c["name"] for c in calls] == ["veraltet"]
    await moisture(hass, "50")
    assert [c["name"] for c in calls] == ["veraltet", "zurück"]
    # Wieder gesund innerhalb der Wartezeit: nichts
    calls.clear()
    await moisture(hass, "unavailable")
    assert [c["name"] for c in calls] == ["veraltet"]
    await moisture(hass, "50")
    assert [c["name"] for c in calls] == ["veraltet", "zurück"]
    await hass.services.async_call("automation", "turn_off", {"entity_id": "all"}, blocking=True)


async def test_catalog(hass: HomeAssistant) -> None:
    triggers = await async_get_triggers(hass)
    conditions = await async_get_conditions(hass)
    # Wie im Plant Monitor: 2 + 9 × 3 + 7 × 2 Auslöser, 1 + 9 × 3 + 7 Bedingungen
    assert len(triggers) == 43
    assert len(conditions) == 35
    assert "vpd_became_high" in triggers
    assert "co2_sensor_is_stale" in conditions


async def test_descriptions_valid(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """HA liest triggers.yaml, conditions.yaml und services.yaml ohne Fehler (prüft auch das Format)."""
    from homeassistant.helpers import (
        condition as condition_helper,
        service as service_helper,
        trigger as trigger_helper,
    )

    await setup(hass, entry)
    assert await async_setup_component(hass, "automation", {})
    triggers = await trigger_helper.async_get_all_descriptions(hass)
    conditions = await condition_helper.async_get_all_descriptions(hass)
    services = await service_helper.async_get_all_descriptions(hass)
    ours = {k: v for k, v in triggers.items() if k.startswith("pflanzenmonitor_plus.")}
    assert len(ours) == 43
    assert all(v is not None for v in ours.values())
    stale = ours["pflanzenmonitor_plus.moisture_sensor_became_stale"]
    assert stale is not None
    assert stale["fields"]["for"]["default"] == "24:00:00"
    assert len([k for k in conditions if k.startswith("pflanzenmonitor_plus.")]) == 35
    assert "meter_entity" in services["pflanzenmonitor_plus"]["replace_sensor"]["fields"]
