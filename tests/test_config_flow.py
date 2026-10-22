"""Einrichten, Einstellungen, Pflanzen anlegen und ändern, Diagnose."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.pflanzenmonitor_plus.const import (
    CONF_AI_TASK,
    CONF_RAIN_SENSOR,
    CONF_WEATHER,
    DOMAIN,
    SUBENTRY_PLANT,
)

from .conftest import INSIDE, OUTSIDE, RAIN, setup


def suggestions(result: Any) -> dict[str, Any]:
    """Vorgeschlagene bzw. voreingestellte Werte eines Formulars."""
    found: dict[str, Any] = {}
    for key in result["data_schema"].schema:
        if key.description and "suggested_value" in key.description:
            found[str(key)] = key.description["suggested_value"]
        elif key.default is not None and callable(key.default):
            found[str(key)] = key.default()
    return found


@pytest.mark.usefixtures("forecast")
async def test_user_flow_suggests_weather(hass: HomeAssistant) -> None:
    """Ohne Pflanzen einrichtbar; Wetter wird vorgeschlagen."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert suggestions(result) == {CONF_AI_TASK: None, CONF_WEATHER: "weather.haus", CONF_RAIN_SENSOR: None}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_WEATHER: "weather.haus"})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Pflanzenmonitor Plus"
    assert result["data"] == {}
    assert result["options"] == {CONF_WEATHER: "weather.haus"}

    # Nur ein Eintrag
    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert again["type"] is FlowResultType.ABORT
    assert again["reason"] == "single_instance_allowed"


async def test_user_flow_without_weather(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Nichts gewählt = nichts gespeichert (die Vorschläge kommen nur ins Formular)
    assert result["options"] == {}


@pytest.mark.usefixtures("house", "stats", "forecast")
async def test_options_reload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    assert hass.states.get("binary_sensor.terrasse_olivenbaum_regen_erwartet") is not None
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert suggestions(result)[CONF_RAIN_SENSOR] == RAIN
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {}
    # Neu geladen: ohne Wetter keine Regen- und Frost-Sensoren mehr
    assert hass.states.get("binary_sensor.terrasse_olivenbaum_regen_erwartet") is None


@pytest.mark.usefixtures("stats", "forecast")
async def test_add_plant(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Name, Art, Raum → Sensoren (draußen nach Raum vorgewählt) → Probleme und Wartezeit."""
    await setup(hass, entry)
    terrace = ar.async_get(hass).async_get_or_create("Balkon")
    hass.states.async_set("sensor.feige_feuchte", "40", {"unit_of_measurement": "%", "device_class": "moisture"})
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_PLANT), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": " Feige ", "species": "Ficus carica", "area": terrace.id}
    )
    assert result["step_id"] == "sensors"
    assert suggestions(result)["outdoor"] is True
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"moisture_sensor": "sensor.feige_feuchte", "outdoor": True}
    )
    assert result["step_id"] == "problems"
    assert suggestions(result)["moisture_grace"] == 0
    assert "vpd" not in suggestions(result)["problems"]
    assert "moisture" in suggestions(result)["problems"]
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"problems": ["moisture"], "moisture_grace": 30}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = next(s for s in entry.subentries.values() if s.title == "Feige")
    assert subentry.data["moisture_grace"] == 1800
    assert subentry.data["problems"] == ["moisture"]
    assert subentry.data["area"] == "Balkon"
    assert subentry.data["temperature_sensor"] is None
    # Neu geladen: Gerät im Raum, Entitäten da
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, subentry.subentry_id), entry.entry_id)
    assert device is not None
    assert device.area_id == terrace.id
    assert device.model == "Ficus carica"
    state = hass.states.get("sensor.balkon_feige_bodenfeuchtigkeit")
    assert state is not None
    assert state.state == "40.0"
    assert hass.states.get("binary_sensor.balkon_feige_regen_erwartet") is not None


@pytest.mark.usefixtures("house", "stats", "forecast")
async def test_reconfigure_plant(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    subentry_id = entry.runtime_data.plants[OUTSIDE].subentry_id
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_PLANT), context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id}
    )
    assert result["step_id"] == "reconfigure"
    assert "area" not in suggestions(result)
    assert suggestions(result)["name"] == "Olivenbaum"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"name": "Olive", "species": "Olea europaea"}
    )
    assert suggestions(result)["moisture_sensor"] == "sensor.olivenbaum_bodenfeuchtigkeit"
    assert suggestions(result)["outdoor"] is True
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"moisture_sensor": "sensor.olivenbaum_bodenfeuchtigkeit", "outdoor": False}
    )
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {"problems": []})
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    subentry = entry.subentries[subentry_id]
    assert subentry.title == "Olive"
    assert subentry.data["area"] == "Terrasse"
    assert subentry.data["key"] == OUTSIDE
    plant = entry.runtime_data.plants[OUTSIDE]
    assert (plant.name, plant.species, plant.outdoor) == ("Olive", "Olea europaea", False)
    # Keine Probleme gewählt = keine; nicht mehr draußen → keine Regen- und Frost-Sensoren
    assert plant.problems == frozenset()
    assert hass.states.get("binary_sensor.terrasse_olivenbaum_regen_erwartet") is None


@pytest.mark.usefixtures("house", "stats", "forecast")
async def test_diagnostics(hass: HomeAssistant, entry: MockConfigEntry, hass_client: ClientSessionGenerator) -> None:
    await setup(hass, entry)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diag["options"][CONF_RAIN_SENSOR] == RAIN
    inside = diag["plants"][INSIDE]
    assert inside["source"] == "sensor.bogenhanf_bodenfeuchtigkeit"
    assert inside["hours"] > 500
    assert inside["learned"]["stage"] == "bereit"
    assert inside["learned"]["rises"][0]["kind"] == "gegossen"
    assert inside["rain"] is None
    assert inside["limits"]["min_moisture"] == 25.0
    assert inside["live"]["state"] == "ok"
    assert inside["live"]["values"]["moisture"] == [50.0, "%"]
    outside = diag["plants"][OUTSIDE]
    assert outside["plant"]["outdoor"] is True
    assert outside["frost"]["limit"] == 2.0


def test_translations_have_no_tags() -> None:
    """Die Oberfläche liest Texte als ICU-Nachrichten: „<…>“ gilt als Tag und bricht die Anzeige ab."""
    from pathlib import Path

    folder = Path(__file__).parent.parent / "custom_components" / "pflanzenmonitor_plus"
    for path in [folder / "strings.json", *(folder / "translations").glob("*.json")]:
        assert "<" not in path.read_text(encoding="utf-8"), path.name
