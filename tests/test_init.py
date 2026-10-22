"""Einrichtung im Test-Haus: Entitäten am Pflanzengerät, Werte, Wetter, Hinweise, Aufräumen."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pflanzenmonitor_plus.const import DOMAIN

from .conftest import INSIDE, OUTSIDE, RAIN, add_now, add_plant, cycles, now, setup, watered

pytestmark = pytest.mark.usefixtures("house", "stats", "forecast")


async def test_sensors_on_plant_device(hass: HomeAssistant, entry: MockConfigEntry, stats: dict[str, Any]) -> None:
    await setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    learning = hass.states.get("sensor.schlafzimmer_bogenhanf_lernstand")
    assert learning is not None
    assert learning.state == "bereit"
    assert learning.attributes["cycles"] == 2
    assert learning.attributes["cycles_needed"] == 2
    assert learning.attributes["habit"] == pytest.approx(20, abs=0.5)

    # 50 % jetzt, Untergrenze 25, früher 6 %/Tag → gut 4 Tage
    water = hass.states.get("sensor.schlafzimmer_bogenhanf_giessen_in")
    assert water is not None
    assert 3.5 <= float(water.state) <= 4.5
    assert water.attributes["unit_of_measurement"] == "d"
    assert water.attributes["target"] == 25.0
    assert water.attributes["method"] == "zyklen"
    assert water.attributes["due"] == "2026-10-20"

    last = hass.states.get("sensor.schlafzimmer_bogenhanf_zuletzt_gegossen")
    assert last is not None
    assert last.state == watered(stats["sensor.bogenhanf_bodenfeuchtigkeit"]).isoformat()
    assert "last_rain" not in last.attributes

    rate = hass.states.get("sensor.schlafzimmer_bogenhanf_abtrocknung")
    assert rate is not None
    assert 5.5 <= float(rate.state) <= 6.5

    # Alles hängt am Gerät der Pflanze (eigenes Gerät von Plus, im Raum der Pflanze)
    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, INSIDE), entry.entry_id)
    assert device is not None
    assert device.name == "Bogenhanf"
    assert device.area_id == "schlafzimmer"
    assert device.config_entries_subentries[entry.entry_id] == {entry.runtime_data.plants[INSIDE].subentry_id}
    for entity_id in (
        "sensor.schlafzimmer_bogenhanf",
        "sensor.schlafzimmer_bogenhanf_lernstand",
        "sensor.schlafzimmer_bogenhanf_giessen_in",
        "sensor.schlafzimmer_bogenhanf_bodenfeuchtigkeit",
        "number.schlafzimmer_bogenhanf_min_bodenfeuchtigkeit",
    ):
        item = registry.async_get(entity_id)
        assert item is not None, entity_id
        assert item.device_id == device.id
    # Drinnen keine Wetter-Sensoren
    assert hass.states.get("binary_sensor.schlafzimmer_bogenhanf_regen_erwartet") is None


async def test_outdoor_rain_and_frost(
    hass: HomeAssistant, entry: MockConfigEntry, forecast: list[dict[str, Any]], stats: dict[str, Any]
) -> None:
    forecast[2]["precipitation"] = 0.6
    forecast[5]["precipitation"] = 0.8
    forecast[30]["temperature"] = -1.5
    forecast[47]["precipitation"] = 5.0  # außerhalb der 12 Std.
    olive = stats["sensor.olivenbaum_bodenfeuchtigkeit"]
    last_rise = watered(olive, period=7, count=3)
    rain_rise = watered(olive, period=7, count=2)
    # Regensensor: vor dem ersten Anstieg hat es geregnet, vor dem zweiten nicht
    stats[RAIN] = [
        {"start": (now() - timedelta(days=30)).timestamp(), "mean": 0.0, "min": 0.0, "max": 0.0},
        {"start": (rain_rise - timedelta(hours=1)).timestamp(), "mean": 0.5, "min": 0.0, "max": 1.2},
    ]
    await setup(hass, entry)

    rain = hass.states.get("binary_sensor.terrasse_olivenbaum_regen_erwartet")
    assert rain is not None
    assert rain.state == "on"
    assert rain.attributes["amount"] == 1.4
    frost = hass.states.get("binary_sensor.terrasse_olivenbaum_frostgefahr")
    assert frost is not None
    assert frost.state == "on"
    assert frost.attributes["lowest"] == -1.5
    assert frost.attributes["limit"] == 2.0

    last = hass.states.get("sensor.terrasse_olivenbaum_zuletzt_gegossen")
    assert last is not None
    assert last.state == last_rise.isoformat()
    assert last.attributes["last_rain"] == rain_rise.isoformat()


async def test_dry_forecast(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    rain = hass.states.get("binary_sensor.terrasse_olivenbaum_regen_erwartet")
    assert rain is not None
    assert rain.state == "off"
    assert rain.attributes["amount"] == 0.0
    assert rain.attributes["until"] is None
    frost = hass.states.get("binary_sensor.terrasse_olivenbaum_frostgefahr")
    assert frost is not None
    assert frost.state == "off"


async def test_forecast_unavailable(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    async def broken(_call: Any) -> None:
        raise HomeAssistantError("kaputt")

    hass.services.async_register("weather", "get_forecasts", broken, supports_response="only")  # type: ignore[arg-type]
    await setup(hass, entry)
    rain = hass.states.get("binary_sensor.terrasse_olivenbaum_regen_erwartet")
    assert rain is not None
    assert rain.state == "off"
    frost = hass.states.get("binary_sensor.terrasse_olivenbaum_frostgefahr")
    assert frost is not None
    assert frost.state == "unknown"


async def test_no_weather_no_binary_sensors(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    await setup(hass, entry)
    # Ohne Wetter-Entität keine Regen- und Frost-Sensoren, auch nicht für Pflanzen draußen
    assert hass.states.get("binary_sensor.terrasse_olivenbaum_regen_erwartet") is None
    assert hass.states.get("binary_sensor.terrasse_olivenbaum_frostgefahr") is None
    last = hass.states.get("sensor.terrasse_olivenbaum_zuletzt_gegossen")
    assert last is not None
    assert last.attributes["rain_data"] == "fehlt"


async def test_old_issues_removed_and_cleanup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Hinweise „keine Untergrenze“ von früher verschwinden; Entfernen räumt Hinweise und Speicher auf."""
    ir.async_create_issue(
        hass, DOMAIN, f"no_minimum_{INSIDE}", is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key="x"
    )
    await setup(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_minimum_{INSIDE}") is None
    ir.async_create_issue(
        hass, DOMAIN, "egal", is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key="x"
    )
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert not [i for (d, i) in ir.async_get(hass).issues if d == DOMAIN]


async def test_mirror_history_fallback(hass: HomeAssistant, entry: MockConfigEntry, stats: dict[str, Any]) -> None:
    """Neu gekoppelter echter Sensor hat nur Stunden, der Spiegel (Verlauf aus dem Plant Monitor) Monate."""
    add_plant(hass, "PLANTEXTERN02", "Efeutute", "efeutute", "Bad", moisture=40)
    stats["sensor.efeutute_bodenfeuchtigkeit"] = cycles(start=70, low=30)[-5:]
    stats["sensor.bad_efeutute_bodenfeuchtigkeit"] = cycles(start=70, low=30)
    await setup(hass, entry)
    # Beim ersten Lesen gibt es den Spiegel schon (Entitäten entstehen vor dem Start von HA)
    assert entry.runtime_data.sources["sensor.efeutute_bodenfeuchtigkeit"] == "sensor.bad_efeutute_bodenfeuchtigkeit"
    learning = hass.states.get("sensor.bad_efeutute_lernstand")
    assert learning is not None
    assert learning.state == "bereit"


async def test_watering_detected_immediately(
    hass: HomeAssistant, entry: MockConfigEntry, stats: dict[str, Any]
) -> None:
    await setup(hass, entry)
    hass.states.async_set("sensor.bogenhanf_bodenfeuchtigkeit", "51", {"unit_of_measurement": "%"})
    await hass.async_block_till_done(wait_background_tasks=True)
    last = hass.states.get("sensor.schlafzimmer_bogenhanf_zuletzt_gegossen")
    assert last is not None
    assert last.state == watered(stats["sensor.bogenhanf_bodenfeuchtigkeit"]).isoformat()

    # Sprung um 30 Punkte → sofort neu gerechnet, ohne auf den 10-Minuten-Takt zu warten
    hass.states.async_set("sensor.bogenhanf_bodenfeuchtigkeit", "80", {"unit_of_measurement": "%"})
    await hass.async_block_till_done(wait_background_tasks=True)
    async_fire_time_changed(hass, now() + timedelta(seconds=11))
    await hass.async_block_till_done(wait_background_tasks=True)
    last = hass.states.get("sensor.schlafzimmer_bogenhanf_zuletzt_gegossen")
    assert last is not None
    assert last.state == now().isoformat()

    # Unbrauchbarer Wert wird ignoriert
    hass.states.async_set("sensor.bogenhanf_bodenfeuchtigkeit", "unavailable")
    await hass.async_block_till_done(wait_background_tasks=True)
    hass.states.async_set("sensor.bogenhanf_bodenfeuchtigkeit", "kaputt")
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_plants_added_and_removed(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    await add_now(hass, entry, add_plant(hass, "PLANTNEW00001", "Yucca", "yucca", "Wohnzimmer"))
    assert hass.states.get("sensor.wohnzimmer_yucca_lernstand") is not None
    assert hass.states.get("sensor.wohnzimmer_yucca") is not None

    # Pflanze gelöscht → Gerät und Entitäten verschwinden
    registry = er.async_get(hass)
    subentry = entry.runtime_data.plants["PLANTNEW00001"].subentry_id
    assert hass.config_entries.async_remove_subentry(entry, subentry)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert registry.async_get("sensor.wohnzimmer_yucca_lernstand") is None
    assert dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "PLANTNEW00001"), entry.entry_id) is None
    assert registry.async_get("sensor.terrasse_olivenbaum_lernstand") is not None
    assert entry.state is ConfigEntryState.LOADED


