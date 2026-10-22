"""1.1: Vorschläge für alle Grenzwerte mit Sensor – freier erster Schritt, danach schrittweise, Lichtverlauf."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import INSIDE, FakeAI, add_plant, now, pending, set_limits, setup
from .test_advisor import ACCEPT, ASK, SUGGESTION, UNDO, advisor_of, next_round, press, state_of

pytestmark = pytest.mark.usefixtures("stats", "forecast")

LIGHT_ANSWER = {
    "max_temperature": 32,
    "min_soil_temperature": 15,
    "max_soil_temperature": 32,
    "min_humidity": 30,
    "max_humidity": 70,
    "min_illuminance": 1500,
    "max_illuminance": 20000,
    "min_dli": 1,
    "max_dli": 8,
    "reason_max_illuminance": "Tagesspitzen um 3000 lx.",
}


@pytest.fixture
def full_house(hass: HomeAssistant, stats: dict[str, list[dict[str, Any]]]) -> None:
    """Bogenhanf mit allen Sensoren; die Helligkeit hat drei Tage Verlauf (mittags bis 3000 lx)."""
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer", moisture=50.0, full=True)
    start = (now() - timedelta(days=3)).replace(hour=0)
    stats["sensor.bogenhanf_beleuchtungsstarke"] = [
        {
            "start": (t := start + timedelta(hours=h)).timestamp(),
            "mean": 1000.0 if 8 <= t.hour < 16 else 0.0,
            "max": 3000.0 if 8 <= t.hour < 16 else 0.0,
            "min": 0.0,
        }
        for h in range(72)
    ]
    stats["sensor.bogenhanf_luftfeuchtigkeit"] = [
        {"start": (now() - timedelta(hours=h)).timestamp(), "mean": 45.0, "min": 35.0, "max": 55.0}
        for h in range(48, 0, -1)
    ]


@pytest.mark.usefixtures("full_house")
async def test_all_limits_free_first_then_stepped(
    hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry, numbers: list[tuple[str, str, float]]
) -> None:
    ai.answer = ai.answer | LIGHT_ANSWER
    await setup(hass, ai_entry)

    call = ai.calls[0]
    # Alle zwölf Grenzwerte gefragt, mit Lichtverlauf und Luftfeuchte im Kontext
    assert {k for k in call["structure"] if k.startswith(("min_", "max_"))} == {
        "min_moisture",
        "max_moisture",
        "min_temperature",
        "max_temperature",
        "min_soil_temperature",
        "max_soil_temperature",
        "min_humidity",
        "max_humidity",
        "min_illuminance",
        "max_illuminance",
        "min_dli",
        "max_dli",
    }
    assert '"daily_peak_lux": {' in call["instructions"]
    assert '"air_humidity": {' in call["instructions"]
    assert "fitting values: max_temperature, min_soil_temperature" in call["instructions"]

    # Startwerte in einem Schritt ersetzt (Helligkeit 100000 → 20000), Feuchte weiter schrittweise
    state = state_of(hass, SUGGESTION)
    assert state.attributes["max_illuminance"] == "100.000 lx → 20.000 lx (erstmals eingestellt)"
    assert state.attributes["max_illuminance_reason"] == "Tagesspitzen um 3000 lx."
    assert state.attributes["max_dli"] == "30 mol/m²/d → 8 mol/m²/d (erstmals eingestellt)"
    assert state.attributes["max_temperature"] == "40 °C → 32 °C (erstmals eingestellt)"
    assert state.attributes["max_moisture"] == "60 % → 45 % (Ziel 40 %, schrittweise)"

    await press(hass, ACCEPT)
    written = {name: value for _key, name, value in numbers}
    assert written["max_illuminance"] == 20000.0
    assert written["min_humidity"] == 30.0
    advisor = advisor_of(ai_entry)
    tuned = set(advisor.record(INSIDE)["tuned"])
    assert {"max_illuminance", "max_dli", "min_moisture"} <= tuned

    # Zweiter Vorschlag: einmal von Plus geschrieben → jetzt schrittweise (20000 → 40000 statt 90000)
    ai.answer = ai.answer | {"max_illuminance": 90000}
    await press(hass, ASK)
    state = state_of(hass, SUGGESTION)
    assert state.attributes["max_illuminance"] == "20.000 lx → 40.000 lx (Ziel 90.000 lx, schrittweise)"
    # Alle Grenzwerte wurden einmal geschrieben → keiner mehr frei
    assert "fitting values:" not in ai.calls[-1]["instructions"]
    assert "max_illuminance by at most 20000 lx" in ai.calls[-1]["instructions"]

    # Zurücknehmen nach erneutem Übernehmen stellt auch „schon geschrieben“ wieder her
    await press(hass, ACCEPT)
    numbers.clear()
    await press(hass, UNDO)
    assert set(advisor.record(INSIDE)["tuned"]) == tuned


async def test_only_limits_with_sensor(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    """Ohne Helligkeitssensor keine Licht-Grenzwerte, auch wenn die KI sie liefert."""
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer", moisture=50.0, full=True)
    pending(hass, INSIDE).pop("illuminance_sensor")
    ai.answer = ai.answer | LIGHT_ANSWER
    await setup(hass, ai_entry)
    structure = ai.calls[0]["structure"]
    assert "max_illuminance" not in structure
    assert "min_dli" not in structure
    assert "max_humidity" in structure
    state = state_of(hass, SUGGESTION)
    assert "max_illuminance" not in state.attributes
    assert state.attributes["max_humidity"] == "60 % → 70 %"


@pytest.mark.usefixtures("full_house")
async def test_old_suggestion_is_asked_again(hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry) -> None:
    """Offener Vorschlag aus 1.0 (ohne Version): nach dem Update verworfen und gleich für alle Werte neu gefragt."""
    await setup(hass, ai_entry)
    advisor = advisor_of(ai_entry)
    advisor.records[INSIDE] = {
        "state": "offen",
        "asked": now().isoformat(),
        "changes": {"min_moisture": {"current": 25.0, "proposed": 15.0, "reason": "", "requested": None}},
    }
    await advisor._save()
    calls = len(ai.calls)
    assert await hass.config_entries.async_reload(ai_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(ai.calls) == calls + 1
    rec = advisor_of(ai_entry).record(INSIDE)
    assert rec["version"] == 2
    assert "max_illuminance" in ai.calls[-1]["structure"]
    assert rec["changes"]["min_moisture"]["proposed"] == 15.0


@pytest.mark.usefixtures("full_house")
async def test_hand_change_species_and_near_values(
    hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry, numbers: list[tuple[str, str, float]]
) -> None:
    ai.answer = ai.answer | LIGHT_ANSWER
    await setup(hass, ai_entry)
    advisor = advisor_of(ai_entry)
    await advisor.async_reject(INSIDE)

    # Helligkeit max von Hand eingestellt → nicht mehr frei, nur noch schrittweise
    set_limits(hass, INSIDE, max_illuminance=8000)
    await press(hass, ASK)
    assert "max_illuminance" in advisor.record(INSIDE)["tuned"]
    free_rule = next(line for line in ai.calls[-1]["instructions"].splitlines() if "fitting values:" in line)
    assert "max_illuminance" not in free_rule
    assert "min_illuminance" in free_rule
    assert state_of(hass, SUGGESTION).attributes["max_illuminance"] == "8.000 lx → 20.000 lx"

    # Fast schon so eingestellt (Abstand unter der Rundung): wird beim Übernehmen nicht geschrieben, kein „veraltet“
    set_limits(hass, INSIDE, max_illuminance=20050)
    numbers.clear()
    await press(hass, ACCEPT)
    written = {name for _key, name, _value in numbers}
    assert "max_illuminance" not in written
    assert "max_dli" in written

    # Art nachgetragen (lädt neu) → fragt gleich neu, obwohl der Monat noch nicht um ist
    calls = len(ai.calls)
    await set_species(hass, ai_entry, "Dracaena trifasciata")
    assert len(ai.calls) == calls + 1
    # Gleiche Art: nicht noch einmal
    await advisor_of(ai_entry).async_reject(INSIDE)
    await next_round(hass, 22)
    assert len(ai.calls) == calls + 1
    # Fehlersperre gilt auch bei geänderter Art
    ai.error = HomeAssistantError("weg")
    await set_species(hass, ai_entry, "Sansevieria")
    await next_round(hass, 33)
    await next_round(hass, 44)
    assert len(ai.calls) == calls + 2


async def set_species(hass: HomeAssistant, entry: MockConfigEntry, species: str) -> None:
    subentry = entry.subentries[entry.runtime_data.plants[INSIDE].subentry_id]
    hass.config_entries.async_update_subentry(entry, subentry, data={**subentry.data, "species": species})
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_fahrenheit_and_ppfd(
    hass: HomeAssistant, ai: FakeAI, ai_entry: MockConfigEntry, stats: dict[str, list[dict[str, Any]]]
) -> None:
    """Grenzwerte sind immer in °C (auch in °F); liefert der Lichtsensor PPFD, keine lx-Grenzen (DLI aber schon)."""
    add_plant(hass, INSIDE, "Bogenhanf", "bogenhanf", "Schlafzimmer", moisture=50.0, full=True)
    hass.config.units = US_CUSTOMARY_SYSTEM
    hass.states.async_set("sensor.bogenhanf_beleuchtungsstarke", "20", {"unit_of_measurement": "µmol/m²/s"})
    stats["sensor.bogenhanf_beleuchtungsstarke"] = [
        {"start": (now() - timedelta(hours=h)).timestamp(), "mean": 100.0, "min": 0.0, "max": 300.0}
        for h in range(48, 0, -1)
    ]
    await setup(hass, ai_entry)
    fields = {k for k in ai.calls[0]["structure"] if k.startswith(("min_", "max_"))}
    assert {"min_temperature", "max_temperature", "min_soil_temperature", "max_soil_temperature"} <= fields
    assert '"min_temperature": 2.0' in ai.calls[0]["instructions"]
    assert not fields & {"min_illuminance", "max_illuminance"}
    assert {"min_dli", "max_dli", "min_humidity", "min_moisture"} <= fields
    # PPFD direkt: ein voller Tag mit 100 µmol/m²/s = 8,64 mol/m²/d
    assert '"median": 8.6' in ai.calls[0]["instructions"]
