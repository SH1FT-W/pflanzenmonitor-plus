"""Live je Pflanze: Spiegel, Zustand wie plant.*, Logbuch, Wartezeit, Licht (PPFD, DLI), Grenzwerte, Neustart."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.pflanzenmonitor_plus.live import ppfd_factor

from .conftest import INSIDE, add_plant, now, pending, setup

pytestmark = pytest.mark.usefixtures("stats", "forecast")

STATUS = "sensor.schlafzimmer_bogenhanf"
MOISTURE = "sensor.schlafzimmer_bogenhanf_bodenfeuchtigkeit"


def state_of(hass: HomeAssistant, entity_id: str) -> Any:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


@pytest.fixture
def logbook(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Fake-Logbuch: schreibt mit, was Plus einträgt."""
    entries: list[dict[str, Any]] = []

    async def log(call: ServiceCall) -> None:
        entries.append(dict(call.data))

    hass.services.async_register("logbook", "log", log)
    hass.config.components.add("logbook")
    return entries


async def later(hass: HomeAssistant, delta: timedelta, freezer: Any) -> None:
    freezer.tick(delta)
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("house")
async def test_status_mirror_and_logbook(
    hass: HomeAssistant, entry: MockConfigEntry, logbook: list[dict[str, Any]]
) -> None:
    pending(hass, INSIDE)["species"] = "Dracaena trifasciata"
    await setup(hass, entry)
    status = state_of(hass, STATUS)
    assert status.state == "ok"
    assert status.name == "Bogenhanf"
    assert status.attributes["species"] == "Dracaena trifasciata"
    assert status.attributes["moisture_status"] == "ok"
    assert status.attributes["temperature_status"] == "ok"
    assert status.attributes["co2_status"] is None
    assert status.attributes["problems"] == []
    mirror = state_of(hass, MOISTURE)
    assert mirror.state == "50.0"
    assert mirror.attributes["unit_of_measurement"] == "%"
    assert mirror.attributes["external_sensor"] == "sensor.bogenhanf_bodenfeuchtigkeit"

    # Zu trocken → Problem, ein Eintrag ins Logbuch
    hass.states.async_set("sensor.bogenhanf_bodenfeuchtigkeit", "10", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    status = state_of(hass, STATUS)
    assert status.state == "problem"
    assert status.attributes["moisture_status"] == "Low"
    assert status.attributes["problems"] == [
        {"sensor_type": "moisture", "status": "Low", "current": "10.0", "min": "25.0", "max": "60.0"}
    ]
    assert logbook == [
        {
            "name": "Bogenhanf",
            "message": "Bodenfeuchte zu niedrig – jetzt 10.0, min 25.0",
            "domain": "pflanzenmonitor_plus",
            "entity_id": STATUS,
        }
    ]
    # Sensor fällt aus: weder neu noch behoben; danach wieder im Bereich → „wieder im Bereich“
    hass.states.async_set("sensor.bogenhanf_bodenfeuchtigkeit", "unavailable")
    await hass.async_block_till_done()
    assert state_of(hass, MOISTURE).state == "unknown"
    assert len(logbook) == 1
    hass.states.async_set("sensor.bogenhanf_bodenfeuchtigkeit", "40", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    assert state_of(hass, STATUS).state == "ok"
    assert logbook[-1]["message"] == "Bodenfeuchte wieder im Bereich"
    # Text statt Zahl: kein Wert
    hass.states.async_set("sensor.bogenhanf_bodenfeuchtigkeit", "kaputt")
    await hass.async_block_till_done()
    assert state_of(hass, MOISTURE).state == "unknown"


@pytest.mark.usefixtures("house")
async def test_limits_from_number_entities(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await setup(hass, entry)
    number = "number.schlafzimmer_bogenhanf_min_bodenfeuchtigkeit"
    assert state_of(hass, number).state == "25.0"
    assert state_of(hass, number).attributes["min"] == 0
    await hass.services.async_call("number", "set_value", {"entity_id": number, "value": 55}, blocking=True)
    await hass.async_block_till_done()
    assert state_of(hass, number).state == "55.0"
    assert state_of(hass, STATUS).state == "problem"
    assert entry.runtime_data.limits.get(INSIDE, "min_moisture") == 55
    # Bleibt nach dem Neuladen
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state_of(hass, number).state == "55.0"
    # Temperaturgrenzen in °C, Leitfähigkeit nur mit Sensor
    assert state_of(hass, "number.schlafzimmer_bogenhanf_max_temperatur").attributes["unit_of_measurement"] == "°C"
    assert hass.states.get("number.schlafzimmer_bogenhanf_max_leitfahigkeit") is None


async def test_restore_after_restart(hass: HomeAssistant, entry: MockConfigEntry, freezer: Any) -> None:
    """Nach einem Neustart hält der Spiegel den letzten Wert, bis der echte Sensor meldet (höchstens 10 Min.)."""
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer", moisture=10.0)
    await setup(hass, entry)
    assert state_of(hass, STATUS).state == "problem"
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.states.async_set("sensor.bogenhanf_bodenfeuchtigkeit", "unavailable")
    hass.states.async_set("sensor.bogenhanf_temperatur", "unavailable")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert state_of(hass, MOISTURE).state == "10.0"
    assert state_of(hass, STATUS).state == "problem"
    # Erste echte Meldung ersetzt den gespeicherten Wert
    hass.states.async_set("sensor.bogenhanf_temperatur", "21", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert state_of(hass, "sensor.schlafzimmer_bogenhanf_temperatur").state == "21.0"
    # Nach 10 Min. ohne Meldung gilt der Ausfall
    await later(hass, timedelta(minutes=11), freezer)
    assert state_of(hass, MOISTURE).state == "unknown"
    assert state_of(hass, STATUS).state == "ok"


async def test_fahrenheit_source_and_vpd(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Temperatur in °F wird für Grenzwerte und VPD in °C gerechnet; der Spiegel behält °F."""
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer", full=True)
    hass.states.async_set("sensor.bogenhanf_temperatur", "77", {"unit_of_measurement": "°F"})
    hass.states.async_set("sensor.bogenhanf_luftfeuchtigkeit", "50", {"unit_of_measurement": "%"})
    await setup(hass, entry)
    status = state_of(hass, STATUS)
    assert status.attributes["temperature_status"] == "ok"
    assert state_of(hass, "sensor.schlafzimmer_bogenhanf_dampfdruckdefizit").state == "1.58"
    hass.states.async_set("sensor.bogenhanf_temperatur", "110", {"unit_of_measurement": "°F"})
    await hass.async_block_till_done()
    assert state_of(hass, STATUS).attributes["temperature_status"] == "High"
    # VPD zählt standardmäßig nicht als Problem
    hass.states.async_set("sensor.bogenhanf_luftfeuchtigkeit", "5", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    status = state_of(hass, STATUS)
    assert status.attributes["vpd_status"] == "High"
    assert "vpd" not in {p["sensor_type"] for p in status.attributes["problems"]}


async def test_grace_after_watering(hass: HomeAssistant, entry: MockConfigEntry, freezer: Any) -> None:
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer", moisture=40.0)
    pending(hass, INSIDE)["moisture_grace"] = 1800
    await setup(hass, entry)
    hass.states.async_set("sensor.bogenhanf_bodenfeuchtigkeit", "75", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    status = state_of(hass, STATUS)
    assert status.attributes["moisture_status"] == "High"
    assert status.state == "ok"
    await later(hass, timedelta(minutes=31), freezer)
    assert state_of(hass, STATUS).state == "problem"
    # Sensor weg → Wartezeit vergessen
    hass.states.async_set("sensor.bogenhanf_bodenfeuchtigkeit", "unavailable")
    await hass.async_block_till_done()


async def test_light_and_dli(hass: HomeAssistant, entry: MockConfigEntry, freezer: Any) -> None:
    """Lux × Faktor → PPFD; Lichtsumme heute, um Mitternacht zu „gestern“, das gegen die DLI-Grenzen geprüft wird."""
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer", full=True)
    hass.states.async_set("sensor.bogenhanf_beleuchtungsstarke", "10000", {"unit_of_measurement": "lx"})
    await setup(hass, entry)
    # 10000 lx × 0,02 = 200 µmol/(s·m²)
    assert float(state_of(hass, "sensor.schlafzimmer_bogenhanf_ppfd").state) == pytest.approx(0.0002)
    await later(hass, timedelta(hours=1), freezer)
    dli = state_of(hass, "sensor.schlafzimmer_bogenhanf_tageslicht_dli")
    assert float(dli.state) == pytest.approx(0.72, abs=0.01)
    assert dli.attributes["last_period"] is None
    assert float(state_of(hass, "sensor.schlafzimmer_bogenhanf_licht_letzte_24_std").state) == pytest.approx(
        0.72, abs=0.01
    )
    assert state_of(hass, STATUS).attributes["dli_status"] is None
    # Bis nach Mitternacht (Berlin): heute wird zu gestern
    hass.states.async_set("sensor.bogenhanf_beleuchtungsstarke", "0", {"unit_of_measurement": "lx"})
    await hass.async_block_till_done()
    midnight = now().replace(hour=22, minute=0, second=0) + timedelta(seconds=1)
    freezer.move_to(midnight)
    async_fire_time_changed(hass, midnight)
    await hass.async_block_till_done(wait_background_tasks=True)
    dli = state_of(hass, "sensor.schlafzimmer_bogenhanf_tageslicht_dli")
    assert float(dli.state) == pytest.approx(0.0, abs=0.001)
    assert dli.attributes["last_period"] == pytest.approx(0.72, abs=0.01)
    # 0,72 mol unter der Untergrenze 2 → Tageslicht zu niedrig (zählt als Problem)
    status = state_of(hass, STATUS)
    assert status.attributes["dli_status"] == "Low"
    assert status.state == "problem"
    assert float(state_of(hass, "sensor.schlafzimmer_bogenhanf_lichtsumme_gesamt").state) > 0.7


async def test_ppfd_source(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer", full=True)
    hass.states.async_set("sensor.bogenhanf_beleuchtungsstarke", "150", {"unit_of_measurement": "µmol/s⋅m²"})
    await setup(hass, entry)
    mirror = state_of(hass, "sensor.schlafzimmer_bogenhanf_beleuchtungsstarke")
    # HA schreibt das Mikro-Zeichen als griechisches µ
    assert mirror.attributes["unit_of_measurement"] in ("µmol/s⋅m²", "μmol/s⋅m²")
    assert "device_class" not in mirror.attributes
    assert float(state_of(hass, "sensor.schlafzimmer_bogenhanf_ppfd").state) == pytest.approx(0.00015)
    assert state_of(hass, STATUS).attributes["illuminance_status"] is None


def test_ppfd_factor() -> None:
    assert ppfd_factor("lx") is None
    assert ppfd_factor(None) is None
    assert ppfd_factor("µmol/m²/s") == 1e-6
    assert ppfd_factor("umol/m²/s") == 1e-6
    assert ppfd_factor("mmol/m²/s") == 1e-3
    assert ppfd_factor("mol/s⋅m²") == 1.0