async def test_stale_entities_and_devices_removed(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Sensor abgewählt → seine Entitäten verschwinden; fremdes Gerät ohne Pflanze verliert den Eintrag."""
    await setup(hass, entry)
    devices = dr.async_get(hass)
    orphan = devices.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "WEG")})
    subentry = entry.subentries[entry.runtime_data.plants[INSIDE].subentry_id]
    hass.config_entries.async_update_subentry(entry, subentry, data={**subentry.data, "temperature_sensor": None})
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    assert registry.async_get("sensor.schlafzimmer_bogenhanf_temperatur") is None
    assert registry.async_get("number.schlafzimmer_bogenhanf_min_temperatur") is None
    assert registry.async_get("sensor.schlafzimmer_bogenhanf_bodenfeuchtigkeit") is not None
    assert devices.async_get(orphan.id) is None


async def test_unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_plant_without_moisture(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Pflanze ohne Sensoren: nur der Zustand (unbekannt), nichts zu lernen."""
    add_plant(hass, "PLANTBARE0001", "Nackt", "nackt", "Flur")
    pending = hass.data["test_pending_plants"][-1][0]
    pending.pop("moisture_sensor")
    pending.pop("temperature_sensor")
    await setup(hass, entry)
    state = hass.states.get("sensor.flur_nackt")
    assert state is not None
    assert state.state == "unknown"
    assert hass.states.get("sensor.flur_nackt_lernstand") is None
    assert entry.runtime_data.due_date("PLANTBARE0001") is None
    assert {INSIDE, OUTSIDE} <= set(entry.runtime_data.plants)


async def test_history_windows(hass: HomeAssistant, ai: Any) -> None:
    """Feuchte und Regen 120 Tage (Zyklen lernen), Kontextsensoren der KI nur 30 Tage."""
    from unittest.mock import patch

    from custom_components.pflanzenmonitor_plus.const import (
        CONF_AI_TASK,
        CONF_RAIN_SENSOR,
        CONF_WEATHER,
        CONTEXT_HISTORY,
        HISTORY,
    )

    add_plant(hass, "PLANTFULL0003", "Monstera", "monstera", "Büro", full=True)
    plus = MockConfigEntry(
        domain=DOMAIN,
        title="Pflanzenmonitor Plus",
        data={},
        options={CONF_AI_TASK: "ai_task.claude", CONF_WEATHER: "weather.haus", CONF_RAIN_SENSOR: RAIN},
    )
    plus.add_to_hass(hass)
    calls: list[tuple[Any, set[str]]] = []

    def fake(_hass: HomeAssistant, start: Any, _end: Any, ids: set[str], *_args: Any) -> dict[str, Any]:
        calls.append((start, set(ids)))
        return {}

    with patch("custom_components.pflanzenmonitor_plus.coordinator.statistics_during_period", side_effect=fake):
        await setup(hass, plus)
    windows = dict(calls)
    assert set(windows) == {now() - HISTORY, now() - CONTEXT_HISTORY}
    long, short = windows[now() - HISTORY], windows[now() - CONTEXT_HISTORY]
    # Echter Sensor und sein Spiegel
    assert {"sensor.monstera_bodenfeuchtigkeit", "sensor.buro_monstera_bodenfeuchtigkeit", RAIN} <= long
    assert {
        "sensor.monstera_temperatur",
        "sensor.buro_monstera_temperatur",
        "sensor.monstera_luftfeuchtigkeit",
        "sensor.monstera_beleuchtungsstarke",
    } <= short
    assert not long & short and not short & {"sensor.bogenhanf_bodenfeuchtigkeit"}


async def test_rename_follows(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Umbenannter echter Sensor und Regensensor ziehen in Pflanze und Optionen nach (dann neu laden)."""
    registry = er.async_get(hass)
    source = registry.async_get_or_create(
        "sensor", "zigbee", "feuchte-1", suggested_object_id="kaktus_bodenfeuchtigkeit"
    )
    rain = registry.async_get_or_create("sensor", "met", "regen", suggested_object_id="regen")
    assert (source.entity_id, rain.entity_id) == ("sensor.kaktus_bodenfeuchtigkeit", RAIN)
    add_plant(hass, "PLANTKAKTUS01", "Kaktus", "kaktus", "Flur")
    await setup(hass, entry)
    registry.async_update_entity(source.entity_id, new_entity_id="sensor.kaktus_feuchte")
    registry.async_update_entity(RAIN, new_entity_id="sensor.regen_neu")
    # Andere Änderungen lösen nichts aus
    registry.async_update_entity("sensor.kaktus_feuchte", name="egal")
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.options["rain_sensor"] == "sensor.regen_neu"
    assert entry.runtime_data.plants["PLANTKAKTUS01"].moisture == "sensor.kaktus_feuchte"
    assert entry.state is ConfigEntryState.LOADED
