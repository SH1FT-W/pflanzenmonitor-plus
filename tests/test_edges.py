"""Randfälle: alte Hinweise, fehlende Mindesttemperatur, kaputte Vorhersage, Pflanze ohne Sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pflanzenmonitor_plus.const import DOMAIN

from .conftest import OUTSIDE, add_plant, pending, setup


@pytest.mark.usefixtures("house", "stats")
async def test_old_issue_and_missing_min_temperature(
    hass: HomeAssistant, entry: MockConfigEntry, forecast: list[dict[str, Any]]
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        "no_minimum_GONE",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="no_minimum",
    )
    pending(hass, OUTSIDE).pop("temperature_sensor")
    forecast.insert(0, {"datetime": None, "temperature": -20.0, "precipitation": 30.0})
    await setup(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "no_minimum_GONE") is None
    # Ohne Mindesttemperatur: Frost = unter 0 °C
    frost = hass.states.get("binary_sensor.terrasse_olivenbaum_frostgefahr")
    assert frost is not None
    assert frost.state == "off"
    assert frost.attributes["limit"] == 0.0
    rain = hass.states.get("binary_sensor.terrasse_olivenbaum_regen_erwartet")
    assert rain is not None
    assert rain.state == "off"


@pytest.mark.usefixtures("stats")
async def test_plant_without_moisture_sensor(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Nur Temperatur: Zustand, Spiegel und Grenzwerte, aber nichts Gelerntes."""
    add_plant(hass, "PLANTBARE0001", "Nackt", "nackt", "Flur")
    pending(hass, "PLANTBARE0001").pop("moisture_sensor")
    await setup(hass, entry)
    ids = {e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    assert ids == {
        "sensor.flur_nackt",
        "sensor.flur_nackt_temperatur",
        "number.flur_nackt_min_temperatur",
        "number.flur_nackt_max_temperatur",
    }
    assert entry.runtime_data.data["PLANTBARE0001"].learned.stage == "keine_daten"


@pytest.mark.usefixtures("house", "stats")
async def test_odd_forecast_response(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    async def odd(_call: Any) -> dict[str, Any]:
        return {"weather.haus": {"forecast": "kaputt"}}

    hass.services.async_register("weather", "get_forecasts", odd, supports_response="only")  # type: ignore[arg-type]
    await setup(hass, entry)
    rain = hass.states.get("binary_sensor.terrasse_olivenbaum_regen_erwartet")
    assert rain is not None
    assert rain.state == "off"


@pytest.mark.usefixtures("house", "stats", "forecast")
async def test_rain_sensor_without_history(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Regensensor eingestellt, aber noch kein Verlauf: draußen ist jeder Anstieg „unbekannt“, nicht Gießen."""
    await setup(hass, entry)
    last = hass.states.get("sensor.terrasse_olivenbaum_zuletzt_gegossen")
    assert last is not None
    assert last.state == "unknown"
    inside = hass.states.get("sensor.schlafzimmer_bogenhanf_zuletzt_gegossen")
    assert inside is not None
    assert inside.state != "unknown"


@pytest.mark.usefixtures("house", "stats")
async def test_forecast_without_timezone_and_bad_values(
    hass: HomeAssistant, entry: MockConfigEntry, forecast: list[dict[str, Any]]
) -> None:
    """Vorhersage ohne Zeitzone gilt als Ortszeit; einzelne kaputte Werte werden übersprungen statt alles zu stoppen."""
    local = dt_util.as_local(dt_util.parse_datetime(forecast[3]["datetime"]))  # type: ignore[arg-type]
    forecast[3]["datetime"] = local.replace(tzinfo=None).isoformat()
    forecast[3]["precipitation"] = 3.0
    forecast[1]["temperature"] = "kalt"
    forecast[2]["precipitation"] = "viel"
    await setup(hass, entry)
    rain = hass.states.get("binary_sensor.terrasse_olivenbaum_regen_erwartet")
    assert rain is not None
    assert rain.state == "on"
    assert rain.attributes["amount"] == 3.0


@pytest.mark.usefixtures("house", "stats", "forecast")
async def test_due_today_and_due_timestamp(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Unter der Untergrenze: fällig = heute 0 Uhr; sonst auf die Stunde gerundet."""
    hass.states.async_set("sensor.bogenhanf_bodenfeuchtigkeit", "20.5", {"unit_of_measurement": "%"})
    await setup(hass, entry)
    days = hass.states.get("sensor.schlafzimmer_bogenhanf_giessen_in")
    due = hass.states.get("sensor.schlafzimmer_bogenhanf_giessen_fallig")
    assert days is not None
    assert due is not None
    assert days.state == "0.0"
    assert due.state == "2026-10-15T22:00:00+00:00"  # 16.10. 0 Uhr in Berlin
    olive = hass.states.get("sensor.terrasse_olivenbaum_giessen_fallig")
    assert olive is not None
    assert olive.state.endswith(":00:00+00:00")


@pytest.mark.usefixtures("house", "stats", "forecast")
async def test_rain_history_hint(hass: HomeAssistant, entry: MockConfigEntry, stats: dict[str, Any]) -> None:
    await setup(hass, entry)
    last = hass.states.get("sensor.terrasse_olivenbaum_zuletzt_gegossen")
    assert last is not None
    assert last.attributes["rain_data"] == "fehlt"
    assert "rain_data_since" not in last.attributes
