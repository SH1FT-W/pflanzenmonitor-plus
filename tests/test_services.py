"""Aktion „Sensor tauschen“."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pflanzenmonitor_plus.const import DOMAIN

from .conftest import INSIDE, setup

pytestmark = pytest.mark.usefixtures("house", "stats", "forecast")

MOISTURE = "sensor.schlafzimmer_bogenhanf_bodenfeuchtigkeit"
TEMPERATURE = "sensor.schlafzimmer_bogenhanf_temperatur"


async def replace(hass: HomeAssistant, meter: str, new: str | None = None) -> None:
    data = {"meter_entity": meter} | ({"new_sensor": new} if new is not None else {})
    await hass.services.async_call(DOMAIN, "replace_sensor", data, blocking=True)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_replace_and_remove(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    hass.states.async_set("sensor.neue_feuchte", "33", {"unit_of_measurement": "%"})
    await replace(hass, MOISTURE, "sensor.neue_feuchte")
    assert entry.runtime_data.plants[INSIDE].moisture == "sensor.neue_feuchte"
    state = hass.states.get(MOISTURE)
    assert state is not None
    assert state.state == "33.0"
    # Leer = Sensor entfernen; seine Entitäten verschwinden
    await replace(hass, TEMPERATURE, "")
    assert "temperature" not in entry.runtime_data.plants[INSIDE].sensors
    assert hass.states.get(TEMPERATURE) is None


async def test_errors(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    for meter, new, key in (
        ("sensor.schlafzimmer_bogenhanf_lernstand", None, "not_a_meter"),
        ("sensor.gibt_es_nicht", None, "not_a_meter"),
        ("number.schlafzimmer_bogenhanf_min_bodenfeuchtigkeit", None, "not_a_meter"),
        (MOISTURE, "sensor.fehlt", "sensor_not_found"),
        (MOISTURE, "light.lampe", "sensor_not_found"),
    ):
        with pytest.raises(ServiceValidationError) as err:
            await replace(hass, meter, new)
        assert err.value.translation_key == key
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(ServiceValidationError) as err:
        await replace(hass, MOISTURE, "sensor.neue_feuchte")
    assert err.value.translation_key == "not_loaded"
